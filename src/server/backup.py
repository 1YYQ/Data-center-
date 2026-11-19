"""Full private pilot backup and fenced, read-only application recovery drill."""

from __future__ import annotations

import argparse
from contextlib import ExitStack, closing, contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import hmac
import json
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
from time import monotonic
from uuid import UUID, uuid4

from sqlalchemy import create_engine, func, inspect, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from .config import Settings
from .db import build_engine
from .feedback_models import FeedbackBase
from .migration_safety import (
    PROTECTED_DATABASE, RESTORE_MARKER, RESTORE_SQLITE_NAME, ROOT, MigrationRefused,
    current_head, desired_head, target_identity,
)
from .models import Base


class BackupRefused(RuntimeError):
    pass


class Deadline:
    def __init__(self, seconds: int):
        if not 1 <= seconds <= 14_400:
            raise BackupRefused("Backup and validation timeout must be between 1 and 14,400 seconds.")
        self.end = monotonic() + seconds

    def remaining(self) -> float:
        left = self.end - monotonic()
        if left <= 0:
            raise BackupRefused("Backup or restore timed out; private staging remains sealed.")
        return left


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value) -> bytes:
    def encode(item):
        if isinstance(item, (datetime, date)):
            if isinstance(item, datetime) and item.tzinfo is not None:
                item = item.astimezone(timezone.utc)
            return item.isoformat()
        if isinstance(item, (bytes, memoryview)):
            return {"bytes_hex": bytes(item).hex()}
        if isinstance(item, (Decimal, UUID)):
            return str(item)
        raise TypeError(f"Unsupported database value type: {type(item).__name__}")

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=encode).encode("utf-8")


def _write_new(path: Path, body: bytes) -> None:
    with path.open("xb") as target:
        target.write(body)
        target.flush()
        os.fsync(target.fileno())


def _disjoint(*paths: Path) -> None:
    for index, path in enumerate(paths):
        for other in paths[:index]:
            overlaps = path.is_relative_to(other) or other.is_relative_to(path)
            # NTFS short names and other aliases need not have equal spelling.
            for target, candidate in ((path, other), (other, path)):
                if target.exists():
                    for ancestor in (candidate, *candidate.parents):
                        if ancestor.exists() and target.samefile(ancestor):
                            overlaps = True
                            break
            if overlaps:
                raise BackupRefused("Source, private storage, backup, and restore paths must be distinct and nonoverlapping.")


def _plain_path(path: Path, *, missing: bool = False) -> Path:
    if os.name == "nt" and str(path).replace("/", "\\").startswith("\\\\"):
        raise BackupRefused("Device, extended-namespace and network paths require a separately approved backup target.")
    if not path.is_absolute() or ".." in path.parts:
        raise BackupRefused("Use an explicit absolute path without parent references.")
    for part in (path, *path.parents):
        try:
            info = part.lstat()
        except FileNotFoundError:
            if missing:
                continue
            raise
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            raise BackupRefused("Symbolic links and reparse points are refused in backup and restore paths.")
    return path.resolve()


def _private_key(key: str) -> list[str]:
    if not isinstance(key, str) or any(c in key for c in '\\:<>"|?*'):
        raise BackupRefused("Unsupported private object key.")
    parts = key.split("/")
    if any(not part or part[-1] in ". " or any(ord(c) < 32 for c in part)
           or PureWindowsPath(part).is_reserved() for part in parts):
        raise BackupRefused("Unsupported private object key.")
    return parts


def _file(root: Path, key: str, *, missing: bool = False) -> Path:
    return _plain_path(root.joinpath(*_private_key(key)), missing=missing)


