"""CACHE-PERM-002: exercise real retrieval/reuse paths with synthetic records."""

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from . import app as app_module
from . import services
from . import governance
from .models import (
    AuthSession, AuthorizationGrant, BusinessApproval, ChunkSet, ContentBlock, Document,
    DocumentVersion, KnowledgeBase, ParseRun, QueryTask, TaskEvidence, TaskShare, User, utc_now,
)
from .test_cached_result_access import saved_query
from .test_services import _activate_fixture_index, fixture_db
from .testing_governance import seed_confirmation, seed_verified_approval


def _login(client, username, password):
    response = client.post("/auth/login", json={"username": username, "password": password})
    assert response.status_code == 200
    return response.json()["csrf_token"]


def _grant(db, user_id, document, operation, *, direct=False):
    scope = {"knowledge_base_id": document.knowledge_base_id,
             "document_id": document.id if direct else None,
             "level": document.access_level, "category": document.category, "need_class": document.need_class}
    approval = BusinessApproval(applicant=user_id, approver=user_id, approved_at=utc_now(),
                                scope=scope, operations=[operation], status="approved", verification_state="verified")
    db.add(approval)
    db.flush()
    seed_verified_approval(db, approval)
    grant = AuthorizationGrant(subject_user_id=user_id, operation=operation, approval_id=approval.id,
                               knowledge_base_id=scope["knowledge_base_id"], document_id=scope["document_id"],
                               level=scope["level"], category=scope["category"], need_class=scope["need_class"], valid_until=approval.valid_until)
    db.add(grant)
    db.flush()
    return grant


def _version(db, document, source, owner_id, text, number=2, status="effective"):
    assert status in {"draft", "effective"}
    version = DocumentVersion(document_id=document.id, version_no=number, source_file_id=source,
                              created_by=owner_id, model_scope=["DEMO-1"], publish_status="draft",
                              source_metadata={"channel": "synthetic_fixture", "source_description": "SYNTHETIC local cache-permission original"})
    db.add(version)
    db.flush()
    run = ParseRun(document_version_id=version.id, origin="manual", status="succeeded")
    db.add(run)
    db.flush()
    block = ContentBlock(parse_run_id=run.id, ordinal=1, text_content=text, locator={"file_page": 1})
    db.add(block)
    chunk = ChunkSet(parse_run_id=run.id, status="ready")
    db.add(chunk)
    db.flush()
    actor = db.get(User, owner_id)
    governance.review_content_block(db, actor, run, block, "approved", "synthetic original checked")
    binding = governance.source_binding(db, version)
    seed_confirmation(db, actor, version, "applicability", "applicable", binding)
    seed_confirmation(db, actor, version, "classification", "classified", {**binding,
        "access_level": document.access_level, "category": document.category, "need_class": document.need_class,
        "rule_version": "synthetic-cache-rule-v1"})
    professional = seed_confirmation(db, actor, version, "key_content", "accepted", {**binding,
        "parse_run_id": run.id, "block_bindings": {block.id: governance.block_binding(block)}})
    governance.create_review_set(db, actor, version, {"parse_run_id": run.id, "chunk_set_id": chunk.id,
        "reviewed_block_refs": [{"id": block.id, "revision": block.revision, "content_hash": governance.block_binding(block)}],
        "professional_confirmation_refs": [professional.id], "reason": "complete synthetic review"})
    db.flush()
    if status == "effective":
        preview = governance.create_publication_preview(db, actor, document, version, {"action": "publish"})
        db.flush()
        governance.publish_from_preview(db, actor, document, version, {"preview_id": preview.id})
    db.flush()
    return version


def _submit(client, csrf, kb_id, query="开机步骤", key=None):
    response = client.post("/knowledge/queries", json={"query": query, "model": "DEMO-1",
                           "knowledge_base_ids": [kb_id]}, headers={"X-CSRF-Token": csrf,
                           "Idempotency-Key": key or uuid4().hex})
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    status = client.get(f"/tasks/{task_id}/status")
    assert status.status_code == 200 and status.json()["status"] == "pending", status.text
    return task_id


def _answer(client, task_id):
    _run_pending(client, task_id)
    response = client.get(f"/tasks/{task_id}/result")
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "private, no-store"
    return response.json()


def _run_pending(client, task_id):
    """Explicit synthetic execution, separate from the strictly read-only GET."""
    with client.app.state.session_factory() as execution_db:
        task = execution_db.get(QueryTask, task_id)
        if task.status in {"pending", "running"}:
            services.process_query_task(execution_db, task, execution_db.get(User, task.owner_user_id))
            execution_db.commit()
            assert task.status == "succeeded", task.error_code


