"""Selected generation uses the real Pi SDK and only isolated loopback fixtures."""

import json
import socket
import ssl
import threading
from collections import namedtuple
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import runtime_gate as gate, service_admission as admission, service_use
from .execution import ENABLED_TOOLS
from .execution_models import QueryAnswerSnapshot, RuntimeJournal
from .models import QueryTask
from .runtime_models import AgentRun, ModelCallAdmission, RuntimeProfile
from .service_models import ServiceProfile
from .service_use_models import ServiceSelection, ServiceTaskSelection, ServiceUseCall, ServiceUsePin
from .services import SafeError
from .test_execution import _headers
from .test_services import fixture_db
from .test_worker import STARTUP, WARNING, local_stack
from .testing_service_use import attest_synthetic_input
from .worker import run_one, runtime_artifacts


PURPOSE = "query.answer"
MODELS = ("synthetic-chat", "synthetic-chat-v2")
CREDENTIAL_ENV = "DATACENTER_SYNTHETIC_PI_TEST_KEY"
FIXTURE_KEY = "SYNTHETIC-pi-loopback-key"
PiStack = namedtuple("PiStack", "db config client address employee admin kb version legacy_requests upstream profile selection")


@pytest.fixture
def pi_upstream(monkeypatch):
    fixture = SimpleNamespace(requests=[], mode="reply", after_request=None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(raw)
            authorized = self.headers.get("Authorization") == "Bearer " + FIXTURE_KEY
            if self.path != "/v1/chat/completions" or not authorized:
                self.send_error(403)
                return
            model = body["model"]
            usage = {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}
            if not body.get("stream"):
                payload = json.dumps({"object": "chat.completion", "model": model, "usage": usage,
                    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "SYNTHETIC OK"}}]}).encode()
                return self.reply(payload, "application/json")
            fixture.requests.append((raw, body, bool(self.headers.get("X-Runtime-Token"))))
            if fixture.after_request:
                fixture.after_request()
            if fixture.mode == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            base = {"id": "synthetic-business", "object": "chat.completion.chunk", "created": 1, "model": model}
            chunks = [{**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": "SYNTHETIC OK"}, "finish_reason": None}]},
                      {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                      {**base, "choices": [], "usage": usage}]
            payload = (": synthetic stream\n\n" + "".join("data: " + json.dumps(row) + "\n\n" for row in chunks)).encode()
            if fixture.mode != "incomplete_stream":
                payload += b"data: [DONE]\n\n"
            self.reply(payload, "text/event-stream")

        def reply(self, payload, content_type):
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv(CREDENTIAL_ENV, FIXTURE_KEY)
    monkeypatch.setenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", json.dumps({"pi_test": {
        "port": server.server_port, "credential_env": CREDENTIAL_ENV, "allowed_models": MODELS,
        "generation_models": {MODELS[1]: {
            "api_family": admission.PI_API_FAMILY, "tokenizer": admission.PI_TOKENIZER, "context_window": 8192,
            "max_output_tokens": 96, "capabilities": admission.PI_CAPABILITIES}}}}))
    try:
        yield fixture
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _select(db, admin_id, selection, action, revision_id):
    endpoints = admission.load_local_endpoints()
    preview = service_use.preview_selection(db, admin_id, selection.purpose, action, revision_id, selection.revision, endpoints)
    service_use.apply_selection(db, selection, admin_id, action, preview.id, endpoints)
    db.commit()


def _activate(db, profile, admin_id, model):
    endpoints = admission.load_local_endpoints()
    revision = admission.create_revision(db, profile, "pi_test", "openai-chat-v1", model, endpoints)
    test = admission.new_test(db, revision, admin_id, endpoints)
    db.commit()
    assert admission.run_test(db, test.id, endpoints).state == "succeeded"
    preview = admission.preview_change(db, profile, admin_id, "activate", revision.id, endpoints)
    admission.apply_change(db, profile, admin_id, "activate", preview.id, endpoints)
    db.commit()
    return revision.id


