"""Advance one accepted embedding job, or scan a bounded local batch."""
from __future__ import annotations

import argparse
import json
import os

from sqlalchemy import and_, literal, or_, select, union_all
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .config import Settings
from .db import build_engine
from .embedding_jobs import ChildEmbeddingBatch, EmbeddingServiceJob, run_child_embedding_batch, run_embedding_job
from .migration_safety import MigrationRefused, require_current_schema, target_identity
from .models import IndexGeneration, utc_now
from .provider_transport import ProviderError
from .service_admission import load_local_endpoints
from .services import SafeError


DEFAULT_SCAN_LIMIT = 8
MAX_SCAN_LIMIT = 32


def _eligible(model, now):
    return or_(and_(model.state == "pending", or_(model.lease_until.is_(None), model.lease_until <= now)),
               and_(model.state == "running", model.lease_until <= now))


def scan_once(db: Session, config: Settings, endpoints: dict, *, limit: int = DEFAULT_SCAN_LIMIT) -> dict:
    """Dispatch existing embedding jobs without changing the worker's submit fence."""
    if type(limit) is not int or not 1 <= limit <= MAX_SCAN_LIMIT:
        raise ValueError(f"Scan limit must be between 1 and {MAX_SCAN_LIMIT}")
    if db.get_bind().dialect.name != "postgresql":
        raise ProviderError("EMBEDDING_VECTOR_STORE_REQUIRED")
    now = utc_now()
    legacy = select(EmbeddingServiceJob.id, literal("embedding").label("kind"), EmbeddingServiceJob.created_at).where(
        _eligible(EmbeddingServiceJob, now))
    current = select(ChildEmbeddingBatch.id, literal("child_embedding").label("kind"), ChildEmbeddingBatch.created_at).join(
        IndexGeneration, IndexGeneration.id == ChildEmbeddingBatch.generation_id).where(
        IndexGeneration.status == "building", _eligible(ChildEmbeddingBatch, now))
    candidates = union_all(legacy, current).subquery()
    work = db.execute(select(candidates.c.id, candidates.c.kind).order_by(
        candidates.c.created_at, candidates.c.id, candidates.c.kind).limit(limit)).all()
    db.rollback()
    jobs = []
    for job_id, kind in work:
        try:
            if kind == "child_embedding":
                job = run_child_embedding_batch(db, job_id, endpoints)
            else:
                job = run_embedding_job(db, job_id, config, endpoints)
            jobs.append({"kind": kind, "id": job_id, "state": job.state})
        except (ProviderError, SafeError):
            jobs.append({"kind": kind, "id": job_id, "state": "blocked"})
        finally:
            db.rollback()
    return {"count": len(jobs), "jobs": jobs}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--job-id")
    target.add_argument("--scan-once", action="store_true")
    parser.add_argument("--limit", type=int, help=f"Maximum jobs for --scan-once (1–{MAX_SCAN_LIMIT}, default {DEFAULT_SCAN_LIMIT})")
    args = parser.parse_args(argv)
    if args.limit is not None and (not args.scan_once or not 1 <= args.limit <= MAX_SCAN_LIMIT):
        parser.error(f"--limit requires --scan-once and a value from 1 to {MAX_SCAN_LIMIT}")
    if args.job_id is not None and not args.job_id.strip():
        parser.error("Job identifier must be nonempty")
    if not os.environ.get("DATACENTER_DATABASE_URL"):
        parser.error("DATACENTER_DATABASE_URL must name an explicit local PostgreSQL database")
    config = Settings()
    try:
        if target_identity(config.database_url)["dialect"] != "postgresql":
            raise ProviderError("EMBEDDING_VECTOR_STORE_REQUIRED")
        endpoints = load_local_endpoints()
        engine = build_engine(config.database_url)
        try:
            require_current_schema(engine)
            with Session(engine, info={"settings": config}) as db:
                if args.scan_once:
                    print(json.dumps(scan_once(db, config, endpoints, limit=args.limit or DEFAULT_SCAN_LIMIT)))
                else:
                    job = run_embedding_job(db, args.job_id, config, endpoints)
                    print(" ".join(filter(None, (job.id, job.state, job.error_code))))
                    if job.state != "succeeded":
                        parser.exit(1)
        finally:
            engine.dispose()
    except (MigrationRefused, SQLAlchemyError):
        parser.exit(1, "EMBEDDING_DATABASE_UNAVAILABLE\n")
    except (ProviderError, SafeError) as error:
        parser.exit(1, error.code + "\n")


if __name__ == "__main__":
    main()
