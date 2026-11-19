"""Isolated synthetic governance checks; no enterprise assertions or network."""
from datetime import timedelta
import uuid
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from .models import (Base, AuthorizationGrant, BusinessApproval, ChunkSet, ContentBlock, Document,
    DocumentVersion, FileObject, KnowledgeBase, ParseRun, User, utc_now)
from .governance_models import AuthorityBasis, BusinessConfirmation, GovernanceEvidence, PublicationEvent, ReviewSet
from . import governance as g
from .services import SafeError, can_access_document
from .testing_governance import seed_confirmation, seed_verified_approval, seed_verified_version
from . import execution_models, runtime_models  # metadata for shared foreign keys
from .test_services import _ready_fixture, fixture_db


@pytest.fixture()
def local_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as db:
        yield db
    engine.dispose()


def synthetic_draft(db):
    actor = User(username="synthetic-" + uuid.uuid4().hex, display_name="Synthetic manager", status="active", roles=["knowledge_manager"])
    db.add(actor)
    kb = KnowledgeBase(name="synthetic-" + uuid.uuid4().hex)
    db.add(kb)
    db.flush()
    scope = {"knowledge_base_id": kb.id, "document_id": None, "level": "internal", "category": "manual", "need_class": "maintenance"}
    for operation in ("knowledge.manage", "document.read", "knowledge.query"):
        approval = g.register_approval(db, actor, {"applicant": actor.id, "scope": scope, "operations": [operation]})
        seed_verified_approval(db, approval)
        db.add(AuthorizationGrant(subject_user_id=actor.id, knowledge_base_id=kb.id,
            operation=operation, level="internal", category="manual", need_class="maintenance", approval_id=approval.id, valid_until=approval.valid_until))
    file = FileObject(object_key="synthetic/" + uuid.uuid4().hex, sha256="a" * 64, byte_size=20, mime_type="application/pdf", status="available", created_by=actor.id)
    db.add(file)
    db.flush()
    doc = Document(knowledge_base_id=kb.id, title="SYNTHETIC manual", access_level="internal", category="manual", need_class="maintenance")
    db.add(doc)
    db.flush()
    version = DocumentVersion(document_id=doc.id, version_no=1, model_scope=["SYNTHETIC-1"], source_file_id=file.id,
        source_metadata={"channel": "synthetic", "source_description": "local synthetic original"}, created_by=actor.id)
    db.add(version)
    db.flush()
    run = ParseRun(document_version_id=version.id, status="needs_review", origin="synthetic")
    db.add(run)
    db.flush()
    for ordinal, kind, text in ((0, "paragraph", "SYNTHETIC startup instruction"), (1, "warning", "SYNTHETIC warning: disconnect before service"), (2, "footnote", "SYNTHETIC limit applies to model 1")):
        db.add(ContentBlock(parse_run_id=run.id, ordinal=ordinal, kind=kind, text_content=text, locator={"file_page": 1, "section": str(ordinal)}))
    chunk = ChunkSet(parse_run_id=run.id, status="ready")
    db.add(chunk)
    db.commit()
    return actor, kb, doc, version, run, chunk


def governed_version(db):
    actor, kb, doc, version, run, chunk = synthetic_draft(db)
    seed_verified_version(db, actor, doc, version, run, chunk.id)
    db.commit()
    assert g.publication_is_current(db, version)
    return actor, kb, doc, version, run, chunk


