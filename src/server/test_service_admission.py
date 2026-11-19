"""The admin self-test workflow against real HTTP on loopback and an isolated SQLite database."""

from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from . import service_admission
from .app import create_app
from .config import Settings
from .db import init_db
from .models import User, utc_now
from .security import hash_password
from .service_admission import LocalEndpoint
from .service_models import ServiceRequestCall, ServiceTestRun
from .services import seed_reference_data
from .synthetic_provider import (
    BATCH_PATH, CHAT_PATH, DOWNLOAD_PATH, EMBEDDING_PATH, POLL_PATH, RESULT_MARKER,
    TRANSFER_TICKET, UPLOAD_PATH, run_synthetic_provider,
)


_BASE = "/admin/service-profiles"
_ENV_REF = "DATACENTER_SYNTHETIC_TEST_KEY"
_SECRET = "SYNTHETIC-http-fixture-secret"
_FIXTURE = {"fixture_id": "service-synthetic-v1"}
_REASON = "SYNTHETIC service admission integration"
_REVISIONS = {
    "generation": {"endpoint_ref": "synthetic", "adapter": "openai-chat-v1", "model": "synthetic-chat"},
    "embedding": {"endpoint_ref": "synthetic", "adapter": "bge-m3-v1", "model": "BAAI/bge-m3"},
    "parsing": {"endpoint_ref": "synthetic", "adapter": "mineru-v4-legacy-v1", "model": "vlm"},
}


@pytest.fixture
def local_admission(tmp_path, monkeypatch):
    monkeypatch.setenv(_ENV_REF, _SECRET)
    monkeypatch.delenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", raising=False)
    config = Settings(
        database_url="sqlite:///" + (tmp_path / "admission.sqlite").as_posix(),
        storage_dir=tmp_path / "storage", bootstrap_token="synthetic-bootstrap", secure_cookie=False,
    )
    app = create_app(config)
    try:
        init_db(app.state.engine, config, migrate=True)
        with app.state.session_factory() as db:
            seed_reference_data(db)
        with run_synthetic_provider(credential=_SECRET) as provider, TestClient(app) as client:
            app.state.service_endpoints = {"synthetic": LocalEndpoint(
                provider.port, _ENV_REF,
                allowed_models=("synthetic-chat", "another-synthetic-chat", "BAAI/bge-m3", "Pro/BAAI/bge-m3", "vlm"),
            )}
            boot = client.post("/auth/bootstrap", json={
                "username": "synthetic-sa", "display_name": "Synthetic service administrator",
                "password": "synthetic-admin-password",
            }, headers={"X-Bootstrap-Token": config.bootstrap_token})
            assert boot.status_code == 201
            client.headers["X-CSRF-Token"] = boot.json()["csrf_token"]
            yield app, client, provider
    finally:
        app.state.engine.dispose()


def _headers(etag=None):
    headers = {"Idempotency-Key": str(uuid4())}
    if etag is not None:
        headers["If-Match"] = etag
    return headers


def _expect(response, status):
    assert response.status_code == status
    return response.json()


def _error(response, status, code):
    data = _expect(response, status)
    assert data.get("error", data)["code"] == code


def _profile(client, kind):
    response = client.post(_BASE, json={"kind": kind, "label": f"Synthetic {kind}"}, headers=_headers())
    profile = _expect(response, 201)
    return f"{_BASE}/{profile['id']}", response.headers["ETag"]


def _configure(client, kind):
    profile, etag = _profile(client, kind)
    revision = _expect(client.post(profile + "/revisions", json=_REVISIONS[kind], headers=_headers(etag)), 201)
    return profile, f"{profile}/revisions/{revision['id']}", revision["id"]


def _etag(client, profile):
    response = client.get(profile)
    assert response.status_code == 200
    return response.headers["ETag"]


def _test_run(client, revision):
    runs_path = revision + "/test-runs"
    test = _expect(client.post(runs_path, json=_FIXTURE, headers=_headers()), 202)
    return test, f"{runs_path}/{test['id']}"