def _no_quote(client, task, evidence, *, denied=False):
    for suffix in ("", "/status", "/result", "/evidence", f"/evidence/{evidence.id}", "/artifacts"):
        response = client.get(f"/tasks/{task.id}{suffix}")
        assert evidence.quote_snapshot not in response.text, (suffix, response.text)
        if denied:
            assert response.status_code in {401, 403, 404} or response.json().get("status") == "access_limited"
    assert evidence.quote_snapshot not in client.get("/tasks").text


def test_publication_invalidates_new_search_but_not_explicit_history(saved_query):
    db, old_task, old_version, evidence, _, _, owner, _ = saved_query
    document = db.get(Document, old_version.document_id)
    kb = db.get(KnowledgeBase, document.knowledge_base_id)
    csrf = _login(owner, db.get(User, old_task.owner_user_id).username, "manager password")
    new_version = _version(db, document, old_version.source_file_id, old_task.owner_user_id,
                           "DEMO-1 开机步骤：NEW-VERSION-ONLY", status="draft")
    db.commit()
    pending = _submit(owner, csrf, kb.id)
    preview = owner.post(f"/documents/{document.id}/versions/{new_version.id}/publication-preview",
        json={"action": "publish"}, headers={"X-CSRF-Token": csrf, "Idempotency-Key": uuid4().hex})
    assert preview.status_code == 200 and preview.json()["allowed"] is True, preview.text
    published = owner.post(f"/documents/{document.id}/versions/{new_version.id}/publish",
                           json={"preview_id": preview.json()["preview_id"]}, headers={"X-CSRF-Token": csrf, "If-Match": preview.json()["document_etag"],
                                             "Idempotency-Key": uuid4().hex})
    assert published.status_code == 200, published.text
    blocked = _answer(owner, pending)
    assert blocked["answer"] is None and "index_stale" in blocked["warnings"]
    db.expire_all()
    _activate_fixture_index(db, kb, db.get(User, old_task.owner_user_id))
    db.commit()
    current = _answer(owner, _submit(owner, csrf, kb.id))
    assert "NEW-VERSION-ONLY" in current["answer"]
    assert evidence.quote_snapshot not in current["answer"]
    assert {e["document_version_id"] for e in current["evidence"]} == {new_version.id}
    history = _answer(owner, old_task.id)
    assert history["answer"] == old_task.result_summary["answer"]
    assert history["evidence"][0]["version"]["publish_status"] == "superseded"


def test_identical_answers_require_common_authorized_evidence(saved_query):
    db, task, version, _, recipient_read, _, owner, recipient = saved_query
    document = db.get(Document, version.document_id)
    kb = db.get(KnowledgeBase, document.knowledge_base_id)
    _grant(db, recipient_read.subject_user_id, document, "knowledge.query")
    secret = Document(knowledge_base_id=kb.id, title="Synthetic private manual", access_mode="restricted",
                      access_level=document.access_level, category=document.category, need_class=document.need_class)
    db.add(secret)
    db.flush()
    # Creating/reviewing a restricted source needs an explicit matching
    # management grant, independently of later read/query permissions.
    _grant(db, task.owner_user_id, secret, "knowledge.manage", direct=True)
    _version(db, secret, version.source_file_id, task.owner_user_id, "PRIVATE-TOKEN 开机步骤", number=1)
    for op in ("document.read", "knowledge.query"):
        _grant(db, task.owner_user_id, secret, op, direct=True)
    _activate_fixture_index(db, kb, db.get(User, task.owner_user_id))
    db.commit()
    owner_csrf = _login(owner, db.get(User, task.owner_user_id).username, "manager password")
    recipient_csrf = _login(recipient, "recipient", "recipient password")
    # Same idempotency key across accounts must not reuse another user's task.
    owner_id = _submit(owner, owner_csrf, kb.id, "连接电源", key="same-user-query-key")
    recipient_id = _submit(recipient, recipient_csrf, kb.id, "连接电源", key="same-user-query-key")
    first, second = _answer(owner, owner_id), _answer(recipient, recipient_id)
    assert owner_id != recipient_id and first["answer"] == second["answer"]
    assert {e["document_version_id"] for e in second["evidence"]} == {version.id}
    authorized = _answer(owner, _submit(owner, owner_csrf, kb.id, "PRIVATE-TOKEN"))
    scoped = _answer(recipient, _submit(recipient, recipient_csrf, kb.id, "PRIVATE-TOKEN"))
    assert "PRIVATE-TOKEN" in authorized["answer"]
    assert scoped["answer"] is None, scoped
    assert not scoped["evidence"]