def test_admin_cannot_create_external_approval_fact(local_db):
    db = local_db
    actor, _, doc, _, _, _ = synthetic_draft(db)
    row = g.register_approval(db, actor, {"applicant": actor.id, "scope": {"knowledge_base_id": doc.knowledge_base_id,
        "document_id": None, "level": "internal", "category": "manual", "need_class": "maintenance"}, "operations": ["document.read"],
        "actual_approver": {"identity_label": "I am the approver", "responsibility": "self asserted", "authority_basis_ref": "invented"},
        "decision": "approved", "approved_at": utc_now(), "valid_until": utc_now() + timedelta(days=1)})
    assert row.status == "pending" and row.approver is None
    verifier = db.get(User, db.scalar(select(BusinessApproval.verified_by).where(BusinessApproval.verified_by.is_not(None))))
    with pytest.raises(SafeError) as error:
        g.verify_approval(db, verifier, row, {"decision": "verified", "reason": "clicked approve", "checked_evidence_refs": [], "authority_basis_ref": "invented"})
    assert error.value.detail["code"] == "PROOF_UNVERIFIABLE"
    with pytest.raises(SafeError) as error:
        g.verify_approval(db, actor, row, {"decision": "verified", "reason": "unassigned registrar"})
    assert error.value.status_code == 403
    assert not g.approval_is_current(db, row)


def test_approval_evidence_authority_scope_and_expiry_are_current_gates(local_db):
    db = local_db
    actor, _, doc, _, _, _ = synthetic_draft(db)
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.operation == "document.read"))
    approval = db.get(BusinessApproval, grant.approval_id)
    assert g.approval_is_current(db, approval)
    proof = db.get(GovernanceEvidence, approval.proof_refs[0])
    proof.assertion = {**proof.assertion, "applicant": "different"}
    db.commit()
    assert not g.approval_is_current(db, approval)
    proof.assertion = g._approval_assertion(approval)
    db.commit()
    assert g.approval_is_current(db, approval)
    basis = db.get(AuthorityBasis, approval.actual_approver_snapshot["authority_basis_ref"])
    basis.valid_until = utc_now() - timedelta(seconds=1)
    db.commit()
    assert not g.approval_is_current(db, approval)


def test_review_coverage_cannot_omit_warning_or_use_wrong_batch(local_db):
    db = local_db
    actor, _, doc, version, run, chunk = synthetic_draft(db)
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)).all()
    for block in blocks:
        g.review_content_block(db, actor, run, block, "approved", "synthetic review")
    db.flush()
    with pytest.raises(SafeError) as error:
        g.create_review_set(db, actor, version, {"parse_run_id": run.id, "chunk_set_id": chunk.id, "reviewed_block_refs": [blocks[0].id]})
    assert error.value.detail["code"] == "REVIEW_COVERAGE_INCOMPLETE"
    with pytest.raises(SafeError) as error:
        g.create_review_set(db, actor, version, {"parse_run_id": run.id, "chunk_set_id": chunk.id, "reviewed_block_refs": [{"id": x.id, "revision": x.revision, "content_hash": g.block_binding(x)} for x in blocks]})
    assert error.value.detail["code"] == "CRITICAL_CONFIRMATION_MISSING"
    other_run = ParseRun(document_version_id=version.id, status="succeeded")
    db.add(other_run)
    db.flush()
    other_chunk = ChunkSet(parse_run_id=other_run.id)
    db.add(other_chunk)
    db.flush()
    with pytest.raises(SafeError) as error:
        g.create_review_set(db, actor, version, {"parse_run_id": run.id, "chunk_set_id": other_chunk.id, "reviewed_block_refs": [x.id for x in blocks]})
    assert error.value.detail["code"] == "INVALID_REVIEW_BATCH"


def test_content_or_locator_changes_invalidate_precise_confirmation(local_db):
    db = local_db
    actor, _, _, version, run, _ = governed_version(db)
    block = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == run.id, ContentBlock.kind == "warning"))
    with pytest.raises(SafeError) as error:
        g.review_content_block(db, actor, run, block, "approved", "cannot mutate published batch")
    assert error.value.detail["code"] == "PUBLISHED_BATCH_IMMUTABLE"
    block.locator = {"file_page": 2, "section": "different"}
    db.commit()
    assert not g.publication_is_current(db, version)
    assert "REVIEW_COVERAGE_INCOMPLETE" in g.publication_blockers(db, version)


