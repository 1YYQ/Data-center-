"""Opt-in PostgreSQL verification using only generated PDFs and fake SDK output."""
from contextlib import contextmanager
from datetime import timedelta
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .db import build_engine
from .migration_safety import apply_local_migrations, current_head, desired_head, require_current_schema
from .models import (AuthorizationGrant, BlockRelation, ContentBlock, DocumentPage, DocumentVersion, FileObject,
                     ParseArtifact, ParseRun, User, utc_now)
from .parse_jobs import ParseServiceJob, accept_local_parse, run_local_parse_job, run_parse_job
from .parsing.artifacts import read_artifact
from .services import save_upload
from .test_governance import synthetic_draft
from .test_local_parse_pipeline import saved_bundle


pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="Requires an explicit isolated local PostgreSQL URL")
ROOT = Path("outputs/pdf-implementation/2026-11-19/postgres")
_CRASHED = 73


def _json_write(path: Path, values: dict) -> None:
    staged = path.with_suffix(".pending")
    staged.write_text(json.dumps(values, sort_keys=True), encoding="utf-8")
    staged.replace(path)


@pytest.fixture(scope="module")
def pdf_pg():
    import psycopg
    from psycopg import sql

    base = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    assert base.drivername == "postgresql+psycopg" and base.host == "127.0.0.1" and base.port == 55440
    assert base.database and base.database.startswith("dc_local_") and not base.query
    database = "dc_local_pdf_" + uuid4().hex[:20]
    report = (ROOT / database).resolve()
    report.mkdir(parents=True, exist_ok=False)
    url = base.set(database=database).render_as_string(hide_password=False)
    admin = base.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)
    settings = Settings(database_url=url, storage_dir=report / "storage", local_mineru_executable=None, local_mineru_home=None)
    lifecycle = {"database": database, "created": False, "dropped": False}
    engine = None
    try:
        with psycopg.connect(admin, autocommit=True, connect_timeout=5) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        lifecycle["created"] = True
        engine = build_engine(url)
        receipt = apply_local_migrations(engine, settings)
        lifecycle.update(from_revision=receipt["from_revision"], applied_revision=receipt["to_revision"],
                         migration_source_sha256=receipt["plan"]["schema_source_sha256"])
        yield SimpleNamespace(engine=engine, settings=settings, report=report)
    finally:
        if engine is not None:
            engine.dispose()
        try:
            if lifecycle["created"]:
                assert database.startswith("dc_local_pdf_") and database != base.database
                with psycopg.connect(admin, autocommit=True, connect_timeout=5) as connection:
                    connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(database)))
                    assert connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (database,)).fetchone() is None
                lifecycle["dropped"] = True
        finally:
            _json_write(report / "lifecycle.json", lifecycle)


def _seed_parse(fixture):
    import pymupdf

    with pymupdf.open() as pdf:
        pdf.new_page().insert_text((72, 72), "SYNTHETIC: switch off before service.")
        pdf.new_page()
        original = pdf.tobytes()
    with Session(fixture.engine, expire_on_commit=False) as db:
        actor, _, _, version, _, _ = synthetic_draft(db)
        file = save_upload([original], "synthetic.pdf", "application/pdf", actor, fixture.settings)
        db.add(file)
        db.flush()
        version.source_file_id = file.id
        run = accept_local_parse(db, actor, version, file, fixture.settings)
        db.commit()
        return SimpleNamespace(id=run.id, actor_id=actor.id, version_id=version.id)


def _wait_for(path: Path, seconds=20) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if path.is_file():
            return
        time.sleep(0.05)
    raise AssertionError(f"SYNTHETIC worker signal timed out: {path.name}")


