"""PostgreSQL authorization changes at document-maintenance commit boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from hashlib import sha256
import json
import os
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, select, text
from sqlalchemy.orm import Session

from . import governance
from .models import ApiIdempotencyRecord, AuditEvent, Document, DocumentVersion, User
from .security import hash_password
from .test_creation_recovery import app_module
from .test_delivery_recovery import _wait_for_lock
from .test_management_concurrency import management_pg, pytestmark


@pytest.mark.parametrize("action", ["patch", "source_metadata"])
@pytest.mark.parametrize("first", ["committed", "disable", "maintenance"])
def test_account_disable_orders_with_maintenance_commit(management_pg, monkeypatch, action, first):
    engine, config, fixture, report_dir = management_pg
    manager_id, document_id, version_id = (fixture[k] for k in ("actor_id", "document_id", "version_id"))
    with Session(engine, expire_on_commit=False) as db:
        version = db.get(DocumentVersion, version_id)
        initial = (db.get(Document, document_id).revision, version.revision,
                   version.revision_label, dict(version.source_metadata))
        admin = User(username="synthetic-maintenance-admin", display_name="Synthetic maintenance admin",
            status="active", roles=["system_admin"], credential_state="active",
            credential_hash=hash_password("synthetic maintenance admin password"))
        db.add(admin)
        revision = db.get(User, manager_id).authz_revision
        db.commit()

    path = f"/documents/{document_id}/versions/{version_id}"
    body = {"revision_label": "SYNTHETIC maintenance writer"}
    actions = ("version.updated",)
    if action == "source_metadata":
        path += "/source-metadata"
        body = {"source_description": "SYNTHETIC maintenance writer"}
        actions = ("version.source_metadata_changed", "governance.document_versions")
    writer_key, disable_key = str(uuid4()), str(uuid4())
    reached, disable_ready, release = Event(), Event(), Event()
    pids, update_order = {}, []
    writer_app, admin_app = (app_module.create_app(config) for _ in range(2))

    @event.listens_for(admin_app.state.engine, "before_cursor_execute")
    def observe_disable_updates(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("UPDATE users SET"):
            update_order.append("users")
        if statement.startswith("UPDATE auth_sessions SET revoked_at"):
            update_order.append("auth_sessions")

    def wait_at_writer_check(db):
        if first != "maintenance":
            pids["maintenance"] = db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            assert disable_ready.wait(20)

    if action == "patch":
        claim = app_module._claim_managed_document

        def pause(db, actor, document):
            claim(db, actor, document)
            wait_at_writer_check(db)

        monkeypatch.setattr(app_module, "_claim_managed_document", pause)
    else:
        change = governance._changed

        def pause(db, actor, version, name, **kwargs):
            if name == "version.source_metadata_changed":
                wait_at_writer_check(db)
            return change(db, actor, version, name, **kwargs)

        monkeypatch.setattr(governance, "_changed", pause)

    @event.listens_for(writer_app.state.session_factory, "before_commit")
    def maybe_pause_writer(db):
        if first == "maintenance" and db.scalar(select(ApiIdempotencyRecord.id).where(
            ApiIdempotencyRecord.idempotency_key == writer_key, ApiIdempotencyRecord.status == "completed"
        )):
            pids["maintenance"] = db.scalar(text("SELECT pg_backend_pid()"))
            reached.set()
            assert release.wait(20)

    @event.listens_for(admin_app.state.session_factory, "before_commit")
    def maybe_pause_admin(db):
        if first == "disable" and db.scalar(select(ApiIdempotencyRecord.id).where(
            ApiIdempotencyRecord.idempotency_key == disable_key, ApiIdempotencyRecord.status == "completed"
        )):
            pids["disable"] = db.scalar(text("SELECT pg_backend_pid()"))
            disable_ready.set()
            assert release.wait(20)

    lock = None
    try:
        with ExitStack() as stack, ThreadPoolExecutor(max_workers=2) as pool:
            writer = stack.enter_context(TestClient(writer_app))
            admin_client = stack.enter_context(TestClient(admin_app))
            for client, name, password in (
                (writer, fixture["username"], "SYNTHETIC management race password"),
                (admin_client, admin.username, "synthetic maintenance admin password"),
            ):
                login = client.post("/auth/login", json={"username": name, "password": password})
                assert login.status_code == 200
                client.headers["X-CSRF-Token"] = login.json()["csrf_token"]

            maintenance = pool.submit(writer.patch, path, json=body, headers={
                "If-Match": f'"{initial[1]}"', "Idempotency-Key": writer_key})
            try:
                assert reached.wait(20)
                disabled = pool.submit(admin_client.patch, f"/admin/users/{manager_id}",
                    json={"status": "disabled"}, headers={"If-Match": f'"{revision}"', "Idempotency-Key": disable_key})
                if first == "committed":
                    assert disabled.result(20).status_code == 200
                    with Session(engine) as db:
                        assert db.get(User, manager_id).status == "disabled"
                    disable_ready.set()
                elif first == "disable":
                    assert disable_ready.wait(20)
                    lock = _wait_for_lock(engine, pids["disable"], maintenance)
                else:
                    lock = _wait_for_lock(engine, pids["maintenance"], disabled)
            finally:
                release.set()
                disable_ready.set()
            result, disabled_result = maintenance.result(20), disabled.result(20)
    finally:
        writer_app.state.engine.dispose()
        admin_app.state.engine.dispose()

    with Session(engine) as db:
        version = db.get(DocumentVersion, version_id)
        current = (db.get(Document, document_id).revision, version.revision,
                   version.revision_label, dict(version.source_metadata))
        receipts = db.scalars(select(ApiIdempotencyRecord.status).where(
            ApiIdempotencyRecord.idempotency_key == writer_key)).all()
        audits = db.scalars(select(AuditEvent.action).where(
            AuditEvent.resource_id == version_id, AuditEvent.action.in_(actions))).all()
    files = [Path(__file__)] + [Path("src/server") / file for file in
        ("app.py", "governance_api.py", "services.py", "document_lifecycle.py")]
    (report_dir / f"{action}-{first}.json").write_text(json.dumps({
        "action": action, "first": first, "writer_status": result.status_code,
        "disable_status": disabled_result.status_code, "disable_update_order": update_order,
        "blocking_pid": pids.get(first), "wait": lock, "initial": initial,
        "current": current, "writer_receipts": receipts, "writer_audits": sorted(audits),
        "source_sha256": {file.name: sha256(file.read_bytes()).hexdigest() for file in files},
    }, indent=2), encoding="utf-8")
    assert disabled_result.status_code == 200
    if first == "maintenance":
        assert result.status_code == 200
        assert current[1] == initial[1] + 1 and receipts == ["completed"] and sorted(audits) == sorted(actions)
    else:
        assert result.status_code in {401, 403}
        assert current == initial and receipts == [] and audits == []
    if first != "committed":
        assert lock and lock["wait_event_type"] == "Lock" and lock["pid"] != pids[first]
