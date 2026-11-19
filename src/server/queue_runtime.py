"""Celery/RabbitMQ adapter over the durable database outbox.

Messages contain opaque task/dispatch references only. Database, storage and
private gateway configuration are loaded by the worker from its environment.
"""
from __future__ import annotations

import os
import json
import re
import argparse
from pathlib import Path
from uuid import UUID

from celery import Celery
from datetime import timedelta

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .config import Settings
from .db import build_engine
from .models import QueryTask, TaskOutbox, TaskStep, utc_now
from .services import _utc


def celery_app(broker_url: str | None = None) -> Celery:
    url = broker_url or os.environ.get("DATACENTER_CELERY_BROKER_URL")
    if not url:
        raise RuntimeError("DATACENTER_CELERY_BROKER_URL is required")
    app = Celery("data_center", broker=url, include=["src.server.queue_runtime"])
    app.conf.update(
        task_serializer="json", accept_content=["json"], result_serializer="json",
        task_acks_late=True, task_reject_on_worker_lost=True, worker_prefetch_multiplier=1,
        task_ignore_result=True, broker_connection_retry_on_startup=True,
        task_publish_retry=False, broker_transport_options={"confirm_publish": True},
        worker_enable_remote_control=False,
        task_routes={"src.server.queue_runtime.execute_query": {"queue": "data-center-query"}},
    )
    return app


app = celery_app() if os.environ.get("DATACENTER_CELERY_BROKER_URL") else Celery("data_center_unconfigured")


@app.task(name="src.server.queue_runtime.execute_query", bind=True, acks_late=True, ignore_result=True)
def execute_query(self, task_id: str, dispatch_id: str) -> dict:
    try:
        if str(UUID(task_id)) != task_id or str(UUID(dispatch_id)) != dispatch_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Opaque task and dispatch identifiers are required")
    database_url = os.environ.get("DATACENTER_DATABASE_URL")
    storage_dir = os.environ.get("DATACENTER_STORAGE_DIR")
    gateway_url = os.environ.get("DATACENTER_PRIVATE_GATEWAY_URL")
    if not database_url or not storage_dir or not gateway_url:
        raise RuntimeError("Worker database, storage and private gateway configuration are required")
    from .worker import run_one
    result = run_one(Settings(database_url=database_url, storage_dir=Path(storage_dir)), gateway_url=gateway_url,
                     profile_id=os.environ.get("DATACENTER_RUNTIME_PROFILE_ID", "local-pi"),
                     profile_revision=int(os.environ.get("DATACENTER_RUNTIME_PROFILE_REVISION", "1")),
                     task_id=task_id, dispatch_id=dispatch_id)
    return {"task_id": task_id, "dispatch_id": dispatch_id, "state": result.get("state", "unknown")}


def publish_pending(database_url: str, broker_url: str, *, limit: int = 100,
                    queue_name: str = "data-center-query") -> dict:
    """Publish a bounded batch; duplicate sends reuse the stable outbox ID."""
    if not 1 <= limit <= 1000:
        raise ValueError("Outbox batch limit must be between 1 and 1000")
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,100}", queue_name):
        raise ValueError("Invalid queue name")
    engine = build_engine(database_url)
    try:
        queue_app = celery_app(broker_url)
        published = []
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            now = utc_now()
            stale = now - timedelta(minutes=2)
            rows = db.scalars(select(TaskOutbox).join(TaskStep, TaskStep.id == TaskOutbox.step_id)
                .join(QueryTask, QueryTask.id == TaskStep.task_id).where(
                QueryTask.kind == "query", TaskStep.step_key == "query",
                or_(TaskOutbox.status == "pending", (TaskOutbox.status == "publishing") & (TaskOutbox.published_at < stale)),
                TaskOutbox.available_at <= now).order_by(TaskOutbox.available_at, TaskOutbox.id).limit(limit)
                .with_for_update(skip_locked=True)).all()
            claimed = []
            for row in rows:
                prior = row.status
                claim_where = [TaskOutbox.id == row.id, TaskOutbox.status == prior]
                if prior == "publishing":
                    claim_where.append(TaskOutbox.published_at == row.published_at)
                acquired = db.execute(update(TaskOutbox).where(*claim_where)
                    .values(status="publishing", published_at=now, publish_attempts=(row.publish_attempts or 0) + 1)
                    .execution_options(synchronize_session=False))
                if acquired.rowcount == 1:
                    claimed.append((row.id, row.step_id, now))
            db.commit()
        # Claim transaction is committed before network I/O. Concurrent
        # publishers cannot send the same active claim; a stale claim reuses
        # the same dispatch id after its lease expires.
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            for dispatch_id, step_id, claimed_at in claimed:
                step = db.get(TaskStep, step_id)
                task = db.get(QueryTask, step.task_id) if step else None
                if not step or not task or task.status not in {"pending", "running"}:
                    row = db.get(TaskOutbox, dispatch_id)
                    if row and row.status == "publishing" and row.published_at and _utc(row.published_at) == _utc(claimed_at):
                        row.status = "superseded"
                    db.commit()
                    continue
                # Kombu publisher confirms are enabled above. A lost confirm
                # leaves an expiring publishing lease; resend retains ID.
                queue_app.send_task(execute_query.name, args=[task.id, dispatch_id],
                    task_id=dispatch_id, queue=queue_name, retry=False)
                db.execute(update(TaskOutbox).where(TaskOutbox.id == dispatch_id,
                    TaskOutbox.status == "publishing", TaskOutbox.published_at == claimed_at)
                    .values(status="published", published_at=utc_now()).execution_options(synchronize_session=False))
                db.commit()
                published.append(dispatch_id)
        return {"published": published, "count": len(published)}
    finally:
        engine.dispose()


def main() -> None:
    import time
    import sys
    from kombu.exceptions import OperationalError as BrokerUnavailable
    from sqlalchemy.exc import OperationalError as DatabaseUnavailable
    parser = argparse.ArgumentParser(description="Publish pending local database outbox rows to RabbitMQ")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--publish-once", action="store_true")
    mode.add_argument("--publish-loop", action="store_true")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()
    database_url = os.environ.get("DATACENTER_DATABASE_URL")
    broker_url = os.environ.get("DATACENTER_CELERY_BROKER_URL")
    queue_name = os.environ.get("DATACENTER_CELERY_QUEUE", "data-center-query")
    if not database_url or not broker_url:
        raise SystemExit("DATACENTER_DATABASE_URL and DATACENTER_CELERY_BROKER_URL must be configured")
    if not args.publish_loop:
        print(json.dumps(publish_pending(database_url, broker_url, limit=args.limit, queue_name=queue_name)))
        return
    if not 0.2 <= args.interval <= 300:
        raise SystemExit("--interval must be between 0.2 and 300 seconds")
    try:
        while True:
            try:
                result = publish_pending(database_url, broker_url, limit=args.limit, queue_name=queue_name)
                if result["count"]:
                    print(json.dumps(result), flush=True)
            except (BrokerUnavailable, DatabaseUnavailable, OSError) as error:
                # Keep the stable outbox identity. No retry/new task ID here;
                # failed claims expire and are reconsidered on a later pass.
                print(f"outbox publish unavailable ({type(error).__name__}); retry after claim lease", file=sys.stderr, flush=True)
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