def test_preview_is_persisted_bound_consumable_and_unknown_effective_date_stays_empty(local_db):
    db = local_db
    actor, _, doc, version, _, _ = governed_version(db)
    assert version.effective_at is None
    events = db.scalars(select(PublicationEvent)).all()
    assert len(events) == 1 and events[0].action == "publish"
    withdraw = g.create_publication_preview(db, actor, doc, version, {"action": "withdraw"})
    db.commit()
    version.revision += 1
    db.commit()
    with pytest.raises(SafeError) as error:
        g.withdraw_from_preview(db, actor, doc, version, {"preview_id": withdraw.id, "reason": "synthetic"})
    assert error.value.detail["code"] == "PUBLICATION_PREVIEW_STALE"
    withdraw = g.create_publication_preview(db, actor, doc, version, {"action": "withdraw"})
    db.flush()
    result = g.withdraw_from_preview(db, actor, doc, version, {"preview_id": withdraw.id, "reason": "synthetic"})
    db.commit()
    assert result["operation_receipt_ref"] and version.publish_status == "withdrawn"
    assert version.applicability_status == "confirmed"
    with pytest.raises(SafeError):
        g.withdraw_from_preview(db, actor, doc, version, {"preview_id": withdraw.id})


def test_supersession_and_hold_do_not_change_actual_business_conclusion(local_db):
    db = local_db
    actor, _, _, version, _, _ = governed_version(db)
    old = g.current_confirmations(db, version, "applicability")[0]
    hold = g.create_hold(db, actor, version, {"kind": "conflict", "reason": "synthetic conflict", "affected_refs": [old.id]})
    db.commit()
    assert old.conclusion == "applicable" and not g.publication_is_current(db, version)
    replacement = seed_confirmation(db, actor, version, "applicability", "applicable", g.source_binding(db, version), supersedes=old.id)
    db.commit()
    assert not g.confirmation_is_current(db, old)
    assert g.confirmation_is_current(db, replacement)
    g.release_hold(db, actor, hold, {"reason": "synthetic resolution", "resolution_refs": [replacement.id]})
    db.commit()
    assert not g.publication_is_current(db, version)


def test_reference_open_is_exact_human_inline_only_and_revocation_is_immediate(local_db):
    db = local_db
    actor, _, doc, version, _, _ = synthetic_draft(db)
    assert not g.reference_is_readable(db, actor, doc, version)
    g.set_reference_access(db, actor, version, {"state": "open", "reason": "synthetic preview"})
    db.commit()
    assert g.reference_is_readable(db, actor, doc, version)
    for purpose in ("answer", "calculate", "report", "model", "download"):
        assert not g.reference_is_readable(db, actor, doc, version, purpose=purpose)
    assert not g.publication_is_current(db, version)
    other_actor, _, other_doc, other_version, _, _ = synthetic_draft(db)
    assert not g.reference_is_readable(db, actor, other_doc, other_version)
    assert not g.reference_is_readable(db, actor, other_doc, version)
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == actor.id, AuthorizationGrant.operation == "document.read"))
    grant.revoked_at = utc_now()
    db.commit()
    assert not g.reference_is_readable(db, actor, doc, version)


def test_classification_keeps_version_tuple_and_document_constraints(local_db):
    db = local_db
    actor, _, doc, version, _, _ = governed_version(db)
    assert g.can_read_version(db, actor, version)
    assert g.published_query_is_readable(db, actor, version, doc, context={"level": "internal"})
    assert not g.published_query_is_readable(db, actor, version, doc, context={"level": "restricted"})
    old = g.current_confirmations(db, version, "classification")[0]
    seed_confirmation(db, actor, version, "classification", "classified", {**g.source_binding(db, version),
        "access_level": "restricted", "category": doc.category, "need_class": doc.need_class, "rule_version": "synthetic-v2"}, supersedes=old.id)
    db.commit()
    assert g.version_access_conditions(db, version)["level"] == "restricted"
    assert not g.can_read_version(db, actor, version)
    assert not g.published_query_is_readable(db, actor, version, doc)


