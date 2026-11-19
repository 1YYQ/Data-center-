"""Selected business parsing over real loopback HTTP and isolated databases."""
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import os
import subprocess
import sys
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from . import parse_jobs
from .app import create_app
from .config import Settings
from .db import init_db
from .models import ChunkSet, ContentBlock, DocumentVersion, FileObject, ParseRun, User, utc_now
from .security import hash_password
from .service_admission import LocalEndpoint, digest, synthetic_pdf
from .service_use_models import ServiceUseCall, ServiceUsePin
from .services import file_path, save_upload, seed_reference_data
from .synthetic_provider import BATCH_PATH, DOWNLOAD_PATH, POLL_PATH, UPLOAD_PATH, _Handler, run_synthetic_provider
from .test_creation_recovery import recovery_pg
from .test_governance import synthetic_draft
from .test_service_admission import _change, _configure, _expect, _headers, _preview, _test_run
from .testing_service_use import attest_synthetic_input


_ENV = "DATACENTER_SYNTHETIC_PARSE_BUSINESS_KEY"
_SECRET = "synthetic-business-parser"
_PASSWORD = "SYNTHETIC testing parse job"
_MODE = {"mode": "selected_service"}
_SELECTION = "/admin/service-selections/document.parse"
_PDF = synthetic_pdf()
_PG = pytest.param("postgres", marks=pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
    reason="Requires explicit isolated local PostgreSQL"))


def _login(client, username):
    login = _expect(client.post("/auth/login", json={"username": username, "password": _PASSWORD}), 200)
    client.headers["X-CSRF-Token"] = login["csrf_token"]


def _select_service(client, revision_id, etag='"0"', action="select"):
    reason = "SYNTHETIC parse business use"
    payload = {"action": action, "reason": reason}
    if revision_id is not None:
        payload["revision_id"] = revision_id
    preview = _expect(client.post(_SELECTION + "/previews", json=payload, headers=_headers(etag)), 200)
    return _expect(client.post(_SELECTION + "/" + action,
        json={"preview_id": preview["id"], "reason": reason}, headers=_headers(etag)), 200)


def _enable_profile(client):
    profile, revision, revision_id = _configure(client, "parsing")
    tested, _ = _test_run(client, revision)
    assert tested["state"] == "succeeded"
    preview = _expect(_preview(client, profile, "activate", revision_id=revision_id), 200)
    assert _expect(_change(client, profile, "activate", preview), 200)["enabled"]
    return revision_id


@pytest.fixture(params=("sqlite", _PG))
def business_parse(request, tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV, _SECRET)
    if request.param == "postgres":
        _, config, _, _ = request.getfixturevalue("recovery_pg")
    else:
        config = Settings(database_url="sqlite:///" + (tmp_path / "parse-business.sqlite").as_posix(),
                          storage_dir=tmp_path / "storage")
    app = create_app(config)
    try:
        if request.param == "sqlite":
            init_db(app.state.engine, config)
        with app.state.session_factory() as db:
            seed_reference_data(db)
            manager, _, document, version, _, _ = synthetic_draft(db)
            manager.credential_state, manager.credential_hash = "active", hash_password(_PASSWORD)
            admin = User(username="synthetic-admin-" + uuid4().hex, display_name="SYNTHETIC configuration administrator",
                status="active", credential_state="active", credential_hash=hash_password(_PASSWORD), roles=["system_admin"])
            file = save_upload([_PDF], "synthetic.pdf", "application/pdf", manager, config)
            db.add_all([admin, file])
            db.flush()
            version.source_file_id = file.id
            db.commit()
            fixture = SimpleNamespace(app=app, config=config, actor_id=manager.id, version_id=version.id,
                document_id=document.id, file_id=file.id, manager=manager.username, admin=admin.username,
                path=f"/documents/{document.id}/versions/{version.id}/parse-runs")
        with run_synthetic_provider(credential=_SECRET) as provider, TestClient(app) as manager_client, TestClient(app) as admin_client:
            app.state.service_endpoints = {"synthetic": LocalEndpoint(provider.port, _ENV)}
            _login(manager_client, fixture.manager)
            _login(admin_client, fixture.admin)
            revision_id = _enable_profile(admin_client)
            _select_service(admin_client, revision_id)
            provider.requests.clear()
            fixture.client, fixture.admin_client, fixture.provider = manager_client, admin_client, provider
            fixture.revision_id = revision_id
            yield fixture
    finally:
        app.state.engine.dispose()