@pytest.fixture
def selected_pi(pi_upstream, local_stack):
    db, config, client, address, employee, admin, kb, _, version, legacy_requests, _ = local_stack
    artifact = runtime_artifacts()
    gate.create_local_profile(db, artifact_manifest_hash=artifact["artifact_manifest_hash"],
                              adapter_fingerprint=artifact["adapter_fingerprint"], artifact_manifest=artifact["manifest"],
                              profile_id="selected-pi")
    profile = ServiceProfile(kind="generation", label="SYNTHETIC Pi", created_by=admin.id)
    selection = ServiceSelection(purpose=PURPOSE)
    db.add_all([profile, selection])
    db.flush()
    revision_id = _activate(db, profile, admin.id, MODELS[0])
    _select(db, admin.id, selection, "select", revision_id)
    return PiStack(db, config, client, address, employee, admin, kb, version, legacy_requests, pi_upstream, profile, selection)


def _submit(f, *, attest=True):
    query = "DEMO-1 startup"
    result = f.client.post("/knowledge/queries", json={"query": query, "model": "DEMO-1", "knowledge_base_ids": [f.kb.id],
                           "service_mode": "selected_service"}, headers={"Idempotency-Key": uuid4().hex})
    assert result.status_code == 202, result.text
    task_id = result.json()["task_id"]
    task = f.db.get(QueryTask, task_id)
    assert task.effective_input["query"] == query and task.input_snapshot["service_mode"] == "selected_service"
    assert task.effective_input["knowledge_base_ids"] == [f.kb.id]
    envelope = gate.canonical_json({"systemPrompt": gate.SYSTEM, "tools": ENABLED_TOOLS, "messages": [
        {"role": "user", "timestamp": 0, "content": [{"type": "text", "text": gate.canonical_json({
            "business_purpose": "answer", "operation": "answer", "task_input": task.effective_input,
            "authorized_sources": [], "completed_tool_results": []})}]}]})
    if attest:
        attest_synthetic_input(f.db, f.employee.id, PURPOSE, "task:" + task_id, envelope.encode())
        f.db.commit()
    return task_id, envelope


def _authorize(f, run, token, *, operation="answer", mutate=None):
    projection = gate.project_context(f.db, run.id, token, operation=operation)
    body = gate._selected_model_body(projection.context_json, projection.generation)
    if mutate:
        mutate(body)
    payload = json.dumps(body).encode()
    call = gate.authorize_model(f.db, run.id, token, projection_id=projection.id, context_digest=projection.context_digest,
        payload_digest=gate.digest(payload), adapter=gate.ADAPTER, request_key=uuid4().hex)
    return call, payload


def _authorized(f, task_id, mutate=None):
    run, token = gate.open_run(f.db, task_id, profile_id="selected-pi")
    call, payload = _authorize(f, run, token, mutate=mutate)
    return run, token, call, payload


def _call_receipts(f, task_id):
    f.db.expire_all()
    model_call = f.db.scalar(select(ModelCallAdmission).where(ModelCallAdmission.task_id == task_id))
    service_call = f.db.scalar(select(ServiceUseCall).where(ServiceUseCall.request_key == model_call.id))
    return model_call, service_call


def test_selected_revision_is_pinned_through_real_sdk_worker(selected_pi):
    f = selected_pi
    old_task, old_envelope = _submit(f)
    revision_id = _activate(f.db, f.profile, f.admin.id, MODELS[1])
    _select(f.db, f.admin.id, f.selection, "select", revision_id)
    new_task, new_envelope = _submit(f)
    for task_id, model, envelope in zip((old_task, new_task), MODELS, (old_envelope, new_envelope)):
        outcome = run_one(f.config, gateway_url=f.address, task_id=task_id, profile_id="selected-pi")
        assert outcome["state"] == "succeeded", outcome
        answer = f.client.get(f"/tasks/{task_id}/result").json()
        assert STARTUP in answer["answer"] and WARNING in answer["answer"]
        assert {e["version"]["id"] for e in answer["evidence"]} == {f.version.id}
        raw, body, runtime_token = f.upstream.requests[-1]
        assert body["model"] == model and not runtime_token
        assert body["messages"][0] == {"role": "system", "content": gate.SYSTEM}
        assert {tool["function"]["name"] for tool in body["tools"]} == {tool["name"] for tool in ENABLED_TOOLS}
        assert FIXTURE_KEY.encode() not in raw
        original, recorded = _call_receipts(f, task_id)
        assert original.state == recorded.state == "received"
        assert recorded.payload_hash == gate.digest(raw) == original.payload_digest
        assert recorded.input_hash == gate.digest(envelope)
        assert recorded.usage["total_tokens"] == 110 and recorded.settled_cost is None
        assert f.db.get(ServiceUsePin, recorded.pin_id).purpose == PURPOSE
    assert len(f.upstream.requests) == 2 and not f.legacy_requests


