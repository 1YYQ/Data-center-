"""Migration target mistakes must fail before changing the protected database."""
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import inspect, text

from .config import Settings
from .db import build_engine, init_db
from . import migration_safety as safety


def test_accident_command_is_refused_without_opening_default_database():
    path = safety.PROTECTED_DATABASE
    before = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    sidecars = {suffix: Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")}
    result = subprocess.run([sys.executable, "-m", "alembic", "-c", "alembic.ini", "-x", "none", "upgrade", "head"],
                            cwd=safety.ROOT, capture_output=True, text=True, encoding="utf-8")
    assert result.returncode != 0 and "Bare Alembic is disabled" in result.stderr
    assert (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None) == before
    assert {suffix: Path(str(path) + suffix).exists() for suffix in sidecars} == sidecars


def test_default_connection_is_refused_even_without_startup_hook():
    path = safety.PROTECTED_DATABASE
    before = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
    engine = build_engine("sqlite:///" + path.as_posix())
    try:
        with pytest.raises(safety.MigrationRefused, match="quarantined"):
            with engine.connect(): pass
        assert (hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None) == before
    finally:
        engine.dispose()


def test_explicit_default_target_and_path_aliases_are_refused(tmp_path, monkeypatch):
    protected = tmp_path / "protected.sqlite"
    protected.write_bytes(b"SYNTHETIC quarantined fixture")
    monkeypatch.setattr(safety, "PROTECTED_DATABASE", protected)
    alias = tmp_path / "hardlink.sqlite"
    os.link(protected, alias)
    for path in (protected, tmp_path / "x/../protected.sqlite", alias):
        with pytest.raises(safety.MigrationRefused, match="quarantined"):
            safety.target_identity("sqlite:///" + path.as_posix())
    with pytest.raises(safety.MigrationRefused):
        safety.target_identity("sqlite:///file:" + protected.as_posix() + "?uri=true")
    assert protected.read_bytes() == b"SYNTHETIC quarantined fixture"


def test_implicit_or_mismatched_schema_creation_is_refused(tmp_path):
    with pytest.raises(safety.MigrationRefused, match="explicit"):
        init_db()
    config = Settings(database_url="sqlite:///" + (tmp_path / "a.sqlite").as_posix(), storage_dir=tmp_path / "storage")
    engine = build_engine("sqlite:///" + (tmp_path / "b.sqlite").as_posix())
    try:
        with pytest.raises(safety.MigrationRefused, match="different"):
            init_db(engine, config)
        with pytest.raises(safety.MigrationRefused, match="bypass"):
            init_db(engine, config, migrate=False)
        assert not (tmp_path / "a.sqlite").exists() and not (tmp_path / "b.sqlite").exists()
    finally:
        engine.dispose()


def test_existing_sqlite_is_backed_up_and_rows_and_triggers_survive(tmp_path):
    path = tmp_path / "existing.sqlite"
    with sqlite3.connect(path) as connection:
        connection.executescript("CREATE TABLE sentinel(id INTEGER PRIMARY KEY, value TEXT);"
                                 "INSERT INTO sentinel VALUES(1,'SYNTHETIC preserve');"
                                 "CREATE TRIGGER preserve_marker AFTER UPDATE ON sentinel BEGIN SELECT 1; END;")
    config = Settings(database_url="sqlite:///" + path.as_posix(), storage_dir=tmp_path / "storage")
    engine = build_engine(config.database_url)
    try:
        receipt = safety.apply_local_migrations(engine, config)
        assert receipt["status"] == "APPLIED"
        backup = Path(receipt["backup"]["path"])
        assert hashlib.sha256(backup.read_bytes()).hexdigest() == receipt["backup"]["sha256"]
        with sqlite3.connect(backup.as_uri() + "?mode=ro", uri=True) as db:
            assert db.execute("SELECT value FROM sentinel").fetchone()[0] == "SYNTHETIC preserve"
            assert db.execute("SELECT count(*) FROM sqlite_master WHERE name='users'").fetchone()[0] == 0
        safety.require_current_schema(engine)
        with engine.connect() as db:
            assert db.scalar(text("SELECT value FROM sentinel")) == "SYNTHETIC preserve"
            assert db.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='preserve_marker'")) == 1
        again = safety.apply_local_migrations(engine, config)
        assert again["status"] == "ALREADY_CURRENT" and "backup" not in again
    finally:
        engine.dispose()


def test_changed_plan_or_prestate_is_refused_before_ddl(tmp_path):
    path = tmp_path / "changed.sqlite"
    config = Settings(database_url="sqlite:///" + path.as_posix(), storage_dir=tmp_path / "storage")
    plan = safety.maintenance_plan(config.database_url)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sentinel(value TEXT)")
    before = path.read_bytes()
    engine = build_engine(config.database_url)
    try:
        with pytest.raises(safety.MigrationRefused, match="changed"):
            safety.apply_local_migrations(engine, config, plan=plan)
        assert path.read_bytes() == before
        with engine.connect() as db:
            assert inspect(db).get_table_names() == ["sentinel"]
    finally:
        engine.dispose()


def test_prestate_race_between_plan_and_lock_is_refused(tmp_path, monkeypatch):
    path = tmp_path / "race.sqlite"
    config = Settings(database_url="sqlite:///" + path.as_posix(), storage_dir=tmp_path / "storage")
    original = safety.maintenance_plan
    calls = 0
    def plan_then_other_writer(url):
        nonlocal calls
        result = original(url)
        calls += 1
        if calls == 1:
            with sqlite3.connect(path) as db:
                db.execute("CREATE TABLE concurrent_writer(value TEXT)")
                db.execute("INSERT INTO concurrent_writer VALUES ('SYNTHETIC other writer')")
        return result
    monkeypatch.setattr(safety, "maintenance_plan", plan_then_other_writer)
    engine = build_engine(config.database_url)
    try:
        with pytest.raises(safety.MigrationRefused, match="appeared"):
            safety.apply_local_migrations(engine, config)
        with engine.connect() as db:
            assert inspect(db).get_table_names() == ["concurrent_writer"]
            assert db.scalar(text("SELECT count(*) FROM concurrent_writer")) == 1
    finally:
        engine.dispose()


def test_evidence_failure_after_commit_is_reported_as_applied(tmp_path, monkeypatch):
    path = tmp_path / "committed.sqlite"
    config = Settings(database_url="sqlite:///" + path.as_posix(), storage_dir=tmp_path / "storage")
    original = safety._write_receipt
    def fail_final_receipt(receipt_path, value):
        if value["status"] == "APPLIED":
            raise OSError("SYNTHETIC evidence disk unavailable after commit")
        original(receipt_path, value)
    monkeypatch.setattr(safety, "_write_receipt", fail_final_receipt)
    engine = build_engine(config.database_url)
    try:
        with pytest.raises(safety.MigrationEvidenceError) as caught:
            safety.apply_local_migrations(engine, config)
        assert caught.value.status == "APPLIED_EVIDENCE_FAILED"
        assert json.loads(Path(caught.value.receipt_path).read_text())["status"] == "COMMITTING"
        safety.require_current_schema(engine)
    finally:
        engine.dispose()


def test_cli_never_reports_refused_after_a_successful_commit(tmp_path, monkeypatch, capsys):
    from . import migrate
    path = tmp_path / "cli.sqlite"
    monkeypatch.setenv("SYNTHETIC_MIGRATION_TARGET", "sqlite:///" + path.as_posix())
    common = ["--url-env", "SYNTHETIC_MIGRATION_TARGET", "--plan", str(tmp_path / "plan.json")]
    monkeypatch.setattr(sys, "argv", ["migrate", "plan", *common])
    assert migrate.main() == 0
    capsys.readouterr()
    original = safety._write_receipt
    def fail_final(receipt_path, value):
        if value["status"] == "APPLIED": raise OSError("SYNTHETIC full evidence disk")
        original(receipt_path, value)
    monkeypatch.setattr(safety, "_write_receipt", fail_final)
    monkeypatch.setattr(sys, "argv", ["migrate", "apply", *common, "--storage-dir", str(tmp_path / "storage"),
                                     "--evidence-dir", str(tmp_path / "evidence")])
    assert migrate.main() == 3
    assert json.loads(capsys.readouterr().out)["status"] == "APPLIED_EVIDENCE_FAILED"


def test_lost_commit_ack_requires_inspection_instead_of_refusal_or_retry(tmp_path, monkeypatch):
    from sqlalchemy.engine import Connection
    path = tmp_path / "unknown.sqlite"
    config = Settings(database_url="sqlite:///" + path.as_posix(), storage_dir=tmp_path / "storage")
    original = Connection.commit
    def committed_but_ack_lost(connection):
        original(connection)
        raise OSError("SYNTHETIC commit acknowledgement lost")
    monkeypatch.setattr(Connection, "commit", committed_but_ack_lost)
    engine = build_engine(config.database_url)
    try:
        with pytest.raises(safety.MigrationEvidenceError) as caught:
            safety.apply_local_migrations(engine, config)
        assert caught.value.status == "COMMIT_UNKNOWN"
        assert json.loads(Path(caught.value.receipt_path).read_text())["status"] == "COMMIT_UNKNOWN"
        with engine.connect() as db:
            assert safety.current_head(db) == safety.desired_head()
    finally:
        engine.dispose()


def test_trigger_on_rebuilt_legacy_table_forces_complete_rollback(tmp_path):
    path = tmp_path / "legacy-trigger.sqlite"
    with sqlite3.connect(path) as db:
        db.executescript((Path(__file__).parent / "migrations/fixtures/v1_schema.sql").read_text(encoding="utf-8"))
        db.executescript("CREATE TABLE alembic_version(version_num VARCHAR(32) PRIMARY KEY);"
                         "INSERT INTO alembic_version VALUES('0001_initial');"
                         "CREATE TRIGGER legacy_trigger AFTER UPDATE ON document_versions BEGIN SELECT 1; END;")
    before = path.read_bytes()
    config = Settings(database_url="sqlite:///" + path.as_posix(), storage_dir=tmp_path / "storage")
    engine = build_engine(config.database_url)
    try:
        with pytest.raises(safety.MigrationRefused, match="triggers"):
            safety.apply_local_migrations(engine, config)
        assert path.read_bytes() == before
        with engine.connect() as db:
            assert safety.current_head(db) == "0001_initial"
            assert db.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='legacy_trigger'")) == 1
    finally:
        engine.dispose()


def test_startup_does_not_create_or_upgrade_schema(tmp_path):
    from fastapi.testclient import TestClient
    from .app import create_app
    path = tmp_path / "uninitialized.sqlite"
    config = Settings(database_url="sqlite:///" + path.as_posix(), storage_dir=tmp_path / "storage")
    app = create_app(config)
    try:
        with pytest.raises(safety.MigrationRefused, match="not initialized"):
            with TestClient(app): pass
        assert not path.exists()
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE sentinel(value TEXT)")
        before = path.read_bytes()
        with pytest.raises(safety.MigrationRefused, match="not current"):
            with TestClient(app): pass
        assert path.read_bytes() == before
    finally:
        app.state.engine.dispose()


@pytest.mark.parametrize("url", ["postgresql+psycopg://user@enterprise.example:5432/business",
                               "postgresql+psycopg://user@127.0.0.1:5432/business",
                               "postgresql+psycopg://user@127.0.0.1/dc_local_test"])
def test_local_maintenance_rejects_external_or_ordinary_postgres_targets(url):
    with pytest.raises(safety.MigrationRefused):
        safety.target_identity(url)