def _file_identity(path: Path, deadline: Deadline, *, copy_to: Path | None = None) -> dict:
    digest, size = sha256(), 0
    with ExitStack() as stack:
        source = stack.enter_context(path.open("rb"))
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not os.path.samestat(before, path.lstat()):
            raise BackupRefused("Backup inputs must be regular private files without additional hard links.")
        target = None
        if copy_to is not None:
            copy_to.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            target = stack.enter_context(copy_to.open("xb"))
        while chunk := source.read(1024 * 1024):
            deadline.remaining()
            digest.update(chunk)
            size += len(chunk)
            if target is not None:
                target.write(chunk)
        if target is not None:
            target.flush()
            os.fsync(target.fileno())
        after = path.lstat()
        if (not os.path.samestat(before, after) or after.st_nlink != 1 or size != before.st_size
                or (after.st_size, after.st_mtime_ns) != (before.st_size, before.st_mtime_ns)):
            raise BackupRefused("A private input changed while being read.")
    return {"sha256": digest.hexdigest(), "byte_size": size}


def _source(url: str, storage: Path) -> tuple[dict, Path]:
    identity = target_identity(url)
    storage = _plain_path(storage)
    if not storage.is_dir() or storage == Path(storage.anchor):
        raise BackupRefused("Select an existing private trial storage directory.")
    _disjoint(storage, ROOT / "storage", PROTECTED_DATABASE.resolve())
    if identity["dialect"] == "sqlite":
        raw = Path(make_url(url).database)
        database = _plain_path(raw)
        _disjoint(database, storage)
        info = database.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise BackupRefused("The isolated SQLite source must be a regular file without hard links.")
        identity.update(device=info.st_dev, inode=info.st_ino)
    return identity, storage


def _sqlite_connection(path: Path, deadline: Deadline, *, readonly: bool):
    connection = sqlite3.connect(path.as_uri() + ("?mode=ro" if readonly else "?mode=rw"),
        uri=True, timeout=min(5, deadline.remaining()))
    connection.set_progress_handler(lambda: int(monotonic() >= deadline.end), 10_000)
    return connection


def _sqlite_snapshot(source: Path, destination: Path, deadline: Deadline):
    with closing(_sqlite_connection(source, deadline, readonly=True)) as original:
        with closing(sqlite3.connect(destination)) as snapshot:
            original.backup(snapshot, pages=128, progress=lambda *_: deadline.remaining(), sleep=.05)
            snapshot.set_progress_handler(lambda: int(monotonic() >= deadline.end), 10_000)
            snapshot.execute("PRAGMA journal_mode=DELETE")
            snapshot.execute(f"CREATE TABLE {RESTORE_MARKER} (sealed INTEGER CHECK (sealed=1))")
            snapshot.execute(f"INSERT INTO {RESTORE_MARKER} VALUES (1)")
            snapshot.commit()
            if (snapshot.execute("PRAGMA integrity_check").fetchall() != [("ok",)]
                    or snapshot.execute("PRAGMA foreign_key_check").fetchone() is not None):
                raise BackupRefused("SQLite snapshot did not pass physical and foreign-key checks.")


def _postgres_cli(command: str, url: str, args: list[str], deadline: Deadline) -> None:
    parsed = make_url(url)
    executable = shutil.which(command)
    if not executable or not parsed.username:
        raise BackupRefused(f"Install access to {command} or select an explicit PostgreSQL login for the pilot.")
    # No DSN or password in a command line, manifest, or diagnostic output.
    environment = {k: v for k, v in os.environ.items() if not k.upper().startswith("PG") or k.upper() == "PGPASSFILE"}
    environment.update(PGHOST=parsed.host, PGPORT=str(parsed.port), PGDATABASE=parsed.database,
        PGUSER=parsed.username, PGCONNECT_TIMEOUT="5", PGOPTIONS="-c statement_timeout=0 -c timezone=UTC")
    if parsed.password is not None:
        environment["PGPASSWORD"] = parsed.password
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    result = subprocess.run([executable, "--no-password", *args], env=environment, capture_output=True,
        stdin=subprocess.DEVNULL, timeout=deadline.remaining(), check=False, **options)
    if result.returncode:
        raise BackupRefused(f"{command} failed (exit {result.returncode}); inspect the isolated database/tool configuration.")


