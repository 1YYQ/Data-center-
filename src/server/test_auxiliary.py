"""Checked auxiliary reference plans and the installed SDK on a loopback gate."""

from copy import deepcopy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from threading import Thread
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from . import runtime_gate as gate
from .auxiliary import (_payload, _plan_from_receipt, auxiliary_dependency_digest, build_auxiliary_context,
                        current_auxiliary_view, run_auxiliary)
from .models import QueryTask, TaskArtifact
from .runtime_models import ModelCallAdmission, ModelInputProjection
from .service_models import ServiceProfile
from .service_use_models import ServiceSelection, ServiceTaskSelection, ServiceUseCall
from .test_pi_service_use import CREDENTIAL_ENV, FIXTURE_KEY, MODELS, _activate, _select
from .test_services import fixture_db
from .test_worker import local_stack
from .testing_service_use import attest_synthetic_input
from .worker import runtime_artifacts


def _business_data():
    return {"task_input": {"query": "SYNTHETIC-1 startup", "model": "SYNTHETIC-1"},
            "answer_contract": {"coverage": {"items": [{"item_ref": "todo-1", "kind": "maintenance", "label": "Check interval"}],
                                            "missing_item_refs": ["todo-1"]},
                                "support_groups": [{"id": "group-1", "predecessor_refs": [],
                                                    "members": [{"evidence_ref": "source-1", "role": "condition"}]},
                                                   {"id": "group-2", "predecessor_refs": ["group-1"],
                                                    "members": [{"evidence_ref": "source-2", "role": "warning"}]}]},
            "authorized_sources": [{"evidence_id": "source-1", "text": "SYNTHETIC prerequisite text"},
                                   {"evidence_id": "source-2", "text": "SYNTHETIC essential warning"},
                                   {"evidence_id": "outside", "text": "SYNTHETIC irrelevant fragment"}],
            "confirmed_facts": [{"id": "fact-1", "value": {"model": "SYNTHETIC-1"}}], "pending_items": [],
            "task_warnings": ["SYNTHETIC requires verification"], "completed_tool_results": [{"data": "excluded tool state"}]}


def _plan(operation):
    return {"schema": "auxiliary-plan/1", "operation": operation, "group_refs": ["group-1", "group-2"],
            "evidence_refs": ["source-1", "source-2"], "fact_refs": ["fact-1"], "pending_refs": ["todo-1"]}


def _stream(plan, model="auxiliary-synthetic"):
    candidate = json.dumps(plan)
    base = {"id": "synthetic-auxiliary", "object": "chat.completion.chunk", "created": 1, "model": model}
    events = ({**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": candidate[:29]}, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {"content": candidate[29:]}, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
              {**base, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 30, "total_tokens": 130}})
    return "".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n"


def test_auxiliary_plan_requires_all_checked_references_and_current_dependencies():
    data = _business_data()
    deps = {"owner": {"id": "employee"}, "task": {"execution_revision": 1}, "profile": "a",
            "grants": [{"id": "grant"}], "knowledge_bases": [], "sources": [{"id": "source-1"}], "binding_epoch": 1}
    proof = auxiliary_dependency_digest(data, deps)
    data["completed_tool_results"].append({"data": "unrelated tool progress"})
    assert auxiliary_dependency_digest(data, {**deps, "binding_epoch": 3}) == proof
    for key, value in (("profile", "b"), ("grants", []), ("sources", [])):
        assert auxiliary_dependency_digest(data, {**deps, key: value}) != proof
    changed = deepcopy(data)
    changed["authorized_sources"][1]["text"] = "SYNTHETIC warning changed"
    changed["pending_items"].append({"id": "new-request"})
    assert auxiliary_dependency_digest(changed, deps) != proof
    summary = build_auxiliary_context("summary", data)
    compaction = build_auxiliary_context("compaction", data)
    assert summary["systemPrompt"] != compaction["systemPrompt"]
    assert "SYNTHETIC essential warning" in json.dumps(summary)
    assert "SYNTHETIC essential warning" not in json.dumps(compaction)
    assert "SYNTHETIC irrelevant fragment" not in json.dumps(summary)
    assert "excluded tool state" not in json.dumps(compaction)
    payload = _payload(summary)
    plan = _plan("summary")
    assert _plan_from_receipt(_stream(plan), payload) == plan
    for changes in ({"evidence_refs": ["source-1"]}, {"group_refs": ["group-2", "group-1"]}, {"fact_refs": ["unconfirmed"]},
                    {"pending_refs": []}, {"summary": "Unverified prose"}, {"operation": "answer"}):
        with pytest.raises(ValueError):
            _plan_from_receipt(_stream({**plan, **changes}), payload)


