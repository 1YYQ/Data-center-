"""Real loopback TLS checks with fresh synthetic trust material."""

from __future__ import annotations

from contextlib import contextmanager
import json
from pathlib import Path
import shutil
import ssl
import subprocess

from fastapi.testclient import TestClient
import pytest

from .app import create_app
from .config import Settings
from .db import init_db
from .provider_transport import ProviderError
from .service_admission import LocalEndpoint, load_local_endpoints
from .services import seed_reference_data
from .synthetic_provider import (
    BATCH_PATH, CHAT_PATH, DOWNLOAD_PATH, EMBEDDING_PATH, POLL_PATH, UPLOAD_PATH, run_synthetic_provider,
)
from .test_service_admission import (
    _BASE, _ENV_REF, _FIXTURE, _SECRET, _change, _configure, _error, _expect, _headers, _preview, _test_run,
)


@pytest.fixture(scope="module")
def certificates(tmp_path_factory):
    openssl = shutil.which("openssl")
    if openssl is None:
        pytest.skip("OpenSSL CLI is required to generate the synthetic TLS test certificates")
    root = tmp_path_factory.mktemp("service-tls-certs")
    config = root / "openssl.cnf"
    config.write_text("[req]\ndistinguished_name = synthetic_dn\n[synthetic_dn]\n", encoding="ascii")
    material = {}
    for name, ip in (("loopback", "127.0.0.1"), ("alternate", "127.0.0.1"), ("wrong-ip", "127.0.0.2")):
        certificate = root / f"{name}.pem"
        private_key = root / f"{name}.key"
        result = subprocess.run([
            openssl, "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes", "-days", "2",
            "-config", str(config), "-subj", f"/CN=SYNTHETIC {name}", "-addext", f"subjectAltName=IP:{ip}",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,digitalSignature,keyEncipherment,keyCertSign",
            "-addext", "extendedKeyUsage=serverAuth", "-out", str(certificate), "-keyout", str(private_key),
        ], cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15)
        assert result.returncode == 0, result.stderr
        material[name] = (certificate, private_key)
    return material


def _registry(port: int, ca_file: Path | None):
    return {"synthetic": {
        "port": port, "credential_env": _ENV_REF, "scheme": "https",
        "tls_ca_file": str(ca_file) if ca_file else None,
    }}


@contextmanager
def _tls_client(tmp_path, monkeypatch, certificate_pair, trust_file):
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(*certificate_pair)
    config = Settings(
        database_url="sqlite:///" + (tmp_path / "tls-admission.sqlite").as_posix(),
        storage_dir=tmp_path / "storage", bootstrap_token="synthetic-tls-bootstrap", secure_cookie=False,
    )
    monkeypatch.setenv(_ENV_REF, _SECRET)
    with run_synthetic_provider(credential=_SECRET, tls_context=context) as provider:
        monkeypatch.setenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", json.dumps(_registry(provider.port, trust_file)))
        app = create_app(config)
        try:
            init_db(app.state.engine, config, migrate=True)
            with app.state.session_factory() as db:
                seed_reference_data(db)
            with TestClient(app) as client:
                boot = _expect(client.post("/auth/bootstrap", json={
                    "username": "synthetic-tls-sa", "display_name": "Synthetic TLS administrator",
                    "password": "synthetic-tls-password",
                }, headers={"X-Bootstrap-Token": config.bootstrap_token}), 201)
                client.headers["X-CSRF-Token"] = boot["csrf_token"]
                yield app, client, provider
        finally:
            app.state.engine.dispose()


@pytest.mark.parametrize("kind", ["generation", "embedding", "parsing"])
def test_admitted_tls_certificate_works_for_each_synthetic_protocol(tmp_path, monkeypatch, certificates, kind):
    certificate = certificates["loopback"]
    with _tls_client(tmp_path, monkeypatch, certificate, certificate[0]) as (app, client, provider):
        endpoint = app.state.service_endpoints["synthetic"]
        _, client_context = endpoint.trust()
        assert client_context.verify_mode == ssl.CERT_REQUIRED
        assert client_context.check_hostname is True
        assert _expect(client.get(_BASE), 200)["endpoint_refs"] == ["synthetic"]
        _, revision, _ = _configure(client, kind)
        test, test_path = _test_run(client, revision)
        assert test["state"] == "succeeded" and test["data_scope"] == "synthetic_only"
        call_paths = [(call.method, call.target.partition("?")[0]) for call in provider.requests]
        if kind == "parsing":
            batch = test["checkpoint"]["batch_id"]
            assert call_paths == [("POST", BATCH_PATH), ("PUT", UPLOAD_PATH + batch),
                                  ("GET", POLL_PATH + batch), ("GET", DOWNLOAD_PATH + batch)]
            assert [call.authorization_present for call in provider.requests] == [True, False, True, False]
            assert test["result"]["file_pages"] == [1]
        else:
            path = CHAT_PATH if kind == "generation" else EMBEDDING_PATH
            assert call_paths == [("POST", path)] and provider.requests[0].authenticated is True
        detail = json.dumps(_expect(client.get(test_path), 200))
        assert _SECRET not in detail and certificate[0].name not in detail