def test_unselected_reparse_does_not_replace_selected_publication(local_db):
    db = local_db
    actor, _, doc, version, _, chunk = governed_version(db)
    selected = version.selected_chunk_set_id
    # make_parse_run's derivative maintenance revision must not change selected
    # source eligibility until a new batch is explicitly selected and reviewed.
    run = ParseRun(document_version_id=version.id, status="needs_review", origin="synthetic")
    db.add(run)
    version.revision += 1
    db.commit()
    assert version.selected_chunk_set_id == selected == chunk.id
    assert g.publication_is_current(db, version)


def test_approval_replacement_is_append_only_and_invalidates_old_grants(local_db):
    db = local_db
    actor, _, doc, _, _, _ = synthetic_draft(db)
    old = db.scalar(select(BusinessApproval).where(BusinessApproval.operations == ["document.read"]))
    assert g.approval_is_current(db, old)
    actor.roles = ["knowledge_manager", "system_admin"]
    db.add(AuthorityBasis(identity={"user_ref": actor.id}, purposes=["access.manage"],
        scope=dict(old.scope), source_reference="SYNTHETIC scoped registration mandate", valid_from=utc_now()))
    db.commit()
    replacement = g.register_approval(db, actor, {"applicant": actor.id, "scope": dict(old.scope),
        "operations": list(old.operations), "supersedes_id": old.id})
    db.commit()
    assert old.actual_decision == "approved" and old.status == "approved"
    assert not g.approval_is_current(db, old)
    assert replacement.status == "pending" and not g.approval_is_current(db, replacement)


def test_http_governance_conditions_replay_and_cross_object_evidence(tmp_path):
    from http.cookies import SimpleCookie
    from fastapi import FastAPI, Response
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from .config import Settings
    from .governance_api import register_governance_routes
    from .services import create_session
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    app = FastAPI()
    app.state.engine, app.state.session_factory = engine, factory
    app.state.settings = Settings(database_url="sqlite://", storage_dir=tmp_path / "synthetic-storage")
    @app.exception_handler(SafeError)
    async def handler(request, error):
        return JSONResponse(error.detail, status_code=error.status_code)
    register_governance_routes(app)
    with factory() as db:
        manager, _, doc, version, _, _ = synthetic_draft(db)
        other, _, other_doc, other_version, _, _ = synthetic_draft(db)
        response = Response()
        session = create_session(db, manager, response, app.state.settings)
        cookie = SimpleCookie(response.headers["set-cookie"])
        token = cookie[app.state.settings.cookie_name].value
        document_id, version_id, initial_revision = doc.id, version.id, version.revision
        other_document_id, other_version_id = other_doc.id, other_version.id
    with TestClient(app) as client:
        client.cookies.set(app.state.settings.cookie_name, token)
        url = f"/documents/{document_id}/versions/{version_id}"
        headers = {"X-CSRF-Token": session["csrf_token"], "Idempotency-Key": "synthetic-reference"}
        assert client.put(url + "/reference-access", json={"state": "open", "reason": "synthetic"}, headers=headers).status_code == 428
        headers["If-Match"] = f'"{initial_revision}"'
        opened = client.put(url + "/reference-access", json={"state": "open", "reason": "synthetic"}, headers=headers)
        assert opened.status_code == 200, opened.text
        assert opened.json()["read_mode"] == "reference_only"
        replay = client.put(url + "/reference-access", json={"state": "open", "reason": "synthetic"}, headers=headers)
        assert replay.status_code == 200 and replay.json() == opened.json()
        changed = client.put(url + "/reference-access", json={"state": "closed", "reason": "synthetic"}, headers=headers)
        assert changed.status_code == 409
        preview = client.post(url + "/publication-preview", json={"action": "publish"}, headers={**headers, "Idempotency-Key": "synthetic-preview"})
        assert preview.status_code == 200 and preview.json()["allowed"] is False
        assert "BUSINESS_CONFIRMATION_PENDING" in preview.json()["blocked_reasons"]
        evidence = client.post(url + "/confirmation-evidence", data={"metadata": '{"purpose":"applicability","source_description":"SYNTHETIC attachment"}'},
            files={"file": ("synthetic.pdf", b"%PDF-1.4 synthetic attachment", "application/pdf")}, headers={**headers, "Idempotency-Key": "synthetic-evidence"})
        assert evidence.status_code == 201, evidence.text
        assert evidence.json()["verification_state"] == "unverified"
        evidence_id = evidence.json()["id"]
        assert client.get(url + f"/confirmation-evidence/{evidence_id}/file").status_code == 200
        cross = f"/documents/{other_document_id}/versions/{other_version_id}/confirmation-evidence/{evidence_id}/file"
        assert client.get(cross).status_code == 403
        approvals = client.get("/admin/business-approvals")
        assert approvals.status_code == 403  # DM never implies AM.
    engine.dispose()


