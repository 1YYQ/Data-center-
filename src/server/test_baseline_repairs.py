"""Real HTTP regressions for the approved baseline repairs; synthetic data only."""

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from .app import create_app
from .config import Settings
from .db import build_engine, init_db
from .models import ApiIdempotencyRecord, AuthorizationGrant, BusinessApproval, ContentBlock, IndexSource, ParseRun, QueryTask
from .services import can_access_document, can_manage_kb, can_query_kb, create_default_kb, create_index
from .test_services import _ready_fixture, fixture_db


@pytest.fixture()
def repair_http(fixture_db):
    db, config = fixture_db
    owner, kb, document, version, block = _ready_fixture(db, config)
    owner.roles.append("system_admin")
    db.commit()
    app = create_app(config)
    client = TestClient(app)
    try:
        login = client.post("/auth/login", json={"username": owner.username, "password": "manager password"})
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        yield db, client, owner, kb, document, version, block
    finally:
        client.close()
        app.state.engine.dispose()


def write_headers(revision=None):
    headers = {"Idempotency-Key": str(uuid.uuid4())}
    if revision is not None:
        headers["If-Match"] = f'"{revision}"'
    return headers


def previewed_action(client, document, version, action, *, expected_allowed=True, **payload):
    path = f"/documents/{document.id}/versions/{version.id}"
    preview = client.post(path + "/publication-preview", json={"action": action}, headers=write_headers())
    assert preview.status_code == 200, preview.text
    assert preview.json()["allowed"] is expected_allowed, preview.text
    return client.post(path + "/" + action, json={"preview_id": preview.json()["preview_id"], **payload},
        headers={**write_headers(), "If-Match": preview.json()["document_etag"]})


def test_query_replay_returns_same_task_and_completed_receipt(repair_http):
    db, client, _, kb, *_ = repair_http
    headers = write_headers()
    payload = {"query": "开机步骤", "model": "DEMO-1", "knowledge_base_ids": [kb.id]}
    first = client.post("/knowledge/queries", json=payload, headers=headers)
    replay = client.post("/knowledge/queries", json=payload, headers=headers)
    assert first.status_code == replay.status_code == 202
    assert replay.json()["task_id"] == first.json()["task_id"]
    assert len(db.scalars(select(QueryTask)).all()) == 1
    receipt = db.scalar(select(ApiIdempotencyRecord).where(ApiIdempotencyRecord.idempotency_key == headers["Idempotency-Key"]))
    db.refresh(receipt)
    assert receipt.status == "completed"
    conflict = client.post("/knowledge/queries", json={**payload, "query": "different"}, headers=headers)
    assert conflict.status_code == 409


def test_cancel_replay_uses_original_receipt_after_revision_changes(repair_http):
    _, client, _, kb, *_ = repair_http
    result = client.post("/knowledge/queries", json={"query": "开机步骤", "knowledge_base_ids": [kb.id]}, headers=write_headers())
    task_id = result.json()["task_id"]
    headers = write_headers(0)
    first = client.post(f"/tasks/{task_id}/cancel", json={"reason": "synthetic cancel"}, headers=headers)
    again = client.post(f"/tasks/{task_id}/cancel", json={"reason": "synthetic cancel"}, headers=headers)
    assert first.status_code == again.status_code == 200
    changed_condition = client.post(f"/tasks/{task_id}/cancel", json={"reason": "synthetic cancel"}, headers={**headers, "If-Match": '"1"'})
    assert changed_condition.status_code == 409
    terminal_headers = write_headers(1)
    terminal = client.post(f"/tasks/{task_id}/cancel", json={"reason": "synthetic cancel"}, headers=terminal_headers)
    terminal_again = client.post(f"/tasks/{task_id}/cancel", json={"reason": "synthetic cancel"}, headers=terminal_headers)
    assert terminal.status_code == terminal_again.status_code == 200


@pytest.mark.parametrize("field,value", [("applicability_status", "confirmed"), ("content_review_status", "approved"), ("model_scope", ["OTHER-MODEL"])])
def test_version_metadata_cannot_write_confirmation_or_scope(repair_http, field, value):
    _, client, _, _, document, version, _ = repair_http
    response = client.patch(f"/documents/{document.id}/versions/{version.id}", json={field: value}, headers=write_headers(version.revision))
    assert response.status_code == 422


def test_withdraw_does_not_rewrite_business_applicability(repair_http):
    db, client, _, _, document, version, _ = repair_http
    response = previewed_action(client, document, version, "withdraw", reason="withdrawal_requested")
    assert response.status_code == 200
    db.refresh(version)
    assert version.publish_status == "withdrawn"
    assert version.applicability_status == "confirmed"