@contextmanager
def _connection(url: str, deadline: Deadline, *, sealed: bool = False, authority: bool = False):
    identity = target_identity(url, restore_validation=sealed)
    if identity["dialect"] == "sqlite" and sealed:
        path = Path(identity["path"])
        engine = create_engine(url, creator=lambda: _sqlite_connection(path, deadline, readonly=True))
    elif identity["dialect"] == "sqlite":
        engine = build_engine(url)
    else:
        engine = create_engine(url, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            if identity["dialect"] == "sqlite":
                if authority:
                    connection.exec_driver_sql("PRAGMA busy_timeout=1000")
                    connection.exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    connection.exec_driver_sql("PRAGMA query_only=ON")
                    connection.exec_driver_sql("BEGIN")
                connection.connection.driver_connection.set_progress_handler(lambda: int(monotonic() >= deadline.end), 10_000)
            else:
                if not authority:
                    connection.exec_driver_sql("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.exec_driver_sql("SET LOCAL TIME ZONE 'UTC'")
                connection.exec_driver_sql(f"SET LOCAL statement_timeout = {max(1, int(deadline.remaining() * 1000))}")
                connection.exec_driver_sql("SET LOCAL lock_timeout = 1000")
            yield connection
    finally:
        engine.dispose()


def _table_names(connection, *, sealed: bool = False) -> list[str]:
    inspector = inspect(connection)
    if connection.dialect.name == "postgresql":
        other = connection.exec_driver_sql("SELECT nspname FROM pg_namespace WHERE nspname NOT IN "
            "('public', 'information_schema') AND left(nspname, 3) <> 'pg_'").first()
        if other or inspector.default_schema_name != "public":
            raise BackupRefused("A dedicated PostgreSQL pilot database with public application schema is required.")
    names = set(inspector.get_table_names())
    if RESTORE_MARKER in names:
        if not sealed:
            raise BackupRefused("A sealed backup cannot provide current governance authority.")
        names.remove(RESTORE_MARKER)
    return sorted(names)


def _logical_state(connection, deadline: Deadline, *, sealed: bool = False, freeze: bool = False) -> dict:
    tables = _table_names(connection, sealed=sealed)
    quote = connection.dialect.identifier_preparer.quote
    if freeze and connection.dialect.name == "postgresql" and tables:
        # Keep the current authority stable all the way through the isolated application check.
        connection.exec_driver_sql("LOCK TABLE " + ", ".join(map(quote, tables)) + " IN SHARE MODE NOWAIT")
        if tables != _table_names(connection):
            raise BackupRefused("Authoritative schema changed during the recovery check.")
    revision = current_head(connection)
    expected = {*Base.metadata.tables, *FeedbackBase.metadata.tables, "alembic_version"}
    if connection.dialect.name == "postgresql":
        from .vector_store import VectorBase

        # These two pgvector job tables deliberately have no portable ORM table.
        expected.update(VectorBase.metadata.tables, ("chunk_embeddings", "index_embedding_batches"))
    if (revision != desired_head() or expected != set(tables)
            or sealed and connection.exec_driver_sql(f"SELECT sealed FROM {RESTORE_MARKER}").scalars().all() != [1]):
        raise BackupRefused("The complete current application schema is required for trial backup and recovery.")
    digest, counts = sha256(), {}
    inspector = inspect(connection)
    for table in tables:
        deadline.remaining()
        primary_key = inspector.get_pk_constraint(table)["constrained_columns"]
        if not primary_key:
            raise BackupRefused("Application database tables must have stable primary keys.")
        with connection.exec_driver_sql(f"SELECT * FROM {quote(table)} ORDER BY " + ", ".join(map(quote, primary_key)),
            execution_options={"stream_results": True}) as rows:
            digest.update(_json([table, list(rows.keys())]) + b"\n")
            count = 0
            for row in rows:
                deadline.remaining()
                digest.update(_json(list(row)) + b"\n")
                count += 1
            counts[table] = count
    return {"revision": revision, "tables": counts, "logical_sha256": digest.hexdigest()}


def _dependencies(connection):
    from .execution_models import RuntimeJournal
    from .models import FileObject

    files = {row.id: row for row in connection.execute(select(FileObject.__table__))}
    entries, unavailable = {}, []

    def add(key, item, role):
        _private_key(key)
        previous = entries.setdefault(key, {"sha256": item.sha256, "byte_size": item.byte_size, "refs": []})
        if (not re.fullmatch(r"[0-9a-f]{64}", item.sha256) or item.byte_size < 0
                or (item.sha256, item.byte_size) != (previous["sha256"], previous["byte_size"])):
            raise BackupRefused("A private file has an invalid or conflicting database identity.")
        previous["refs"].append(role)

    for file_id, file in sorted(files.items()):
        if file.storage_name != "local":
            raise BackupRefused("This trial backup requires the configured local private storage adapter.")
        if file.status in {"pending", "deleted"}:
            unavailable.append({"file_id": file_id, "state": file.status})
        elif file.status in {"available", "quarantined", "deletion_pending"}:
            add(file.object_key, file, {"file_id": file_id, "kind": "registered"})
        else:
            raise BackupRefused("Unrecognized private file lifecycle state.")
    latest = {}
    journals = connection.execute(select(RuntimeJournal.__table__).order_by(
        RuntimeJournal.generation.desc(), RuntimeJournal.created_at.desc(), RuntimeJournal.id.desc()))
    for journal in journals:
        if journal.binding_id not in latest:
            latest[journal.binding_id] = journal.id
        if journal.state != "active" or latest[journal.binding_id] != journal.id:
            continue
        file = files.get(journal.snapshot_file_id)
        prefix = f"runtime/{journal.binding_id}/journal/"
        if (not file or file.status != "available" or file.sha256 != journal.content_sha256
                or not journal.working_key.startswith(prefix) or not journal.working_key.endswith(".jsonl")):
            raise BackupRefused("A recoverable working journal has no matching registered snapshot.")
        add(journal.working_key, file, {"journal_id": journal.id, "kind": "working_journal"})
    if len({key.casefold() for key in entries}) != len(entries):
        raise BackupRefused("Private file keys collide under Windows case folding.")
    return entries, unavailable


def _verify_files(entries: dict, source: Path, deadline: Deadline, *, destination: Path | None = None):
    for key, entry in entries.items():
        copied = _file_identity(_file(source, key), deadline,
            copy_to=_file(destination, key, missing=True) if destination is not None else None)
        if any(copied[field] != entry[field] for field in ("sha256", "byte_size")):
            raise BackupRefused("A required private file no longer matches the database snapshot.")


def _sqlite_url(path: Path) -> str:
    return URL.create("sqlite", database=str(path)).render_as_string(hide_password=False)


def _database_oid(connection) -> int:
    return connection.exec_driver_sql("SELECT oid FROM pg_database WHERE datname=current_database()").scalar_one()


def create_backup(source_url: str, source_storage: Path, backup_dir: Path, *, timeout: int = 3600) -> dict:
    deadline = Deadline(timeout)
    identity, storage = _source(source_url, source_storage)
    backup_dir = _plain_path(backup_dir, missing=True)
    _disjoint(storage, backup_dir, PROTECTED_DATABASE.resolve(), *([Path(identity["path"])] if "path" in identity else []))
    _disjoint(backup_dir, ROOT / "storage")
    backup_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    job = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "Z-" + uuid4().hex
    staging = backup_dir / (".pending-" + job)
    staging.mkdir(mode=0o700)
    (staging / "private").mkdir(mode=0o700)
    started = _now()
    sqlite = identity["dialect"] == "sqlite"
    database = staging / (RESTORE_SQLITE_NAME if sqlite else "database.pgcustom")
    if sqlite:
        _sqlite_snapshot(Path(identity["path"]), database, deadline)
    with _connection(_sqlite_url(database) if sqlite else source_url, deadline, sealed=sqlite) as snapshot:
        captured = _now()
        if not sqlite:
            identity["database_oid"] = _database_oid(snapshot)
            snapshot_id = snapshot.exec_driver_sql("SELECT pg_export_snapshot()").scalar_one()
            if not re.fullmatch(r"[0-9A-Fa-f-]+", snapshot_id):
                raise BackupRefused("Invalid PostgreSQL snapshot identity.")
        state = _logical_state(snapshot, deadline, sealed=sqlite)
        entries, unavailable = _dependencies(snapshot)
        if not sqlite:
            _postgres_cli("pg_dump", source_url, ["--format=custom", "--no-owner", "--no-privileges", "--no-tablespaces",
                "--lock-wait-timeout=2000", "--snapshot=" + snapshot_id, "--file=" + str(database)], deadline)
        _verify_files(entries, storage, deadline, destination=staging / "private")
    database_identity = _file_identity(database, deadline)
    manifest = {"format": "datacenter-pilot-backup-v1", "job": job, "started_at": started, "captured_at": captured,
        "finished_at": _now(), "source": identity, "storage": str(storage), "state": state,
        "database": {"name": database.name, **database_identity},
        "files": entries, "unavailable": unavailable}
    encoded = _json(manifest)
    _write_new(staging / "manifest.json", encoded)
    package = backup_dir / job
    staging.rename(package)
    return {"status": "BACKUP_COMPLETE", "package": str(package), "manifest_sha256": sha256(encoded).hexdigest(),
        "captured_at": captured, "table_count": len(state["tables"]), "private_file_count": len(entries)}


def _load_manifest(package: Path, pinned: str, deadline: Deadline) -> dict:
    if not re.fullmatch(r"[0-9a-f]{64}", pinned):
        raise BackupRefused("Supply the manifest SHA-256 from the original backup receipt.")
    manifest_file = _file(package, "manifest.json")
    identity = _file_identity(manifest_file, deadline)
    if identity["byte_size"] > 64 * 1024 * 1024 or not hmac.compare_digest(identity["sha256"], pinned):
        raise BackupRefused("Package manifest does not match the pinned identity.")
    body = manifest_file.read_bytes()
    if not hmac.compare_digest(sha256(body).hexdigest(), pinned):
        raise BackupRefused("Package manifest changed during validation.")
    manifest = json.loads(body)
    if manifest["format"] != "datacenter-pilot-backup-v1" or manifest["job"] != package.name:
        raise BackupRefused("Backup package manifest has an unexpected format or job identity.")
    return manifest


def _empty_postgres(target_url: str, source: dict, deadline: Deadline) -> None:
    target = target_identity(target_url, restore_validation=True)
    if (target["dialect"] != "postgresql" or not re.fullmatch(r"dc_local_restore_[0-9a-f]{12,32}", target["database"])
            or (target["host"], target["port"], target["database"]) == (source["host"], source["port"], source["database"])):
        raise BackupRefused("Use a new, empty PostgreSQL database named dc_local_restore_<random hex>.")
    with _connection(target_url, deadline, sealed=True) as connection:
        if (_table_names(connection, sealed=True) or connection.exec_driver_sql(
            "SELECT 1 FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' LIMIT 1").first()
                or connection.exec_driver_sql("SELECT 1 FROM pg_extension WHERE extname <> 'plpgsql' LIMIT 1").first()):
            raise BackupRefused("The isolated PostgreSQL restore target is not empty.")


def _seal_postgres(target_url: str, deadline: Deadline) -> None:
    target_identity(target_url, restore_validation=True)
    engine = create_engine(target_url, connect_args={"connect_timeout": min(5, max(1, int(deadline.remaining())))})
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("SET LOCAL lock_timeout = 1000")
            connection.exec_driver_sql(f"CREATE TABLE public.{RESTORE_MARKER} (sealed INTEGER CHECK (sealed=1))")
            connection.exec_driver_sql(f"INSERT INTO public.{RESTORE_MARKER} VALUES (1)")
    finally:
        engine.dispose()


def _check_task(connection, storage: Path, actor_id: str, task_id: str, expect_denied: bool) -> dict:
    from .evidence_api import _source as evidence_original, _source_bytes, visible_task_evidence
    from .execution_models import QueryAnswerSnapshot
    from .models import QueryTask, User
    from .query_evidence import shared_task_answer
    from .services import SafeError

    config = Settings(database_url="", storage_dir=storage)
    identity = {"actor_id": actor_id, "task_id": task_id}
    with Session(bind=connection, autoflush=False, info={"settings": config}) as db:
        actor = db.get(User, actor_id)
        try:
            if not actor or actor.status != "active":
                raise SafeError("ACCESS_LIMITED", "Current account does not have access", 403)
            evidence = visible_task_evidence(db, actor, task_id)
            task = db.get(QueryTask, task_id)
            if task.status != "succeeded":
                raise BackupRefused("The requested answer has not succeeded.")
            snapshot = db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task.id,
                QueryAnswerSnapshot.execution_revision == task.execution_revision)) if task.owner_user_id == actor_id else shared_task_answer(db, actor, task)[0]
            if not snapshot or not evidence or not snapshot.result.get("answer"):
                raise BackupRefused("The application did not recover an answer snapshot with evidence.")
            for item in evidence:
                _source_bytes(evidence_original(db, actor, task.id, item.id).file, config)
        except SafeError as error:
            if not expect_denied or error.status_code not in {403, 404}:
                raise BackupRefused("Current application evidence or original is inaccessible.") from None
            return {**identity, "state": "DENIED_AS_EXPECTED", "status_code": error.status_code}
        if expect_denied:
            raise BackupRefused("Recovery unexpectedly opened an answer whose access was expected to be denied.")
        return {**identity, "state": "READABLE", "snapshot_id": snapshot.id, "evidence_count": len(evidence),
            "answer_sha256": sha256(_json(snapshot.result["answer"])).hexdigest()}