def test_governance_migration_preserves_unverified_legacy_facts(tmp_path):
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import inspect, text
    engine = create_engine("sqlite:///" + str(tmp_path / "synthetic-old0003.sqlite"))
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)")
        conn.exec_driver_sql("CREATE TABLE business_approvals (id VARCHAR(36) PRIMARY KEY, applicant VARCHAR(36), approver VARCHAR(36), status VARCHAR(20), recorded_by VARCHAR(36), verification_state VARCHAR(30))")
        conn.exec_driver_sql("INSERT INTO business_approvals VALUES ('synthetic-old','subject','old-actor','approved',NULL,'unverified')")
        conn.exec_driver_sql("CREATE TABLE document_versions (id VARCHAR(36) PRIMARY KEY, source_file_id VARCHAR(36), publish_status VARCHAR(20), selected_chunk_set_id VARCHAR(36))")
        conn.exec_driver_sql("INSERT INTO document_versions VALUES ('synthetic-version','synthetic-file','effective',NULL)")
        module = importlib.import_module("src.server.migrations.versions.0004_governance")
        with Operations.context(MigrationContext.configure(conn)):
            module.upgrade()
            module.upgrade()
        row = conn.execute(text("SELECT approver,status,verification_state,actual_decision,proof_refs,recorded_at FROM business_approvals")).one()
        assert tuple(row) == ("old-actor", "approved", "unverified", None, "[]", None)
        row = conn.execute(text("SELECT source_file_id,publish_status,selected_chunk_set_id,selected_review_set_id,reference_access_state,source_metadata FROM document_versions")).one()
        assert tuple(row) == ("synthetic-file", "effective", None, None, "closed", "{}")
        assert "review_sets" in inspect(conn).get_table_names()
        assert conn.scalar(text("SELECT COUNT(*) FROM authority_bases")) == 0
        assert conn.scalar(text("SELECT COUNT(*) FROM governance_evidence")) == 0
    engine.dispose()


def test_publication_flushes_authorized_state_with_http_autoflush_disabled(local_db):
    db = local_db
    db.autoflush = False  # Same configuration as create_app's request sessions.
    actor, _, doc, version, _, _ = governed_version(db)
    db.expire_all()
    assert db.get(DocumentVersion, version.id).publish_status == "effective"
    assert g.publication_is_current(db, version)
    preview = g.create_publication_preview(db, actor, doc, version, {"action": "withdraw"})
    result = g.withdraw_from_preview(db, actor, doc, version, {"preview_id": preview.id, "reason": "synthetic withdrawal"})
    db.commit()
    db.expire_all()
    current = db.get(DocumentVersion, version.id)
    assert current.publish_status == "withdrawn" and current.applicability_status == "confirmed"
    assert db.get(PublicationEvent, result["operation_receipt_ref"]).action == "withdraw"