def test_generation_compatibility_profile_binds_revision_budget_and_final_wire(selected_pi):
    f = selected_pi
    other_task, _ = _submit(f)
    revision_id = _activate(f.db, f.profile, f.admin.id, MODELS[1])
    _select(f.db, f.admin.id, f.selection, "select", revision_id)
    profile_id = "generation-compatibility"
    artifacts = f.db.get(RuntimeProfile, ("selected-pi", 1))
    profile = gate.create_local_profile(f.db, generation_revision_id=revision_id, profile_id=profile_id,
        artifact_manifest_hash=artifacts.artifact_manifest_hash, adapter_fingerprint=artifacts.adapter_fingerprint,
        artifact_manifest=artifacts.configuration["artifact_manifest"])
    assert profile.configuration["generation"] == admission.generation_model(MODELS[1], max_output_tokens=96)
    with pytest.raises(SafeError) as denied:
        gate.open_run(f.db, other_task, profile_id=profile_id)
    assert denied.value.code == "RUNTIME_SERVICE_BINDING_CHANGED"
    f.db.rollback()
    assert not f.db.scalar(select(AgentRun.id).where(AgentRun.task_id == other_task))
    task_id, envelope = _submit(f)
    assert run_one(f.config, gateway_url=f.address, task_id=task_id, profile_id=profile_id)["state"] == "succeeded"
    original, ledger = _call_receipts(f, task_id)
    raw, outgoing, runtime_token = f.upstream.requests[0]
    assert original.payload_digest == ledger.payload_hash == gate.digest(raw)
    assert original.reserved_tokens == 8192 and outgoing["max_completion_tokens"] == 96
    assert outgoing["model"] == MODELS[1] and outgoing["store"] is False and not runtime_token
    assert outgoing == gate._selected_model_body(envelope, profile.configuration["generation"])
    assert len(f.upstream.requests) == 1 and not f.legacy_requests


@pytest.mark.parametrize("override", [
    {"api_family": "responses"},
    {"tokenizer": "unconfigured-tokenizer"},
    {"capabilities": [*admission.PI_CAPABILITIES, "image"]},
    {"context_window": 8191, "max_output_tokens": 2048},
])
def test_generation_profile_rejects_unimplemented_wire_capabilities(override):
    with pytest.raises(admission.ProviderError) as denied:
        admission.generation_model(MODELS[0], **override)
    assert denied.value.code == "RUNTIME_SERVICE_PROTOCOL_INCOMPATIBLE"


def test_generation_network_requires_https_and_rejects_nonpublic_pinned_peers(monkeypatch):
    endpoint = {
        "port": 443, "credential_env": "DATACENTER_GENERATION_TEST_KEY", "scheme": "https",
        "tls_ca_file": "not-read-in-this-test.pem", "hostname": "fixture.example",
        "allowed_models": [MODELS[0]], "egress": admission.GENERATION_EGRESS,
        "generation_models": {MODELS[0]: {
            "api_family": admission.PI_API_FAMILY, "tokenizer": admission.PI_TOKENIZER,
            "context_window": 8192, "max_output_tokens": 128, "capabilities": admission.PI_CAPABILITIES,
        }},
    }
    for peer in ("127.0.0.1", "10.0.0.1", "169.254.169.254", "::ffff:127.0.0.1", "224.0.0.1", "fixture.example"):
        with pytest.raises(admission.ProviderError) as denied:
            admission.LocalEndpoint(**endpoint, peer_address=peer).validate()
        assert denied.value.code == "SERVICE_DESTINATION_DENIED"
    monkeypatch.setenv("DATACENTER_GENERATION_TEST_KEY", "synthetic-credential")
    approved = admission.LocalEndpoint(**endpoint, peer_address="8.8.4.4")
    assert approved.validate() == "synthetic-credential"
    with pytest.raises(admission.ProviderError):
        admission.LocalEndpoint(**dict(endpoint, scheme="http"), peer_address="8.8.4.4").validate()
    dials = []
    monkeypatch.setattr(admission.socket, "create_connection", lambda next_peer, *_: dials.append(next_peer))
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    connection = approved.connect(2, tls)
    assert connection.host == "fixture.example" and tls.check_hostname
    connection._create_connection(("fixture.example", 443), 2)
    assert dials == [("8.8.4.4", 443)]