def _preview(client, profile, action, *, revision_id=None):
    return client.post(profile + "/change-preview", json={
        "action": action, "revision_id": revision_id, "reason": _REASON,
    }, headers=_headers(_etag(client, profile)))


def _change(client, profile, action, preview, *, headers=None):
    return client.post(profile + f"/{action}", json={
        "preview_id": preview["preview_id"], "reason": _REASON,
    }, headers=headers or _headers(preview["profile_etag"]))


@pytest.mark.parametrize("kind", ["generation", "embedding", "parsing"])
def test_fixed_fixture_uses_real_provider_protocol_and_safe_ledger(local_admission, kind):
    app, client, provider = local_admission
    profile, revision, _ = _configure(client, kind)
    run, run_path = _test_run(client, revision)
    assert run["state"] == "succeeded" and run["valid_until"]
    assert run["data_scope"] == "synthetic_only"
    provider_calls = provider.requests
    if kind == "generation":
        assert [call.target for call in provider_calls] == [CHAT_PATH]
        assert json.loads(provider_calls[0].body) == {
            "model": "synthetic-chat", "stream": False, "max_tokens": 256,
            "messages": [{"role": "user", "content": "SYNTHETIC fixture: reply SYNTHETIC OK."}],
        }
        assert run["result"]["usage"]["total_tokens"] == 9
        assert RESULT_MARKER not in json.dumps(run)
        assert run["result"]["text_present"] is True
    elif kind == "embedding":
        assert [call.target for call in provider_calls] == [EMBEDDING_PATH]
        assert json.loads(provider_calls[0].body) == {
            "model": "BAAI/bge-m3", "encoding_format": "float",
            "input": ["SYNTHETIC DEMO-1 power on", "SYNTHETIC DEMO-1 maintenance warning"],
        }
        assert run["result"]["count"] == 2 and run["result"]["dimensions"] == 1024
        assert run["result"]["usage"]["total_tokens"] == 6
        assert "vectors" not in run["result"]
    else:
        batch = run["checkpoint"]["batch_id"]
        assert [call.target.partition("?")[0] for call in provider_calls] == [
            BATCH_PATH, UPLOAD_PATH + batch, POLL_PATH + batch, DOWNLOAD_PATH + batch,
        ]
        assert json.loads(provider_calls[0].body) == {
            "files": [{"name": "synthetic.pdf", "data_id": run["id"]}],
            "model_version": "vlm", "enable_formula": True, "enable_table": True,
        }
        assert provider_calls[1].body.startswith(b"%PDF-")
        assert run["result"]["file_pages"] == [1] and run["result"]["block_count"] == 3
        assert run["result"]["review_state"] == "unreviewed"

    detail = _expect(client.get(run_path), 200)
    detail_text = json.dumps(detail)
    assert all(sensitive not in detail_text for sensitive in (_SECRET, TRANSFER_TICKET, f"127.0.0.1:{provider.port}"))
    assert [call["ordinal"] for call in detail["calls"]] == list(range(1, len(provider_calls) + 1))
    with app.state.session_factory() as db:
        records = db.scalars(select(ServiceRequestCall).where(ServiceRequestCall.test_run_id == run["id"])
                             .order_by(ServiceRequestCall.ordinal)).all()
    assert len(records) == len(detail["calls"]) == len(provider_calls)
    for record, http_call, view in zip(records, provider_calls, detail["calls"]):
        target = http_call.target
        if record.purpose in {"upload", "download"}:
            target = f"http://127.0.0.1:{provider.port}" + target
        assert record.target_hash == hashlib.sha256(target.encode()).hexdigest()
        assert record.payload_hash == hashlib.sha256(http_call.body).hexdigest()
        assert record.response_hash and len(record.response_hash) == 64
        assert record.state == "received" and record.response_status == 200
        assert http_call.authenticated is http_call.authorization_present is (record.purpose == "api")
        assert set(view) == {"id", "ordinal", "method", "purpose", "state", "response_status", "safe_error"}
        assert view["method"] == http_call.method
    assert _expect(client.get(profile), 200)["current_test_ids"] == [run["id"]]


