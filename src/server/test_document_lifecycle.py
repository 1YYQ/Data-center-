"""Synthetic-only lifecycle checks on explicitly migrated isolated SQLite."""
from datetime import timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.requests import Request

from .app import create_app
from .document_lifecycle import delete_document, eligible_resolution_refs, lifecycle_view
from .governance import create_hold
from .governance_models import BusinessConfirmation, GovernanceOutbox, ReviewSet
from .lifecycle_api import register_lifecycle_routes
from .models import AuditEvent, AuthorizationGrant, Document, DocumentVersion, FileObject, QueryTask, User, utc_now
from .security import hash_password
from .services import SafeError, build_query_result, can_access_document
from .test_services import _ready_fixture, fixture_db


def _app(config):
    app = create_app(config)
    if not any(route.path == "/documents/{document_id}/lifecycle" for route in app.routes):
        register_lifecycle_routes(app)
    return app


def _request(document, revision):
    return Request({"type": "http", "method": "DELETE", "scheme": "http", "server": ("testserver", 80),
        "path": f"/documents/{document.id}", "query_string": b"", "headers": [(b"if-match", str(revision).encode())]})


def _login(client, actor):
    response = client.post("/auth/login", json={"username": actor.username, "password": "manager password"})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def test_deleted_receipt_replays_without_redeleting_and_retains_original(fixture_db):
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    task = QueryTask(owner_user_id=actor.id, kind="query", status="running", idempotency_key="lifecycle-result",
        input_snapshot={"query": "开机步骤", "model": "DEMO-1", "knowledge_base_ids": [kb.id]})
    db.add(task); db.flush()
    build_query_result(db, task, actor); db.commit()
    assert task.status == "succeeded"
    source = db.get(FileObject, version.source_file_id)
    source_key = source.object_key
    revision = document.revision
    request, key = _request(document, revision), str(uuid4())
    delete_document(db, actor, request, document, str(revision), key, "synthetic deletion", b"delete")
    assert document.deleted_at is not None
    assert db.get(FileObject, version.source_file_id).status == "available"
    assert (config.storage_dir / source_key).is_file()
    first_revision = document.revision
    delete_document(db, actor, request, document, str(revision), key, "synthetic deletion", b"delete")
    assert document.revision == first_revision
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.resource_id == document.id,
        AuditEvent.action == "document.deleted")) == 1
    with pytest.raises(SafeError) as failure:
        delete_document(db, actor, _request(document, first_revision), document, str(first_revision), str(uuid4()), "again", b"delete")
    assert failure.value.code == "DOCUMENT_ALREADY_DELETED"
    db.rollback()
    assert not can_access_document(db, actor, document)
    assert not can_access_document(db, actor, document, include_deleted=True)
    assert not can_access_document(db, actor, document, "knowledge.query", include_deleted=True)
    app = _app(config)
    try:
        with TestClient(app) as client:
            _login(client, actor)
            assert client.get(f"/documents/{document.id}/versions/{version.id}/file").status_code == 404
            assert client.get(f"/tasks/{task.id}/result").status_code in (403, 404)
            result = client.get(f"/documents/{document.id}/lifecycle")
            assert result.status_code == 200, result.text
            assert result.headers["ETag"] == result.json()["document"]["etag"]
            assert "no-store" in result.headers["Cache-Control"]
            assert result.json()["versions"][0]["original"]["status"] == "available"
            assert result.json()["purge_allowed"] is False
    finally:
        app.state.engine.dispose()


def test_lifecycle_whitelists_metadata_and_filters_current_management(fixture_db):
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    employee = User(username="synthetic-life-employee-" + uuid4().hex, display_name="SYNTHETIC employee",
        credential_state="active", credential_hash=hash_password("manager password"), roles=["employee"], status="active")
    db.add(employee)
    db.add(AuditEvent(actor_user_id=actor.id, action="task.shared", resource_type="document", resource_id=document.id,
        reason="SYNTHETIC forbidden reason", metadata_json={"body": "SYNTHETIC arbitrary audit body"}))
    db.add(AuditEvent(actor_user_id=actor.id, action="document.updated", resource_type="document", resource_id=document.id,
        reason="SYNTHETIC untrusted reason", metadata_json={"body": "SYNTHETIC arbitrary audit body"}))
    other = Document(knowledge_base_id=kb.id, title="SYNTHETIC OUTSIDE CATEGORY", access_level=document.access_level,
        category="SYNTHETIC outside class", need_class=document.need_class)
    db.add(other); db.commit()
    app = _app(config)
    try:
        with TestClient(app) as client:
            _login(client, actor)
            base = f"/documents/{document.id}"
            response = client.get(base + "/lifecycle")
            assert response.status_code == 200, response.text
            data = response.json()
            assert all("metadata" not in event for event in data["history"])
            assert "SYNTHETIC arbitrary audit body" not in response.text
            assert "SYNTHETIC untrusted reason" not in response.text
            assert "task.shared" not in response.text
            assert block.text_content not in response.text
            assert "task_count" not in client.get(base + "/deletion-impact").text
            listed = client.get(f"/knowledge-bases/{kb.id}/document-lifecycle?state=all").json()
            assert [row["id"] for row in listed["items"]] == [document.id]
            assert client.get(f"/documents/{other.id}/lifecycle").status_code == 404
            assert client.get(f"/documents/{other.id}/deletion-impact").status_code == 404
            _login(client, employee)
            denied = client.get(base + "/lifecycle")
            assert denied.status_code == 404
            assert document.title not in denied.text
            assert client.get(f"/knowledge-bases/{kb.id}/document-lifecycle").status_code == 404
    finally:
        app.state.engine.dispose()