def _attest(fixture, *, version_id=None, payload=_PDF):
    with fixture.app.state.session_factory() as db:
        attest_synthetic_input(db, fixture.actor_id, parse_jobs.PURPOSE,
                               "version:" + (version_id or fixture.version_id), payload)
        db.commit()


def _accept(fixture, headers=None):
    return _expect(fixture.client.post(fixture.path, json=_MODE, headers=headers or _headers()), 202)


def _advance(fixture, run_id):
    with fixture.app.state.session_factory() as db:
        run = parse_jobs.run_parse_job(db, run_id, fixture.config, fixture.app.state.service_endpoints)
        return run.status, run.error_code, run.provider_task_id


def _calls(fixture, run_id):
    with fixture.app.state.session_factory() as db:
        job = db.get(parse_jobs.ParseServiceJob, run_id)
        return db.scalars(select(ServiceUseCall).where(ServiceUseCall.pin_id == job.pin_id)
                          .order_by(ServiceUseCall.ordinal)).all()


def test_selected_parse_accepts_one_business_run_and_emits_unreviewed_source_blocks(business_parse):
    f = business_parse
    _attest(f)
    headers = _headers()
    receipt = _accept(f, headers)
    run_id = receipt["parse_run_id"]
    assert receipt["status"] == "pending" and _accept(f, headers) == receipt
    assert f.client.post(f.path, headers=headers, json={"mode": "local"}).status_code == 409
    assert f.admin_client.post(f.path, json=_MODE, headers=_headers()).status_code == 403
    assert f.provider.requests == []
    with f.app.state.session_factory() as db:
        job = db.get(parse_jobs.ParseServiceJob, run_id)
        pin = db.get(ServiceUsePin, job.pin_id)
        assert (job.source_file_id, job.source_sha256, pin.resource_key, pin.revision_id) == (
            f.file_id, digest(_PDF), "version:" + f.version_id, f.revision_id)
        assert db.get(ParseRun, run_id).document_version_id == f.version_id

    latest = _enable_profile(f.admin_client)
    _select_service(f.admin_client, latest, etag='"1"')
    f.provider.requests.clear()
    status, error, batch_id = _advance(f, run_id)
    assert (status, error, batch_id) == ("needs_review", None, "synthetic-" + run_id)
    assert [(r.method, r.target.partition("?")[0], r.authorization_present) for r in f.provider.requests] == [
        ("POST", BATCH_PATH, True), ("PUT", UPLOAD_PATH + batch_id, False),
        ("GET", POLL_PATH + batch_id, True), ("GET", DOWNLOAD_PATH + batch_id, False),
    ]
    assert f.provider.requests[1].body == _PDF
    calls = _calls(f, run_id)
    assert [r.state for r in calls] == ["received"] * 4
    assert all(r.input_hash == digest(_PDF) and r.settled_cost is None for r in calls)
    assert _accept(f, headers)["status"] == "needs_review"
    with f.app.state.session_factory() as db:
        job = db.get(parse_jobs.ParseServiceJob, run_id)
        assert db.get(ServiceUsePin, job.pin_id).revision_id == f.revision_id
        version = db.get(DocumentVersion, f.version_id)
        assert (version.publish_status, version.selected_chunk_set_id) == ("draft", None)
        artifact = db.get(FileObject, job.raw_result_file_id)
        path = file_path(artifact, f.config)
        assert artifact.status == "available" and path.is_relative_to(f.config.storage_dir.resolve() / "internal")
        assert calls[-1].response_hash == artifact.sha256 == digest(path.read_bytes())
        with ZipFile(path) as archive:
            entries = json.loads(archive.read("synthetic/synthetic_content_list.json"))
            assert [block["page_idx"] for block in entries] == [0, 0, 0]
    blocks = _expect(f.client.get(f"/parse-runs/{run_id}/blocks"), 200)["items"]
    chunks = _expect(f.client.get(f"/parse-runs/{run_id}/chunk-sets"), 200)["items"]
    assert [block["kind"] for block in blocks] == ["paragraph", "table", "image"]
    assert "Disconnect power before maintenance." in blocks[0]["text"]
    assert all(block["review_status"] == "unreviewed" and block["locator"]["file_page"] == 1 for block in blocks)
    assert len(chunks) == 1 and chunks[0]["status"] == "ready"


