"""Explicit local schema maintenance; never upgrade a database during startup.

This is an accident-prevention boundary, not a substitute for deployment approval.
The incident database is quarantined, including aliases and hard links to it.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import inspect, text
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
PROTECTED_DATABASE = ROOT / "outputs/data-center.db"
RESTORE_DATABASE_PREFIX = "dc_local_restore_"
RESTORE_SQLITE_NAME = "_dc_restore_sealed.sqlite"
RESTORE_MARKER = "_dc_restore_validation_only"


class MigrationRefused(RuntimeError):
    pass


class MigrationEvidenceError(RuntimeError):
    def __init__(self, status: str, receipt_path: Path):
        self.status = status
        self.receipt_path = str(receipt_path.resolve())
        super().__init__("Inspect the database and migration receipt before any further maintenance.")


def _write_receipt(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def target_identity(url: str, *, restore_validation: bool = False) -> dict:
    """Validate before creating directories, opening a connection or running DDL."""
    value = make_url(url)
    if value.get_backend_name() == "sqlite":
        if not value.database or value.database == ":memory:" or value.query or value.database.startswith("file:"):
            raise MigrationRefused("Use an explicit local SQLite file, without URI options.")
        path = Path(value.database).resolve()
        protected = PROTECTED_DATABASE.resolve()
        alias = os.path.normcase(str(path)) == os.path.normcase(str(protected))
        if path.exists() and protected.exists():
            alias = alias or path.samefile(protected)
        if alias:
            raise MigrationRefused("The incident default SQLite database is quarantined; use an isolated database.")
        if (path.name.casefold() == RESTORE_SQLITE_NAME) != restore_validation:
            raise MigrationRefused("Restore databases can only be opened by the isolated validator.")
        return {"dialect": "sqlite", "path": str(path)}
    if value.drivername != "postgresql+psycopg" or value.host not in {"127.0.0.1", "::1"} or not value.port:
        raise MigrationRefused("Local maintenance requires an explicit loopback PostgreSQL endpoint.")
    if not value.database or not value.database.startswith("dc_local_") or value.query:
        raise MigrationRefused("Local PostgreSQL maintenance requires a dedicated dc_local_ database without URL options.")
    if value.database.startswith(RESTORE_DATABASE_PREFIX) != restore_validation:
        raise MigrationRefused("Restore databases can only be opened by the isolated validator.")
    return {"dialect": "postgresql", "host": value.host, "port": value.port,
            "database": value.database, "username": value.username}


def migration_config() -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "src/server/migrations"))
    return config


def current_head(connection) -> str | None:
    if not inspect(connection).has_table("alembic_version"):
        return None
    heads = list(connection.execute(text("SELECT version_num FROM alembic_version")).scalars())
    if len(heads) > 1:
        raise MigrationRefused("Multiple schema heads require a reviewed migration.")
    return heads[0] if heads else None


def desired_head() -> str:
    return ScriptDirectory.from_config(migration_config()).get_current_head()


def _schema_source() -> str:
    paths = sorted((ROOT / "src/server/migrations").rglob("*.py"))
    paths += [ROOT / "src/server" / name for name in
              ("models.py", "governance_models.py", "runtime_models.py", "execution_models.py", "vector_store.py",
               "service_models.py", "service_use_models.py", "parse_jobs.py", "embedding_jobs.py", "feedback_models.py")]
    return hashlib.sha256(json.dumps({p.relative_to(ROOT).as_posix(): _hash(p) for p in paths}, sort_keys=True).encode()).hexdigest()


def _sqlite_state(path: Path) -> dict:
    # The SHM file is a volatile lock index. Our own BEGIN IMMEDIATE may
    # create a rollback journal, which is not part of the pre-lock data state.
    return {"exists": path.exists(), "sha256": _hash(path) if path.exists() else None,
            "sidecars": {"-wal": _hash(Path(str(path) + "-wal"))} if Path(str(path) + "-wal").exists() else {}}


def maintenance_plan(url: str) -> dict:
    identity = target_identity(url)
    plan = {"target": identity, "to_revision": desired_head(), "schema_source_sha256": _schema_source()}
    if identity["dialect"] == "sqlite":
        path = Path(identity["path"])
        plan["before"] = _sqlite_state(path)
        if Path(str(path) + "-journal").exists():
            raise MigrationRefused("An existing SQLite rollback journal requires recovery review before maintenance.")
    else:
        # Deliberately contains no password/DSN. The apply path checks this DB's
        # OID and revision under a transaction-scoped migration lock as well.
        from .db import build_engine
        engine = build_engine(url)
        try:
            with engine.connect() as connection:
                plan["before"] = _pg_state(connection)
        finally:
            engine.dispose()
    return plan


def _pg_state(connection) -> dict:
    oid = connection.execute(text("SELECT oid FROM pg_database WHERE datname=current_database()" )).scalar_one()
    return {"database_oid": oid, "revision": current_head(connection),
            "tables": sorted(inspect(connection).get_table_names())}


def _counts(connection) -> dict[str, int]:
    quote = connection.dialect.identifier_preparer.quote
    return {name: connection.execute(text("SELECT COUNT(*) FROM " + quote(name))).scalar_one()
            for name in inspect(connection).get_table_names() if name != "alembic_version"}


def _triggers(connection) -> dict[str, str]:
    return {name: hashlib.sha256(sql.encode()).hexdigest() for name, sql in connection.exec_driver_sql(
        "SELECT name, sql FROM sqlite_master WHERE type='trigger' AND sql IS NOT NULL")}


def require_current_schema(engine) -> None:
    """Startup is a read check, never implicit file creation, migration or seed."""
    identity = target_identity(engine.url.render_as_string(hide_password=False))
    if identity["dialect"] == "sqlite" and not Path(identity["path"]).is_file():
        raise MigrationRefused("Database is not initialized. Run the explicit local migration command first.")
    with engine.connect() as connection:
        if current_head(connection) != desired_head():
            raise MigrationRefused("Schema is not current. Startup will not migrate it; run reviewed local maintenance.")


def apply_local_migrations(engine, settings, *, evidence_dir: Path | None = None, plan: dict | None = None) -> dict:
    identity = target_identity(settings.database_url)
    if target_identity(engine.url.render_as_string(hide_password=False)) != identity:
        raise MigrationRefused("Engine and settings refer to different databases.")
    actual_plan = maintenance_plan(settings.database_url)
    if plan is not None and plan != actual_plan:
        raise MigrationRefused("Target, database pre-state or migration sources changed after planning.")
    plan = actual_plan
    evidence = Path(evidence_dir) if evidence_dir else settings.storage_dir / "migration-evidence"
    evidence = evidence / uuid4().hex
    evidence.mkdir(parents=True, exist_ok=False)
    receipt = {"started_at": datetime.now(timezone.utc).isoformat(), "plan": plan, "status": "STARTED"}
    receipt_path = evidence / "receipt.json"
    _write_receipt(receipt_path, receipt)
    if identity["dialect"] == "sqlite":
        Path(identity["path"]).parent.mkdir(parents=True, exist_ok=True)
    committing = committed = False
    try:
        with engine.connect() as connection:
            if identity["dialect"] == "sqlite":
                # Serializes other writers. A separate read connection can take
                # a consistent backup while this connection holds RESERVED.
                connection.exec_driver_sql("BEGIN IMMEDIATE")
            else:
                connection.execute(text("SELECT pg_advisory_xact_lock(731824193)"))
                if _pg_state(connection) != plan["before"]:
                    raise MigrationRefused("PostgreSQL database identity/state changed after planning.")
            before_head = current_head(connection)
            before_counts = _counts(connection)
            if identity["dialect"] == "sqlite":
                # Recheck after the write lock, not just before connecting.
                # SHM is a volatile lock index, not part of the data identity.
                locked = _sqlite_state(Path(identity["path"]))
                if _schema_source() != plan["schema_source_sha256"] or desired_head() != plan["to_revision"]:
                    raise MigrationRefused("Migration sources changed after planning.")
                if plan["before"]["exists"]:
                    if locked != plan["before"]:
                        raise MigrationRefused("SQLite pre-state changed before the migration lock was acquired.")
                elif before_counts or before_head is not None:
                    raise MigrationRefused("A schema appeared at the planned new database path; re-plan explicitly.")
            receipt.update(from_revision=before_head, row_counts_before=before_counts)
            if before_head == desired_head():
                receipt["status"] = "ALREADY_CURRENT"
                connection.rollback()
            else:
                if identity["dialect"] == "postgresql" and before_counts:
                    raise MigrationRefused("Upgrading an existing PostgreSQL schema requires a separate stopped-write backup and migration review; local initialization refuses it.")
                triggers = _triggers(connection) if identity["dialect"] == "sqlite" else {}
                if identity["dialect"] == "sqlite" and plan["before"]["exists"]:
                    backup = evidence / "before.sqlite"
                    source = sqlite3.connect(Path(identity["path"]).as_uri() + "?mode=ro", uri=True)
                    destination = sqlite3.connect(backup)
                    try:
                        source.backup(destination)
                        if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise MigrationRefused("Pre-migration backup did not pass SQLite integrity check.")
                    finally:
                        destination.close(); source.close()
                    receipt["backup"] = {"path": str(backup.resolve()), "sha256": _hash(backup), "integrity": "ok"}
                config = migration_config()
                config.attributes["guarded_connection"] = connection
                config.attributes["guarded_target"] = identity
                receipt["status"] = "PREPARED"
                _write_receipt(receipt_path, receipt)
                command.upgrade(config, "head")
                after_counts = _counts(connection)
                if any(after_counts.get(name) != count for name, count in before_counts.items()):
                    raise MigrationRefused("Migration changed existing table row counts; transaction rolled back.")
                if identity["dialect"] == "sqlite":
                    after_triggers = _triggers(connection)
                    expected = config.attributes.get("declared_sqlite_triggers", {})
                    if (any(after_triggers.get(name) != sql for name, sql in triggers.items())
                            or any(name not in expected or sql != expected[name]
                                   for name, sql in after_triggers.items() if name not in triggers)):
                        raise MigrationRefused("Migration changed existing triggers or added undeclared triggers; transaction rolled back for review.")
                if current_head(connection) != desired_head():
                    raise MigrationRefused("Migration failed to reach the expected revision.")
                receipt.update(status="COMMITTING", to_revision=desired_head(), row_counts_after=after_counts)
                _write_receipt(receipt_path, receipt)
                committing = True
                connection.commit()
                committed = True
                receipt["status"] = "APPLIED"
    except BaseException as error:
        receipt.update(status="COMMIT_UNKNOWN" if committing else "FAILED", error_type=type(error).__name__)
        if committing:
            raise MigrationEvidenceError("COMMIT_UNKNOWN", receipt_path) from error
        raise
    finally:
        receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
        try:
            _write_receipt(receipt_path, receipt)
        except OSError as error:
            if committed:
                raise MigrationEvidenceError("APPLIED_EVIDENCE_FAILED", receipt_path) from error
            if committing:
                raise MigrationEvidenceError("COMMIT_UNKNOWN", receipt_path) from error
            if receipt["status"] == "ALREADY_CURRENT":
                raise MigrationEvidenceError("CURRENT_EVIDENCE_FAILED", receipt_path) from error
            raise
    return {**receipt, "receipt_path": str(receipt_path.resolve())}
