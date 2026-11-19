"""Check the PDF migration against the persisted pre-PDF table definitions."""
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import event, inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings
from .db import build_engine
from .migration_safety import apply_local_migrations, desired_head
from .models import Chunk, ChunkSet, ContentBlock, QueryTask, TaskEvidence
from .test_pdf_postgres import _seed_parse, pdf_pg


# The affected 0010 table shapes are pinned here because 0001 and 0009 import
# live ORM metadata, which now creates the post-PDF shapes on fresh databases.
# The other tables below are minimal parents for the affected foreign keys.
_LEGACY_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
INSERT INTO alembic_version VALUES ('0010_document_feedback');
CREATE TABLE users (id VARCHAR(36) NOT NULL PRIMARY KEY);
CREATE TABLE document_versions (id VARCHAR(36) NOT NULL PRIMARY KEY);
CREATE TABLE file_objects (id VARCHAR(36) NOT NULL PRIMARY KEY);
CREATE TABLE service_use_pins (id VARCHAR(36) NOT NULL PRIMARY KEY);
CREATE TABLE query_tasks (id VARCHAR(36) NOT NULL PRIMARY KEY);
CREATE TABLE agent_runs (id VARCHAR(36) NOT NULL PRIMARY KEY);
CREATE TABLE agent_session_bindings (id VARCHAR(36) NOT NULL PRIMARY KEY);
CREATE TABLE parse_runs (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    document_version_id VARCHAR(36) NOT NULL REFERENCES document_versions(id),
    origin VARCHAR(30) NOT NULL,
    provider_task_id VARCHAR(200),
    status VARCHAR(30) NOT NULL,
    output_revision VARCHAR(40) NOT NULL,
    error_code VARCHAR(100),
    created_at DATETIME NOT NULL
);
CREATE INDEX ix_parse_runs_document_version_id ON parse_runs(document_version_id);
CREATE INDEX ix_parse_runs_status ON parse_runs(status);
CREATE TABLE parse_service_jobs (
    parse_run_id VARCHAR(36) NOT NULL PRIMARY KEY REFERENCES parse_runs(id),
    pin_id VARCHAR(36) NOT NULL REFERENCES service_use_pins(id),
    actor_id VARCHAR(36) NOT NULL REFERENCES users(id),
    source_file_id VARCHAR(36) NOT NULL REFERENCES file_objects(id),
    source_sha256 VARCHAR(64) NOT NULL,
    raw_result_file_id VARCHAR(36) REFERENCES file_objects(id),
    attempt INTEGER NOT NULL,
    lease_until DATETIME,
    UNIQUE (pin_id)
);
CREATE TABLE content_blocks (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    parse_run_id VARCHAR(36) NOT NULL REFERENCES parse_runs(id),
    ordinal INTEGER NOT NULL,
    kind VARCHAR(40) NOT NULL,
    text_content TEXT NOT NULL,
    structured_content JSON NOT NULL,
    locator JSON NOT NULL,
    review_status VARCHAR(20) NOT NULL,
    review_reason VARCHAR(500),
    revision INTEGER DEFAULT '0' NOT NULL
);
CREATE INDEX ix_content_blocks_parse_run_id ON content_blocks(parse_run_id);
CREATE TABLE chunk_sets (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    parse_run_id VARCHAR(36) NOT NULL REFERENCES parse_runs(id),
    splitter_version VARCHAR(60) NOT NULL,
    status VARCHAR(20) NOT NULL
);
CREATE INDEX ix_chunk_sets_parse_run_id ON chunk_sets(parse_run_id);
CREATE TABLE task_evidence (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    task_id VARCHAR(36) NOT NULL REFERENCES query_tasks(id),
    document_version_id VARCHAR(36) NOT NULL REFERENCES document_versions(id),
    block_id VARCHAR(36) NOT NULL REFERENCES content_blocks(id),
    quote_snapshot TEXT NOT NULL,
    locator JSON NOT NULL,
    review_at_run VARCHAR(20) NOT NULL,
    execution_revision INTEGER DEFAULT '1' NOT NULL
);
CREATE INDEX ix_task_evidence_task_id ON task_evidence(task_id);
CREATE TABLE runtime_journals (
    id VARCHAR(36) NOT NULL PRIMARY KEY,
    run_id VARCHAR(36) NOT NULL REFERENCES agent_runs(id),
    binding_id VARCHAR(36) NOT NULL REFERENCES agent_session_bindings(id),
    generation INTEGER NOT NULL,
    pi_session_id VARCHAR(36) NOT NULL,
    working_key VARCHAR(500) NOT NULL,
    snapshot_file_id VARCHAR(36) NOT NULL REFERENCES file_objects(id),
    content_sha256 VARCHAR(64) NOT NULL,
    version_ids JSON NOT NULL,
    state VARCHAR(30) NOT NULL,
    created_at DATETIME NOT NULL,
    UNIQUE (run_id)
);
CREATE INDEX ix_runtime_journals_binding_id ON runtime_journals(binding_id);
CREATE TABLE audit_probe (value TEXT NOT NULL);
CREATE TRIGGER legacy_job_trigger AFTER UPDATE OF attempt ON parse_service_jobs
BEGIN INSERT INTO audit_probe VALUES ('attempt:' || NEW.attempt); END;
CREATE TRIGGER legacy_run_trigger AFTER UPDATE OF status ON parse_runs
BEGIN
    INSERT INTO audit_probe SELECT pin_id FROM parse_service_jobs WHERE parse_run_id=NEW.id;