@pytest.fixture
def auxiliary_upstream(monkeypatch):
    seen = []

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path != "/v1/chat/completions" or self.headers.get("Authorization") != "Bearer " + FIXTURE_KEY:
                self.send_error(403)
                return
            if not data.get("stream"):
                self.reply({"object": "chat.completion", "model": data["model"],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 1, "total_tokens": 11},
                    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "SYNTHETIC OK"}}]})
                return
            assert not self.headers.get("X-Runtime-Token") and not data.get("tools")
            payload = json.loads(data["messages"][1]["content"][0]["text"])
            operation = payload["operation"]
            assert len(data["messages"]) == 2
            assert data["messages"][0]["content"] == build_auxiliary_context(operation, _business_data())["systemPrompt"]
            assert ("SYNTHETIC essential warning" in json.dumps(payload)) == (operation == "summary")
            seen.append(operation)
            self.reply(_stream(_plan(operation), data["model"]), "text/event-stream")

        def reply(self, data, media="application/json"):
            raw = (json.dumps(data) if media == "application/json" else data).encode()
            self.send_response(200)
            self.send_header("Content-Type", media)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    with ThreadingHTTPServer(("127.0.0.1", 0), Provider) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setenv(CREDENTIAL_ENV, FIXTURE_KEY)
        monkeypatch.setenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", json.dumps({"pi_test": {
            "port": server.server_port, "credential_env": CREDENTIAL_ENV, "allowed_models": [MODELS[0]]}}))
        try:
            yield seen
        finally:
            server.shutdown()
            thread.join(timeout=4)


def test_real_sdk_auxiliary_gate_binds_each_private_candidate_and_revalidates(auxiliary_upstream, local_stack, monkeypatch):
    db, config, client, address, employee, admin, kb, _, _, legacy_requests, _ = local_stack
    factory = sessionmaker(bind=db.get_bind(), expire_on_commit=False)
    artifacts = runtime_artifacts()
    manifest = artifacts["manifest"]
    runtime = gate.create_local_profile(db, artifact_manifest_hash=artifacts["artifact_manifest_hash"],
        adapter_fingerprint=artifacts["adapter_fingerprint"], artifact_manifest=manifest, profile_id="auxiliary-test")
    service = ServiceProfile(kind="generation", label="SYNTHETIC auxiliary", created_by=admin.id)
    choices = [ServiceSelection(purpose="query." + name) for name in ("answer", "summary", "compaction")]
    db.add_all([service, *choices])
    db.flush()
    revision = _activate(db, service, admin.id, MODELS[0])
    for choice in choices:
        _select(db, admin.id, choice, "select", revision)
    response = client.post("/knowledge/queries", json={"query": "DEMO-1 startup", "model": "DEMO-1",
        "knowledge_base_ids": [kb.id], "service_mode": "selected_service"}, headers={"Idempotency-Key": uuid4().hex})
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    run, token = gate.open_run(db, task_id, profile_id=runtime.id)
    original = gate._assemble
    data = _business_data()

    def assemble(session, run, task, owner, profile, operation, purpose, **kwargs):
        # Simulate the already checked child evidence shape at the projection boundary.
        # Task authority, service selection, native wire, receipts and attachments are real.
        context, dependencies = original(session, run, task, owner, profile, "answer", purpose, **kwargs)
        if operation in ("summary", "compaction"):
            context = gate.canonical_json(build_auxiliary_context(operation, data))
            dependencies["auxiliary_base"] = auxiliary_dependency_digest(data, dependencies)
        return context, dependencies

    monkeypatch.setattr(gate, "_assemble", assemble)
    task = db.get(QueryTask, task_id)
    pins = db.get(ServiceTaskSelection, task.id).pin_ids
    assert len(set(pins.values())) == 3
    proofs = {operation: attest_synthetic_input(db, employee.id, "query." + operation, "task:" + task.id,
        gate.canonical_json(build_auxiliary_context(operation, data)).encode()) for operation in ("summary", "compaction")}
    db.commit()

    views = {operation: run_auxiliary(factory, config, run.id, token, address, artifacts["node"], manifest, operation=operation)
             for operation in ("summary", "compaction")}
    assert auxiliary_upstream == ["summary", "compaction"] and not legacy_requests
    assert all(views.values())
    db.expire_all()
    for operation, view in views.items():
        artifact = db.get(TaskArtifact, view["artifact_id"])
        admission = db.scalar(select(ModelCallAdmission).join(ModelInputProjection,
            ModelCallAdmission.projection_id == ModelInputProjection.id).where(ModelCallAdmission.run_id == run.id,
            ModelInputProjection.operation == operation))
        receipt = db.scalar(select(ServiceUseCall).where(ServiceUseCall.request_key == admission.id))
        assert artifact.kind == operation and artifact.validation_status == "verified"
        assert receipt.pin_id == pins["query." + operation] and receipt.state == "received"
    _, dependencies = assemble(db, run, task, employee, runtime, "summary", "answer")
    assert current_auxiliary_view(db, task, data, dependencies, settings=config, profile=runtime) == views
    proofs["summary"].revoked = True
    db.commit()
    assert current_auxiliary_view(db, task, data, dependencies, settings=config, profile=runtime) == {"compaction": views["compaction"]}
    data["pending_items"] = [{"id": "new-task-input"}]
    _, dependencies = assemble(db, run, task, employee, runtime, "summary", "answer")
    assert not current_auxiliary_view(db, task, data, dependencies, settings=config, profile=runtime)
