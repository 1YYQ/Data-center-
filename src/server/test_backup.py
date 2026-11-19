"""Complete application recovery, including live authorization and effect fences."""

from datetime import timedelta
from hashlib import sha256
import os
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from . import backup, operations, runtime_gate, test_runtime_gate
from .config import Settings
from .db import build_engine, init_db
from .execution_models import QueryAnswerSnapshot, RuntimeJournal, ToolInvocation
from .migration_safety import MigrationRefused
from .models import AuthorizationGrant, Document, FileObject, QueryTask, TaskEvidence, utc_now
from .parsing.artifacts import archive_bytes
from .runtime_models import ModelCallAdmission, TaskBudget
from .service_use_models import ServiceSelection
from .test_runtime_gate import local_upstream


@pytest.fixture(params=["sqlite", "postgresql"])
def backup_source(request, tmp_path, local_upstream, monkeypatch):
    root = tmp_path.resolve()
    storage = root / "pilot-storage"
    storage.mkdir()
    created_databases = []
    if request.param == "postgresql":
        configured = os.getenv("DATACENTER_TEST_POSTGRES_URL")
        if not configured:
            pytest.skip("An explicit loopback PostgreSQL test endpoint is required")
        import psycopg
        from psycopg import sql
        base = make_url(configured)
        assert base.drivername == "postgresql+psycopg" and base.host == "127.0.0.1" and base.port
        assert base.database.startswith("dc_local_pgvector_")
        admin = base.set(drivername="postgresql", database="postgres").render_as_string(hide_password=False)

        def new_database(prefix):
            name = prefix + uuid4().hex[:20]
            with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
                conn.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0").format(sql.Identifier(name)))
            created_databases.append(name)
            return base.set(database=name).render_as_string(hide_password=False)

        source_url = new_database("dc_local_pgvector_backup_")

        def target_url():
            return new_database("dc_local_restore_")
    else:
        source_url = backup._sqlite_url(root / "source.sqlite")
        def target_url():
            return None

    engine = build_engine(source_url)
    try:
        config = Settings(database_url=source_url, storage_dir=storage)
        init_db(engine, config)
        monkeypatch.setattr(test_runtime_gate, "create_engine", lambda *_args, **_kwargs: engine)
        import pymupdf
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((40, 40), "SYNTHETIC: Check isolation before startup.")
            original = pdf.tobytes()

        def file_with_bytes(**kwargs):
            key = kwargs["object_key"]
            path = storage / key
            path.parent.mkdir(parents=True)
            path.write_bytes(original)
            return FileObject(**{**kwargs, "sha256": sha256(original).hexdigest(), "byte_size": len(original)})

        monkeypatch.setattr(test_runtime_gate, "FileObject", file_with_bytes)
        fixture = test_runtime_gate.runtime_db.__wrapped__(tmp_path, local_upstream)
        f = next(fixture)
        try:
            yield f, config, target_url
        finally:
            fixture.close()
    finally:
        engine.dispose()
        for name in reversed(created_databases):
            # Only this fixture's randomly named synthetic databases are eligible.
            assert name != base.database and name.startswith(("dc_local_pgvector_backup_", "dc_local_restore_"))
            with psycopg.connect(admin, autocommit=True, connect_timeout=5) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _saved_application_contents(f, config):
    db = f.db
    artifact = archive_bytes(db, f.block.parse_run_id, f.actor.id, config, b"SYNTHETIC PARSE ARTIFACT",
        kind="raw_result", logical_name="SYNTHETIC.json")
    saved = QueryTask(owner_user_id=f.actor.id, status="succeeded", kind="query",
        accepted_scope={"operation": "knowledge.query"}, idempotency_key="backed-up-answer")
    db.add(saved)
    db.flush()
    evidence = TaskEvidence(task_id=saved.id, document_version_id=f.version.id, block_id=f.block.id,
        quote_snapshot=f.block.text_content, locator=f.block.locator, review_at_run="approved")
    receipt = ToolInvocation(actor_user_id=f.actor.id, task_id=saved.id, tool_name="dc_artifacts_validate",
        logical_action_key="synthetic-backup-validation", args_hash="a" * 64, execution_revision=1,
        state="completed", output={"ready": True})
    db.add_all([evidence, receipt])
    db.flush()
    snapshot = QueryAnswerSnapshot(task_id=saved.id, execution_revision=1, validation_ref=receipt.id,
        result={"answer": "Check isolation before startup.", "result_status": "answered"}, evidence_ids=[evidence.id])
    working_key = f"runtime/{f.run.session_binding_id}/journal/synthetic.jsonl"
    snapshot_key = f"internal/runtime-journals/{f.run.id}.jsonl"
    pi_id = str(uuid4())
    journal_body = b'{"type":"session","version":3,"id":"' + pi_id.encode() + b'"}\n'
    for key in (working_key, snapshot_key):
        path = config.storage_dir / key
        path.parent.mkdir(parents=True)
        path.write_bytes(journal_body)
    file = FileObject(object_key=snapshot_key, created_by=f.actor.id, status="available", mime_type="application/x-ndjson",
        sha256=sha256(journal_body).hexdigest(), byte_size=len(journal_body))
    selection = ServiceSelection(purpose="query.answer", revision=1, epoch=1, enabled=True)
    db.add_all([snapshot, file, selection])
    db.flush()
    journal = RuntimeJournal(run_id=f.run.id, binding_id=f.run.session_binding_id, generation=1, pi_session_id=pi_id,
        working_key=working_key, snapshot_file_id=file.id, content_sha256=file.sha256,
        version_ids=[f.version.id], state="active", recovery_expires_at=utc_now() + timedelta(hours=1))
    db.add(journal)
    db.commit()
    return saved, snapshot, artifact, journal, selection