def test_other_resource_attestation_and_changed_original_cannot_send(business_parse):
    f = business_parse
    _attest(f, version_id=str(uuid4()))
    denied = _expect(f.client.post(f.path, json=_MODE, headers=_headers()), 409)
    assert denied["code"] == "SERVICE_SYNTHETIC_INPUT_REQUIRED"
    _attest(f)
    run_id = _accept(f)["parse_run_id"]
    with f.app.state.session_factory() as db:
        original = file_path(db.get(FileObject, f.file_id), f.config)
    original.write_bytes(_PDF.replace(b"DEMO-1", b"DEMO-2"))
    assert _advance(f, run_id) == ("failed", "PARSE_SOURCE_CHANGED", None)
    assert _calls(f, run_id) == [] and f.provider.requests == []


def test_unknown_post_keeps_its_receipt_and_never_submits_again(business_parse):
    f = business_parse
    _attest(f)
    headers = _headers()
    run_id = _accept(f, headers)["parse_run_id"]
    f.provider.mode = "disconnect"
    observed = ("reconciliation_required", "SERVICE_OUTCOME_UNKNOWN", None)
    assert _advance(f, run_id) == observed
    f.provider.mode = "normal"
    assert _accept(f, headers) == {"parse_run_id": run_id, "status": observed[0]}
    assert _accept(f) == {"parse_run_id": run_id, "status": observed[0]}  # Fresh key, same live source.
    assert _advance(f, run_id) == observed
    assert [(r.method, r.target) for r in f.provider.requests] == [("POST", BATCH_PATH)]
    assert [(r.method, r.state) for r in _calls(f, run_id)] == [("POST", "unknown")]


def test_concurrent_accepts_for_same_source_share_one_unresolved_run(business_parse):
    f = business_parse
    _attest(f)
    start = Barrier(2, timeout=10)

    def accept(client):
        start.wait()
        return _expect(client.post(f.path, json=_MODE, headers=_headers()), 202)

    with TestClient(f.app) as second, ThreadPoolExecutor(max_workers=2) as pool:
        _login(second, f.manager)
        first = pool.submit(accept, f.client)
        another = pool.submit(accept, second)
        assert first.result(timeout=15) == another.result(timeout=15)
    with f.app.state.session_factory() as db:
        assert len(db.scalars(select(parse_jobs.ParseServiceJob)).all()) == 1
    assert f.provider.requests == []


