"""Task-workspace read projections: actual HTTP, synthetic isolated data."""
from uuid import uuid4

from sqlalchemy import func, select

from .models import AuthorizationGrant, QueryTask, utc_now
from .test_worker import local_stack, submit
from .test_services import fixture_db


def test_receipt_lookup_is_owner_scoped_read_only_and_absence_is_unknown(local_stack):
    db, _, client, _, employee, administrator, kb, _, _, requests, _ = local_stack
    key = uuid4().hex
    before = db.scalar(select(func.count()).select_from(QueryTask))
    absent = client.get("/operation-receipts", params={"key": key})
    assert absent.status_code == 200
    assert absent.json() == {"state": "unresolved", "retry_allowed": False, "resource_refs": {}}
    accepted = client.post("/knowledge/queries", json={"query": "SYNTHETIC private question", "model": "DEMO-1"},
                           headers={"Idempotency-Key": key})
    assert accepted.status_code == 202
    old_csrf = client.headers["X-CSRF-Token"]
    task_view = client.get("/tasks/" + accepted.json()["task_id"]).json()
    condition = client.get("/conversations/" + task_view["conversation_id"] + "/conditions")
    assert condition.status_code == 200 and condition.json()["latest_task_id"] == accepted.json()["task_id"]
    for _ in range(2):
        receipt = client.get("/operation-receipts", params={"key": key})
        assert receipt.status_code == 200 and receipt.headers["Cache-Control"] == "private, no-store"
        assert receipt.json()["resource_refs"] == {"task_id": accepted.json()["task_id"]}
        assert receipt.json()["state"] == "completed" and receipt.json()["retry_allowed"] is False
        assert "SYNTHETIC private question" not in receipt.text
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(QueryTask)) == before + 1
    assert not requests
    second = client.post("/knowledge/queries", json={"query": "SYNTHETIC follow up", "conversation_id": task_view["conversation_id"],
        "context_revision": condition.json()["revision"]}, headers={"Idempotency-Key": uuid4().hex})
    assert second.status_code == 202
    listed = client.get("/workspace-data/works")
    assert listed.status_code == 200 and [item["id"] for item in listed.json()["items"]] == [second.json()["task_id"]]
    history = client.get("/conversations/" + task_view["conversation_id"] + "/executions")
    assert len(history.json()["items"]) == 2
    login = client.post("/auth/login", json={"username": administrator.username, "password": "synthetic admin password"})
    assert login.status_code == 200
    assert client.get("/operation-receipts", params={"key": key}).json()["state"] == "unresolved"
    bound = client.get("/me", headers={"X-Workspace-CSRF": old_csrf})
    assert bound.status_code == 401 and administrator.id not in bound.text
    assert client.get("/conversations/" + task_view["conversation_id"] + "/executions").status_code == 404


def test_original_access_uses_same_gate_and_admin_operation_list_is_sanitized(local_stack):
    db, _, client, _, employee, administrator, kb, doc, version, requests, _ = local_stack
    task_id = submit(client, kb)
    path = f"/documents/{doc.id}/versions/{version.id}/file"
    assert client.get(path + "-access").json()["read_mode"] == "published_original"
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == employee.id,
                     AuthorizationGrant.operation == "document.read"))
    grant.revoked_at = utc_now(); db.commit()
    assert client.get(path).status_code == client.get(path + "-access").status_code == 404
    assert client.get("/admin/operations/tasks").status_code == 403
    assert client.post("/auth/login", json={"username": administrator.username, "password": "synthetic admin password"}).status_code == 200
    response = client.get("/admin/operations/tasks")
    assert response.status_code == 200
    row = next(item for item in response.json()["items"] if item["id"] == task_id)
    assert set(row) == {"id", "kind", "status", "error_code", "created_at", "finished_at"}
    assert "startup" not in response.text and "input" not in response.text
    assert client.get(path + "-access").status_code == 404
    assert not requests