def test_pending_draft_and_unselected_review_keep_existing_formal_index(fixture_db):
    from .models import IndexGeneration
    from .services import search_blocks
    db, config = fixture_db
    actor, kb, doc, published, _ = _ready_fixture(db, config)
    index_revision, generation_id = kb.content_revision, kb.active_generation_id
    selected = (published.selected_chunk_set_id, published.selected_review_set_id)
    draft = DocumentVersion(document_id=doc.id, version_no=2, source_file_id=published.source_file_id,
        created_by=actor.id, model_scope=["DEMO-1"], source_metadata={"channel": "synthetic", "source_description": "SYNTHETIC pending revision"})
    db.add(draft)
    db.commit()

    def still_formal():
        db.expire_all()
        assert kb.content_revision == index_revision
        assert kb.active_generation_id == generation_id
        assert db.get(IndexGeneration, generation_id).source_revision == kb.content_revision
        matches, warnings = search_blocks(db, actor, "开机步骤", "DEMO-1", [kb.id])
        assert matches and {version.id for _, version, _ in matches} == {published.id}
        assert warnings == []
        assert (published.selected_chunk_set_id, published.selected_review_set_id) == selected

    for state in ("open", "closed"):
        g.set_reference_access(db, actor, draft, {"state": state, "reason": "synthetic reference visibility"})
        db.commit()
        still_formal()
    # An additional parse derivative on the published version is not selected
    # and cannot replace, edit or invalidate its published batch.
    run = ParseRun(document_version_id=published.id, status="needs_review", origin="synthetic")
    db.add(run)
    db.flush()
    block = ContentBlock(parse_run_id=run.id, ordinal=1, text_content="SYNTHETIC unselected reparse", locator={"file_page": 1})
    db.add(block)
    db.flush()
    g.review_content_block(db, actor, run, block, "approved", "synthetic derivative checked")
    db.commit()
    still_formal()
    # Pending draft parsing must not leak its preparation status into a valid
    # old edition's formal result either.
    draft_run = ParseRun(document_version_id=draft.id, status="needs_review", origin="synthetic")
    db.add(draft_run)
    db.flush()
    db.add(ContentBlock(parse_run_id=draft_run.id, ordinal=1, text_content="SYNTHETIC pending revision startup", locator={"file_page": 1}))
    db.commit()
    still_formal()


@pytest.mark.parametrize("change", ["withdraw", "hold", "source_metadata"])
def test_actual_formal_changes_advance_corpus_revision(fixture_db, change):
    from .models import IndexGeneration
    db, config = fixture_db
    actor, kb, doc, version, _ = _ready_fixture(db, config)
    revision, generation_id = kb.content_revision, kb.active_generation_id
    if change == "withdraw":
        preview = g.create_publication_preview(db, actor, doc, version, {"action": "withdraw"})
        g.withdraw_from_preview(db, actor, doc, version, {"preview_id": preview.id, "reason": "synthetic withdrawal"})
    elif change == "hold":
        g.create_hold(db, actor, version, {"kind": "quality", "reason": "synthetic quality concern", "affected_refs": []})
    else:
        version.source_metadata = {"channel": "synthetic", "source_description": "SYNTHETIC corrected provenance"}
        g._changed(db, actor, version, "version.source_metadata_changed")
    db.commit()
    db.expire_all()
    assert kb.content_revision > revision
    assert db.get(IndexGeneration, generation_id).source_revision != kb.content_revision
    assert not g.publication_is_current(db, version)


def test_first_attested_classification_establishes_unknown_baseline_and_publishes(local_db):
    db = local_db
    actor, _, doc, version, run, chunk = synthetic_draft(db)
    doc.access_level = doc.category = doc.need_class = None
    db.commit()
    binding = g.source_binding(db, version)
    classification = seed_confirmation(db, actor, version, "classification", "classified", {**binding,
        "access_level": "internal", "category": "manual", "need_class": "maintenance", "rule_version": "synthetic-initial-class-v1"})
    db.flush()
    assert (doc.access_level, doc.category, doc.need_class) == ("internal", "manual", "maintenance")
    assert g.confirmation_is_current(db, classification)
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)).all()
    for block in blocks:
        g.review_content_block(db, actor, run, block, "approved", "synthetic review")
    seed_confirmation(db, actor, version, "applicability", "applicable", binding)
    professional = seed_confirmation(db, actor, version, "key_content", "accepted", {**binding,
        "parse_run_id": run.id, "block_bindings": {x.id: g.block_binding(x) for x in blocks}})
    g.create_review_set(db, actor, version, {"parse_run_id": run.id, "chunk_set_id": chunk.id,
        "reviewed_block_refs": [{"id": x.id, "revision": x.revision, "content_hash": g.block_binding(x)} for x in blocks],
        "professional_confirmation_refs": [professional.id], "reason": "synthetic complete review"})
    preview = g.create_publication_preview(db, actor, doc, version, {"action": "publish"})
    assert preview.allowed
    g.publish_from_preview(db, actor, doc, version, {"preview_id": preview.id})
    db.commit()
    assert g.publication_is_current(db, version)
    assert g.can_read_version(db, actor, version)