def test_deleted_metadata_rechecks_revocation_even_for_original_receipt(fixture_db):
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    revision, key = document.revision, str(uuid4())
    request = _request(document, revision)
    delete_document(db, actor, request, document, str(revision), key, "synthetic", b"delete")
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == actor.id,
        AuthorizationGrant.operation == "knowledge.manage"))
    grant.revoked_at = utc_now(); db.commit()
    with pytest.raises(SafeError) as failure:
        delete_document(db, actor, request, document, str(revision), key, "synthetic", b"delete")
    assert failure.value.status_code == 404
    app = _app(config)
    try:
        with TestClient(app) as client:
            _login(client, actor)
            assert client.get(f"/documents/{document.id}/lifecycle").status_code == 404
            assert client.get(f"/documents/{document.id}/deletion-impact").status_code == 404
            assert client.get(f"/knowledge-bases/{kb.id}/document-lifecycle?state=deleted").json()["items"] == []
    finally:
        app.state.engine.dispose()


def test_hold_resolution_options_require_current_actual_evidence_and_time(fixture_db):
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    quality = create_hold(db, actor, version, {"kind": "quality", "reason": "SYNTHETIC quality"})
    conflict = create_hold(db, actor, version, {"kind": "conflict", "reason": "SYNTHETIC conflict"})
    db.commit()
    assert eligible_resolution_refs(db, version, quality) == []
    assert eligible_resolution_refs(db, version, conflict) == []
    quality.created_at = conflict.created_at = utc_now() - timedelta(days=1)
    db.commit()
    review = db.get(ReviewSet, version.selected_review_set_id)
    confirmation = db.scalar(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == version.id,
        BusinessConfirmation.kind == "applicability"))
    assert eligible_resolution_refs(db, version, quality) == [review.id]
    assert eligible_resolution_refs(db, version, conflict) == [confirmation.id]
    projected = lifecycle_view(db, actor, document)
    holds = projected["versions"][0]["holds"]
    assert next(h for h in holds if h["id"] == quality.id)["eligible_resolution_refs"] == [review.id]
    assert all(h["etag"].startswith('"') for h in holds)
    block.review_status = "unreviewed"
    confirmation.verification_state = "pending"
    db.commit()
    assert eligible_resolution_refs(db, version, quality) == []
    # Verification state columns alone never establish truth; break the bound source.
    version.source_metadata = {"channel": "synthetic", "source_description": "SYNTHETIC changed source"}
    db.commit()
    assert eligible_resolution_refs(db, version, conflict) == []


def test_delete_requires_document_etag_and_preserves_scope(fixture_db):
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    with pytest.raises(SafeError) as failure:
        delete_document(db, actor, _request(document, -1), document, "-1", str(uuid4()), "synthetic", b"delete")
    assert failure.value.status_code == 412
    db.rollback()
    assert document.deleted_at is None
    assert db.get(FileObject, version.source_file_id).status == "available"


def test_http_delete_is_one_atomic_tombstone_receipt_and_safe_metadata(fixture_db):
    db, config = fixture_db
    actor, kb, document, version, block = _ready_fixture(db, config)
    hold = create_hold(db, actor, version, {"kind": "conflict", "reason": "SYNTHETIC conflict"})
    hold.created_at = utc_now() - timedelta(days=1)
    db.commit()
    assert eligible_resolution_refs(db, version, hold)
    content_revision, acl_revision, document_revision = kb.content_revision, kb.acl_revision, document.revision
    app = _app(config)
    try:
        with TestClient(app) as client:
            _login(client, actor)
            path = f"/documents/{document.id}"
            before = client.get(path + "/lifecycle")
            version_projection = before.json()["versions"][0]
            assert version_projection["etag"] == f'"{version.revision}"'
            assert before.json()["physical_integrity_verified"] is False
            assert client.delete(path, headers={"Idempotency-Key": str(uuid4())}).status_code == 428
            headers = {"Idempotency-Key": str(uuid4()), "If-Match": before.headers["ETag"]}
            assert client.delete(path, headers=headers).status_code == 204
            assert client.delete(path, headers=headers).status_code == 204
            deleted = client.get(path + "/lifecycle")
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["document"]["state"] == "deleted"
            assert deleted.json()["versions"][0]["holds"][0]["eligible_resolution_refs"] == []
            assert deleted.json()["purge_allowed"] is False
            assert client.get(f"/knowledge-bases/{kb.id}/document-lifecycle?state=active").json()["items"] == []
            rows = client.get(f"/knowledge-bases/{kb.id}/document-lifecycle?state=deleted&limit=1").json()["items"]
            assert [row["id"] for row in rows] == [document.id]
            newer = {"Idempotency-Key": str(uuid4()), "If-Match": deleted.headers["ETag"]}
            assert client.delete(path, headers=newer).status_code == 409
            assert client.get(path + f"/versions/{version.id}/file").status_code == 404
        db.refresh(kb); db.refresh(document)
        assert kb.content_revision == content_revision + 1
        assert kb.acl_revision == acl_revision + 1
        assert document.revision == document_revision + 1
        assert db.scalar(select(func.count()).select_from(GovernanceOutbox).where(
            GovernanceOutbox.event_kind == "document.deleted", GovernanceOutbox.resource_id == document.id)) == 1
        assert db.get(FileObject, version.source_file_id, populate_existing=True).status == "available"
    finally:
        app.state.engine.dispose()
