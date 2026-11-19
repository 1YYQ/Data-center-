"""Cancellation returns a status receipt independent of source read access."""

from uuid import uuid4

from sqlalchemy import select

from .models import AuthorizationGrant, QueryTask, TaskArtifact, User, utc_now
from .test_cached_result_access import saved_query
from .test_services import fixture_db


def test_owner_cancel_receipts_are_metadata_only_after_source_access_is_revoked(saved_query):
    db, completed, _, evidence, _, _, client, _ = saved_query
    owner = db.get(User, completed.owner_user_id)
    login = client.post("/auth/login", json={"username": owner.username, "password": "manager password"})
    assert login.status_code == 200
    source = client.get(f"/tasks/{completed.id}/result")
    assert source.status_code == 200 and source.json()["evidence"][0]["quote"] == evidence.quote_snapshot

    candidate = TaskArtifact(task_id=completed.id, kind="answer_candidate", validation_status="verified")
    pending = QueryTask(owner_user_id=owner.id, idempotency_key="synthetic-pending",
                        input_snapshot={"query": "SYNTHETIC private pending question"})
    db.add_all([candidate, pending])
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == owner.id,
        AuthorizationGrant.operation == "document.read"))
    grant.revoked_at = utc_now()
    db.commit()
    assert client.get(f"/tasks/{completed.id}/result").status_code == 403

    for task, state, revision in ((completed, "succeeded", completed.revision),
                                  (pending, "cancelled", pending.revision + 1)):
        headers = {"X-CSRF-Token": login.json()["csrf_token"], "If-Match": f'"{task.revision}"',
                   "Idempotency-Key": str(uuid4())}
        payload = {"reason": "SYNTHETIC verify status-only cancellation"}
        first = client.post(f"/tasks/{task.id}/cancel", json=payload, headers=headers)
        replay = client.post(f"/tasks/{task.id}/cancel", json=payload, headers=headers)
        assert first.status_code == replay.status_code == 200
        receipt = first.json()
        assert receipt == replay.json()
        assert set(receipt) == {"id", "status", "revision", "next_action"}
        assert receipt["id"] == task.id and receipt["status"] == state and receipt["revision"] == revision
        for response in (first, replay):
            assert response.headers["cache-control"] == "private, no-store"
            assert evidence.quote_snapshot not in response.text and candidate.id not in response.text
            assert task.input_snapshot["query"] not in response.text
        db.refresh(task)
        assert task.status == state and task.revision == revision