def test_selected_purpose_and_input_are_each_required(selected_pi):
    f = selected_pi
    task_id, envelope = _submit(f, attest=False)
    run, token = gate.open_run(f.db, task_id, profile_id="selected-pi")
    attest_synthetic_input(f.db, f.employee.id, PURPOSE, "task:" + task_id, (envelope + " ").encode())
    f.db.commit()
    with pytest.raises(SafeError) as error:
        gate.project_context(f.db, run.id, token)
    assert error.value.code == "SERVICE_SYNTHETIC_INPUT_REQUIRED"
    f.db.rollback()
    attest_synthetic_input(f.db, f.employee.id, PURPOSE, "task:" + task_id, envelope.encode())
    f.db.commit()
    assert gate.project_context(f.db, run.id, token).context_json == envelope
    for operation in ("summary", "compaction"):
        with pytest.raises(SafeError) as error:
            gate.project_context(f.db, run.id, token, operation=operation)
        assert error.value.code == "SERVICE_PURPOSE_NOT_PERMITTED"
        f.db.rollback()
    assert not f.upstream.requests and not f.legacy_requests


def test_enabled_auxiliary_purpose_requires_separate_pin_and_exact_proof(selected_pi):
    f = selected_pi
    summary = ServiceSelection(purpose="query.summary")
    f.db.add(summary)
    f.db.flush()
    _select(f.db, f.admin.id, summary, "select", f.selection.revision_id)
    task_id, envelope = _submit(f)
    pins = f.db.get(ServiceTaskSelection, task_id).pin_ids
    assert pins["query.summary"] != pins[PURPOSE]
    run, token = gate.open_run(f.db, task_id, profile_id="selected-pi")
    with pytest.raises(SafeError) as error:
        gate.project_context(f.db, run.id, token, operation="summary")
    assert error.value.code == "AUXILIARY_SUPPORT_UNAVAILABLE"
    f.db.rollback()
    # This old Block-only query does not have complete child groups. A separate
    # purpose pin and a relabelled answer proof cannot manufacture that input.
    context = json.loads(envelope)
    message = context["messages"][0]["content"][0]
    data = json.loads(message["text"])
    data["operation"] = "summary"
    message["text"] = gate.canonical_json(data)
    attest_synthetic_input(f.db, f.employee.id, "query.summary", "task:" + task_id, gate.canonical_json(context).encode())
    f.db.commit()
    with pytest.raises(SafeError) as unsupported:
        gate.project_context(f.db, run.id, token, operation="summary")
    assert unsupported.value.code == "AUXILIARY_SUPPORT_UNAVAILABLE"
    assert not f.upstream.requests and not f.legacy_requests


def test_selected_wire_rejects_unattested_messages_even_with_matching_request_digest(selected_pi):
    f = selected_pi
    task_id, _ = _submit(f)
    run, token, call, payload = _authorized(f, task_id, lambda body: body["messages"].append({"role": "user", "content": "unattested"}))
    with pytest.raises(SafeError) as error:
        gate.dispatch_model(f.db, run.id, token, call.id, payload)
    assert error.value.code == "RUNTIME_PAYLOAD_MISMATCH"
    f.db.rollback()
    model_call, service_call = _call_receipts(f, task_id)
    assert model_call.state == "reserved" and service_call is None
    assert not f.upstream.requests and not f.legacy_requests