END;
INSERT INTO users VALUES ('actor');
INSERT INTO document_versions VALUES ('version');
INSERT INTO file_objects VALUES ('source'), ('legacy-zip');
INSERT INTO service_use_pins VALUES ('legacy-pin');
INSERT INTO query_tasks VALUES ('legacy-task');
INSERT INTO agent_runs VALUES ('legacy-agent');
INSERT INTO agent_session_bindings VALUES ('legacy-binding');
INSERT INTO parse_runs VALUES (
    'legacy-run', 'version', 'provider', 'remote-task-1', 'waiting_external',
    'legacy-output', 'SERVICE_OUTCOME_UNKNOWN', '2026-11-18 12:00:00'
);
INSERT INTO parse_service_jobs VALUES (
    'legacy-run', 'legacy-pin', 'actor', 'source',
    'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    'legacy-zip', 4, '2026-11-18 12:01:00'
);
INSERT INTO content_blocks VALUES ('legacy-block', 'legacy-run', 1, 'paragraph', '历史内容', '{}', '{"file_page":1}', 'unreviewed', NULL, 0);
INSERT INTO chunk_sets VALUES ('legacy-set', 'legacy-run', 'local-keyword-v1', 'ready');
INSERT INTO task_evidence VALUES (
    'legacy-evidence', 'legacy-task', 'version', 'legacy-block', '历史摘录', '{"file_page":1}', 'unreviewed', 3
);
INSERT INTO runtime_journals VALUES (
    'legacy-journal', 'legacy-agent', 'legacy-binding', 2, 'legacy-pi', 'private/legacy', 'legacy-zip',
    'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb', '["version"]', 'active', '2026-11-18 12:03:00'
);
"""


def _fixture_engine(path: Path):
    settings = Settings(database_url=f"sqlite:///{path.as_posix()}", storage_dir=path.parent / path.stem)
    engine = build_engine(settings.database_url)

    @event.listens_for(engine, "connect")
    def enable_foreign_keys(connection, _record):
        connection.execute("PRAGMA foreign_keys=ON")

    return settings, engine


def _table_shape(connection, table):
    inspector = inspect(connection)
    return {
        "columns": {(col["name"], str(col["type"]), col["nullable"], col["default"]) for col in inspector.get_columns(table)},
        "foreign_keys": {
            (tuple(fk["constrained_columns"]), fk["referred_table"], tuple(fk["referred_columns"]))
            for fk in inspector.get_foreign_keys(table)
        },
        "indexes": {(idx["name"], tuple(idx["column_names"]), idx["unique"]) for idx in inspector.get_indexes(table)},
        "unique": {tuple(item["column_names"]) for item in inspector.get_unique_constraints(table)},
        "primary_key": inspector.get_pk_constraint(table)["constrained_columns"],
    }


def _sqlite_exact_child(db):
    db.exec_driver_sql("INSERT INTO chunk_sets (id, parse_run_id, splitter_version, status) VALUES "
                       "('alternate-set','legacy-run','v1','ready')")
    for identifier, level, ordinal in (("child", "child", 1), ("parent", "parent", 2)):
        db.exec_driver_sql("INSERT INTO chunks (id, chunk_set_id, parse_run_id, ordinal, content_hash, level, kind, "
                           "eligibility, required_relation_ids, assembly, support_group_refs) VALUES "
                           "(?, 'legacy-set', 'legacy-run', ?, ?, ?, 'text', 'searchable', '[]', '[]', '[]')",
                           (identifier, ordinal, "c" * 64, level))
    insert = ("INSERT INTO task_evidence (id, task_id, document_version_id, block_id, quote_snapshot, locator, "
              "review_at_run, chunk_id, chunk_set_id) VALUES "
              "(?, 'legacy-task', 'version', 'legacy-block', 'quote', '{}', 'unreviewed', ?, ?)")
    for invalid in (("child", "alternate-set"), ("parent", "legacy-set"), (None, "legacy-set"), ("child", None)):
        with pytest.raises(IntegrityError, match="evidence must reference an exact child"):
            db.exec_driver_sql(insert, ("invalid", *invalid))
    db.exec_driver_sql(insert, ("exact", "child", "legacy-set"))
    with pytest.raises(IntegrityError, match="evidence must reference an exact child"):
        db.exec_driver_sql("UPDATE task_evidence SET chunk_set_id='alternate-set' WHERE id='exact'")
    for mutation in ("UPDATE chunks SET level='parent' WHERE id='child'", "DELETE FROM chunks WHERE id='child'"):
        with pytest.raises(IntegrityError, match="evidence references this child identity"):
            db.exec_driver_sql(mutation)


def test_pdf_migration_preserves_real_0010_shapes_rows_triggers_and_foreign_keys():
    fixtures = Path("outputs/test-fixtures")
    fixtures.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="pdf-migration-", dir=fixtures) as name:
        root = Path(name)
        legacy_path = root / "legacy.sqlite"
        connection = sqlite3.connect(legacy_path)
        try:
            connection.executescript(_LEGACY_SCHEMA)
        finally:
            connection.close()

        legacy_settings, legacy_engine = _fixture_engine(legacy_path)
        fresh_settings, fresh_engine = _fixture_engine(root / "fresh.sqlite")
        try:
            result = apply_local_migrations(legacy_engine, legacy_settings)
            assert (result["status"], result["from_revision"], result["to_revision"]) == (
                "APPLIED", "0010_document_feedback", desired_head(),
            )
            apply_local_migrations(fresh_engine, fresh_settings)
            with legacy_engine.begin() as db, fresh_engine.connect() as fresh:
                for table in ("parse_runs", "parse_artifacts", "parse_service_jobs", "task_evidence", "runtime_journals"):
                    assert _table_shape(db, table) == _table_shape(fresh, table)
                assert db.exec_driver_sql("SELECT quote_snapshot, locator, execution_revision, chunk_id, chunk_set_id, "
                                          "evidence_kind, source_spans, asset_refs, source_binding_hash, source_context "
                                          "FROM task_evidence WHERE id='legacy-evidence'").one() == (
                    "历史摘录", '{"file_page":1}', 3, None, None, "text", "[]", "[]", None, "{}",
                )
                assert db.exec_driver_sql("SELECT generation, snapshot_file_id, version_ids, source_bindings, "
                                          "source_binding_hash, runtime_compatibility, recovery_expires_at "
                                          "FROM runtime_journals WHERE id='legacy-journal'").one() == (
                    2, "legacy-zip", '["version"]', None, None, None, None,
                )

                assert db.exec_driver_sql("SELECT provider_task_id, status, error_code, output_revision, parent_run_id, "
                                          "ir_artifact_id, actual_profile, request_config, raw_contract, processing_stage, "
                                          "stage_checkpoint FROM parse_runs").one() == (
                    "remote-task-1", "waiting_external", "SERVICE_OUTCOME_UNKNOWN", "legacy-output",
                    None, None, "{}", "{}", "{}", "ingestion", "{}",
                )
                assert db.exec_driver_sql("SELECT pin_id, raw_result_file_id, attempt, lease_until, mode, checkpoint, "
                                          "cancel_requested FROM parse_service_jobs").one() == (
                    "legacy-pin", "legacy-zip", 4, "2026-11-18 12:01:00", "selected_service", "{}", 0,
                )
                assert db.exec_driver_sql("SELECT COUNT(*) FROM parse_artifacts").scalar_one() == 0
                assert db.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
                for name in ("parent_run_id", "ir_artifact_id"):
                    with pytest.raises(IntegrityError, match="FOREIGN KEY constraint failed"):
                        db.exec_driver_sql(f"UPDATE parse_runs SET {name}='missing' WHERE id='legacy-run'")

                db.exec_driver_sql("UPDATE parse_service_jobs SET attempt=5 WHERE parse_run_id='legacy-run'")
                db.exec_driver_sql("UPDATE parse_runs SET status='running' WHERE id='legacy-run'")
                assert db.exec_driver_sql("SELECT value FROM audit_probe ORDER BY rowid").scalars().all() == [
                    "attempt:5", "legacy-pin",
                ]
                db.exec_driver_sql("INSERT INTO parse_runs (id, document_version_id, origin, status, output_revision, "
                                   "created_at, parent_run_id) VALUES "
                                   "('local-run','version','local','pending','v1','2026-11-19 00:00:00','legacy-run')")
                db.exec_driver_sql("INSERT INTO parse_service_jobs (parse_run_id, mode, actor_id, source_file_id, "
                                   "source_sha256, attempt) VALUES ('local-run','local','actor','source','x',0)")
                assert db.exec_driver_sql("SELECT pin_id FROM parse_service_jobs WHERE parse_run_id='local-run'").scalar_one() is None
                with pytest.raises(IntegrityError, match="UNIQUE constraint failed"):
                    db.exec_driver_sql("UPDATE parse_service_jobs SET pin_id='legacy-pin' WHERE parse_run_id='local-run'")
                _sqlite_exact_child(db)
                assert db.exec_driver_sql("PRAGMA foreign_key_check").all() == []
        finally:
            legacy_engine.dispose()
            fresh_engine.dispose()


@pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="Requires an explicit isolated local PostgreSQL URL")
def test_postgres_exact_evidence_requires_child_and_its_actual_set(pdf_pg):
    seed = _seed_parse(pdf_pg)
    with Session(pdf_pg.engine, expire_on_commit=False) as db:
        task = QueryTask(owner_user_id=seed.actor_id, idempotency_key="synthetic-exact-evidence")
        block = ContentBlock(parse_run_id=seed.id, ordinal=1, text_content="SYNTHETIC text")
        first = ChunkSet(parse_run_id=seed.id)
        second = ChunkSet(parse_run_id=seed.id)
        db.add_all([task, block, first, second])
        db.flush()
        child = Chunk(parse_run_id=seed.id, chunk_set_id=first.id, ordinal=1, level="child",
                      kind="text", eligibility="searchable", content_hash="a" * 64)
        parent = Chunk(parse_run_id=seed.id, chunk_set_id=first.id, ordinal=2, level="parent",
                       kind="container", eligibility="context", content_hash="b" * 64)
        db.add_all([child, parent])
        db.flush()

        def evidence(chunk, chunk_set):
            return TaskEvidence(task_id=task.id, block_id=block.id, document_version_id=seed.version_id,
                quote_snapshot="SYNTHETIC quote", chunk_id=chunk, chunk_set_id=chunk_set)

        legacy = evidence(None, None)
        exact = evidence(child.id, first.id)
        db.add_all([legacy, exact])
        db.flush()

        def rejected(operation):
            with pytest.raises(IntegrityError) as caught:
                with db.begin_nested():
                    operation()
                    db.flush()
            return caught.value.orig

        mismatch = rejected(lambda: db.add(evidence(child.id, second.id)))
        assert mismatch.sqlstate == "23503" and mismatch.diag.constraint_name == "fk_evidence_exact_chunk"
        incomplete = rejected(lambda: db.add(evidence(None, first.id)))
        assert incomplete.sqlstate == "23514" and incomplete.diag.constraint_name == "ck_evidence_chunk_identity_complete"
        assert rejected(lambda: setattr(exact, "chunk_id", parent.id)).sqlstate == "23514"
        assert rejected(lambda: setattr(child, "level", "parent")).sqlstate == "23514"
        db.commit()