def test_revision_precondition_immutability_and_activation_then_safety_stop(local_admission, monkeypatch):
    _, client, provider = local_admission
    profile, original_etag = _profile(client, "generation")
    payload = _REVISIONS["generation"]
    _error(client.post(profile + "/revisions", json=payload, headers=_headers()), 428, "PRECONDITION_REQUIRED")
    original = _expect(client.post(profile + "/revisions", json=payload, headers=_headers(original_etag)), 201)
    modified = {**payload, "model": "another-synthetic-chat"}
    _error(client.post(profile + "/revisions", json=modified, headers=_headers(original_etag)), 412, "PRECONDITION_FAILED")
    second = _expect(client.post(profile + "/revisions", json=modified, headers=_headers(_etag(client, profile))), 201)
    assert (second["id"], second["configuration_hash"]) != (original["id"], original["configuration_hash"])
    revision = f"{profile}/revisions/{original['id']}"
    assert _expect(client.get(revision), 200) == original
    _error(_preview(client, profile, "activate", revision_id=original["id"]), 409, "SERVICE_TEST_REQUIRED")
    test, _ = _test_run(client, revision)
    assert test["state"] == "succeeded"
    preview = _expect(_preview(client, profile, "activate", revision_id=original["id"]), 200)
    activated = _expect(_change(client, profile, "activate", preview), 200)
    assert activated["enabled"] is True and activated["active_revision_id"] == original["id"]
    assert activated["enterprise_admission_state"] == "not_configured"
    assert activated["business_dispatch_enabled"] is False
    assert activated["health"]["state"] == "unknown"
    _error(_change(client, profile, "activate", preview, headers=_headers(_etag(client, profile))), 409, "SERVICE_PREVIEW_STALE")

    monkeypatch.delenv(_ENV_REF)
    disable = _expect(_preview(client, profile, "disable"), 200)
    stopped = _expect(_change(client, profile, "disable", disable), 200)
    assert stopped["enabled"] is False and stopped["current_test_ids"] == []
    assert len(provider.requests) == 1


def test_credential_rotation_invalidates_test_and_preview(local_admission, monkeypatch):
    _, client, provider = local_admission
    profile, revision, revision_id = _configure(client, "generation")
    original, _ = _test_run(client, revision)
    assert original["state"] == "succeeded"
    preview = _expect(_preview(client, profile, "activate", revision_id=revision_id), 200)
    monkeypatch.setenv(_ENV_REF, "SYNTHETIC-http-rotated-secret")
    provider.credential = "SYNTHETIC-http-rotated-secret"
    assert _expect(client.get(profile), 200)["current_test_ids"] == []
    _error(_change(client, profile, "activate", preview), 409, "SERVICE_PREVIEW_STALE")
    _error(_preview(client, profile, "activate", revision_id=revision_id), 409, "SERVICE_TEST_REQUIRED")
    assert len(provider.requests) == 1
    refreshed, _ = _test_run(client, revision)
    assert refreshed["state"] == "succeeded"
    assert _expect(client.get(profile), 200)["current_test_ids"] == [refreshed["id"]]


def test_test_expiry_invalidates_an_existing_preview(local_admission):
    app, client, provider = local_admission
    profile, revision, revision_id = _configure(client, "generation")
    test, _ = _test_run(client, revision)
    preview = _expect(_preview(client, profile, "activate", revision_id=revision_id), 200)
    with app.state.session_factory() as db:
        db.get(ServiceTestRun, test["id"]).valid_until = utc_now() - timedelta(seconds=1)
        db.commit()
    _error(_change(client, profile, "activate", preview), 409, "SERVICE_PREVIEW_STALE")
    _error(_preview(client, profile, "activate", revision_id=revision_id), 409, "SERVICE_TEST_REQUIRED")
    assert len(provider.requests) == 1


