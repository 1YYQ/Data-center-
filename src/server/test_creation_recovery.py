"""Opt-in synthetic HTTP races and real process death on isolated PostgreSQL.

Hooks establish precise windows, but never replace file writes, transactions,
idempotency, authorization or CAS. No provider calls or production DB writes.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager, ExitStack
import hashlib
import importlib
import inspect
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from . import document_lifecycle, services
from .config import Settings
from .db import build_engine, init_db
from .governance_models import GovernanceOutbox
from .models import ApiIdempotencyRecord, AuthorizationGrant, Document, DocumentVersion, FileObject, KnowledgeBase
from .security import hash_password
from .test_governance import synthetic_draft

app_module = importlib.import_module(f"{__package__}.app")
pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
    reason="Requires an explicit isolated local PostgreSQL URL")
OUTPUT = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/local-browser")) / "creation-recovery"
PASSWORD = "SYNTHETIC creation recovery password"
PDF = b"%PDF-1.4\n% SYNTHETIC recovery fixture\n%%EOF\n"
METADATA = {"title": "SYNTHETIC recovered manual", "category": "manual", "model_scope": ["SYNTHETIC-1"],
            "revision_label": "SYNTHETIC R1", "source_metadata": {"channel": "synthetic", "source_description": "SYNTHETIC test only"}}


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.fixture
def recovery_pg(request):
    import psycopg
    from psycopg import sql
    base = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    assert base.drivername == "postgresql+psycopg" and base.host == "127.0.0.1" and base.port == 55440
    assert base.database.startswith("dc_local_") and not base.query
    name = "dc_local_creation_recovery_" + uuid4().hex[:20]
    report = OUTPUT / name
    report.mkdir(parents=True, exist_ok=False)
    config = Settings(database_url=base.set(database=name).render_as_string(hide_password=False), storage_dir=(report / "storage").resolve())
    admin = base.set(database="postgres").render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://", 1)
    protected = Path("outputs/data-center.db")
    lifecycle = {"database": name, "test": request.node.name, "created": False, "dropped": False,
                 "default_database_before": _hash(protected), "source_sha256": {str(p): _hash(p) for p in
                    [Path(__file__), Path("src/server/app.py"), Path("src/server/services.py"), Path("src/server/document_lifecycle.py"), Path("src/server/operations.py")]}}
    engine = None
    try:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        lifecycle["created"] = True
        engine = build_engine(config.database_url)
        init_db(engine, config, migrate=True)
        with Session(engine, expire_on_commit=False) as db:
            actor, kb, doc, version, _, _ = synthetic_draft(db)
            actor.credential_state = "active"
            actor.credential_hash = hash_password(PASSWORD)
            db.commit()
            fixture = {"username": actor.username, "actor_id": actor.id, "kb_id": kb.id, "doc_id": doc.id,
                       "version_id": version.id, "revision": doc.revision,
                       "kb_revisions": [kb.content_revision, kb.acl_revision]}
        yield engine, config, fixture, report
    finally:
        if engine is not None:
            engine.dispose()
        if lifecycle["created"]:
            assert name.startswith("dc_local_creation_recovery_") and name != base.database
            with psycopg.connect(admin, autocommit=True) as conn:
                conn.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
                assert not conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            lifecycle["dropped"] = True
        lifecycle["default_database_after"] = _hash(protected)
        (report / "lifecycle.json").write_text(json.dumps(lifecycle, indent=2), encoding="utf-8")
        assert lifecycle["default_database_before"] == lifecycle["default_database_after"]


@contextmanager
def _clients(config, fixture, count=2):
    apps = []
    try:
        with ExitStack() as stack:
            clients, headers = [], []
            for _ in range(count):
                app = app_module.create_app(config)
                apps.append(app)
                client = stack.enter_context(TestClient(app))
                response = client.post("/auth/login", json={"username": fixture["username"], "password": PASSWORD})
                assert response.status_code == 200, response.text
                clients.append(client)
                headers.append({"X-CSRF-Token": response.json()["csrf_token"]})
            yield clients, headers, apps
    finally:
        for app in apps:
            app.state.engine.dispose()


def _upload(client, endpoint, headers, key, *, new_document=False):
    return client.post(endpoint, headers={**headers, "Idempotency-Key": key},
        files={"file": ("synthetic.pdf", PDF, "application/pdf")},
        data={"metadata": json.dumps(METADATA)} if new_document else None)


def _receipt_count(db, endpoint):
    return db.scalar(select(func.count()).select_from(ApiIdempotencyRecord).where(ApiIdempotencyRecord.request_path == endpoint))


def _write(report, name, data):
    (report / (name + ".json")).write_text(json.dumps(data, indent=2), encoding="utf-8")


@pytest.mark.parametrize("same_key", [False, True])
def test_kb_name_race_has_one_business_creation_without_grants(recovery_pg, monkeypatch, same_key):
    engine, config, f, report = recovery_pg
    endpoint, name = "/admin/knowledge-bases", "SYNTHETIC contested " + uuid4().hex
    barrier = threading.Barrier(2, timeout=20)
    pids = []
    original = app_module.idempotency_record
    def observe(db, actor, request, key, body):
        if request.url.path == endpoint:
            pids.append(db.scalar(text("SELECT pg_backend_pid()")))
        return original(db, actor, request, key, body)
    monkeypatch.setattr(app_module, "idempotency_record", observe)
    with _clients(config, f) as (clients, headers, apps):
        if not same_key:
            def before_insert(conn, cursor, statement, parameters, context, many):
                if statement.startswith("INSERT INTO knowledge_bases"):
                    barrier.wait()
            for app in apps:
                event.listen(app.state.engine, "before_cursor_execute", before_insert)
        keys = [uuid4().hex, uuid4().hex]
        if same_key:
            keys[1] = keys[0]
        def send(i):
            if same_key:
                barrier.wait()
            return clients[i].post(endpoint, json={"name": name}, headers={**headers[i], "Idempotency-Key": keys[i]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(send, [0, 1]))
        if not same_key:
            for app in apps:
                event.remove(app.state.engine, "before_cursor_execute", before_insert)
        replay = clients[0].post(endpoint, json={"name": name}, headers={**headers[0], "Idempotency-Key": keys[0]}) if same_key else None
    with Session(engine) as db:
        kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
        assert db.scalar(select(func.count()).select_from(KnowledgeBase).where(KnowledgeBase.name == name)) == 1
        assert db.scalar(select(func.count()).select_from(AuthorizationGrant).where(AuthorizationGrant.knowledge_base_id == kb.id)) == 0
        assert _receipt_count(db, endpoint) == 1
    _write(report, "kb-race", {"statuses": [r.status_code for r in responses], "pids": pids,
        "same_key": same_key, "knowledge_base_id": kb.id})
    assert len(set(pids[:2])) == 2
    assert sorted(r.status_code for r in responses) == ([201, 201] if same_key else [201, 409])
    if same_key:
        assert responses[0].json() == responses[1].json() == replay.json() and replay.status_code == 201


def test_concurrent_version_uploads_allocate_distinct_numbers(recovery_pg, monkeypatch):
    engine, config, f, report = recovery_pg
    endpoint = f'/documents/{f["doc_id"]}/versions'
    barrier, pids = threading.Barrier(2, timeout=20), []
    original = app_module.idempotency_record
    def observe(db, actor, request, key, body):
        result = original(db, actor, request, key, body)
        if request.url.path == endpoint:
            pids.append(db.scalar(text("SELECT pg_backend_pid()")))
            barrier.wait()
        return result
    monkeypatch.setattr(app_module, "idempotency_record", observe)
    with _clients(config, f) as (clients, headers, _):
        keys = [uuid4().hex, uuid4().hex]
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda i: _upload(clients[i], endpoint, headers[i], keys[i]), [0, 1]))
    with Session(engine) as db:
        numbers = db.scalars(select(DocumentVersion.version_no).where(DocumentVersion.document_id == f["doc_id"]).order_by(DocumentVersion.version_no)).all()
        assert numbers == [1, 2, 3] and _receipt_count(db, endpoint) == 2
        assert db.scalar(select(func.count()).select_from(FileObject)) == 3
    _write(report, "version-race", {"statuses": [r.status_code for r in responses], "pids": pids, "version_numbers": numbers})
    assert len(set(pids)) == 2 and [r.status_code for r in responses] == [201, 201]


@pytest.mark.parametrize("same_key", [False, True])
def test_delete_cas_and_replay_update_outbox_and_both_kb_revisions_once(recovery_pg, monkeypatch, same_key):
    engine, config, f, report = recovery_pg
    endpoint = f'/documents/{f["doc_id"]}'
    barrier, pids = threading.Barrier(2, timeout=20), []
    original_check, original_record = document_lifecycle.check_if_match, document_lifecycle.idempotency_record
    def checked(value, revision, **kwargs):
        original_check(value, revision, **kwargs)
        barrier.wait()
    def observe(db, actor, request, key, body):
        pids.append(db.scalar(text("SELECT pg_backend_pid()")))
        return original_record(db, actor, request, key, body)
    monkeypatch.setattr(document_lifecycle, "idempotency_record", observe)
    if not same_key:
        monkeypatch.setattr(document_lifecycle, "check_if_match", checked)
    with _clients(config, f) as (clients, headers, _):
        keys = [uuid4().hex, uuid4().hex]
        if same_key:
            keys[1] = keys[0]
        def send(i):
            if same_key:
                barrier.wait()
            return clients[i].delete(endpoint, headers={**headers[i], "If-Match": f'"{f["revision"]}"', "Idempotency-Key": keys[i]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(send, [0, 1]))
        replay = clients[0].delete(endpoint, headers={**headers[0], "If-Match": f'"{f["revision"]}"', "Idempotency-Key": keys[0]}) if same_key else None
    with Session(engine) as db:
        doc, kb = db.get(Document, f["doc_id"]), db.get(KnowledgeBase, f["kb_id"])
        assert doc.deleted_at and doc.revision == f["revision"] + 1
        assert [kb.content_revision, kb.acl_revision] == [n + 1 for n in f["kb_revisions"]]
        assert db.scalar(select(func.count()).select_from(GovernanceOutbox).where(GovernanceOutbox.resource_id == doc.id)) == 1
        assert _receipt_count(db, endpoint) == 1
        assert db.get(FileObject, db.get(DocumentVersion, f["version_id"]).source_file_id).status == "available"
    _write(report, "delete-race", {"statuses": [r.status_code for r in responses], "pids": pids,
        "same_key": same_key, "kb_revisions": [kb.content_revision, kb.acl_revision]})
    assert len(set(pids[:2])) == 2
    assert sorted(r.status_code for r in responses) == ([204, 204] if same_key else [204, 412])
    if same_key:
        assert replay.status_code == 204


def test_delete_lock_prevents_waiting_upload_after_tombstone(recovery_pg, monkeypatch):
    """Delete has the Doc CAS lock; a pre-authorized upload waits, then loses."""
    from . import operations
    engine, config, f, report = recovery_pg
    with Session(engine) as db:
        source = db.get(FileObject, db.get(DocumentVersion, f["version_id"]).source_file_id)
        original_path = config.storage_dir / source.object_key
        original_path.parent.mkdir(parents=True, exist_ok=True)
        original_path.write_bytes(PDF)
        source.sha256, source.byte_size = hashlib.sha256(PDF).hexdigest(), len(PDF)
        db.commit()
    entered, release, upload_at_cas = threading.Event(), threading.Event(), threading.Event()
    pids = {}
    original = operations.logical_delete_document
    def hold(db, document, actor_id, reason):
        pids["delete"] = db.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        assert release.wait(20)
        return original(db, document, actor_id, reason)
    monkeypatch.setattr(operations, "logical_delete_document", hold)
    with _clients(config, f) as (clients, headers, apps):
        original_url = f'/documents/{f["doc_id"]}/versions/{f["version_id"]}/file'
        before = clients[0].get(original_url)
        assert before.status_code == 200 and before.content == PDF
        def before_upload_cas(conn, cursor, statement, parameters, context, many):
            if statement.startswith("UPDATE documents SET revision=documents.revision"):
                pids["upload"] = conn.scalar(text("SELECT pg_backend_pid()"))
                upload_at_cas.set()
        event.listen(apps[1].state.engine, "before_cursor_execute", before_upload_cas)
        with ThreadPoolExecutor(max_workers=2) as pool:
            deleting = pool.submit(clients[0].delete, f'/documents/{f["doc_id"]}', headers={**headers[0], "If-Match": f'"{f["revision"]}"', "Idempotency-Key": uuid4().hex})
            try:
                assert entered.wait(20)
                uploading = pool.submit(_upload, clients[1], f'/documents/{f["doc_id"]}/versions', headers[1], uuid4().hex)
                assert upload_at_cas.wait(20)
                deadline = time.monotonic() + 10
                locked = False
                while time.monotonic() < deadline:
                    with engine.connect() as conn:
                        locked = conn.scalar(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids["upload"]})
                    if locked:
                        break
                    threading.Event().wait(.02)
                assert locked and not uploading.done()
            finally:
                release.set()
            deleted, uploaded = deleting.result(20), uploading.result(20)
        assert deleted.status_code == 204 and uploaded.status_code == 404
        assert clients[0].get(f'/documents/{f["doc_id"]}').status_code == 404
        assert clients[0].get(f'/documents/{f["doc_id"]}/versions').status_code == 404
        assert clients[0].get(original_url).status_code == 404
    with Session(engine) as db:
        assert db.scalar(select(func.count()).select_from(DocumentVersion).where(DocumentVersion.document_id == f["doc_id"])) == 1
        assert db.scalar(select(func.count()).select_from(FileObject)) == 1
        assert db.get(Document, f["doc_id"]).deleted_at is not None
        assert db.get(FileObject, db.get(DocumentVersion, f["version_id"]).source_file_id).status == "available"
    assert original_path.read_bytes() == PDF
    _write(report, "delete-upload-lock", {"pids": pids, "observed_wait_event_type": "Lock", "delete_status": deleted.status_code,
        "upload_status": uploaded.status_code, "original_status_before_after": [200, 404], "original_bytes_retained": True,
        "window": "delete CAS acquired; preauthorized upload blocks on Doc UPDATE until tombstone commit"})
    assert pids["delete"] != pids["upload"]


def _crash_child():
    """Called only in a separate Python process; credentials stay in its env."""
    config_data = json.loads(os.environ["DC_RECOVERY_CHILD"])
    config = Settings(database_url=config_data["url"], storage_dir=Path(config_data["storage"]))
    original = app_module.save_upload
    def installed(*args, **kwargs):
        result = original(*args, **kwargs)
        caller = inspect.currentframe().f_back
        db = caller.f_locals["db"]
        print(json.dumps({"object_key": result.object_key, "pid": os.getpid(),
                          "pg_pid": db.scalar(text("SELECT pg_backend_pid()"))}), flush=True)
        threading.Event().wait(120)
        raise RuntimeError("Parent failed to kill controlled crash child")
    app_module.save_upload = installed
    with _clients(config, config_data, count=1) as (clients, headers, _):
        _upload(clients[0], config_data["endpoint"], headers[0], config_data["key"], new_document=config_data["new_document"])


@pytest.mark.parametrize("new_document", [True, False])
def test_process_death_after_file_install_before_commit_reuses_exact_blob(recovery_pg, new_document):
    engine, config, f, report = recovery_pg
    endpoint = f'/knowledge-bases/{f["kb_id"]}/documents' if new_document else f'/documents/{f["doc_id"]}/versions'
    key = uuid4().hex
    env = dict(os.environ)
    env["DC_RECOVERY_CHILD"] = json.dumps({"url": config.database_url, "storage": str(config.storage_dir),
        "username": f["username"], "endpoint": endpoint, "key": key, "new_document": new_document})
    command = [sys.executable, "-c", "from src.server.test_creation_recovery import _crash_child; _crash_child()"]
    child = subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    ready = queue.Queue()
    threading.Thread(target=lambda: ready.put(child.stdout.readline()), daemon=True).start()
    try:
        line = ready.get(timeout=40)
        assert line, "Child exited without installed-file handshake"
        installed = json.loads(line)
        assert installed["pid"] == child.pid and child.poll() is None
        path = config.storage_dir / installed["object_key"]
        assert path.read_bytes() == PDF
        with Session(engine) as db:
            assert _receipt_count(db, endpoint) == 0
            assert db.scalar(select(func.count()).select_from(Document)) == 1
            assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 1
            assert db.scalar(select(func.count()).select_from(FileObject)) == 1
    finally:
        if child.poll() is None:
            child.kill()
        stdout, stderr = child.communicate(timeout=20)
        (report / "child-stderr.txt").write_text(stderr, encoding="utf-8")
    assert child.returncode != 0
    with _clients(config, f, count=1) as (clients, headers, _):
        response = _upload(clients[0], endpoint, headers[0], key, new_document=new_document)
        replay = _upload(clients[0], endpoint, headers[0], key, new_document=new_document)
        assert response.status_code == replay.status_code == 201, response.text
        assert response.json() == replay.json()
    with Session(engine) as db:
        assert _receipt_count(db, endpoint) == 1
        assert db.scalar(select(func.count()).select_from(Document)) == (2 if new_document else 1)
        assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 2
        assert db.scalar(select(func.count()).select_from(FileObject)) == 2
        files = db.scalars(select(FileObject).where(FileObject.object_key == installed["object_key"])).all()
        assert len(files) == 1 and files[0].status == "available"
    blobs = list((config.storage_dir / "documents").glob("*.pdf"))
    assert blobs == [path] and path.read_bytes() == PDF and not list(config.storage_dir.glob(".upload-*.tmp"))
    _write(report, "child-crash", {"command": command, "child_pid": child.pid, "child_returncode": child.returncode,
        "installed": installed, "retry_status": response.status_code, "replay_status": replay.status_code,
        "physical_blobs": [str(p.relative_to(config.storage_dir)) for p in blobs],
        "window": "save_upload returned installed fsynced file; no business flush/commit yet",
        "not_covered": ["before file install", "after business commit before HTTP response", "power loss or multi-host storage", "physical purge"]})


def test_conflicting_stable_target_is_rejected_without_overwrite(recovery_pg):
    from .security import request_hash
    engine, config, f, report = recovery_pg
    endpoint, key = f'/knowledge-bases/{f["kb_id"]}/documents', uuid4().hex
    normalized = json.dumps({"kb_id": f["kb_id"], "metadata": METADATA, "filename": "synthetic.pdf",
        "sha256": hashlib.sha256(PDF).hexdigest(), "byte_size": len(PDF)}, ensure_ascii=False, sort_keys=True).encode()
    digest = request_hash("POST", endpoint, normalized + b"\x00if-match:")
    identity = hashlib.sha256(f'{f["actor_id"]}\0{key}\0{digest}'.encode()).hexdigest()
    path = config.storage_dir / "documents" / f'{hashlib.sha256(PDF).hexdigest()}-{identity}.pdf'
    path.parent.mkdir(parents=True, exist_ok=True)
    conflicting = b"SYNTHETIC existing unrelated object; preserve these bytes"
    path.write_bytes(conflicting)
    with _clients(config, f, count=1) as (clients, headers, _):
        response = _upload(clients[0], endpoint, headers[0], key, new_document=True)
    _write(report, "conflicting-target", {"status": response.status_code, "body": response.json(), "object_key": str(path.relative_to(config.storage_dir))})
    assert response.status_code == 409 and response.json()["code"] == "UPLOAD_RECONCILIATION_REQUIRED"
    assert path.read_bytes() == conflicting and not list(config.storage_dir.glob(".upload-*.tmp"))
    with Session(engine) as db:
        assert _receipt_count(db, endpoint) == 0
        assert db.scalar(select(func.count()).select_from(Document)) == 1
        assert db.scalar(select(func.count()).select_from(DocumentVersion)) == 1
        assert db.scalar(select(func.count()).select_from(FileObject)) == 1


def _qualified_draft_preview(engine, fixture):
    """Use real synthetic review/confirmation services, stopping before publish."""
    from . import governance as g
    from .models import ChunkSet, ContentBlock, ParseRun, User
    from .testing_governance import seed_confirmation
    with Session(engine, expire_on_commit=False) as db:
        actor, document = db.get(User, fixture["actor_id"]), db.get(Document, fixture["doc_id"])
        version = db.get(DocumentVersion, fixture["version_id"])
        run = db.scalar(select(ParseRun).where(ParseRun.document_version_id == version.id))
        chunk = db.scalar(select(ChunkSet).where(ChunkSet.parse_run_id == run.id))
        blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)).all()
        for block in blocks:
            g.review_content_block(db, actor, run, block, "approved", "SYNTHETIC controlled review")
        binding = g.source_binding(db, version)
        seed_confirmation(db, actor, version, "applicability", "applicable", binding)
        seed_confirmation(db, actor, version, "classification", "classified", {**binding,
            "access_level": document.access_level, "category": document.category, "need_class": document.need_class,
            "rule_version": "synthetic-rule-v1"})
        professional = seed_confirmation(db, actor, version, "key_content", "accepted", {**binding,
            "parse_run_id": run.id, "block_bindings": {b.id: g.block_binding(b) for b in blocks}})
        g.create_review_set(db, actor, version, {"parse_run_id": run.id, "chunk_set_id": chunk.id,
            "reviewed_block_refs": [{"id": b.id, "revision": b.revision, "content_hash": g.block_binding(b)} for b in blocks],
            "professional_confirmation_refs": [professional.id], "reason": "SYNTHETIC complete review"})
        preview = g.create_publication_preview(db, actor, document, version, {"action": "publish"})
        db.commit()
        assert preview.allowed and not preview.blocked_reasons and version.publish_status == "draft"
        fixture["revision"] = document.revision
        return preview.id


def test_delete_lock_fences_preauthorized_http_publication(recovery_pg, monkeypatch):
    """The publish request reaches its Doc lock after HTTP authorization passes."""
    from . import operations
    from .governance_models import PublicationEvent, PublicationPreview
    engine, config, f, report = recovery_pg
    preview_id = _qualified_draft_preview(engine, f)
    entered, release, publication_at_lock = threading.Event(), threading.Event(), threading.Event()
    pids = {}
    original = operations.logical_delete_document
    def hold_delete(db, document, actor_id, reason):
        pids["delete"] = db.scalar(text("SELECT pg_backend_pid()"))
        entered.set()
        assert release.wait(20)
        return original(db, document, actor_id, reason)
    monkeypatch.setattr(operations, "logical_delete_document", hold_delete)
    endpoint = f'/documents/{f["doc_id"]}/versions/{f["version_id"]}/publish'
    with _clients(config, f) as (clients, headers, apps):
        def before_publication_lock(conn, cursor, statement, parameters, context, many):
            if ("FROM documents" in statement and "FOR UPDATE" in statement) or ("UPDATE documents" in statement and "deleted_at IS NULL" in statement):
                pids["publish"] = conn.scalar(text("SELECT pg_backend_pid()"))
                publication_at_lock.set()
        event.listen(apps[1].state.engine, "before_cursor_execute", before_publication_lock)
        with ThreadPoolExecutor(max_workers=2) as pool:
            deleting = pool.submit(clients[0].delete, f'/documents/{f["doc_id"]}', headers={**headers[0],
                "If-Match": f'"{f["revision"]}"', "Idempotency-Key": uuid4().hex})
            try:
                assert entered.wait(20)
                publishing = pool.submit(clients[1].post, endpoint, json={"preview_id": preview_id}, headers={**headers[1],
                    "If-Match": f'"{f["revision"]}"', "Idempotency-Key": uuid4().hex})
                assert publication_at_lock.wait(20)
                locked, deadline = False, time.monotonic() + 10
                while time.monotonic() < deadline:
                    with engine.connect() as conn:
                        locked = conn.scalar(text("SELECT wait_event_type='Lock' FROM pg_stat_activity WHERE pid=:pid"), {"pid": pids["publish"]})
                    if locked:
                        break
                    threading.Event().wait(.02)
                assert locked and not publishing.done()
            finally:
                release.set()
            deleted, published = deleting.result(20), publishing.result(20)
    with Session(engine) as db:
        version, preview = db.get(DocumentVersion, f["version_id"]), db.get(PublicationPreview, preview_id)
        count = db.scalar(select(func.count()).select_from(PublicationEvent).where(PublicationEvent.document_version_id == version.id))
        state = {"publish_status": version.publish_status, "preview_consumed": preview.consumed_at is not None,
                 "publication_events": count, "publication_receipts": _receipt_count(db, endpoint),
                 "delete_receipts": _receipt_count(db, f'/documents/{f["doc_id"]}'),
                 "deleted": db.get(Document, f["doc_id"]).deleted_at is not None}
    _write(report, "delete-publication-lock", {"pids": pids, "wait_event_type": "Lock", "delete_status": deleted.status_code,
        "publish_status": published.status_code, "publish_body": published.json(), "final": state,
        "window": "DELETE holds Doc CAS; authorized publish blocks at earliest active-Doc fence until tombstone commits"})
    assert deleted.status_code == 204 and published.status_code in (403, 404, 409, 412)
    assert pids["delete"] != pids["publish"]
    assert state == {"publish_status": "draft", "preview_consumed": False, "publication_events": 0,
        "publication_receipts": 0, "delete_receipts": 1, "deleted": True}


def test_tombstone_fences_inflight_hold_after_manage_passes(recovery_pg, monkeypatch):
    """No hold/event/receipt may commit after the tombstone won this window."""
    from .governance_models import DocumentUseHold
    engine, config, f, report = recovery_pg
    managed, release = threading.Event(), threading.Event()
    pids = {}
    endpoint = f'/documents/{f["doc_id"]}/versions/{f["version_id"]}/holds'
    original, original_delete = services.idempotency_record, document_lifecycle.idempotency_record
    def pre_fence_boundary(db, actor, request, key, body):
        result = original(db, actor, request, key, body)
        caller = inspect.currentframe().f_back
        if request.url.path == endpoint and caller.f_code.co_name == "mutation":
            # The adapter objects() already passed g.manage. Stop before its
            # shared Doc fence, where DELETE can still win without lock cycles.
            # The original red trace stopped after create_hold's final manage;
            # that later point is protected by the new transaction-held fence.
            pids["hold"] = db.scalar(text("SELECT pg_backend_pid()"))
            managed.set()
            assert release.wait(20)
        return result
    def observe_delete(db, actor, request, key, body):
        pids["delete"] = db.scalar(text("SELECT pg_backend_pid()"))
        return original_delete(db, actor, request, key, body)
    monkeypatch.setattr(services, "idempotency_record", pre_fence_boundary)
    monkeypatch.setattr(document_lifecycle, "idempotency_record", observe_delete)
    with _clients(config, f) as (clients, headers, _):
        with ThreadPoolExecutor(max_workers=1) as pool:
            creating = pool.submit(clients[1].post, endpoint, json={"kind": "quality", "reason": "SYNTHETIC in-flight hold"},
                headers={**headers[1], "If-Match": '"0"', "Idempotency-Key": uuid4().hex})
            try:
                assert managed.wait(20)
                with engine.connect() as conn:
                    pids["observer"] = conn.scalar(text("SELECT pg_backend_pid()"))
                deleted = clients[0].delete(f'/documents/{f["doc_id"]}', headers={**headers[0],
                    "If-Match": f'"{f["revision"]}"', "Idempotency-Key": uuid4().hex})
                assert deleted.status_code == 204
                with Session(engine) as db:
                    assert db.get(Document, f["doc_id"]).deleted_at is not None
            finally:
                release.set()
            created = creating.result(20)
    with Session(engine) as db:
        state = {"holds": db.scalar(select(func.count()).select_from(DocumentUseHold).where(DocumentUseHold.document_version_id == f["version_id"])),
            "held_outbox": db.scalar(select(func.count()).select_from(GovernanceOutbox).where(
                GovernanceOutbox.resource_id == f["version_id"], GovernanceOutbox.event_kind == "version.held")),
            "hold_receipts": _receipt_count(db, endpoint), "version_revision": db.get(DocumentVersion, f["version_id"]).revision,
            "deleted": db.get(Document, f["doc_id"]).deleted_at is not None}
    _write(report, "delete-inflight-hold", {"pids": pids, "delete_status": deleted.status_code,
        "hold_status": created.status_code, "hold_body": created.json(), "final": state,
        "window": "HTTP objects.g.manage passed and receipt inserted; pause before shared Doc fence; DELETE commits; resume fence/hold",
        "original_red_window": "run4-governance-red.xml stopped after create_hold.manage; new shared fence protects that later point"})
    assert pids["hold"] != pids["delete"]
    assert created.status_code == 404, "In-flight hold must reject when the tombstone wins before its Doc fence"
    assert state == {"holds": 0, "held_outbox": 0, "hold_receipts": 0, "version_revision": 0, "deleted": True}
