"""Saved query results use current permissions, with explicit historical versions."""

import pytest

from fastapi.testclient import TestClient
from sqlalchemy import select

from .app import create_app
from .models import AuthorizationGrant, BusinessApproval, QueryTask, TaskEvidence, TaskShare, User, utc_now
from .security import hash_password
from .services import build_query_result
from .test_services import _ready_fixture, fixture_db
from .testing_governance import seed_verified_approval

@pytest.fixture()
def saved_query(fixture_db):
    db, config = fixture_db
    owner, kb, document, version, _ = _ready_fixture(db, config)
    # The legacy fixture predates mandatory authorization dimensions. Supply
    # explicit synthetic dimensions here; never weaken the production guard.
    document.access_level = "test-level"
    document.category = "test-category"
    document.need_class = "test-need"
    for grant in db.scalars(select(AuthorizationGrant)).all():
        grant.level, grant.category, grant.need_class = document.access_level, document.category, document.need_class
        db.get(BusinessApproval, grant.approval_id).scope = {
            "knowledge_base_id": kb.id, "document_id": None,
            "level": grant.level, "category": grant.category, "need_class": grant.need_class,
        }
    recipient = User(username="recipient", display_name="Recipient", status="active", roles=["employee"],
                     credential_state="active", credential_hash=hash_password("recipient password"))
    db.add(recipient)
    db.flush()
    approval = BusinessApproval(applicant=recipient.id, approver=owner.id, approved_at=utc_now(),
                                scope={"knowledge_base_id": kb.id, "document_id": None,
                                       "level": document.access_level, "category": document.category,
                                       "need_class": document.need_class}, operations=["document.read"], status="approved", verification_state="verified")
    db.add(approval)
    db.flush()
    seed_verified_approval(db, approval)
    grant = AuthorizationGrant(subject_user_id=recipient.id, knowledge_base_id=kb.id, operation="document.read",
                               level=document.access_level, category=document.category, need_class=document.need_class,
                               approval_id=approval.id, granted_by=owner.id, valid_until=approval.valid_until)
    db.add(grant)
    db.commit()
    task = QueryTask(owner_user_id=owner.id, kind="query", status="running", idempotency_key="cached-result",
                     input_snapshot={"query": "开机步骤", "model": "DEMO-1", "knowledge_base_ids": [kb.id]})
    db.add(task)
    db.flush()
    build_query_result(db, task, owner)
    db.flush()
    evidence = db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == task.id))
    assert evidence is not None and task.result_summary["result_status"] == "formal"
    share = TaskShare(task_id=task.id, recipient_user_id=recipient.id, granted_by=owner.id)
    db.add(share)
    db.commit()
    app = create_app(config)
    # No lifespan: fixture_db already initialized this isolated database.
    owner_client, recipient_client = TestClient(app), TestClient(app)
    try:
        for client, username, password in ((owner_client, owner.username, "manager password"),
                                            (recipient_client, recipient.username, "recipient password")):
            assert client.post("/auth/login", json={"username": username, "password": password}).status_code == 200
        yield db, task, version, evidence, grant, share, owner_client, recipient_client
    finally:
        owner_client.close()
        recipient_client.close()
        app.state.engine.dispose()


def _assert_saved_content(client, task, evidence, expected_status):
    for prefix in ("", "/api/v1"):
        for suffix in ("result", "evidence", f"evidence/{evidence.id}", "artifacts"):
            response = client.get(f"{prefix}/tasks/{task.id}/{suffix}")
            assert response.headers["cache-control"] == "private, no-store"
            assert response.status_code == expected_status, (suffix, response.text)
            if expected_status != 200:
                assert evidence.quote_snapshot not in response.text


@pytest.mark.parametrize("publish_status", ["superseded", "withdrawn", "draft"])
def test_saved_result_distinguishes_historical_and_unavailable_versions(saved_query, publish_status):
    db, task, version, evidence, _, _, owner, recipient = saved_query
    _assert_saved_content(owner, task, evidence, 200)
    _assert_saved_content(recipient, task, evidence, 200)
    version.publish_status = publish_status
    db.commit()
    readable = publish_status == "superseded"
    _assert_saved_content(owner, task, evidence, 200 if readable else 403)
    _assert_saved_content(recipient, task, evidence, 200 if readable else 404)
    if readable:
        response = recipient.get(f"/tasks/{task.id}/result").json()
        assert response["answer"] == task.result_summary["answer"]
        assert response["evidence"][0]["version"]["publish_status"] == "superseded"
        assert response["evidence"][0]["quote"] == evidence.quote_snapshot
        # Historical readability must not put replaced manuals into new answers.
        new_task = QueryTask(owner_user_id=task.owner_user_id, idempotency_key="new-query",
                             status="running", input_snapshot=dict(task.input_snapshot))
        db.add(new_task)
        db.flush()
        build_query_result(db, new_task, db.get(User, task.owner_user_id))
        db.flush()
        assert new_task.result_summary["answer"] is None
        assert not db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == new_task.id)).all()


@pytest.mark.parametrize("revocation", ["document_grant", "share"])
def test_historical_result_rechecks_current_recipient_access(saved_query, revocation):
    db, task, version, evidence, grant, share, owner, recipient = saved_query
    version.publish_status = "superseded"
    db.commit()
    _assert_saved_content(recipient, task, evidence, 200)
    if revocation == "document_grant":
        grant.revoked_at = utc_now()
    else:
        share.revoked_at = utc_now()
        share.status = "revoked"
    db.commit()
    _assert_saved_content(recipient, task, evidence, 404)
    _assert_saved_content(owner, task, evidence, 200)
