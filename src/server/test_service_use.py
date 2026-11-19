"""Purpose policy and business-wire fences against a real loopback fixture."""
import json
from datetime import timedelta
import pytest
from sqlalchemy import select
from .test_service_admission import (local_admission, _configure, _test_run, _preview, _change,
    _headers, _expect, _etag, _REASON)
from .service_models import ServiceRevision, ServiceProfile
from .service_use_models import ServiceSelection, ServiceUseCall
from .service_use import pin_use, check_pin, prove_input, BusinessTransport, reserve_call, settle_call
from .testing_service_use import attest_synthetic_input
from .provider_transport import ProviderError
from .inference_adapters import BgeM3Adapter
from .models import utc_now
from .test_service_tls import certificates, _tls_client


def activate(client, kind):
    profile, revision_path, revision_id = _configure(client, kind)
    _test_run(client, revision_path)
    preview = _expect(_preview(client, profile, "activate", revision_id=revision_id), 200)
    _expect(_change(client, profile, "activate", preview), 200)
    return profile, revision_path, revision_id


def select_purpose(client, purpose, revision_id=None, action="select"):
    path = "/admin/service-selections/" + purpose
    row = next(x for x in client.get("/admin/service-selections").json()["items"] if x["purpose"] == purpose)
    preview = _expect(client.post(path + "/previews", json={"action": action, "revision_id": revision_id,
        "reason": _REASON}, headers=_headers(row["etag"])), 200)
    return _expect(client.post(path + "/" + action, json={"preview_id": preview["id"], "reason": _REASON},
        headers=_headers(row["etag"])), 200)


def test_purpose_preview_idempotency_and_kind_boundaries(local_admission):
    app, client, provider = local_admission
    profile, _, revision = activate(client, "embedding")
    path = "/admin/service-selections/document.parse/previews"
    denied = client.post(path, json={"action": "select", "revision_id": revision, "reason": _REASON}, headers=_headers('"0"'))
    assert denied.status_code == 409 and denied.json()["code"] == "SERVICE_SELECTION_UNAVAILABLE"
    path = "/admin/service-selections/document.embed"
    key = _headers('"0"')
    body = {"action": "select", "revision_id": revision, "reason": _REASON}
    preview = _expect(client.post(path + "/previews", json=body, headers=key), 200)
    assert _expect(client.post(path + "/previews", json=body, headers=key), 200)["id"] == preview["id"]
    apply_headers = _headers('"0"')
    apply_body = {"preview_id": preview["id"], "reason": _REASON}
    applied = _expect(client.post(path + "/select", json=apply_body, headers=apply_headers), 200)
    assert applied["revision"] == 1 and applied["enterprise_admission_state"] == "not_configured"
    assert _expect(client.post(path + "/select", json=apply_body, headers=apply_headers), 200)["revision"] == 1
    assert client.post(path + "/select", json=apply_body, headers=_headers('"0"')).status_code == 412
    assert client.post(path + "/previews", json=body | {"approved": True}, headers=_headers('"1"')).status_code == 422


def test_exact_input_purpose_proof_and_real_embedding_wire(local_admission):
    app, client, provider = local_admission
    _, _, revision = activate(client, "embedding")
    select_purpose(client, "document.embed", revision)
    with app.state.session_factory() as db:
        actor = db.get(ServiceProfile, db.get(ServiceRevision, revision).profile_id).created_by
        pin = pin_use(db, actor, "document.embed", "version:synthetic", app.state.service_endpoints)
        payload = json.dumps(["SYNTHETIC DEMO-1 maintenance warning"]).encode()
        db.commit()
        before = len(provider.requests)
        transport = BusinessTransport(db, pin, app.state.service_endpoints, lambda: None, lambda: payload)
        with pytest.raises(ProviderError, match="SERVICE_SYNTHETIC_INPUT_REQUIRED"):
            BgeM3Adapter(transport, "BAAI/bge-m3").embed(json.loads(payload))
        assert len(provider.requests) == before
        attest_synthetic_input(db, actor, "query.answer", pin.resource_key, payload)
        db.commit()
        with pytest.raises(ProviderError, match="SERVICE_SYNTHETIC_INPUT_REQUIRED"):
            prove_input(db, pin, payload)
        attest_synthetic_input(db, actor, pin.purpose, pin.resource_key, payload)
        db.commit()
        result = BgeM3Adapter(transport, "BAAI/bge-m3").embed(json.loads(payload))
        assert len(result["vectors"]) == 1 and len(result["vectors"][0]) == 1024
        assert len(provider.requests) == before + 1
        with pytest.raises(ProviderError, match="SERVICE_RECONCILIATION_REQUIRED"):
            BgeM3Adapter(transport, "BAAI/bge-m3").embed(json.loads(payload))
        assert len(provider.requests) == before + 1
        calls = db.scalars(select(ServiceUseCall).where(ServiceUseCall.pin_id == pin.id)).all()
        assert len(calls) == 1 and calls[0].state == "received" and calls[0].settled_cost is None
        with pytest.raises(ProviderError, match="SERVICE_SYNTHETIC_INPUT_REQUIRED"):
            prove_input(db, pin, payload + b" ")