def test_full_application_backup_restore_and_stale_authority_refusal(backup_source, tmp_path):
    f, config, target_factory = backup_source
    saved, snapshot, artifact, journal, selection = _saved_application_contents(f, config)
    _, received, response_payload = test_runtime_gate._admit(f)
    assert runtime_gate.dispatch_model(f.db, f.run.id, f.token, received.id, response_payload)["state"] == "received"
    assert f.db.get(ModelCallAdmission, received.id, populate_existing=True).response_body
    f.db.rollback()
    root = tmp_path.resolve()
    source = (config.database_url, config.storage_dir)
    backup_root = root / "packages"

    def restore(receipt, destination, target_url=None, *, denied=False):
        return backup.restore_backup(Path(receipt["package"]), receipt["manifest_sha256"], *source,
            root / destination, target_url=target_url, task_checks=((f.actor.id, saved.id, denied),), timeout=90)

    captured = backup.create_backup(*source, backup_root, timeout=90)
    target = target_factory()
    positive = restore(captured, "restore-readable", target)
    assert positive["checks"] == [{"actor_id": f.actor.id, "task_id": saved.id, "state": "READABLE", "snapshot_id": snapshot.id,
        "evidence_count": 1, "answer_sha256": sha256(backup._json(snapshot.result["answer"])).hexdigest()}]
    tables = positive["state"]["tables"]
    assert all(tables[table] > 0 for table in ("file_objects", "parse_artifacts", "task_evidence", "answer_snapshots",
        "runtime_journals", "model_call_admissions", "task_budgets"))
    assert (root / "restore-readable" / "private" / journal.working_key).is_file()
    assert artifact.file_id
    sealed_url = target or backup._sqlite_url(root / "restore-readable" / backup.RESTORE_SQLITE_NAME)
    with pytest.raises(MigrationRefused):
        with build_engine(sealed_url).connect():
            pytest.fail("Ordinary application connection must refuse an isolated restore")
    if target is None:
        alias = root / "renamed-restore.sqlite"
        os.link(root / "restore-readable" / backup.RESTORE_SQLITE_NAME, alias)
        with pytest.raises(MigrationRefused, match="sealed"):
            with build_engine(backup._sqlite_url(alias)).connect():
                pytest.fail("A SQLite hard-link alias must still refuse ordinary application connections")

    # Unknown external result, revoked access, logical deletion, and service
    # disable all happen after the verified recovery point.
    f.upstream.mode = "drop"
    _, unknown, pending_payload = test_runtime_gate._admit(f, request_key="after-backup-effect")
    assert runtime_gate.dispatch_model(f.db, f.run.id, f.token, unknown.id, pending_payload)["state"] == "unknown"
    grant = f.db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.actor.id,
        AuthorizationGrant.operation == "knowledge.query"))
    grant.revoked_at = utc_now()
    doc = f.db.get(Document, f.version.document_id)
    operations.logical_delete_document(f.db, doc, f.actor.id, "SYNTHETIC recovery drill")
    selection.enabled, selection.revision, selection.epoch = False, 2, 2
    f.db.commit()
    target = target_factory()
    with pytest.raises(backup.BackupRefused, match="differs from the backup cutoff"):
        restore(captured, "restore-stale", target)
    assert not (root / "restore-stale").exists()

    current = backup.create_backup(*source, backup_root, timeout=90)
    current_result = restore(current, "restore-current", target, denied=True)
    assert current_result["checks"][0]["state"] == "DENIED_AS_EXPECTED"
    assert current_result["execution"]["model_call_states"] == {"received": 1, "unknown": 1}
    current_url = target or backup._sqlite_url(root / "restore-current" / backup.RESTORE_SQLITE_NAME)
    with backup._connection(current_url, backup.Deadline(20), sealed=True) as inspection:
        model_state = inspection.execute(select(ModelCallAdmission.state, ModelCallAdmission.usage_state)
            .where(ModelCallAdmission.id == unknown.id)).one()
        reserved = inspection.execute(select(TaskBudget.reserved_calls, TaskBudget.reserved_tokens)
            .where(TaskBudget.task_id == f.task.id)).one()
        disabled = inspection.execute(select(ServiceSelection.enabled, ServiceSelection.epoch)).one()
        assert model_state == ("unknown", "unknown")
        assert reserved == (2, 2 * f.profile.configuration["tokens_per_call"])
        assert disabled == (False, 2)
        assert inspection.scalar(select(Document.deleted_at).where(Document.id == doc.id)) is not None
    assert len(f.upstream.requests) == 2  # The restore and both authority checks never dispatched.

    with pytest.raises(backup.BackupRefused, match="pinned identity"):
        backup.restore_backup(Path(current["package"]), "0" * 64, *source, root / "restore-wrong-manifest")
    tampered = Path(current["package"]) / "private" / f.file.object_key
    tampered.write_bytes(b"SYNTHETIC damaged backup object")
    damaged_target = target_factory()
    with pytest.raises(backup.BackupRefused, match="no longer matches"):
        restore(current, "restore-tampered", damaged_target, denied=True)
    assert not (root / "restore-tampered" / "validation.json").exists()
    if damaged_target:
        backup._empty_postgres(damaged_target, backup.target_identity(config.database_url), backup.Deadline(20))
