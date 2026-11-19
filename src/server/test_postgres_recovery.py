"""Opt-in recovery checks on unique, explicitly migrated synthetic PostgreSQL DBs.

Broker delivery itself is covered by test_local_queue. Publisher network I/O is
controlled here to hold exact race windows; database claims use real PostgreSQL.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from . import execution, queue_runtime as q, runtime_gate as gate, test_runtime_gate as runtime_fixtures
from .config import Settings
from .db import build_engine, init_db
from .execution_models import QueryAnswerSnapshot, ToolInvocation
from .models import QueryTask, TaskOutbox, TaskStep, User, utc_now
from .runtime_models import AgentSessionBinding, ModelCallAdmission, TaskBudget
from .services import SafeError
from .test_runtime_gate import local_upstream


pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
    reason="Recovery verification requires an explicit local PostgreSQL URL")


@pytest.fixture
def pg_database():
    import psycopg
    from psycopg import sql
    base = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    # This destructive lifecycle is intentionally restricted to local test DBs.
    assert base.drivername == "postgresql+psycopg" and base.host == "127.0.0.1"
    assert base.database.startswith("dc_local_pgvector_")
    name = "dc_local_pgvector_recovery_" + uuid4().hex[:16]
    admin_url = base.set(database="postgres").render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://", 1)
    target_url = base.set(database=name).render_as_string(hide_password=False)
    config = Settings(database_url=target_url,
        storage_dir=Path("outputs/local-hardening/2026-11-17/recovery") / name)
    config.storage_dir.mkdir(parents=True, exist_ok=True)
    receipt = {"database": name, "created": False, "dropped": False}
    receipt_path = config.storage_dir / "lifecycle.json"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    receipt["created"] = True
    engine = build_engine(target_url)
    try:
        init_db(engine, config, migrate=True)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == name
            receipt["migration"] = connection.scalar(text("SELECT version_num FROM alembic_version"))
        yield engine, config
    finally:
        engine.dispose()
        with psycopg.connect(admin_url, autocommit=True) as conn:
            # Only the random database created by this fixture is eligible.
            assert name.startswith("dc_local_pgvector_recovery_") and name != base.database
            conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        receipt["dropped"] = True
        receipt_path.write_text(json.dumps(receipt, indent=2), encoding="utf-8")


@pytest.fixture
def pg_runtime(pg_database, local_upstream, monkeypatch, tmp_path):
    engine, config = pg_database
    # Reuse the established synthetic authority/publication fixture. Its engine
    # factory alone is substituted; no SQLite engine/file is opened or migrated.
    def explicit_engine(url, **kwargs):
        assert url.startswith("sqlite:///")  # Existing fixture's local test target.
        return engine
    monkeypatch.setattr(runtime_fixtures, "create_engine", explicit_engine)
    generator = runtime_fixtures.runtime_db.__wrapped__(tmp_path, local_upstream)
    f = next(generator)
    f.config = config
    try:
        assert f.engine is engine and engine.dialect.name == "postgresql"
        yield f
    finally:
        try:
            next(generator)
        except StopIteration:
            pass


def _outbox(engine):
    with Session(engine) as db:
        actor = User(username="recovery-" + uuid4().hex, display_name="SYNTHETIC", status="active", roles=["employee"])
        db.add(actor); db.flush()
        task = QueryTask(owner_user_id=actor.id, input_snapshot={}, status="pending",
            idempotency_key="recovery-" + uuid4().hex)
        db.add(task); db.flush()
        step = TaskStep(task_id=task.id, step_key="query", position=1, kind="knowledge_query", status="pending")
        db.add(step); db.flush()
        row = TaskOutbox(step_id=step.id, dispatch_no=1, status="pending", available_at=utc_now())
        db.add(row); db.commit()
        return task.id, row.id


def _invoke(f, name, *, run=None, token=None, pi_id=None):
    try:
        result = execution.invoke_tool(f.db, (run or f.run).id, token or f.token,
            pi_id or uuid4().hex, name, {})
        f.db.commit()
        return result
    except Exception:
        f.db.rollback()
        raise


def test_two_postgres_publishers_compete_for_one_active_claim(pg_database, monkeypatch):
    engine, config = pg_database
    task_id, dispatch_id = _outbox(engine)
    barrier, entered, release = threading.Barrier(2), threading.Event(), threading.Event()
    sent, pids = [], []
    mutex = threading.Lock()
    original_build = q.build_engine

    def independent_engine(url):
        result = original_build(url)
        @event.listens_for(result, "connect")
        def observe_pid(connection, _):
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_backend_pid()")
                pid = cursor.fetchone()[0]
            with mutex:
                pids.append(pid)
        return result

    class ControlledPublisher:
        def send_task(self, name, **kwargs):
            with mutex:
                sent.append(kwargs)
            entered.set()
            assert release.wait(10)

    monkeypatch.setattr(q, "build_engine", independent_engine)
    monkeypatch.setattr(q, "celery_app", lambda _: ControlledPublisher())
    def publish():
        barrier.wait(5)
        return q.publish_pending(config.database_url, "amqp://controlled-boundary")
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish) for _ in range(2)]
        try:
            assert entered.wait(10)
            done, pending = wait(futures, timeout=10, return_when=FIRST_COMPLETED)
            assert len(done) == len(pending) == 1
            assert next(iter(done)).result() == {"count": 0, "published": []}
        finally:
            release.set()
        results = [future.result(10) for future in futures]
    assert len(set(pids)) == 2
    assert sum(result["count"] for result in results) == 1
    assert len(sent) == 1 and sent[0]["args"] == [task_id, dispatch_id]
    assert sent[0]["task_id"] == dispatch_id
    with Session(engine) as db:
        row = db.get(TaskOutbox, dispatch_id)
        assert row.status == "published" and row.publish_attempts == 1


def test_expired_publisher_completion_cannot_overwrite_new_claim(pg_database, monkeypatch):
    engine, config = pg_database
    task_id, dispatch_id = _outbox(engine)
    entered, release = threading.Event(), threading.Event()
    sent = []
    class ControlledPublisher:
        def send_task(self, name, **kwargs):
            sent.append(kwargs)
            if len(sent) == 1:
                entered.set()
                assert release.wait(10)
    monkeypatch.setattr(q, "celery_app", lambda _: ControlledPublisher())
    with ThreadPoolExecutor(max_workers=1) as pool:
        old = pool.submit(q.publish_pending, config.database_url, "amqp://controlled-boundary")
        try:
            assert entered.wait(10)
            with Session(engine) as independent:
                row = independent.get(TaskOutbox, dispatch_id)
                assert row.status == "publishing"
                row.published_at = utc_now() - timedelta(minutes=3)
                independent.commit()
            assert q.publish_pending(config.database_url, "amqp://controlled-boundary")["count"] == 1
            with Session(engine) as independent:
                row = independent.get(TaskOutbox, dispatch_id)
                new_receipt = (row.status, row.published_at, row.publish_attempts)
        finally:
            release.set()
        old.result(10)
    with Session(engine) as db:
        row = db.get(TaskOutbox, dispatch_id)
        assert (row.status, row.published_at, row.publish_attempts) == new_receipt
        assert row.status == "published" and row.publish_attempts == 2
    assert [item["args"] for item in sent] == [[task_id, dispatch_id]] * 2
    assert {item["task_id"] for item in sent} == {dispatch_id}


def test_expired_session_and_step_leases_fence_validated_old_worker(pg_runtime, monkeypatch):
    f = pg_runtime
    _invoke(f, "dc_artifacts_validate")
    original = gate.validate_run
    replacement = []
    def validate_then_replace(db, run_id, credential):
        result = original(db, run_id, credential)
        if not replacement:
            with f.factory() as independent:
                independent.get(AgentSessionBinding, f.run.session_binding_id).lease_until = utc_now() - timedelta(seconds=1)
                independent.get(TaskStep, f.run.step_id).lease_until = utc_now() - timedelta(seconds=1)
                independent.commit()
                new_run, new_token = gate.open_run(independent, f.task.id)
                replacement.append((new_run, new_token))
        return result
    monkeypatch.setattr(gate, "validate_run", validate_then_replace)
    with pytest.raises(SafeError) as denied:
        _invoke(f, "dc_artifacts_deliver")
    assert denied.value.code == "RUNTIME_LEASE_EXPIRED"
    new_run, new_token = replacement[0]
    assert new_run.lease_epoch > f.run.lease_epoch and new_run.step_lease_epoch > f.run.step_lease_epoch
    assert f.db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == f.task.id)) is None
    assert f.db.get(QueryTask, f.task.id, populate_existing=True).status == "running"
    gate.close_run(f.db, f.run.id, f.token, state="interrupted")
    binding = f.db.get(AgentSessionBinding, f.run.session_binding_id, populate_existing=True)
    step = f.db.get(TaskStep, f.run.step_id, populate_existing=True)
    assert binding.lease_owner == step.lease_owner == new_run.id
    gate.validate_run(f.db, new_run.id, new_token)


@pytest.mark.parametrize("change", ["cancel", "revoke_grant", "disable_actor"])
def test_committed_cancel_or_revocation_before_delivery_writes_cannot_publish(pg_runtime, monkeypatch, change):
    from .models import AuthorizationGrant
    f = pg_runtime
    _invoke(f, "dc_artifacts_validate")
    original, changed = gate.validate_run, []
    def validate_then_change(db, run_id, credential):
        result = original(db, run_id, credential)
        if not changed:
            with f.factory() as independent:
                if change == "cancel":
                    task = independent.get(QueryTask, f.task.id)
                    task.status, task.revision = "cancelled", task.revision + 1
                elif change == "disable_actor":
                    actor = independent.get(User, f.actor.id)
                    actor.status, actor.authz_revision = "disabled", actor.authz_revision + 1
                else:
                    grant = independent.scalar(select(AuthorizationGrant).where(
                        AuthorizationGrant.subject_user_id == f.actor.id, AuthorizationGrant.operation == "knowledge.query"))
                    grant.revoked_at = utc_now()
                independent.commit()
            changed.append(True)
        return result
    monkeypatch.setattr(gate, "validate_run", validate_then_change)
    with pytest.raises(SafeError):
        _invoke(f, "dc_artifacts_deliver")
    assert changed
    assert f.db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == f.task.id)) is None
    assert f.db.scalar(select(ToolInvocation).where(ToolInvocation.task_id == f.task.id,
        ToolInvocation.tool_name == "dc_artifacts_deliver")) is None
    assert f.db.get(QueryTask, f.task.id, populate_existing=True).status == ("cancelled" if change == "cancel" else "running")


def test_killed_process_keeps_dispatch_identity_logical_receipt_and_unknown_call_budget(pg_runtime):
    f = pg_runtime
    logical = _invoke(f, "dc_context_read", pi_id="before-interruption")
    _, call, payload = runtime_fixtures._admit(f)
    row = TaskOutbox(step_id=f.run.step_id, dispatch_no=1, status="published", available_at=utc_now(), published_at=utc_now())
    f.db.add(row); f.db.commit()
    dispatch_id = row.id
    budget = f.db.get(TaskBudget, f.task.id, populate_existing=True)
    reserved = (budget.reserved_calls, budget.reserved_tokens, budget.revision)
    request_seen, release_response = threading.Event(), threading.Event()
    def hold_response():
        request_seen.set()
        assert release_response.wait(15)
    f.upstream.after_request = hold_response
    script = """