@pytest.mark.parametrize("invalid", ["unattested", "wrong_source", "no_tuple_management_grant", "known_restriction"])
def test_initial_classification_does_not_fill_or_lower_outside_verified_scope(local_db, invalid):
    db = local_db
    actor, _, doc, version, _, _ = synthetic_draft(db)
    doc.access_level = "internal" if invalid == "known_restriction" else None
    doc.category = doc.need_class = None
    db.commit()
    initial = (doc.access_level, doc.category, doc.need_class)
    scope = {**g.source_binding(db, version), "access_level": "internal", "category": "manual",
        "need_class": "maintenance", "rule_version": "synthetic-initial-class-v1"}
    if invalid == "wrong_source":
        scope["source_sha256"] = "b" * 64
    elif invalid != "unattested":
        scope["access_level"] = "unapproved-level"
    if invalid == "unattested":
        from .testing_governance import _authority, _proof
        basis, identity = _authority(db, purpose="classification", scope={key: scope[key] for key in ("version_id", "source_sha256", "scope_hash")})
        item = g.register_confirmation(db, actor, version, {"kind": "classification", "conclusion": "classified", "scope": scope,
            "actual_confirmer": identity, "confirmed_at": utc_now() - timedelta(seconds=1), "proof_refs": []})
        proof = _proof(db, actor, "version", version.id, g._confirmation_assertion(item))
        proof.attested = False
        item.proof_refs = [proof.id]
        db.flush()
        with pytest.raises(SafeError):
            g.verify_confirmation(db, actor, item, {"decision": "verified", "reason": "synthetic untrusted upload",
                "checked_proof_refs": [proof.id], "authority_basis_ref": basis.id})
    else:
        with pytest.raises(SafeError):
            seed_confirmation(db, actor, version, "classification", "classified", scope)
    # Even a caller catching the domain error and committing must not persist a
    # partially filled baseline or a successful verification event.
    db.commit()
    db.refresh(doc)
    assert (doc.access_level, doc.category, doc.need_class) == initial
    item = db.scalar(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == version.id))
    assert item.verification_state == "pending"
    assert not g.confirmation_is_current(db, item)


def test_new_draft_cannot_define_missing_baseline_for_existing_published_version(fixture_db):
    db, config = fixture_db
    actor, _, doc, published, _ = _ready_fixture(db, config)
    old_classification = g.current_confirmations(db, published, "classification")[0]
    doc.need_class = None  # Explicit synthetic incomplete legacy metadata.
    draft = DocumentVersion(document_id=doc.id, version_no=2, model_scope=["DEMO-1"], source_file_id=published.source_file_id,
        created_by=actor.id, source_metadata={"channel": "synthetic", "source_description": "SYNTHETIC pending revision"})
    db.add(draft)
    db.commit()
    with pytest.raises(SafeError) as error:
        seed_confirmation(db, actor, draft, "classification", "classified", {**g.source_binding(db, draft),
            "access_level": "test-level", "category": "test-category", "need_class": "test-need", "rule_version": "synthetic-new-rule"})
    assert error.value.detail["code"] == "VERSION_SCOPE_CONFLICT"
    db.commit()
    db.refresh(doc)
    assert doc.need_class is None
    assert old_classification.scope["need_class"] == "test-need"
    assert old_classification.verification_state == "verified"


