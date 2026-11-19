"""Opt-in management races over real isolated PostgreSQL and two HTTP sessions.

No sleeps: distinct-key tests meet after receipt acquisition, before the parent
Document fence; the same-key test synchronizes only HTTP request entry.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import datetime, timezone
import inspect as python_inspect
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from . import services
from .app import create_app
from .config import Settings
from .db import build_engine, init_db
from .models import DocumentVersion
from .security import hash_password
from .test_governance import synthetic_draft

pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
    reason="Management concurrency requires an explicit local PostgreSQL URL")
OUTPUT = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/local-browser")) / "management-concurrency"


@pytest.fixture
def management_pg():
    import psycopg
    from psycopg import sql
    base = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    assert base.drivername == "postgresql+psycopg" and base.host == "127.0.0.1" and base.port
    assert base.database.startswith("dc_local_pgvector_") and not base.query
    name = "dc_local_pgvector_mgmt_race_" + uuid4().hex[:24]
    admin = base.set(database="postgres").render_as_string(hide_password=False).replace(
        "postgresql+psycopg://", "postgresql://", 1)
    config = Settings(database_url=base.set(database=name).render_as_string(hide_password=False),
                      storage_dir=OUTPUT / name / "storage")
    report_dir = OUTPUT / name
    report_dir.mkdir(parents=True, exist_ok=False)
    lifecycle = {"database": name, "created": False, "dropped": False}
    engine = None
    try:
        with psycopg.connect(admin, autocommit=True) as connection:
            assert not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        lifecycle["created"] = True
        engine = build_engine(config.database_url)
        init_db(engine, config, migrate=True)
        with Session(engine, expire_on_commit=False) as db:
            actor, _, document, version, _, _ = synthetic_draft(db)
            actor.credential_state = "active"
            actor.credential_hash = hash_password("SYNTHETIC management race password")
            db.commit()
            fixture = {"actor_id": actor.id, "username": actor.username, "document_id": document.id,
                       "version_id": version.id, "revision": version.revision,
                       "reference_revision": version.reference_access_revision}
        yield engine, config, fixture, report_dir
    finally:
        if engine is not None:
            engine.dispose()
        if lifecycle["created"]:
            assert name.startswith("dc_local_pgvector_mgmt_race_") and name != base.database
            with psycopg.connect(admin, autocommit=True) as connection:
                connection.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
                assert not connection.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
            lifecycle["dropped"] = True
        (report_dir / "lifecycle.json").write_text(json.dumps(lifecycle, indent=2), encoding="utf-8")


def _two_requests(management_pg, monkeypatch, *, action, method, payloads, same_key=False):
    engine, config, fixture, report_dir = management_pg
    endpoint = f'/documents/{fixture["document_id"]}/versions/{fixture["version_id"]}/{action}'
    barrier = threading.Barrier(2, timeout=20)
    mutex = threading.Lock()
    conditions, arrivals = [], []
    original_check, original_idempotency = services.check_if_match, services.idempotency_record

    def conditional_boundary(value, expected_revision, *, required=True):
        original_check(value, expected_revision, required=required)
        frame = python_inspect.currentframe().f_back
        request = frame.f_locals.get("request")
        if request is not None and request.url.path == endpoint:
            db = frame.f_locals["db"]
            pid = db.scalar(text("SELECT pg_backend_pid()"))
            with mutex:
                conditions.append({"pid": pid, "revision": expected_revision, "if_match": value})

    def observe_idempotency(db, actor, request, key, body):
        if request.url.path == endpoint:
            pid = db.scalar(text("SELECT pg_backend_pid()"))
            with mutex:
                arrivals.append({"pid": pid, "key": key})
        result = original_idempotency(db, actor, request, key, body)
        if request.url.path == endpoint and not same_key:
            # Both requests entered with the original ETag. The Doc fence now
            # deliberately serializes them before the production If-Match test;
            # a barrier after that fence would force a test-only lock cycle.
            barrier.wait()
        return result

    monkeypatch.setattr(services, "check_if_match", conditional_boundary)
    monkeypatch.setattr(services, "idempotency_record", observe_idempotency)
    apps = []
    try:
        with ExitStack() as stack:
            apps = [create_app(config), create_app(config)]
            clients = [stack.enter_context(TestClient(app)) for app in apps]
            csrf = []
            for client in clients:
                login = client.post("/auth/login", json={"username": fixture["username"],
                    "password": "SYNTHETIC management race password"})
                assert login.status_code == 200
                csrf.append(login.json()["csrf_token"])
            keys = [uuid4().hex, uuid4().hex]
            if same_key:
                keys[1] = keys[0]
            def send(index):
                if same_key:
                    barrier.wait()  # Outside HTTP and unique-key acquisition.
                response = clients[index].request(method, endpoint, json=payloads[index], headers={
                    "X-CSRF-Token": csrf[index], "If-Match": f'"{fixture["revision"]}"',
                    "Idempotency-Key": keys[index]})
                return {"writer": index, "status": response.status_code, "body": response.json()}
            with ThreadPoolExecutor(max_workers=2) as pool:
                responses = list(pool.map(send, (0, 1)))
            replay = clients[0].request(method, endpoint, json=payloads[0], headers={
                "X-CSRF-Token": csrf[0], "If-Match": f'"{fixture["revision"]}"',
                "Idempotency-Key": keys[0]}) if same_key else None
    finally:
        for app in apps:
            app.state.engine.dispose()
    with Session(engine) as db:
        version = db.get(DocumentVersion, fixture["version_id"])
        current = {"revision": version.revision, "metadata": dict(version.source_metadata),
                   "reference_state": version.reference_access_state,
                   "reference_revision": version.reference_access_revision}
        receipts = [dict(row._mapping) for row in db.execute(text(
            "SELECT id,idempotency_key,status,response_status,resource_refs FROM api_idempotency_records "
            "WHERE request_path=:p ORDER BY id"), {"p": endpoint})]
        outbox = [dict(row._mapping) for row in db.execute(text(
            "SELECT id,event_kind,revision FROM governance_outbox WHERE resource_id=:v ORDER BY id"), {"v": fixture["version_id"]})]
        audits = [dict(row._mapping) for row in db.execute(text(
            "SELECT id,action FROM audit_events WHERE resource_id=:v ORDER BY id"), {"v": fixture["version_id"]})]
        session_count = db.scalar(text("SELECT count(*) FROM auth_sessions WHERE user_id=:u"), {"u": fixture["actor_id"]})
    report = {"observed_at_utc": datetime.now(timezone.utc).isoformat(), "action": action,
              "before_revision": fixture["revision"], "responses": responses, "conditions": conditions,
              "arrivals": arrivals, "current": current, "receipts": receipts, "outbox": outbox,
              "audits": audits, "auth_sessions": session_count,
              "replay": {"status": replay.status_code, "body": replay.json()} if replay is not None else None}
    (report_dir / (action + ("-same-key" if same_key else "-different-key") + ".json")).write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    assert session_count == 2
    initial_arrivals = arrivals[:2]
    assert len(initial_arrivals) == 2 and len({item["pid"] for item in initial_arrivals}) == 2
    return report


def _one_business_change(report, initial_revision, event):
    assert report["current"]["revision"] == initial_revision + 1
    assert len(report["outbox"]) == 1 and report["outbox"][0]["event_kind"] == event
    assert report["outbox"][0]["revision"] == initial_revision + 1
    assert sorted(item["action"] for item in report["audits"]) == ["governance.document_versions", event]
    assert len(report["receipts"]) == 1
    assert report["receipts"][0]["status"] == "completed" and report["receipts"][0]["response_status"] == 200


def test_source_metadata_stale_if_match_has_one_winner(management_pg, monkeypatch):
    payloads = [{"channel": "SYNTHETIC", "source_description": "SYNTHETIC writer " + label} for label in ("A", "B")]
    report = _two_requests(management_pg, monkeypatch, action="source-metadata", method="PATCH", payloads=payloads)
    assert sorted(item["status"] for item in report["responses"]) == [200, 412]
    winner = next(item["writer"] for item in report["responses"] if item["status"] == 200)
    assert report["current"]["metadata"] == payloads[winner]
    _one_business_change(report, management_pg[2]["revision"], "version.source_metadata_changed")


def test_same_key_same_input_replays_one_management_receipt(management_pg, monkeypatch):
    payload = {"channel": "SYNTHETIC", "source_description": "SYNTHETIC one logical action"}
    report = _two_requests(management_pg, monkeypatch, action="source-metadata", method="PATCH",
                           payloads=[payload, payload], same_key=True)
    assert [item["status"] for item in report["responses"]] == [200, 200]
    assert report["responses"][0]["body"] == report["responses"][1]["body"] == report["replay"]["body"]
    assert report["replay"]["status"] == 200
    _one_business_change(report, management_pg[2]["revision"], "version.source_metadata_changed")


def test_reference_access_stale_if_match_has_one_winner(management_pg, monkeypatch):
    payloads = [{"state": state, "reason": "SYNTHETIC concurrent reference decision"} for state in ("open", "closed")]
    report = _two_requests(management_pg, monkeypatch, action="reference-access", method="PUT", payloads=payloads)
    assert sorted(item["status"] for item in report["responses"]) == [200, 412]
    winner = next(item["writer"] for item in report["responses"] if item["status"] == 200)
    assert report["current"]["reference_state"] == payloads[winner]["state"]
    assert report["current"]["reference_revision"] == management_pg[2]["reference_revision"] + 1
    _one_business_change(report, management_pg[2]["revision"], "version.reference_access_changed")
