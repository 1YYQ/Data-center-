"""Real PostgreSQL replacement races and source loss on a synthetic saved answer."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import services
from .app import create_app
from .execution_models import QueryAnswerSnapshot, ToolInvocation
from .models import AuthorizationGrant, QueryTask, TaskEvidence, TaskShare, User, utc_now
from .test_creation_recovery import recovery_pg
from .test_services import _ready_fixture


def test_share_replace_is_atomic_and_source_loss_allows_only_revocation(recovery_pg, monkeypatch):
    engine, config, _, _ = recovery_pg
    with Session(engine, expire_on_commit=False) as db:
        owner, kb, document, version, block = _ready_fixture(db, config)
        readers = [User(username="synthetic-share-" + uuid4().hex, display_name="SYNTHETIC share recipient",
                        roles=["employee"], status="active") for _ in range(2)]
        task = QueryTask(owner_user_id=owner.id, kind="query", input_snapshot={"query": "SYNTHETIC private question"},
            effective_input={"model": "DEMO-1", "knowledge_base_ids": [kb.id]}, accepted_scope={"synthetic_fixture": True},
            status="succeeded", idempotency_key=uuid4().hex, result_summary={"answer": block.text_content, "result_status": "formal"})
        db.add_all([task, *readers]); db.flush()
        evidence = TaskEvidence(task_id=task.id, document_version_id=version.id, block_id=block.id,
            quote_snapshot=block.text_content, locator=block.locator, review_at_run="approved")
        validation = ToolInvocation(actor_user_id=owner.id, task_id=task.id, tool_name="dc_artifacts_validate",
            logical_action_key=uuid4().hex, args_hash="a" * 64, execution_revision=1, state="completed", output={"ready": True})
        db.add_all([evidence, validation]); db.flush()
        snapshot = QueryAnswerSnapshot(task_id=task.id, execution_revision=1, result=task.result_summary,
            evidence_ids=[evidence.id], validation_ref=validation.id)
        db.add(snapshot); db.commit()

        app = create_app(config)
        clients = [TestClient(app), TestClient(app)]
        try:
            headers = []
            for client in clients:
                login = client.post("/auth/login", json={"username": owner.username, "password": "manager password"})
                assert login.status_code == 200
                headers.append({"X-CSRF-Token": login.json()["csrf_token"], "If-Match": f'"{task.revision}"', "Idempotency-Key": uuid4().hex})
            rendezvous = Barrier(2)
            original = services.task_write_guard

            def contend(session, current, **kwargs):
                if current.id == task.id:
                    rendezvous.wait(15)
                return original(session, current, **kwargs)

            monkeypatch.setattr(services, "task_write_guard", contend)

            def replace(index):
                return clients[index].put(f"/tasks/{task.id}/shares", json={"answer_snapshot_id": snapshot.id,
                    "entries": [{"user_id": readers[index].id}]}, headers=headers[index])

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(replace, range(2)))
            assert sorted(result.status_code for result in results) == [200, 412], [result.text for result in results]
            monkeypatch.setattr(services, "task_write_guard", original)
            winner = next(index for index, result in enumerate(results) if result.status_code == 200)
            db.expire_all(); db.refresh(task)
            active = db.scalars(select(TaskShare).where(TaskShare.task_id == task.id, TaskShare.status == "active")).all()
            assert [row.recipient_user_id for row in active] == [readers[winner].id]
            assert task.revision == 1

            for grant in db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == owner.id,
                    AuthorizationGrant.operation == "document.read")):
                grant.revoked_at = utc_now()
            db.commit()
            client = clients[winner]
            assert client.get("/me").status_code == 200
            assert client.get(f"/tasks/{task.id}/result").status_code == 403
            assert client.get(f"/tasks/{task.id}/shares").status_code == 403
            latest = {**headers[winner], "If-Match": f'"{task.revision}"', "Idempotency-Key": uuid4().hex}
            denied = client.put(f"/tasks/{task.id}/shares", json={"answer_snapshot_id": snapshot.id,
                "entries": [{"user_id": readers[1 - winner].id}]}, headers=latest)
            assert denied.status_code == 403
            revoked = client.put(f"/tasks/{task.id}/shares", json={"answer_snapshot_id": snapshot.id, "entries": []},
                                headers={**latest, "Idempotency-Key": uuid4().hex})
            assert revoked.status_code == 200
            db.expire_all()
            assert not db.scalars(select(TaskShare).where(TaskShare.task_id == task.id, TaskShare.status == "active")).all()
        finally:
            for client in clients:
                client.close()
            app.state.engine.dispose()