def _execution_summary(connection) -> dict:
    from .runtime_models import ModelCallAdmission, TaskBudget
    from .service_use_models import ServiceSelection

    calls = connection.execute(select(ModelCallAdmission.state, func.count()).group_by(ModelCallAdmission.state))
    budgets = connection.execute(select(func.sum(TaskBudget.reserved_calls), func.sum(TaskBudget.reserved_tokens))).one()
    selections = connection.execute(select(ServiceSelection.purpose, ServiceSelection.enabled, ServiceSelection.epoch)
        .order_by(ServiceSelection.purpose))
    return {"model_call_states": {state: count for state, count in calls}, "reserved_calls": budgets[0] or 0,
        "reserved_tokens": budgets[1] or 0, "service_selections": [dict(row._mapping) for row in selections]}


def restore_backup(package: Path, manifest_sha256: str, source_url: str, source_storage: Path, restore_dir: Path, *,
                   target_url: str | None = None, task_checks: tuple[tuple[str, str, bool], ...] = (), timeout: int = 3600) -> dict:
    deadline = Deadline(timeout)
    started = _now()
    source, storage = _source(source_url, source_storage)
    package, restore_dir = _plain_path(package), _plain_path(restore_dir, missing=True)
    _disjoint(storage, package, restore_dir, PROTECTED_DATABASE.resolve(), *([Path(source["path"])] if "path" in source else []))
    _disjoint(restore_dir, ROOT / "storage")
    manifest = _load_manifest(package, manifest_sha256, deadline)
    sqlite = source["dialect"] == "sqlite"
    expected_name = RESTORE_SQLITE_NAME if sqlite else "database.pgcustom"
    if manifest["database"]["name"] != expected_name or sqlite == bool(target_url):
        raise BackupRefused("Select a same-engine target; only PostgreSQL requires a separate target URL.")
    database = _file(package, expected_name)
    backup_private = _file(package, "private")
    if {"name": expected_name, **_file_identity(database, deadline)} != manifest["database"]:
        raise BackupRefused("Database snapshot does not match its backup manifest.")
    # Refuse stale snapshots before creating a target, then recheck under a writer fence
    # before disclosing anything. The copy never becomes an ordinary runtime database.
    with _connection(source_url, deadline) as authority:
        if not sqlite:
            source["database_oid"] = _database_oid(authority)
        if manifest["source"] != source or manifest["storage"] != str(storage):
            raise BackupRefused("The live authority is not the source bound to this backup manifest.")
        if _logical_state(authority, deadline) != manifest["state"]:
            raise BackupRefused("Current governance or execution evidence differs from the backup cutoff; take a fresh backup.")
    if target_url is not None:
        _empty_postgres(target_url, source, deadline)
    restore_dir.mkdir(mode=0o700, exist_ok=False)
    private = restore_dir / "private"
    private.mkdir(mode=0o700)
    protected_copy = restore_dir / expected_name
    if {"name": expected_name, **_file_identity(database, deadline, copy_to=protected_copy)} != manifest["database"]:
        raise BackupRefused("The copy changed before isolated restore.")
    _verify_files(manifest["files"], backup_private, deadline, destination=private)
    if target_url is not None:
        _seal_postgres(target_url, deadline)
        _postgres_cli("pg_restore", target_url, ["--dbname=" + make_url(target_url).database, "--single-transaction",
            "--exit-on-error", "--no-owner", "--no-privileges", "--no-tablespaces", str(protected_copy)], deadline)
    else:
        target_url = _sqlite_url(protected_copy)
    with _connection(target_url, deadline, sealed=True) as restored:
        state = _logical_state(restored, deadline, sealed=True)
        if state != manifest["state"]:
            raise BackupRefused("Restored application database does not match the captured state.")
        entries, unavailable = _dependencies(restored)
        if entries != manifest["files"] or unavailable != manifest["unavailable"]:
            raise BackupRefused("Restored database private references do not match their manifest.")
        with _connection(source_url, deadline, authority=True) as authority:
            current_source, _ = _source(source_url, storage)
            if not sqlite:
                current_source["database_oid"] = _database_oid(authority)
            if current_source != source:
                raise BackupRefused("Current authority changed identity during restore; the copy remains sealed.")
            if _logical_state(authority, deadline, freeze=True) != state:
                raise BackupRefused("Current authority moved during restore; the copy remains sealed.")
            _verify_files(entries, storage, deadline)
            checks = [_check_task(restored, private, actor, task, denied) for actor, task, denied in task_checks]
            execution = _execution_summary(restored)
    receipt = {"status": "ISOLATED_RESTORE_VERIFIED", "restore_started_at": started, "finished_at": _now(),
        "manifest_sha256": manifest_sha256, "captured_at": manifest["captured_at"], "state": state,
        "checks": checks, "execution": execution, "private_file_count": len(entries), "runtime_fenced": True}
    _write_new(restore_dir / "validation.json", _json(receipt))
    return receipt