@pytest.mark.parametrize(("server_cert", "trusted_cert"), [
    ("loopback", "alternate"), ("wrong-ip", "wrong-ip"),
], ids=["untrusted-ca", "wrong-ip-san"])
def test_tls_handshake_rejection_sends_no_application_request(tmp_path, monkeypatch, certificates, server_cert, trusted_cert):
    with _tls_client(tmp_path, monkeypatch, certificates[server_cert], certificates[trusted_cert][0]) as (_, client, provider):
        _, revision, _ = _configure(client, "generation")
        test, test_path = _test_run(client, revision)
        assert (test["state"], test["safe_error"]) == ("unknown", "SERVICE_OUTCOME_UNKNOWN")
        ledger = _expect(client.get(test_path), 200)["calls"]
        assert len(ledger) == 1 and ledger[0]["state"] == "unknown" and ledger[0]["response_status"] is None
        assert provider.requests == []


@pytest.mark.parametrize("unavailable_ca", [None, "missing", "invalid"], ids=["no-ca", "missing-file", "invalid-pem"])
def test_https_registry_fails_closed_with_unavailable_ca(tmp_path, monkeypatch, unavailable_ca):
    trust_file = None if unavailable_ca is None else tmp_path / "synthetic.pem"
    if unavailable_ca == "invalid":
        trust_file.write_text("SYNTHETIC invalid CA data", encoding="ascii")
    settings = _registry(8443, trust_file)
    monkeypatch.setenv(_ENV_REF, _SECRET)
    monkeypatch.setenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", json.dumps(settings))
    with pytest.raises(ProviderError, match=r"^SERVICE_TLS_NOT_CONFIGURED$"):
        LocalEndpoint(**settings["synthetic"]).trust()
    with pytest.raises(ProviderError, match=r"^SERVICE_ENDPOINT_CONFIGURATION_INVALID$"):
        load_local_endpoints()


def test_rotating_ca_bytes_invalidates_test_and_preview(tmp_path, monkeypatch, certificates):
    server_cert = certificates["loopback"]
    trust_file = tmp_path / "active-trust.pem"
    trust_file.write_bytes(server_cert[0].read_bytes())
    with _tls_client(tmp_path, monkeypatch, server_cert, trust_file) as (_, client, provider):
        profile, revision, revision_id = _configure(client, "generation")
        test, _ = _test_run(client, revision)
        assert test["state"] == "succeeded"
        preview = _expect(_preview(client, profile, "activate", revision_id=revision_id), 200)
        trust_file.write_bytes(certificates["alternate"][0].read_bytes())
        assert _expect(client.get(profile), 200)["current_test_ids"] == []
        _error(_change(client, profile, "activate", preview), 409, "SERVICE_PREVIEW_STALE")
        _error(_preview(client, profile, "activate", revision_id=revision_id), 409, "SERVICE_TEST_REQUIRED")
        trust_file.unlink()
        _error(client.post(revision + "/test-runs", json=_FIXTURE, headers=_headers()), 409, "SERVICE_TLS_NOT_CONFIGURED")
        assert len(provider.requests) == 1


def test_tls_mineru_upload_cannot_downgrade_to_http(tmp_path, monkeypatch, certificates):
    certificate = certificates["loopback"]
    with _tls_client(tmp_path, monkeypatch, certificate, certificate[0]) as (_, client, provider):
        provider.scheme = "http"  # Advertise an invalid transfer URL while serving only TLS.
        _, revision, _ = _configure(client, "parsing")
        test, test_path = _test_run(client, revision)
        assert (test["state"], test["safe_error"]) == ("failed", "SERVICE_DESTINATION_DENIED")
        assert [call.target for call in provider.requests] == [BATCH_PATH]
        assert [call["purpose"] for call in _expect(client.get(test_path), 200)["calls"]] == ["api"]