@contextmanager
def _spawn_worker(fixture, run_id, case, label, directory):
    # DSN is passed in the child's private environment and never in argv or logs.
    env = {**os.environ, "DATACENTER_TEST_POSTGRES_URL": fixture.settings.database_url,
           "DC_PDF_WORKER_STORAGE": str(fixture.settings.storage_dir), "DC_PDF_WORKER_CASE": case,
           "DC_PDF_WORKER_ID": run_id, "DC_PDF_WORKER_LABEL": label, "DC_PDF_WORKER_SIGNALS": str(directory)}
    args = [sys.executable, "-m", "src.server.test_pdf_postgres"]
    child = subprocess.Popen(args, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        yield child
    finally:
        if child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def _worker_main() -> int:
    root = Path(os.environ["DC_PDF_WORKER_SIGNALS"])
    run_id, case, label = (os.environ[f"DC_PDF_WORKER_{key}"] for key in ("ID", "CASE", "LABEL"))
    config = Settings(database_url=os.environ["DATACENTER_TEST_POSTGRES_URL"], storage_dir=Path(os.environ["DC_PDF_WORKER_STORAGE"]),
                      local_mineru_executable=None, local_mineru_home=None)
    engine = build_engine(config.database_url)
    result = root / f"{label}-result.json"
    try:
        require_current_schema(engine)
        with Session(engine, expire_on_commit=False) as db:
            session_pid = db.scalar(text("SELECT pg_backend_pid()"))
            db.rollback()
            _json_write(root / f"{label}-ready.json", {"process_pid": os.getpid(), "database_pid": session_pid})
            if case == "race":
                _wait_for(root / "start.flag")

            def runner(source, workspace, executable, home, heartbeat):
                _json_write(root / f"{label}-runner.json", {"process_pid": os.getpid(), "epoch": int(workspace.name)})
                if case == "recover":
                    raise AssertionError("SYNTHETIC recovery repeated inference")
                if case in {"race", "late"}:
                    _wait_for(root / "return.flag")
                archive = saved_bundle(image=True)
                with (workspace / "result.zip").open("xb") as stream:
                    stream.write(archive)
                    stream.flush()
                    os.fsync(stream.fileno())
                _json_write(root / f"{label}-closed.json", {"sha256": hashlib.sha256(archive).hexdigest()})
                if case == "crash":
                    os._exit(_CRASHED)

            raw = run_local_parse_job(db, run_id, config, runner=runner)
            if case in {"recover", "late"} and raw.status == "needs_review":
                raw = run_parse_job(db, run_id, config, {})
            _json_write(result, {"status": raw.status, "error_code": raw.error_code,
                "stage_state": raw.stage_checkpoint.get("stage_state"), "process_pid": os.getpid(), "database_pid": session_pid})
        return 0
    except Exception as error:
        _json_write(result, {"error_type": type(error).__name__})
        return 1
    finally:
        engine.dispose()


def _result(child, directory, label):
    status = child.wait(timeout=30)
    report = directory / f"{label}-result.json"
    evidence = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    assert status == 0, evidence
    return evidence


def test_postgres_enforces_same_run_page_parent_and_relation_foreign_keys(pdf_pg):
    seed = _seed_parse(pdf_pg)
    with Session(pdf_pg.engine, expire_on_commit=False) as db:
        assert current_head(db.connection()) == desired_head()
        first = db.get(ParseRun, seed.id)
        second = ParseRun(document_version_id=seed.version_id, status="pending")
        db.add(second)
        db.flush()
        db.add_all([DocumentPage(parse_run_id=first.id, file_page=1), DocumentPage(parse_run_id=second.id, file_page=2)])
        db.flush()
        left = ContentBlock(parse_run_id=first.id, file_page=1, ordinal=1)
        right = ContentBlock(parse_run_id=first.id, file_page=1, ordinal=2)
        foreign = ContentBlock(parse_run_id=second.id, file_page=2, ordinal=1)
        db.add_all([left, right, foreign])
        db.flush()
        db.add(BlockRelation(parse_run_id=first.id, from_block_id=left.id, to_block_id=right.id, relation_type="warns"))
        db.flush()
        outcomes = []

        def reject(change, constraint):
            with pytest.raises(IntegrityError) as caught:
                with db.begin_nested():
                    change()
                    db.flush()
            assert caught.value.orig.sqlstate == "23503" and caught.value.orig.diag.constraint_name == constraint
            outcomes.append(constraint)

        reject(lambda: setattr(left, "file_page", 2), "fk_content_blocks_page")
        reject(lambda: setattr(left, "parent_block_id", foreign.id), "fk_content_blocks_parent")
        reject(lambda: db.add(BlockRelation(parse_run_id=first.id, from_block_id=foreign.id, to_block_id=right.id,
                                           relation_type="continues")), "fk_relation_from")
        reject(lambda: db.add(BlockRelation(parse_run_id=first.id, from_block_id=left.id, to_block_id=foreign.id,
                                           relation_type="continues")), "fk_relation_to")
        db.commit()
    _json_write(pdf_pg.report / "foreign-keys.json", {"sqlstate": "23503", "rejected_by": outcomes})


def test_postgres_workers_contend_for_one_lease_and_execute_one_runner(pdf_pg):
    run = _seed_parse(pdf_pg)
    signals = pdf_pg.report / "lease-race"
    signals.mkdir()
    with _spawn_worker(pdf_pg, run.id, "race", "left", signals) as left, _spawn_worker(pdf_pg, run.id, "race", "right", signals) as right:
        _wait_for(signals / "left-ready.json")
        _wait_for(signals / "right-ready.json")
        (signals / "start.flag").touch()
        # The actual winner pauses inside the fake SDK, keeping its lease live
        # until the independent contender has returned without calling a runner.
        end = time.monotonic() + 20
        while time.monotonic() < end and left.poll() is None and right.poll() is None:
            time.sleep(0.05)
        assert (left.poll() is None) != (right.poll() is None)
        (signals / "return.flag").touch()
        first, second = _result(left, signals, "left"), _result(right, signals, "right")
    assert first["process_pid"] != second["process_pid"] and first["database_pid"] != second["database_pid"]
    assert sorted([first["status"], second["status"]]) == ["needs_review", "running"]
    assert len(list(signals.glob("*-runner.json"))) == 1
    with Session(pdf_pg.engine) as db:
        assert db.get(ParseServiceJob, run.id).attempt == 1
        assert db.get(ParseRun, run.id).stage_checkpoint["outputs"].get("raw_manifest")


def test_postgres_grant_revocation_commits_before_late_sdk_output_and_blocks_promotion(pdf_pg):
    run = _seed_parse(pdf_pg)
    signals = pdf_pg.report / "revoked-late-result"
    signals.mkdir()
    with _spawn_worker(pdf_pg, run.id, "late", "late", signals) as child:
        _wait_for(signals / "late-runner.json")
        with Session(pdf_pg.engine) as db:
            # A lock held through inference would keep this concurrent revoke
            # from finishing. Bound that failure without altering worker code.
            db.execute(text("SET LOCAL lock_timeout = '4s'"))
            result = db.execute(update(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == run.actor_id,
                AuthorizationGrant.operation == "knowledge.manage").values(revoked_at=utc_now()))
            assert result.rowcount >= 1
            db.commit()
        (signals / "return.flag").touch()
        stopped = _result(child, signals, "late")
    assert (stopped["status"], stopped["error_code"]) == ("failed", "PARSE_AUTHORITY_REVOKED")
    assert (signals / "late-closed.json").is_file()
    with Session(pdf_pg.engine) as db:
        actual = db.get(ParseRun, run.id)
        assert not actual.stage_checkpoint["outputs"].get("raw_manifest") and not actual.ir_artifact_id
        assert db.scalar(select(func.count()).select_from(DocumentPage).where(DocumentPage.parse_run_id == run.id)) == 0
        assert db.scalar(select(func.count()).select_from(ContentBlock).where(ContentBlock.parse_run_id == run.id)) == 0


def test_postgres_recovers_closed_zip_from_a_dead_worker_process_without_another_runner(pdf_pg):
    run = _seed_parse(pdf_pg)
    signals = pdf_pg.report / "closed-zip-recovery"
    signals.mkdir()
    with _spawn_worker(pdf_pg, run.id, "crash", "interrupted", signals) as child:
        assert child.wait(timeout=30) == _CRASHED
    closed = json.loads((signals / "interrupted-closed.json").read_text(encoding="utf-8"))
    with Session(pdf_pg.engine) as db:
        old_job = db.get(ParseServiceJob, run.id)
        assert old_job.attempt == 1 and db.get(ParseRun, run.id).status == "running"
        assert old_job.lease_until > utc_now()
        # Advance only the isolated fixture's lease deadline. The worker host
        # actually exited, and all recovery uses durable PG and workspace state.
        old_job.lease_until = utc_now() - timedelta(seconds=1)
        db.commit()
    with _spawn_worker(pdf_pg, run.id, "recover", "recovery", signals) as child:
        recovered = _result(child, signals, "recovery")
    assert recovered["status"] == "needs_review" and recovered["error_code"] is None
    assert not (signals / "recovery-runner.json").exists()
    with Session(pdf_pg.engine) as db:
        actual = db.get(ParseRun, run.id)
        outputs = actual.stage_checkpoint["outputs"]
        assert outputs["raw_epoch"] == 1 and outputs.get("normalized_ir")
        assert db.get(ParseServiceJob, run.id).attempt >= 2
        assert actual.stage_checkpoint["attempts"][0]["state"] == "interrupted"
        original = db.get(ParseArtifact, actual.stage_checkpoint["page_selection"]["1"]["raw_artifact_id"])
        assert hashlib.sha256(read_artifact(db, original, pdf_pg.settings)).hexdigest() == closed["sha256"]
        assert db.scalar(select(func.count()).select_from(DocumentPage).where(DocumentPage.parse_run_id == run.id)) == 2


if __name__ == "__main__":
    raise SystemExit(_worker_main())
