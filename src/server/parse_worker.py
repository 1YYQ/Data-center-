"""Advance an accepted parse or chunk set, or scan a bounded local batch."""
from __future__ import annotations

import argparse
import json
import os

from sqlalchemy import and_, or_, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from .chunk_jobs import run_chunk_job
from .config import Settings
from .db import build_engine
from .migration_safety import MigrationRefused, require_current_schema, target_identity
from .models import ChunkSet, ParseRun, utc_now
from .parse_jobs import ParseServiceJob, run_parse_job
from .parsing.artifacts import ArtifactError
from .parsing.local_pipeline import ParseContractError
from .provider_transport import ProviderError
from .service_admission import load_local_endpoints
from .services import SafeError


DEFAULT_SCAN_LIMIT = 8
MAX_SCAN_LIMIT = 32


def _parse_candidates(now):
    outputs = ParseRun.stage_checkpoint["outputs"]
    raw = outputs["raw_manifest"].as_string()
    source_qc = outputs["source_qc"].as_string()
    lease = ParseServiceJob.lease_until
    return select(ParseRun.id).join(ParseServiceJob, ParseServiceJob.parse_run_id == ParseRun.id).where(
        ParseServiceJob.cancel_requested.is_(False), or_(
            and_(ParseRun.status == "pending", or_(lease.is_(None), lease <= now)),
            and_(ParseRun.status == "running", lease <= now),
            and_(ParseServiceJob.mode == "local", ParseRun.status == "needs_review", raw.is_not(None),
                 source_qc.is_(None), or_(lease.is_(None), lease <= now))))


def _chunk_candidates(now):
    outputs = ParseRun.stage_checkpoint["outputs"]
    phase = ChunkSet.stage_checkpoint["phase"].as_string()
    lease = ChunkSet.stage_checkpoint["lease_until"].as_string()
    return select(ChunkSet.id).join(ParseRun, ParseRun.id == ChunkSet.parse_run_id).where(
        ChunkSet.status == "building", ChunkSet.config_hash.is_not(None), ParseRun.ir_artifact_id.is_not(None),
        ParseRun.status.in_(("needs_review", "succeeded")), outputs["source_qc"].as_string().is_not(None),
        or_(and_(phase == "accepted", lease.is_(None)), and_(phase == "building", lease <= now.isoformat())))


def scan_once(db: Session, config: Settings, endpoints: dict, *, limit: int = DEFAULT_SCAN_LIMIT) -> dict:
    """Select existing work only. All claims and processing belong to the workers."""
    if type(limit) is not int or not 1 <= limit <= MAX_SCAN_LIMIT:
        raise ValueError(f"Scan limit must be between 1 and {MAX_SCAN_LIMIT}")
    jobs = []
    candidates = _parse_candidates(utc_now()).order_by(ParseRun.created_at, ParseRun.id).limit(limit)
    parse_ids = db.scalars(candidates).all()
    db.rollback()
    for run_id in parse_ids:
        eligible = _parse_candidates(utc_now()).where(ParseRun.id == run_id).with_for_update(of=ParseServiceJob, skip_locked=True)
        try:
            if db.scalar(eligible):
                row = run_parse_job(db, run_id, config, endpoints)
                jobs.append({"kind": "parse", "id": run_id, "state": row.status})
        except (ProviderError, SafeError, ArtifactError, ParseContractError):
            jobs.append({"kind": "parse", "id": run_id, "state": "blocked"})
        finally:
            db.rollback()

    remaining = limit - len(parse_ids)
    if remaining and config.local_bge_tokenizer and config.local_bge_tokenizer.is_file():
        chunk_ids = db.scalars(_chunk_candidates(utc_now()).order_by(ChunkSet.id).limit(remaining)).all()
        db.rollback()
        for set_id in chunk_ids:
            try:
                if db.scalar(_chunk_candidates(utc_now()).where(ChunkSet.id == set_id)):
                    row = run_chunk_job(db, set_id, config, accepted_only=True)
                    jobs.append({"kind": "chunk", "id": set_id, "state": row.status})
            except (ProviderError, SafeError, ArtifactError, ParseContractError):
                jobs.append({"kind": "chunk", "id": set_id, "state": "blocked"})
            finally:
                db.rollback()
    return {"count": len(jobs), "jobs": jobs}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--run-id")
    target.add_argument("--chunk-set-id")
    target.add_argument("--scan-once", action="store_true")
    parser.add_argument("--limit", type=int, help=f"Maximum jobs for --scan-once (1–{MAX_SCAN_LIMIT}, default {DEFAULT_SCAN_LIMIT})")
    args = parser.parse_args(argv)
    if args.limit is not None and (not args.scan_once or not 1 <= args.limit <= MAX_SCAN_LIMIT):
        parser.error(f"--limit requires --scan-once and a value from 1 to {MAX_SCAN_LIMIT}")
    if any(value is not None and not value.strip() for value in (args.run_id, args.chunk_set_id)):
        parser.error("Job identifiers must be nonempty")
    if not os.environ.get("DATACENTER_DATABASE_URL") or not os.environ.get("DATACENTER_STORAGE_DIR"):
        parser.error("DATACENTER_DATABASE_URL and DATACENTER_STORAGE_DIR must be explicitly set")
    config = Settings()
    engine = None
    try:
        target_identity(config.database_url)
        engine = build_engine(config.database_url)
        require_current_schema(engine)
        with Session(engine, info={"settings": config}) as db:
            if args.scan_once:
                print(json.dumps(scan_once(db, config, load_local_endpoints(), limit=args.limit or DEFAULT_SCAN_LIMIT)))
            else:
                result = (run_chunk_job(db, args.chunk_set_id, config) if args.chunk_set_id else
                          run_parse_job(db, args.run_id, config, load_local_endpoints()))
                print(result.id, result.status)
    except (ProviderError, SafeError, ArtifactError, ParseContractError) as error:
        code = error.detail["code"] if isinstance(error, SafeError) else error.code
        parser.exit(1, f"{code}\n")
    except MigrationRefused:
        parser.exit(1, "PARSE_DATABASE_NOT_READY\n")
    except SQLAlchemyError:
        parser.exit(1, "PARSE_DATABASE_UNAVAILABLE\n")
    finally:
        if engine is not None:
            engine.dispose()


if __name__ == "__main__":
    main()