def test_selected_disable_epoch_survives_reenable_and_never_falls_back(selected_pi):
    f = selected_pi
    task_id, _ = _submit(f)
    run, token = gate.open_run(f.db, task_id)
    call, payload = _authorize(f, run, token)
    revision_id = f.selection.revision_id
    _select(f.db, f.admin.id, f.selection, "disable", None)
    _select(f.db, f.admin.id, f.selection, "select", revision_id)
    with pytest.raises(SafeError) as error:
        gate.dispatch_model(f.db, run.id, token, call.id, payload)
    assert error.value.code == "SERVICE_USE_REVOKED"
    f.db.rollback()
    assert not f.upstream.requests and not f.legacy_requests
    new_task, _ = _submit(f)
    assert run_one(f.config, gateway_url=f.address, task_id=new_task, profile_id="selected-pi")["state"] == "succeeded"
    assert len(f.upstream.requests) == 1 and not f.legacy_requests


def test_selected_worker_rejects_missing_binding_even_with_legacy_profile(selected_pi):
    f = selected_pi
    task_id, _ = _submit(f)
    f.db.delete(f.db.get(ServiceTaskSelection, task_id))
    f.db.commit()
    outcome = run_one(f.config, gateway_url=f.address, task_id=task_id)
    assert outcome["state"] == "blocked" and outcome["error_code"] == "RUNTIME_SERVICE_BINDING_CHANGED", outcome
    assert not f.upstream.requests and not f.legacy_requests


def test_selected_revocation_during_http_withholds_reply_and_preserves_receipts(selected_pi):
    f = selected_pi
    task_id, _ = _submit(f)
    run, token, call, payload = _authorized(f, task_id)
    engine, admin_id = f.db.get_bind(), f.admin.id

    def disable():
        with Session(engine) as db:
            _select(db, admin_id, db.get(ServiceSelection, PURPOSE), "disable", None)

    f.upstream.after_request = disable
    with pytest.raises(SafeError) as error:
        gate.dispatch_model(f.db, run.id, token, call.id, payload)
    assert error.value.code == "SERVICE_USE_REVOKED"
    f.db.rollback()
    original, recorded = _call_receipts(f, task_id)
    assert original.state == recorded.state == "received"
    assert len(f.upstream.requests) == 1 and not f.legacy_requests


@pytest.mark.parametrize("upstream_mode", ["drop", "incomplete_stream"])
def test_selected_unknown_response_is_durable_and_never_resent(selected_pi, upstream_mode):
    f = selected_pi
    task_id, _ = _submit(f)
    f.upstream.mode = upstream_mode
    outcome = run_one(f.config, gateway_url=f.address, task_id=task_id, profile_id="selected-pi")
    assert outcome["state"] == "reconciliation_required", outcome
    original, recorded = _call_receipts(f, task_id)
    assert original.state == recorded.state == "unknown"
    assert original.response_status == recorded.response_status == (200 if upstream_mode == "incomplete_stream" else None)
    assert recorded.usage is None and recorded.settled_cost is None
    assert f.client.get(f"/tasks/{task_id}/result").status_code == 409
    assert run_one(f.config, gateway_url=f.address, task_id=task_id, profile_id="selected-pi")["state"] == "idle"
    assert len(f.upstream.requests) == 1 and not f.legacy_requests


def test_selected_worker_cancel_after_dispatch_withholds_reply_and_does_not_resend(selected_pi):
    f = selected_pi
    task_id, _ = _submit(f)
    cancellation_status = []

    def cancel():
        task = f.client.get(f"/tasks/{task_id}").json()
        response = f.client.post(f"/tasks/{task_id}/cancel", json={"reason": "SYNTHETIC active-run cancellation"},
                                 headers=_headers(task["revision"]))
        cancellation_status.append(response.status_code)

    f.upstream.after_request = cancel
    handled = run_one(f.config, gateway_url=f.address, task_id=task_id, profile_id="selected-pi")
    assert cancellation_status == [200] and handled["state"] == "cancelled"
    original, recorded = _call_receipts(f, task_id)
    run = f.db.get(AgentRun, original.run_id)
    assert run.state == "cancelled" and original.state == recorded.state == "received"
    assert not f.db.scalar(select(QueryAnswerSnapshot.id).where(QueryAnswerSnapshot.task_id == task_id))
    assert not f.db.scalar(select(RuntimeJournal.id).where(RuntimeJournal.run_id == run.id))
    assert run_one(f.config, gateway_url=f.address, task_id=task_id, profile_id="selected-pi")["state"] == "idle"
    assert len(f.upstream.requests) == 1 and not f.legacy_requests