def test_switch_pins_revision_and_disable_cannot_resurrect(local_admission):
    app, client, provider = local_admission
    profile, _, first = activate(client, "generation")
    select_purpose(client, "query.answer", first)
    with app.state.session_factory() as db:
        actor = db.get(ServiceProfile, db.get(ServiceRevision, first).profile_id).created_by
        old = pin_use(db, actor, "query.answer", "task:synthetic", app.state.service_endpoints)
        db.commit()
        old_id = old.id
    second = _expect(client.post(profile + "/revisions", json={"endpoint_ref": "synthetic", "adapter": "openai-chat-v1",
        "model": "synthetic-chat"}, headers=_headers(_etag(client, profile))), 201)["id"]
    _test_run(client, profile + "/revisions/" + second)
    _expect(_change(client, profile, "activate", _expect(_preview(client, profile, "activate", revision_id=second), 200)), 200)
    select_purpose(client, "query.answer", second)
    with app.state.session_factory() as db:
        from .service_use_models import ServiceUsePin
        old = db.get(ServiceUsePin, old_id)
        assert check_pin(db, old, app.state.service_endpoints)[0].id == first
        fresh = pin_use(db, actor, "query.answer", "task:new-synthetic", app.state.service_endpoints)
        assert fresh.revision_id == second
        db.commit()
    select_purpose(client, "query.answer", action="disable")
    select_purpose(client, "query.answer", second)
    with app.state.session_factory() as db:
        with pytest.raises(ProviderError, match="SERVICE_USE_REVOKED"):
            check_pin(db, db.get(ServiceUsePin, old_id), app.state.service_endpoints)


def test_profile_disable_epoch_and_secretless_safety_stop(local_admission, monkeypatch):
    app, client, provider = local_admission
    profile, _, revision = activate(client, "generation")
    select_purpose(client, "query.answer", revision)
    with app.state.session_factory() as db:
        actor = db.get(ServiceProfile, db.get(ServiceRevision, revision).profile_id).created_by
        pin = pin_use(db, actor, "query.answer", "task:synthetic-stop", app.state.service_endpoints)
        db.commit()
        ident = pin.id
    monkeypatch.delenv("DATACENTER_SYNTHETIC_TEST_KEY")
    _expect(_change(client, profile, "disable", _expect(_preview(client, profile, "disable"), 200)), 200)
    select_purpose(client, "query.answer", action="disable")
    with app.state.session_factory() as db:
        from .service_use_models import ServiceUsePin
        with pytest.raises(ProviderError, match="SERVICE_USE_REVOKED"):
            check_pin(db, db.get(ServiceUsePin, ident), app.state.service_endpoints)


def test_reservation_budget_and_stale_settlement(local_admission):
    app, client, provider = local_admission
    _, _, revision = activate(client, "generation")
    select_purpose(client, "query.answer", revision)
    with app.state.session_factory() as db:
        actor = db.get(ServiceProfile, db.get(ServiceRevision, revision).profile_id).created_by
        pin = pin_use(db, actor, "query.answer", "task:synthetic-budget", app.state.service_endpoints)
        payload = b"SYNTHETIC context"
        attest_synthetic_input(db, actor, pin.purpose, pin.resource_key, payload)
        db.commit()
        for n in range(12):
            call = reserve_call(db, pin, app.state.service_endpoints, str(n), "POST", "/v1/chat/completions", b"{}", payload)
            settle_call(db, call, "unknown", error="SERVICE_OUTCOME_UNKNOWN")
            db.commit()
            settle_call(db, call, "received", status=200)
            assert call.state == "unknown"
        with pytest.raises(ProviderError, match="SERVICE_CALL_BUDGET"):
            reserve_call(db, pin, app.state.service_endpoints, "13", "POST", "/v1/chat/completions", b"{}", payload)
        assert len(provider.requests) == 1  # Only fixed self-test went on wire.


def test_business_tls_uses_exact_trust_and_proof_revocation(tmp_path, monkeypatch, certificates):
    certificate = certificates["loopback"]
    with _tls_client(tmp_path, monkeypatch, certificate, certificate[0]) as (app, client, provider):
        _, _, revision = activate(client, "embedding")
        select_purpose(client, "document.embed", revision)
        with app.state.session_factory() as db:
            actor = db.get(ServiceProfile, db.get(ServiceRevision, revision).profile_id).created_by
            pin = pin_use(db, actor, "document.embed", "version:synthetic-tls", app.state.service_endpoints)
            payload = json.dumps(["SYNTHETIC TLS business embedding"]).encode()
            proof = attest_synthetic_input(db, actor, pin.purpose, pin.resource_key, payload)
            db.commit()
            before = len(provider.requests)
            transport = BusinessTransport(db, pin, app.state.service_endpoints, lambda: None, lambda: payload)
            with pytest.raises(ProviderError, match="SERVICE_REQUEST_MISMATCH"):
                BgeM3Adapter(transport, "BAAI/bge-m3").embed(["SYNTHETIC unapproved replacement"])
            assert len(provider.requests) == before
            result = BgeM3Adapter(transport, "BAAI/bge-m3").embed(json.loads(payload))
            assert len(result["vectors"][0]) == 1024
            proof.revoked = True
            db.commit()
            with pytest.raises(ProviderError, match="SERVICE_SYNTHETIC_INPUT_REQUIRED"):
                transport.check()
            assert len(provider.requests) == before + 1
