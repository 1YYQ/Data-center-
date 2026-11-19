"""Real PostgreSQL races between final query delivery and lifecycle changes."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text

from . import execution, governance
from .app import create_app
from .execution_models import QueryAnswerSnapshot, ToolInvocation
from .models import ApiIdempotencyRecord, AuditEvent, AuthorizationGrant, BusinessApproval, Document, QueryTask, User, utc_now
from .security import hash_password
from .services import SafeError, can_read_task
from .test_postgres_recovery import _invoke, pg_database, pg_runtime, pytestmark
from .test_runtime_gate import local_upstream
from .testing_governance import _authority, _proof


def _wait_for_lock(engine, blocker, waiter):
    """Wait for an actual database blocker, or the unfenced operation to finish."""
    deadline = monotonic() + 12
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as observer:
        while not waiter.done() and monotonic() < deadline:
            row = observer.execute(text("""
                SELECT pid, wait_event_type, wait_event FROM pg_stat_activity
                WHERE :blocker = ANY(pg_blocking_pids(pid)) AND wait_event_type = 'Lock'
            """), {"blocker": blocker}).mappings().first()
            if row:
                return dict(row)
    return None


@pytest.mark.parametrize("change", ["delete", "withdraw", "hold", "revoke_grant", "revoke_approval", "disable_actor"])
@pytest.mark.parametrize("first", ["mutation", "delivery"])
def test_delivery_commit_obeys_current_lifecycle(pg_runtime, monkeypatch, change, first):
    f = pg_runtime
    _invoke(f, "dc_artifacts_validate")
    task_id, run_id, actor_id = f.task.id, f.run.id, f.actor.id
    document = f.db.get(Document, f.version.document_id)
    key, password = str(uuid4()), "SYNTHETIC delivery race password"
    f.actor.credential_state, f.actor.credential_hash = "active", hash_password(password)
    approval = f.db.scalar(select(BusinessApproval).where(BusinessApproval.applicant == actor_id))
    admin = f.db.get(User, approval.verified_by)
    admin.credential_state, admin.credential_hash = "active", f.actor.credential_hash
    revocation_scope = {"approval_id": approval.id, **approval.scope}
    _, revoker = _authority(f.db, purpose="access_revocation", scope=revocation_scope)
    proof = _proof(f.db, admin, "approval", approval.id, {
        "kind": "access_revocation", "decision": "revoked", "actual_identity": revoker, "scope": revocation_scope,
        "decided_at": utc_now().isoformat(),
    })
    preview = governance.create_publication_preview(f.db, f.actor, document, f.version, {"action": "withdraw"})
    f.db.commit()
    prefix = f"/documents/{document.id}/versions/{f.version.id}"
    mutations = {
        "delete": ("DELETE", f"/documents/{document.id}", {"reason": "SYNTHETIC delete"}, document.revision, 204),
        "withdraw": ("POST", prefix + "/withdraw",
            {"preview_id": preview.id, "reason": "SYNTHETIC withdrawal"}, document.revision, 200),
        "hold": ("POST", prefix + "/holds", {"kind": "quality", "reason": "SYNTHETIC hold"}, f.version.revision, 201),
        "revoke_approval": ("POST", f"/admin/business-approvals/{approval.id}/revocations",
            {"verified_revocation_evidence_ref": proof.id, "reason": "SYNTHETIC approval revocation"}, approval.revision, 201),
        "disable_actor": ("PATCH", f"/admin/users/{actor_id}", {"status": "disabled"}, f.actor.authz_revision, 200),
    }
    app = create_app(f.config)
    reached_boundary, release = Event(), Event()
    pids = {}

    def mutation_committing(db):
        pids["mutation"] = db.scalar(text("SELECT pg_backend_pid()"))
        if first == "mutation":
            reached_boundary.set()
            assert release.wait(20), "Timed out awaiting mutation commit"

    @event.listens_for(app.state.session_factory, "before_commit")
    def observe_http_commit(db):
        if db.scalar(select(ApiIdempotencyRecord.id).where(
            ApiIdempotencyRecord.idempotency_key == key, ApiIdempotencyRecord.status == "completed"
        )):
            mutation_committing(db)

    original = execution._current_evidence

    def observe_evidence(db, task, actor):
        evidence = original(db, task, actor)
        if first == "delivery":
            reached_boundary.set()
            assert release.wait(20), "Timed out awaiting answer commit"
        return evidence

    monkeypatch.setattr(execution, "_current_evidence", observe_evidence)

    def deliver():
        with f.factory() as db:
            pids["delivery"] = db.scalar(text("SELECT pg_backend_pid()"))
            try:
                execution.invoke_tool(db, run_id, f.token, "host:present", "dc_artifacts_deliver", {})
                db.commit()
            except SafeError as error:
                db.rollback()
                return error.code
            return "succeeded"

    def mutate(client):
        if change == "revoke_grant":
            # The local app has no grant-revoke HTTP route. Exercise the row
            # used by the current grant predicate without simulating a UI.
            with f.factory() as db:
                grant = db.scalar(select(AuthorizationGrant).where(
                    AuthorizationGrant.subject_user_id == actor_id, AuthorizationGrant.operation == "document.read"))
                grant.revoked_at = utc_now()
                db.flush()
                mutation_committing(db)
                db.commit()
            return "committed"
        method, path, payload, revision, expected_status = mutations[change]
        response = client.request(method, path, json=payload,
            headers={"If-Match": f'"{revision}"', "Idempotency-Key": key})
        assert response.status_code == expected_status, response.status_code
        return "committed"

    try:
        with TestClient(app) as client, ThreadPoolExecutor(max_workers=2) as pool:
            username = admin.username if change in {"disable_actor", "revoke_approval"} else f.actor.username
            login = client.post("/auth/login", json={"username": username, "password": password})
            assert login.status_code == 200
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            initial, competing = (mutate, deliver) if first == "mutation" else (deliver, mutate)

            def submit(action):
                return pool.submit(action, client) if action is mutate else pool.submit(action)

            leader = submit(initial)
            try:
                if not reached_boundary.wait(10):
                    if leader.done():
                        leader.result()
                    pytest.fail("First operation did not reach the selected boundary")
                follower = submit(competing)
                lock = _wait_for_lock(f.engine, pids[first], follower)
            finally:
                release.set()
            leader_result, follower_result = leader.result(20), follower.result(20)
    finally:
        app.state.engine.dispose()

    outcome, mutation = (follower_result, leader_result) if first == "mutation" else (leader_result, follower_result)
    with f.factory() as db:
        task = db.get(QueryTask, task_id)
        result = {
            "task_status": task.status,
            "snapshot_count": db.scalar(select(func.count()).select_from(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task_id)),
            "delivery_receipts": db.scalar(select(func.count()).select_from(ToolInvocation).where(
                ToolInvocation.task_id == task_id, ToolInvocation.tool_name == "dc_artifacts_deliver")),
            "presented_events": db.scalar(select(func.count()).select_from(AuditEvent).where(
                AuditEvent.resource_id == task_id, AuditEvent.action == "query.presented")),
            "current_saved_result_access": can_read_task(db, db.get(User, actor_id), task),
        }
    if path := os.environ.get("DATACENTER_DELIVERY_TEST_EVIDENCE"):
        output = Path("outputs/local-continuation/2026-11-18/delivery") / path
        output.mkdir(parents=True, exist_ok=True)
        identities = [Path(__file__), Path("src/server/execution.py"), Path("src/server/runtime_gate.py")]
        (output / f"{first}-{change}.json").write_text(json.dumps({
            "database": f.engine.url.database,
            "first_boundary": first, "change": change, "backend_pids": pids, "observed_wait": lock,
            "mutation_outcome": mutation, "delivery_outcome": outcome, "durable_state": result,
            "source_sha256": {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in identities},
        }, indent=2), encoding="utf-8")
    assert mutation == "committed"
    assert lock and lock["wait_event_type"] == "Lock", "The second transaction bypassed the first operation's fence"
    assert pids["delivery"] != pids["mutation"]
    assert lock["pid"] == pids["delivery" if first == "mutation" else "mutation"]
    assert outcome == ("ACCESS_LIMITED" if first == "mutation" else "succeeded")
    count = int(first == "delivery")
    assert result == {"task_status": "succeeded" if count else "running", "snapshot_count": count,
        "delivery_receipts": count, "presented_events": count, "current_saved_result_access": False}