def test_process_exit_preserves_batch_before_upload_and_recovers_by_get_only(business_parse, monkeypatch):
    f = business_parse
    _attest(f)
    run_id = _accept(f)["parse_run_id"]
    batch_id, observed_at_put = "synthetic-" + run_id, []
    original_transfer = _Handler._transfer

    def observe_upload(handler, path, body):
        if handler.command == "PUT":
            with f.app.state.session_factory() as db:
                observed_at_put.append(db.get(ParseRun, run_id).provider_task_id)
        original_transfer(handler, path, body)

    monkeypatch.setattr(_Handler, "_transfer", observe_upload)
    child = subprocess.run([sys.executable, "-c", """
import os
from src.server.parse_worker import main
from src.server.parsing.mineru import MinerUAdapter
def exit_before_poll(*args):
    os._exit(83)
MinerUAdapter.poll = exit_before_poll
main()
""", "--run-id", run_id], env={**os.environ,
        "DATACENTER_DATABASE_URL": f.config.database_url,
        "DATACENTER_STORAGE_DIR": str(f.config.storage_dir.resolve()),
        "DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS": json.dumps({"synthetic": {
            "port": f.provider.port, "credential_env": _ENV}}),
    }, capture_output=True, timeout=30)
    assert child.returncode == 83, child.stderr.decode(errors="replace")
    assert observed_at_put == [batch_id]
    assert [r.method for r in f.provider.requests] == ["POST", "PUT"]
    assert _advance(f, run_id) == ("running", None, batch_id)  # Live lease fences a second worker.
    monkeypatch.setattr(parse_jobs, "utc_now", lambda: utc_now() + timedelta(seconds=121))
    assert _advance(f, run_id) == ("needs_review", None, batch_id)
    assert [r.method for r in f.provider.requests] == ["POST", "PUT", "GET", "GET"]


def test_selection_revocation_during_poll_withholds_the_result(business_parse, monkeypatch):
    f = business_parse
    _attest(f)
    run_id = _accept(f)["parse_run_id"]
    original_poll = _Handler._poll

    def revoke_before_poll_reply(handler, batch_id):
        _select_service(f.admin_client, None, etag='"1"', action="disable")
        original_poll(handler, batch_id)

    monkeypatch.setattr(_Handler, "_poll", revoke_before_poll_reply)
    assert _advance(f, run_id) == ("failed", "SERVICE_USE_REVOKED", "synthetic-" + run_id)
    assert [r.method for r in f.provider.requests] == ["POST", "PUT", "GET"]
    with f.app.state.session_factory() as db:
        assert db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id)).all() == []
        assert db.scalars(select(ChunkSet).where(ChunkSet.parse_run_id == run_id)).all() == []


def test_invalid_download_is_retained_privately_without_blocks(business_parse, monkeypatch):
    f = business_parse
    _attest(f)
    run_id = _accept(f)["parse_run_id"]
    invalid = b"SYNTHETIC invalid zip result"
    original_transfer = _Handler._transfer

    def send_invalid_archive(handler, path, body):
        if path.startswith(DOWNLOAD_PATH):
            handler._reply(200, invalid, content_type="application/zip")
        else:
            original_transfer(handler, path, body)

    monkeypatch.setattr(_Handler, "_transfer", send_invalid_archive)
    assert _advance(f, run_id) == ("failed", "MINERU_INVALID_ARCHIVE", "synthetic-" + run_id)
    with f.app.state.session_factory() as db:
        job = db.get(parse_jobs.ParseServiceJob, run_id)
        artifact = db.get(FileObject, job.raw_result_file_id)
        assert artifact.status == "quarantined" and artifact.sha256 == digest(invalid)
        assert file_path(artifact, f.config).read_bytes() == invalid
        assert db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id)).all() == []
        assert db.scalars(select(ChunkSet).where(ChunkSet.parse_run_id == run_id)).all() == []


def test_worker_refuses_to_initialize_an_unmigrated_database(tmp_path):
    database = tmp_path / "not-initialized.sqlite"
    child = subprocess.run([sys.executable, "-m", "src.server.parse_worker", "--run-id", str(uuid4())],
        env={**os.environ, "DATACENTER_DATABASE_URL": "sqlite:///" + database.as_posix(),
             "DATACENTER_STORAGE_DIR": str(tmp_path / "storage")}, capture_output=True, timeout=20)
    assert child.returncode == 1 and child.stderr.strip() == b"PARSE_DATABASE_NOT_READY"
    assert not database.exists()