def test_legacy_admin_approve_cannot_create_business_approval(repair_http):
    db, client, owner, kb, *_ = repair_http
    approval = BusinessApproval(applicant=owner.id, approver=owner.id, scope={"knowledge_base_id": kb.id}, operations=["document.read"], status="pending")
    db.add(approval)
    db.commit()
    response = client.post(f"/admin/business-approvals/{approval.id}/approve", headers=write_headers())
    assert response.status_code == 409
    db.refresh(approval)
    assert approval.status == "pending"


def test_approval_recorder_is_not_invented_as_actual_approver(repair_http):
    db, client, owner, kb, *_ = repair_http
    response = client.post("/admin/business-approvals", json={"applicant": owner.id, "scope": {"knowledge_base_id": kb.id}, "operations": ["document.read"]}, headers=write_headers())
    assert response.status_code == 201
    approval = db.get(BusinessApproval, response.json()["id"])
    assert approval.status == "pending" and approval.approved_at is None
    assert approval.approver is None
    assert approval.recorded_by == owner.id
    assert approval.verification_state == "unverified"


@pytest.mark.parametrize("invalid", ["unverified", "scope", "subject"])
def test_current_access_rejects_unverified_or_mismatched_approval(repair_http, invalid):
    db, _, owner, kb, document, *_ = repair_http
    approvals = db.scalars(select(BusinessApproval).where(BusinessApproval.applicant == owner.id)).all()
    for approval in approvals:
        if invalid == "unverified":
            approval.verification_state = "unverified"
        elif invalid == "scope":
            approval.scope = {**approval.scope, "category": "other-category"}
        else:
            approval.applicant = "other-subject"
    db.commit()
    assert not can_access_document(db, owner, document)
    assert not can_manage_kb(db, owner, kb.id)
    assert not can_query_kb(db, owner, kb.id)


def test_default_kb_creation_does_not_manufacture_business_approvals(repair_http):
    db, _, owner, *_ = repair_http
    kb = create_default_kb(db, owner, "synthetic-empty-kb")
    db.flush()
    assert not db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.knowledge_base_id == kb.id)).all()
    assert not can_manage_kb(db, owner, kb.id)


def test_publish_uses_selected_batch_and_preserves_unknown_effective_date(repair_http):
    db, client, actor, kb, document, version, block = repair_http
    selected = version.selected_chunk_set_id
    version.publish_status = "draft"
    db.add(ParseRun(document_version_id=version.id, origin="manual", status="needs_review"))
    db.commit()
    response = previewed_action(client, document, version, "publish")
    assert response.status_code == 200, response.text
    db.expire_all()  # Publication committed in the HTTP session.
    db.refresh(version)
    assert version.effective_at is None
    assert version.selected_chunk_set_id == selected
    from .governance import publication_blockers, publication_is_current, qualification_snapshot
    from .governance_models import PublicationEvent
    events = db.scalars(select(PublicationEvent).where(PublicationEvent.document_version_id == version.id).order_by(PublicationEvent.created_at.desc())).all()
    assert events[0].bound_revisions["qualification"] == qualification_snapshot(db, version)
    assert publication_is_current(db, version), {"blockers": publication_blockers(db, version),
        "events": [x.bound_revisions.get("qualification") for x in events], "current": qualification_snapshot(db, version)}
    generation = create_index(db, kb, actor)
    db.flush()
    source = db.scalar(select(IndexSource).where(IndexSource.index_generation_id == generation.id))
    assert source.parse_run_id == block.parse_run_id
    assert source.chunk_set_id == selected


def test_unbound_legacy_version_is_not_implicitly_selected(repair_http):
    db, client, actor, kb, document, version, _ = repair_http
    version.publish_status = "draft"
    version.selected_chunk_set_id = None
    db.commit()
    response = previewed_action(client, document, version, "publish", expected_allowed=False)
    assert response.status_code == 409
    version.publish_status = "effective"  # Synthetic legacy database state.
    db.commit()
    generation = create_index(db, kb, actor)
    db.flush()
    assert generation.build_report["included_versions"] == []


def test_parse_replay_does_not_invent_success_for_pending_review(repair_http):
    _, client, _, _, document, version, _ = repair_http
    headers = write_headers()
    path = f"/documents/{document.id}/versions/{version.id}/parse-runs"
    first = client.post(path, headers=headers)
    replay = client.post(path, headers=headers)
    assert first.status_code == replay.status_code == 202
    assert first.json()["status"] == replay.json()["status"] == "pending"
    assert first.json()["parse_run_id"] == replay.json()["parse_run_id"]


def test_index_build_fails_if_selected_warning_block_is_unreviewed(repair_http):
    db, _, actor, kb, _, version, block = repair_http
    db.add(ContentBlock(parse_run_id=block.parse_run_id, ordinal=2, kind="warning", text_content="Synthetic critical warning pending review", review_status="unreviewed"))
    db.commit()
    generation = create_index(db, kb, actor)
    assert generation.status == "failed"
    assert generation.build_report["integrity"]["blockers"][0]["version_id"] == version.id