@pytest.mark.parametrize("mode", ["disconnect", "short-body", "timeout"])
def test_unknown_generation_is_recorded_and_replay_never_resends(local_admission, mode):
    _, client, provider = local_admission
    profile, revision, revision_id = _configure(client, "generation")
    path = revision + "/test-runs"
    headers = _headers()
    provider.mode = mode
    first = _expect(client.post(path, json=_FIXTURE, headers=headers), 202)
    assert (first["state"], first["safe_error"]) == ("unknown", "SERVICE_OUTCOME_UNKNOWN")
    provider.mode = "normal"
    assert _expect(client.post(path, json=_FIXTURE, headers=headers), 202) == first
    run_path = f"{path}/{first['id']}"
    result = _expect(client.get(run_path), 200)
    assert len(result["calls"]) == 1 and result["calls"][0]["state"] == "unknown"
    reconciliation = _expect(client.post(
        run_path + "/resume", json={"reason": _REASON}, headers=_headers(_etag(client, profile)),
    ), 202)
    assert (reconciliation["state"], reconciliation["safe_error"]) == ("unknown", "SERVICE_RECONCILIATION_REQUIRED")
    _error(_preview(client, profile, "activate", revision_id=revision_id), 409, "SERVICE_TEST_REQUIRED")
    assert len(provider.requests) == 1 and provider.requests[0].target == CHAT_PATH


def test_accepted_pending_test_can_be_claimed_once_after_interruption(local_admission, monkeypatch):
    app, client, provider = local_admission
    _, revision, _ = _configure(client, "generation")
    path = revision + "/test-runs"
    headers = _headers()

    def interrupted(*args, **kwargs):
        raise RuntimeError("SYNTHETIC interruption before claiming the pending test")

    with monkeypatch.context() as pause:
        pause.setattr(service_admission, "run_test", interrupted)
        with pytest.raises(RuntimeError, match="SYNTHETIC interruption"):
            client.post(path, json=_FIXTURE, headers=headers)
    with app.state.session_factory() as db:
        pending = db.scalar(select(ServiceTestRun))
        assert pending.state == "pending"
        pending_id = pending.id
    assert provider.requests == []
    replayed = _expect(client.post(path, json=_FIXTURE, headers=headers), 202)
    assert (replayed["id"], replayed["state"]) == (pending_id, "succeeded")
    assert _expect(client.post(path, json=_FIXTURE, headers=headers), 202) == replayed
    assert len(provider.requests) == 1


@pytest.mark.parametrize(("mode", "safe_error", "upstream_status"), [
    ("malformed", "PROVIDER_INVALID_RESPONSE", 200),
    ("wrong-content", "SERVICE_FIXTURE_RESULT_MISMATCH", 200),
    ("redirect", "SERVICE_UPSTREAM_REJECTED", 307),
    ("oversize", "SERVICE_RESPONSE_BUDGET", 200),
])
def test_provider_response_failures_are_safe_and_do_not_follow_redirects(local_admission, mode, safe_error, upstream_status):
    _, client, provider = local_admission
    _, revision, _ = _configure(client, "generation")
    provider.mode = mode
    first, run_path = _test_run(client, revision)
    assert (first["state"], first["safe_error"]) == ("failed", safe_error)
    detail = _expect(client.get(run_path), 200)
    assert len(detail["calls"]) == 1 and detail["calls"][0]["response_status"] == upstream_status
    assert "SYNTHETIC upstream body" not in json.dumps(detail)
    assert [call.target for call in provider.requests] == [CHAT_PATH]


