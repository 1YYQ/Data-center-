"""Focused queue contract tests; no broker is simulated as RabbitMQ."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import timedelta
from uuid import uuid4

from . import queue_runtime as q
from .models import QueryTask, TaskOutbox, TaskStep, User, utc_now
from .test_services import fixture_db
from .config import Settings
from .worker import run_one
from .test_worker import local_stack

@__import__("pytest").mark.skipif(not __import__("os").environ.get("DATACENTER_TEST_RABBITMQ_URL"),
    reason="Real RabbitMQ integration requires an explicitly supplied local broker")
def test_real_celery_worker_consumes_outbox_and_completes_original_task(local_stack, monkeypatch):
    import os
    from .models import QueryTask
    from .runtime_models import AgentRun, ModelCallAdmission, TaskBudget
    from .execution_models import QueryAnswerSnapshot
    from .test_worker import submit
    import pytest
    db, config, client, address, employee, admin, kb, *_ = local_stack
    broker = os.environ["DATACENTER_TEST_RABBITMQ_URL"]
    queue = "dc-local-it-" + uuid4().hex
    monkeypatch.setenv("DATACENTER_CELERY_BROKER_URL", broker)
    monkeypatch.setenv("DATACENTER_DATABASE_URL", config.database_url)
    monkeypatch.setenv("DATACENTER_STORAGE_DIR", str(config.storage_dir.resolve()))
    monkeypatch.setenv("DATACENTER_PRIVATE_GATEWAY_URL", address)
    task_id = submit(client, kb)
    result = q.publish_pending(config.database_url, broker, queue_name=queue)
    assert result["count"] == 1
    log_path=__import__("pathlib").Path("outputs/local-stack/2026-11-17/queue") / (queue+".worker.log")
    log_path.parent.mkdir(parents=True,exist_ok=True)
    log_file=log_path.open("w",encoding="utf-8")
    worker = subprocess.Popen([sys.executable, "-m", "celery", "-A", "src.server.queue_runtime:app", "worker",
        "-P", "solo", "-Q", queue, "-c", "1", "--without-gossip", "--without-mingle", "--without-heartbeat", "-l", "WARNING"],
        cwd=__import__("pathlib").Path(__file__).resolve().parents[2], env=os.environ.copy(),
        stdout=log_file, stderr=subprocess.STDOUT)
    try:
        deadline=time.monotonic()+100
        while time.monotonic()<deadline:
            db.expire_all()
            task=db.get(QueryTask,task_id)
            if task and task.status in {"succeeded","blocked","failed","cancelled"}:
                break
            if worker.poll() is not None:
                pytest.fail(f"Celery worker exited with {worker.returncode}: {log_path.read_text(encoding='utf-8')}")
            time.sleep(.25)
        assert task and task.status=="succeeded", {"status":task.status if task else None,"error":task.error_code if task else None,"worker_log":log_path.read_text(encoding='utf-8')}
        assert client.get(f"/tasks/{task_id}/result").status_code==200
    finally:
        worker.terminate()
        try: worker.wait(timeout=10)
        except subprocess.TimeoutExpired: worker.kill(); worker.wait(timeout=5)
        log_file.close()


@__import__("pytest").mark.skipif(not __import__("os").environ.get("DATACENTER_TEST_POSTGRES_URL") or not __import__("os").environ.get("DATACENTER_TEST_RABBITMQ_URL"),
    reason="Joint real PostgreSQL and RabbitMQ integration requires explicit local service URLs")
def test_postgres_and_rabbitmq_celery_worker_completes_original_task(monkeypatch):
    import os
    import psycopg
    from sqlalchemy.engine import make_url
    from sqlalchemy.orm import Session
    from .db import build_engine, init_db
    from .test_worker import local_stack as worker_fixture, submit
    from .runtime_models import AgentRun, ModelCallAdmission, TaskBudget
    from .execution_models import QueryAnswerSnapshot
    base = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    db_name = "dc_local_pgvector_queue_" + uuid4().hex[:16]
    admin_url = base.set(database="postgres").render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://", 1)
    target_url = base.set(database=db_name).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(__import__("psycopg").sql.SQL("CREATE DATABASE {}").format(__import__("psycopg").sql.Identifier(db_name)))
    engine = build_engine(target_url)
    config = Settings(database_url=target_url, storage_dir=__import__("pathlib").Path("outputs/local-stack/2026-11-17/queue")/db_name)
    init_db(engine, config, migrate=True)
    db = Session(engine, expire_on_commit=False, autoflush=False)
    stack = worker_fixture.__wrapped__((db, config))
    values = next(stack)
    try:
        db, config, client, address, employee, admin, kb, *_ = values
        broker = os.environ["DATACENTER_TEST_RABBITMQ_URL"]
        queue = "dc-local-pg-it-" + uuid4().hex
        monkeypatch.setenv("DATACENTER_CELERY_BROKER_URL", broker)
        monkeypatch.setenv("DATACENTER_DATABASE_URL", target_url)
        monkeypatch.setenv("DATACENTER_STORAGE_DIR", str(config.storage_dir.resolve()))
        monkeypatch.setenv("DATACENTER_PRIVATE_GATEWAY_URL", address)
        task_id=submit(client,kb)
        assert q.publish_pending(target_url,broker,queue_name=queue)["count"]==1
        log_path=__import__("pathlib").Path("outputs/local-stack/2026-11-17/queue")/(queue+".worker.log")
        log_path.parent.mkdir(parents=True,exist_ok=True)
        log=log_path.open("w",encoding="utf-8")
        worker=subprocess.Popen([sys.executable,"-m","celery","-A","src.server.queue_runtime:app","worker","-P","solo","-Q",queue,"-c","1","--without-gossip","--without-mingle","--without-heartbeat","-l","WARNING"],
            cwd=__import__("pathlib").Path(__file__).resolve().parents[2],env=os.environ.copy(),stdout=log,stderr=subprocess.STDOUT)
        try:
            deadline=time.monotonic()+100
            task=None
            while time.monotonic()<deadline:
                db.expire_all();task=db.get(QueryTask,task_id)
                if task and task.status in {"succeeded","blocked","failed","cancelled"}:break
                if worker.poll() is not None:raise AssertionError(log_path.read_text(encoding="utf-8"))
                time.sleep(.25)
            assert task and task.status=="succeeded", {"status":task.status if task else None,"error":task.error_code if task else None,"worker_log":log_path.read_text(encoding="utf-8")}
            assert client.get(f"/tasks/{task_id}/result").status_code==200
            snapshot=db.scalar(__import__("sqlalchemy").select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id==task_id))
            snapshot_id=snapshot.id
            runs=db.query(AgentRun).filter_by(task_id=task_id).count()
            calls=db.query(ModelCallAdmission).filter_by(task_id=task_id).count()
            budget=db.get(TaskBudget,task_id)
            reservations=(budget.reserved_calls,budget.reserved_tokens,budget.revision)
            outbox=db.query(TaskOutbox).filter_by(step_id=db.query(TaskStep).filter_by(task_id=task_id).one().id).one()
            dispatch_id=outbox.id
            # Simulate a duplicate broker delivery with the identical Celery
            # task ID after the business transaction already committed.
            q.celery_app(broker).send_task(q.execute_query.name,args=[task_id,dispatch_id],
                task_id=dispatch_id,queue=queue,retry=False)
            deadline=time.monotonic()+15
            while time.monotonic()<deadline:
                with __import__("kombu").Connection(broker) as connection:
                    channel=connection.channel()
                    state=__import__("kombu").Queue(queue,channel=channel,durable=True).queue_declare(passive=True)
                    pending=state.message_count
                    channel.close()
                if pending==0:break
                time.sleep(.1)
            db.expire_all()
            assert pending==0
            assert db.scalar(__import__("sqlalchemy").select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id==task_id)).id==snapshot_id
            assert db.query(AgentRun).filter_by(task_id=task_id).count()==runs
            assert db.query(ModelCallAdmission).filter_by(task_id=task_id).count()==calls
            budget=db.get(TaskBudget,task_id)
            assert (budget.reserved_calls,budget.reserved_tokens,budget.revision)==reservations
        finally:
            worker.terminate()
            try:worker.wait(timeout=10)
            except subprocess.TimeoutExpired:worker.kill();worker.wait(timeout=5)
            log.close()
    finally:
        try:next(stack)
        except StopIteration:pass
        db.close();engine.dispose()
        with psycopg.connect(admin_url,autocommit=True) as conn:
            conn.execute(__import__("psycopg").sql.SQL("DROP DATABASE IF EXISTS {}").format(__import__("psycopg").sql.Identifier(db_name)))


def test_celery_queue_uses_json_late_ack_and_ignores_results():
    app = q.celery_app("amqp://guest:guest@127.0.0.1:5672//")
    assert app.conf.task_serializer == "json"
    assert app.conf.accept_content == ["json"]
    assert app.conf.task_acks_late is True
    assert app.conf.task_reject_on_worker_lost is True
    assert app.conf.task_ignore_result is True
    assert app.conf.worker_prefetch_multiplier == 1
    assert app.conf.task_publish_retry is False
    assert app.conf.broker_transport_options == {"confirm_publish": True}
    assert app.conf.task_routes[q.execute_query.name]["queue"] == "data-center-query"


def test_queue_envelope_contains_only_task_and_dispatch_ids(monkeypatch):
    seen = {}
    class FakeTask:
        name = q.execute_query.name
        def apply_async(self, args, **kwargs):
            seen.update(args=args, kwargs=kwargs)
    monkeypatch.setattr(q, "execute_query", FakeTask())
    # Publisher path is exercised through Celery's task name and JSON protocol
    # contract; no text, scope, configuration or filesystem path belongs in it.
    envelope = {"task_id": "a" * 36, "dispatch_id": "b" * 36}
    serialized = json.dumps(envelope)
    assert set(json.loads(serialized)) == {"task_id", "dispatch_id"}


def test_worker_refuses_unconfigured_task_execution(monkeypatch):
    monkeypatch.delenv("DATACENTER_DATABASE_URL", raising=False)
    monkeypatch.delenv("DATACENTER_STORAGE_DIR", raising=False)
    monkeypatch.delenv("DATACENTER_PRIVATE_GATEWAY_URL", raising=False)
    try:
        q.execute_query.run(str(uuid4()), str(uuid4()))
    except RuntimeError as error:
        assert "database, storage and private gateway" in str(error)
    else:
        raise AssertionError("Worker must require explicit local configuration")


def test_outbox_batch_bounds_are_explicit():
    for invalid in (0, -1, 1001):
        try:
            q.publish_pending("sqlite://", "amqp://guest:guest@127.0.0.1:5672//", limit=invalid)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted invalid batch size {invalid}")


def test_publisher_claims_database_rows_and_emits_stable_id_only_envelope(fixture_db, monkeypatch):
    db, config = fixture_db
    actor = User(username="queue-"+uuid4().hex, display_name="Synthetic", status="active", roles=["employee"])
    db.add(actor); db.flush()
    task = QueryTask(owner_user_id=actor.id, input_snapshot={"query": "SYNTHETIC PRIVATE TEXT"},
        idempotency_key="queue-" + uuid4().hex, status="pending")
    db.add(task); db.flush()
    step = TaskStep(task_id=task.id, step_key="query", position=1, kind="knowledge_query", status="pending")
    db.add(step); db.flush()
    row = TaskOutbox(step_id=step.id, dispatch_no=1, status="pending", available_at=utc_now())
    db.add(row); db.commit()
    sent = []
    class FakeApp:
        def send_task(self, name, **kwargs): sent.append((name, kwargs))
    monkeypatch.setattr(q, "celery_app", lambda url: FakeApp())
    result = q.publish_pending(config.database_url, "amqp://synthetic", limit=3)
    assert result == {"published": [row.id], "count": 1}
    assert sent[0][1]["args"] == [task.id, row.id]
    assert sent[0][1]["task_id"] == row.id
    assert "SYNTHETIC PRIVATE TEXT" not in json.dumps(sent[0])
    db.refresh(row)
    assert row.status == "published" and row.publish_attempts == 1


def test_stale_publisher_cannot_overwrite_newer_claim(fixture_db, monkeypatch):
    db, config = fixture_db
    actor = User(username="lease-"+uuid4().hex, display_name="Synthetic", status="active", roles=["employee"])
    db.add(actor); db.flush()
    task = QueryTask(owner_user_id=actor.id, input_snapshot={}, idempotency_key="lease-"+uuid4().hex, status="pending")
    db.add(task); db.flush()
    step = TaskStep(task_id=task.id, step_key="query", position=1, kind="knowledge_query", status="pending")
    db.add(step); db.flush()
    row = TaskOutbox(step_id=step.id, dispatch_no=1, status="publishing", available_at=utc_now(), published_at=utc_now()-timedelta(minutes=3))
    db.add(row); db.commit()
    class FakeApp:
        def send_task(self, name, **kwargs):
            db.query(TaskOutbox).filter_by(id=row.id).update({"published_at": utc_now()+timedelta(seconds=5), "status":"publishing"})
            db.commit()
    monkeypatch.setattr(q, "celery_app", lambda url: FakeApp())
    q.publish_pending(config.database_url, "amqp://synthetic")
    db.refresh(row)
    assert row.status == "publishing" and row.published_at is not None


def test_query_publisher_leaves_nonquery_outbox_pending(fixture_db, monkeypatch):
    from .models import QueryTask, TaskOutbox, TaskStep, User, utc_now
    db, config = fixture_db
    user=User(username="maintenance-"+uuid4().hex,display_name="Synthetic",status="active",roles=["knowledge_manager"])
    db.add(user);db.flush()
    task=QueryTask(owner_user_id=user.id,kind="index_generation",input_snapshot={},idempotency_key=uuid4().hex,status="pending")
    db.add(task);db.flush()
    step=TaskStep(task_id=task.id,step_key="index",position=1,kind="index",status="pending")
    db.add(step);db.flush()
    row=TaskOutbox(step_id=step.id,dispatch_no=1,event_type="index.build",status="pending",available_at=utc_now())
    db.add(row);db.commit()
    class ForbiddenPublisher:
        def send_task(self,*args,**kwargs): raise AssertionError("non-query work entered query queue")
    monkeypatch.setattr(q,"celery_app",lambda url:ForbiddenPublisher())
    assert q.publish_pending(config.database_url,"amqp://local")=={"published":[],"count":0}
    db.refresh(row)
    assert row.status=="pending"


def test_worker_refuses_unknown_dispatch_for_exact_task(fixture_db):
    db, config = fixture_db
    user = User(username="dispatch-"+uuid4().hex, display_name="Synthetic", status="active", roles=["employee"])
    db.add(user); db.flush()
    task = QueryTask(owner_user_id=user.id, input_snapshot={}, idempotency_key=uuid4().hex, status="pending")
    db.add(task); db.flush()
    step = TaskStep(task_id=task.id, step_key="query", position=1, kind="knowledge_query", status="pending")
    db.add(step); db.flush()
    outbox = TaskOutbox(step_id=step.id, dispatch_no=1, status="published", available_at=utc_now())
    db.add(outbox); db.commit()
    result = run_one(config, gateway_url="http://127.0.0.1:65530/", task_id=task.id, dispatch_id=str(uuid4()))
    assert result == {"state": "unknown_dispatch"}


def test_celery_rejects_empty_and_noncanonical_ids():
    for task_id, dispatch_id in (("", str(uuid4())), (str(uuid4()), " "), ("not-a-uuid", str(uuid4()))):
        try:
            q.execute_query.run(task_id, dispatch_id)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid queue envelope was accepted")
