"""Answer snapshot sharing: synthetic current governance + real HTTP/tools."""
from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from .execution_models import QueryAnswerSnapshot
from .models import AuthorizationGrant, BusinessApproval, QueryTask, TaskShare, TaskStep, User, utc_now
from .runtime_gate import close_run, open_run
from .security import hash_password
from .test_execution import execution_http, _create, _headers, _tool
from .test_runtime_gate import local_upstream, runtime_db
from .testing_governance import seed_verified_approval


@pytest.fixture
def share_http(execution_http):
    f = execution_http
    readers = []
    for readable in (True, False):
        user = User(username="synthetic-share-" + uuid.uuid4().hex, display_name="SYNTHETIC recipient",
                    status="active", roles=["employee"], credential_state="active",
                    credential_hash=hash_password("synthetic share password"))
        f.db.add(user)
        f.db.flush()
        if readable:
            approval = BusinessApproval(applicant=user.id, scope={"knowledge_base_id": f.kb.id,
                "document_id": None, "level": "synthetic-level", "category": "synthetic-category", "need_class": "synthetic-need"},
                operations=["document.read"])
            f.db.add(approval)
            f.db.flush()
            seed_verified_approval(f.db, approval)
            f.db.add(AuthorizationGrant(subject_user_id=user.id, knowledge_base_id=f.kb.id,
                operation="document.read", level="synthetic-level", category="synthetic-category", need_class="synthetic-need",
                approval_id=approval.id, valid_until=approval.valid_until))
        readers.append(user)
    f.db.commit()
    clients = []
    # Authenticate after the proof service incremented each beneficiary's authz revision.
    for user in readers:
        client = TestClient(f.app)
        login = client.post("/auth/login", json={"username": user.username, "password": "synthetic share password"})
        assert login.status_code == 200, login.text
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        clients.append(client)
    try:
        yield SimpleNamespace(**vars(f), reader=readers[0], reader_client=clients[0],
                              no_dr=readers[1], no_dr_client=clients[1])
    finally:
        for client in clients:
            client.close()


def _produce_answer(f, task=None):
    secret = "SYNTHETIC_PRIVATE_QUERY_" + uuid.uuid4().hex
    task = task or _create(f, query=secret + " 开机注意事项")
    run, token = open_run(f.db, task.id)
    _tool(f, run, token, "dc_evidence_search")
    assert _tool(f, run, token, "dc_artifacts_validate")["data"]["ready"]
    _tool(f, run, token, "dc_artifacts_deliver")
    close_run(f.db, run.id, token)
    f.db.refresh(task)
    result = f.client.get(f"/tasks/{task.id}/result")
    assert result.status_code == 200
    snapshot = f.db.get(QueryAnswerSnapshot, result.json()["answer_snapshot_id"])
    assert snapshot.task_id == task.id and snapshot.execution_revision == task.execution_revision
    return task, snapshot, secret


def _share(f, task, snapshot, recipients, headers=None):
    f.db.refresh(task)
    payload = {"entries": [{"user_id": user.id} for user in recipients]}
    if snapshot is not None:
        payload["answer_snapshot_id"] = snapshot.id
    return f.client.put(f"/tasks/{task.id}/shares", json=payload,
                        headers=headers or _headers(task.revision))


def test_share_requires_same_task_current_completed_answer_snapshot(share_http):
    f = share_http
    task, snapshot, _ = _produce_answer(f)
    assert _share(f, task, None, [f.reader]).status_code == 409
    other, other_snapshot, _ = _produce_answer(f)
    assert _share(f, task, other_snapshot, [f.reader]).status_code == 409
    task.status = "running"
    f.db.commit()
    assert _share(f, task, snapshot, [f.reader]).status_code == 409
    task.status, task.kind = "succeeded", "workbook"
    f.db.commit()
    assert _share(f, task, snapshot, [f.reader]).status_code == 409
    assert f.db.scalar(select(func.count()).select_from(TaskShare).where(TaskShare.task_id == task.id)) == 0


def test_recipient_reads_only_answer_with_current_dr_and_cannot_read_owner_input_or_conditions(share_http):
    f = share_http
    task, snapshot, secret = _produce_answer(f)
    assert _share(f, task, snapshot, [f.reader, f.no_dr]).status_code == 200
    owner = f.client.get(f"/tasks/{task.id}")
    assert secret in owner.text
    result = f.reader_client.get(f"/tasks/{task.id}/result")
    assert result.status_code == 200, result.text
    assert result.json()["answer_snapshot_id"] == snapshot.id
    assert result.json()["task"]["input"] is None
    assert "警告：不得跳过隔离检查" in result.json()["answer"]
    for path in (f"/tasks/{task.id}", "/tasks", f"/tasks/{task.id}/status", f"/tasks/{task.id}/evidence"):
        response = f.reader_client.get(path)
        assert response.status_code == 200, response.text
        assert secret not in response.text
    assert f.reader_client.get(f"/conversations/{task.conversation_id}/conditions").status_code == 404
    assert f.reader_client.get(f"/tasks/{task.id}/input-requests").status_code == 404
    assert f.reader_client.get(f"/tasks/{task.id}/shares").status_code == 404
    assert f.reader_client.get(f"/tasks/{f.task.id}").status_code == 404
    assert f.no_dr_client.get(f"/tasks/{task.id}/result").status_code in {403, 404}
    assert f.no_dr_client.get(f"/tasks/{task.id}/evidence").status_code in {403, 404}