def test_direct_revocation_cannot_fall_back_to_inherited_access(saved_query):
    db, task, version, evidence, kb_grant, share, owner, recipient = saved_query
    document = db.get(Document, version.document_id)
    document.access_mode = "restricted"
    for op in ("document.read", "knowledge.query"):
        _grant(db, task.owner_user_id, document, op, direct=True)
    direct = _grant(db, kb_grant.subject_user_id, document, "document.read", direct=True)
    _grant(db, kb_grant.subject_user_id, document, "knowledge.query", direct=True)
    db.commit()
    # New synthetic approvals legitimately advanced both users' authz revisions;
    # establish current sessions before testing the subsequent grant revocation.
    _login(owner, db.get(User, task.owner_user_id).username, "manager password")
    _login(recipient, "recipient", "recipient password")
    assert evidence.quote_snapshot in recipient.get(f"/tasks/{task.id}/result").text
    direct.revoked_at = utc_now()
    db.commit()
    _no_quote(recipient, task, evidence, denied=True)
    csrf = _login(recipient, "recipient", "recipient password")
    fresh = _answer(recipient, _submit(recipient, csrf, document.knowledge_base_id))
    assert fresh["answer"] is None and not fresh["evidence"]
    source = recipient.get(f"/documents/{document.id}/versions/{version.id}/file")
    assert source.status_code == 404
    # A new session and a share must not replace document permissions.
    _no_quote(recipient, task, evidence, denied=True)
    direct.revoked_at = None
    share.expires_at = utc_now() - timedelta(seconds=1)
    db.commit()
    _no_quote(recipient, task, evidence, denied=True)
    share.expires_at = None
    share.revoked_at = utc_now()
    share.status = "revoked"
    db.commit()
    _no_quote(recipient, task, evidence, denied=True)
    assert evidence.quote_snapshot in _answer(owner, task.id)["answer"]


@pytest.mark.parametrize("suffix", ["", "/status", "/result"])
@pytest.mark.parametrize("change", ["account", "session"])
def test_in_flight_request_observes_committed_account_revocation(saved_query, monkeypatch, suffix, change):
    db, task, _, evidence, _, _, owner, _ = saved_query
    original = app_module._user_or_401
    fired = False
    target_path = f"/tasks/{task.id}{suffix}"

    def revoke_after_auth(request, request_db):
        nonlocal fired
        actor, session = original(request, request_db)
        if not fired and request.url.path == target_path:
            fired = True
            # Another transaction commits after HTTP authentication, before
            # final task authorization. No sleeps, fake responses or fake ACLs.
            if change == "account":
                target = db.get(User, actor.id, populate_existing=True)
                target.status = "disabled"
                target.authz_revision += 1
            else:
                for session in db.scalars(select(AuthSession).where(AuthSession.user_id == actor.id)).all():
                    session.revoked_at = utc_now()
            db.commit()
        return actor, session

    monkeypatch.setattr(app_module, "_user_or_401", revoke_after_auth)
    response = owner.get(f"/tasks/{task.id}{suffix}")
    assert fired
    assert evidence.quote_snapshot not in response.text
    assert response.status_code in {401, 403} or response.json().get("status") == "access_limited"
    assert owner.get(f"/tasks/{task.id}/result").status_code == 401


@pytest.mark.parametrize("change", ["publication", "permission"])
def test_publication_between_search_and_save_does_not_write_obsolete_answer(saved_query, monkeypatch, change):
    db, old_task, old_version, evidence, _, _, owner, _ = saved_query
    document = db.get(Document, old_version.document_id)
    csrf = _login(owner, db.get(User, old_task.owner_user_id).username, "manager password")
    pending = _submit(owner, csrf, document.knowledge_base_id)
    original = services.search_blocks
    fired = False

    def publish_after_search(*args, **kwargs):
        nonlocal fired
        matches, warnings = original(*args, **kwargs)
        assert matches
        if not fired:
            fired = True
            if change == "publication":
                old_version.publish_status = "superseded"
                _version(db, document, old_version.source_file_id, old_task.owner_user_id,
                         "DEMO-1 开机步骤：RACE-NEW-VERSION")
                db.get(KnowledgeBase, document.knowledge_base_id).content_revision += 1
            else:
                for grant in db.scalars(select(AuthorizationGrant).where(
                    AuthorizationGrant.subject_user_id == old_task.owner_user_id,
                    AuthorizationGrant.operation == "document.read",
                )).all():
                    grant.revoked_at = utc_now()
            db.commit()
        return matches, warnings

    monkeypatch.setattr(services, "search_blocks", publish_after_search)
    _run_pending(owner, pending)
    result = owner.get(f"/tasks/{pending}/result")
    assert fired
    assert evidence.quote_snapshot not in result.text, result.text
    db.expire_all()
    stored = db.get(QueryTask, pending)
    assert evidence.quote_snapshot not in str(stored.result_summary)
    assert not db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == pending)).all()