def _env(name: str) -> str:
    if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name) or not os.getenv(name):
        raise BackupRefused("The selected credential/configuration environment variable is unavailable.")
    return os.environ[name]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    backup = commands.add_parser("create")
    backup.add_argument("--backup-dir", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--package", type=Path, required=True)
    restore.add_argument("--manifest-sha256", required=True)
    restore.add_argument("--restore-dir", type=Path, required=True)
    restore.add_argument("--target-url-env")
    restore.add_argument("--readable-task", nargs=2, action="append", metavar=("ACTOR_ID", "TASK_ID"), default=[])
    restore.add_argument("--denied-task", nargs=2, action="append", metavar=("ACTOR_ID", "TASK_ID"), default=[])
    for command in (backup, restore):
        command.add_argument("--source-url-env", required=True)
        command.add_argument("--source-storage", type=Path, required=True)
        command.add_argument("--timeout-seconds", type=int, default=3600)
    args = parser.parse_args()
    try:
        source_url = _env(args.source_url_env)
        if args.command == "create":
            result = create_backup(source_url, args.source_storage, args.backup_dir, timeout=args.timeout_seconds)
        else:
            checks = tuple((actor, task, False) for actor, task in args.readable_task) + tuple(
                (actor, task, True) for actor, task in args.denied_task)
            result = restore_backup(args.package, args.manifest_sha256, source_url, args.source_storage,
                args.restore_dir, target_url=_env(args.target_url_env) if args.target_url_env else None,
                task_checks=checks, timeout=args.timeout_seconds)
    except (BackupRefused, MigrationRefused) as error:
        print(f"Backup/recovery refused: {error}", file=sys.stderr)
        return 2
    except Exception as error:
        # SQL/driver errors can carry credentials or row data. Do not put them in a scheduler log.
        print(f"Backup/recovery failed ({type(error).__name__}); inspect the isolated configuration and staging.", file=sys.stderr)
        return 2
    print(_json(result).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