import json,sys
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from src.server import execution, runtime_gate as gate
data=json.load(sys.stdin)
with Session(create_engine(data['database_url']),expire_on_commit=False) as db:
    gate.dispatch_model(db,data['run_id'],data['token'],data['call_id'],data['payload'].encode())
"""
    child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    request = {"database_url": f.config.database_url, "run_id": f.run.id, "token": f.token,
        "call_id": call.id, "payload": payload.decode()}
    child.stdin.write(json.dumps(request)); child.stdin.close()
    try:
        assert request_seen.wait(15), "Child did not reach the real loopback HTTP boundary"
        child.kill(); child.wait(timeout=10)
        assert child.returncode != 0
    finally:
        if child.poll() is None:
            child.kill(); child.wait(timeout=5)
        release_response.set()
        child.stdout.close(); child.stderr.close()
    f.db.expire_all()
    stored = f.db.get(ModelCallAdmission, call.id)
    assert stored.state == "dispatching" and stored.usage_state == "unknown"
    assert len(f.upstream.requests) == 1
    with f.factory() as independent:
        independent.get(AgentSessionBinding, f.run.session_binding_id).lease_until = utc_now() - timedelta(seconds=1)
        independent.get(TaskStep, f.run.step_id).lease_until = utc_now() - timedelta(seconds=1)
        independent.commit()
    new_run, new_token = gate.open_run(f.db, f.task.id)
    replay = _invoke(f, "dc_context_read", run=new_run, token=new_token, pi_id="after-interruption")
    assert replay["call_id"] == logical["call_id"]
    assert len(f.db.scalars(select(ToolInvocation).where(ToolInvocation.task_id == f.task.id,
        ToolInvocation.tool_name == "dc_context_read")).all()) == 1
    projection = gate.project_context(f.db, new_run.id, new_token)
    with pytest.raises(SafeError) as denied:
        gate.authorize_model(f.db, new_run.id, new_token, projection_id=projection.id,
            context_digest=projection.context_digest, adapter=gate.ADAPTER,
            payload_digest=gate.digest(payload), request_key="new-key-after-process-kill")
    assert denied.value.code == "RUNTIME_CALL_UNSETTLED"
    f.db.rollback()
    budget = f.db.get(TaskBudget, f.task.id, populate_existing=True)
    assert (budget.reserved_calls, budget.reserved_tokens, budget.revision) == reserved
    assert f.db.get(TaskOutbox, dispatch_id, populate_existing=True).id == dispatch_id
    assert len(f.db.scalars(select(TaskOutbox).where(TaskOutbox.step_id == f.run.step_id)).all()) == 1
    assert f.db.get(ModelCallAdmission, call.id, populate_existing=True).state == "dispatching"
    assert len(f.upstream.requests) == 1
    assert f.db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == f.task.id)) is None