def test_real_http_draft_uploads_and_initial_classification_preserve_ready_old_index(fixture_db):
    from .app import create_app
    from .models import IndexGeneration
    from .services import search_blocks
    from .testing_governance import _authority, _proof
    from fastapi.testclient import TestClient
    db, config = fixture_db
    actor, kb, doc, published, _ = _ready_fixture(db, config)
    original_revision, generation_id = kb.content_revision, kb.active_generation_id
    app = create_app(config)
    client = TestClient(app)
    def headers(**extra):
        return {"Idempotency-Key": uuid.uuid4().hex, **extra}
    try:
        login = client.post("/auth/login", json={"username": actor.username, "password": "manager password"})
        assert login.status_code == 200
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        uploaded_version = client.post(f"/documents/{doc.id}/versions", params={"model_scope": "DEMO-1"},
            files={"file": ("synthetic-revision.pdf", b"%PDF-1.4 synthetic pending revision", "application/pdf")}, headers=headers())
        assert uploaded_version.status_code == 201, uploaded_version.text
        draft_data = uploaded_version.json()["version"]
        assert draft_data["publish_status"] == "draft" and draft_data["reference_access_state"] == "closed"
        reference_path = f"/documents/{doc.id}/versions/{draft_data['id']}/reference-access"
        revision = draft_data["revision"]
        for state in ("open", "closed"):
            response = client.put(reference_path, json={"state": state, "reason": "synthetic inline reference"}, headers=headers(**{"If-Match": f'"{revision}"'}))
            assert response.status_code == 200, response.text
            revision = int(response.json()["etag"].strip('"'))
        uploaded_document = client.post(f"/knowledge-bases/{kb.id}/documents", params={"model_scope": "DEMO-1"},
            files={"file": ("synthetic-new.pdf", b"%PDF-1.4 synthetic first upload", "application/pdf")}, headers=headers())
        assert uploaded_document.status_code == 201, uploaded_document.text
        new_document_id = uploaded_document.json()["document"]["id"]
        new_version_id = uploaded_document.json()["version"]["id"]
        source = client.patch(f"/documents/{new_document_id}/versions/{new_version_id}/source-metadata",
            json={"channel": "synthetic", "source_description": "SYNTHETIC received original"}, headers=headers(**{"If-Match": '"0"'}))
        assert source.status_code == 200, source.text
        db.expire_all()
        new_document, new_version = db.get(Document, new_document_id), db.get(DocumentVersion, new_version_id)
        assert (new_document.access_level, new_document.category, new_document.need_class) == (None, None, None)
        scope = {**g.source_binding(db, new_version), "access_level": "test-level", "category": "test-category",
            "need_class": "test-need", "rule_version": "synthetic-http-first-class"}
        basis, identity = _authority(db, purpose="classification", scope={key: scope[key] for key in ("version_id", "source_sha256", "scope_hash")})
        claim = g.register_confirmation(db, actor, new_version, {"kind": "classification", "conclusion": "classified",
            "scope": scope, "actual_confirmer": identity, "confirmed_at": utc_now() - timedelta(seconds=1), "proof_refs": []})
        proof = _proof(db, actor, "version", new_version.id, g._confirmation_assertion(claim))
        claim.proof_refs = [proof.id]
        db.commit()
        verified = client.post(f"/documents/{new_document_id}/versions/{new_version_id}/business-confirmations/{claim.id}/verification",
            json={"decision": "verified", "reason": "synthetic pre-attested controlled proof checked", "checked_proof_refs": [proof.id], "authority_basis_ref": basis.id},
            headers=headers(**{"If-Match": '"0"'}))
        assert verified.status_code == 200 and verified.json()["current"] is True, verified.text
        db.expire_all()
        assert (new_document.access_level, new_document.category, new_document.need_class) == ("test-level", "test-category", "test-need")
        assert kb.content_revision == original_revision
        assert kb.active_generation_id == generation_id
        assert db.get(IndexGeneration, generation_id).source_revision == kb.content_revision
        matches, warnings = search_blocks(db, actor, "开机步骤", "DEMO-1", [kb.id])
        assert matches and {version.id for _, version, _ in matches} == {published.id}
        assert warnings == []
    finally:
        client.close()
        app.state.engine.dispose()