def test_parser_cross_origin_upload_is_blocked_before_send(local_admission):
    _, client, provider = local_admission
    _, revision, _ = _configure(client, "parsing")
    with run_synthetic_provider(credential=_SECRET) as other:
        provider.transfer_port = other.port
        run, run_path = _test_run(client, revision)
        assert (run["state"], run["safe_error"]) == ("failed", "SERVICE_DESTINATION_DENIED")
        assert other.requests == []
    assert [call.target for call in provider.requests] == [BATCH_PATH]
    assert [call["purpose"] for call in _expect(client.get(run_path), 200)["calls"]] == ["api"]


@pytest.mark.parametrize(("mode", "start_state"), [("pending", "awaiting_remote"), ("disconnect-upload", "unknown")])
def test_parser_resume_only_polls_the_persisted_batch(local_admission, mode, start_state):
    _, client, provider = local_admission
    profile, revision, _ = _configure(client, "parsing")
    provider.mode = mode
    initial, run_path = _test_run(client, revision)
    assert initial["state"] == start_state and initial["checkpoint"]["batch_id"]
    before_resume = len(provider.requests)
    path = run_path + "/resume"
    headers = _headers(_etag(client, profile))
    resumed = _expect(client.post(path, json={"reason": _REASON}, headers=headers), 202)
    assert resumed["id"] == initial["id"] and resumed["state"] == "succeeded"
    assert resumed["checkpoint"] == initial["checkpoint"]
    assert _expect(client.post(path, json={"reason": _REASON}, headers=headers), 202) == resumed
    batch_id = initial["checkpoint"]["batch_id"]
    assert [(call.method, call.target.partition("?")[0]) for call in provider.requests[before_resume:]] == [
        ("GET", POLL_PATH + batch_id), ("GET", DOWNLOAD_PATH + batch_id),
    ]
    calls = _expect(client.get(run_path), 200)["calls"]
    assert sum(call["method"] == "POST" for call in calls) == 1
    assert sum(call["method"] == "PUT" for call in calls) == 1
    if mode == "disconnect-upload":
        assert calls[1]["state"] == "unknown"


def test_service_routes_require_current_admin_csrf_and_fixed_payload(local_admission):
    app, client, provider = local_admission
    profile, revision, _ = _configure(client, "generation")
    runs = revision + "/test-runs"
    with TestClient(app) as unprivileged:
        assert unprivileged.get(profile).status_code == 401
        with app.state.session_factory() as db:
            db.add(User(username="synthetic-employee", display_name="Synthetic employee", status="active",
                        credential_state="active", credential_hash=hash_password("synthetic-employee-password"),
                        roles=["employee"]))
            db.commit()
        login = _expect(unprivileged.post("/auth/login", json={
            "username": "synthetic-employee", "password": "synthetic-employee-password",
        }), 200)
        unprivileged.headers["X-CSRF-Token"] = login["csrf_token"]
        _error(unprivileged.get(profile), 403, "FORBIDDEN")
        _error(unprivileged.post(runs, json=_FIXTURE, headers=_headers()), 403, "FORBIDDEN")
    _error(client.post(runs, json=_FIXTURE, headers={**_headers(), "X-CSRF-Token": "invalid"}), 403, "CSRF_REQUIRED")
    assert client.post(runs, json={**_FIXTURE, "prompt": "SYNTHETIC caller content"}, headers=_headers()).status_code == 422
    assert client.post(runs, json={"fixture_id": "unregistered"}, headers=_headers()).status_code == 422
    revision_payload = {**_REVISIONS["generation"], "endpoint_ref": "http://127.0.0.1:12345"}
    _error(client.post(profile + "/revisions", json=revision_payload, headers=_headers(_etag(client, profile))),
           409, "SERVICE_ENDPOINT_NOT_ADMITTED")
    revision_payload = {**_REVISIONS["generation"], "model": "unregistered-model"}
    _error(client.post(profile + "/revisions", json=revision_payload, headers=_headers(_etag(client, profile))),
           409, "SERVICE_MODEL_NOT_ADMITTED")
    assert provider.requests == []