def test_existing_share_does_not_grant_source_access_after_dr_is_revoked(share_http):
    f = share_http
    task, snapshot, _ = _produce_answer(f)
    assert _share(f, task, snapshot, [f.reader]).status_code == 200
    assert f.reader_client.get(f"/tasks/{task.id}/result").status_code == 200
    grant = f.db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.reader.id,
                                                       AuthorizationGrant.operation == "document.read"))
    grant.revoked_at = utc_now()
    f.db.commit()
    # Keep authentication valid: this must exercise live source authorization,
    # not accidentally pass because a fixture login became stale.
    assert f.reader_client.get("/me").status_code == 200
    assert f.reader_client.get(f"/tasks/{task.id}/result").status_code in {403, 404}
    assert f.reader_client.get(f"/tasks/{task.id}/evidence").status_code in {403, 404}


def test_share_put_requires_csrf_etag_idempotency_and_replay_cannot_undo_revocation(share_http):
    f = share_http
    task, snapshot, _ = _produce_answer(f)
    payload = {"answer_snapshot_id": snapshot.id, "entries": [{"user_id": f.reader.id}]}
    path = f"/tasks/{task.id}/shares"
    csrf = f.client.headers.pop("X-CSRF-Token")
    assert f.client.put(path, json=payload, headers=_headers(task.revision)).status_code == 403
    f.client.headers["X-CSRF-Token"] = csrf
    assert f.client.put(path, json=payload, headers={"If-Match": f'"{task.revision}"'}).status_code == 428
    assert f.client.put(path, json=payload, headers=_headers()).status_code == 428
    assert f.client.put(path, json=payload, headers=_headers(task.revision - 1)).status_code == 412
    headers = _headers(task.revision)
    first = f.client.put(path, json=payload, headers=headers)
    assert first.status_code == 200, first.text
    f.db.refresh(task)
    revision = task.revision
    again = f.client.put(path, json=payload, headers=headers)
    assert again.status_code == 200
    f.db.refresh(task)
    assert task.revision == revision
    assert f.db.scalar(select(func.count()).select_from(TaskShare).where(TaskShare.task_id == task.id)) == 1
    conflict = f.client.put(path, json={**payload, "entries": [{"user_id": f.no_dr.id}]}, headers=headers)
    assert conflict.status_code == 409
    assert _share(f, task, snapshot, []).status_code == 200
    assert f.reader_client.get(f"/tasks/{task.id}/result").status_code in {403, 404}
    assert f.client.put(path, json=payload, headers=headers).status_code == 200
    share = f.db.scalar(select(TaskShare).where(TaskShare.task_id == task.id).execution_options(populate_existing=True))
    assert share.status == "revoked" and share.revoked_at is not None
    assert f.reader_client.get(f"/tasks/{task.id}/result").status_code in {403, 404}


def test_old_share_never_follows_a_later_execution_answer(share_http):
    f = share_http
    task, original, _ = _produce_answer(f)
    assert _share(f, task, original, [f.reader]).status_code == 200
    assert f.reader_client.get(f"/tasks/{task.id}/result").json()["answer_snapshot_id"] == original.id
    # Model a trusted later execution while preserving the old immutable answer
    # and the old share. All new evidence/validation/presentation still run via
    # the real services rather than fabricating a successful snapshot.
    f.db.refresh(task)
    task.execution_revision += 1
    task.status = "pending"
    step = f.db.scalar(select(TaskStep).where(TaskStep.task_id == task.id, TaskStep.step_key == "query"))
    step.status = "pending"
    f.db.commit()
    _, newer, _ = _produce_answer(f, task)
    assert newer.id != original.id
    share = f.db.scalar(select(TaskShare).where(TaskShare.task_id == task.id).execution_options(populate_existing=True))
    assert share.answer_snapshot_id == original.id
    result = f.reader_client.get(f"/tasks/{task.id}/result")
    assert result.status_code in {403, 404}
    assert newer.id not in result.text
    assert f.db.scalar(select(func.count()).select_from(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task.id)) == 2