def test_publish_rejects_unknown_applicable_models(repair_http):
    db, client, _, _, document, version, _ = repair_http
    version.publish_status = "draft"
    version.model_scope = []
    db.commit()
    response = previewed_action(client, document, version, "publish", expected_allowed=False)
    assert response.status_code == 409


def test_existing_effective_version_cannot_be_republished_with_new_key(repair_http):
    _, client, _, _, document, version, _ = repair_http
    response = previewed_action(client, document, version, "publish", expected_allowed=False)
    assert response.status_code == 409


@pytest.mark.parametrize("legacy", [False, True])
def test_selected_binding_migration_preserves_old_rows_without_guessing(tmp_path, legacy):
    config = Settings(database_url="sqlite:///" + (tmp_path / "migration.sqlite").as_posix(), storage_dir=tmp_path / "storage")
    engine = build_engine(config.database_url)
    try:
        if legacy:
            # Full v1 DDL generated from the preserved pre-0002 model snapshot,
            # never from the live ORM. The fixture contains no business data.
            raw = engine.raw_connection()
            try:
                raw.driver_connection.executescript((Path(__file__).parent / "migrations" / "fixtures" / "v1_schema.sql").read_text(encoding="utf-8"))
                raw.commit()
            finally:
                raw.close()
            with engine.begin() as conn:
                conn.exec_driver_sql("INSERT INTO users(id,display_name,username,status,credential_state,credential_revision,authz_revision,roles,created_at) VALUES ('legacy-subject','SYNTHETIC legacy subject','synthetic-legacy','active','pending',0,0,'[]','2026-01-01')")
                conn.exec_driver_sql("INSERT INTO knowledge_bases(id,name,status,content_revision,acl_revision,created_at) VALUES ('legacy-kb','SYNTHETIC legacy KB','active',0,0,'2026-01-01')")
                conn.exec_driver_sql("INSERT INTO documents(id,knowledge_base_id,title,access_mode,revision,created_at) VALUES ('legacy-document','legacy-kb','SYNTHETIC legacy document','inherit',0,'2026-01-01')")
                conn.exec_driver_sql("INSERT INTO file_objects(id,storage_name,object_key,sha256,byte_size,mime_type,status,created_at) VALUES ('legacy-file','local','synthetic/legacy.pdf','synthetic-hash',1,'application/pdf','available','2026-01-01')")
                conn.exec_driver_sql("INSERT INTO document_versions(id,document_id,version_no,model_scope,applicability_status,content_review_status,source_file_id,publish_status,created_by,revision,created_at) VALUES ('legacy-version','legacy-document',1,'[]','confirmed','approved','legacy-file','effective','legacy-subject',0,'2026-01-01')")
                conn.exec_driver_sql("INSERT INTO business_approvals(id,applicant,approver,scope,operations,valid_from,status) VALUES ('legacy-approval','legacy-subject','legacy-actor','{}','[]','2026-01-01','approved')")
                conn.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
                conn.exec_driver_sql("INSERT INTO alembic_version VALUES ('0001_initial')")
        init_db(engine, config, migrate=True)
        assert "selected_chunk_set_id" in {column["name"] for column in inspect(engine).get_columns("document_versions")}
        with engine.connect() as conn:
            from alembic.config import Config
            from alembic.script import ScriptDirectory
            migration_config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
            migration_config.set_main_option("script_location", str(Path(__file__).parent / "migrations"))
            assert conn.scalar(text("SELECT version_num FROM alembic_version")) == ScriptDirectory.from_config(migration_config).get_current_head()
            if legacy:
                row = conn.execute(text("SELECT id, source_file_id, selected_chunk_set_id FROM document_versions")).one()
                assert tuple(row) == ("legacy-version", "legacy-file", None)
                approval = conn.execute(text("SELECT approver, status, recorded_by, verification_state FROM business_approvals")).one()
                assert tuple(approval) == ("legacy-actor", "approved", None, "unverified")
                version_unknowns = conn.execute(text("SELECT selected_review_set_id,reference_access_state,source_metadata,effective_at FROM document_versions")).one()
                assert tuple(version_unknowns) == (None, "closed", "{}", None)
                for table in ("users", "knowledge_bases", "documents", "file_objects", "document_versions", "business_approvals"):
                    assert conn.scalar(text(f"SELECT COUNT(*) FROM {table}")) == 1
                assert conn.scalar(text("SELECT COUNT(*) FROM document_business_confirmations")) == 0
                assert conn.scalar(text("SELECT COUNT(*) FROM authority_bases")) == 0
    finally:
        engine.dispose()
