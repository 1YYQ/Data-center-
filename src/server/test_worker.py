"""Real local HTTP + fixed Node SDK + durable worker; synthetic PDF only."""
import hashlib
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

import httpx
import pytest
import uvicorn
from sqlalchemy import select

from .app import create_app
from .models import AuthorizationGrant, BusinessApproval, ChunkSet, ContentBlock, DocumentVersion, ParseRun, QueryTask, TaskEvidence, User
from .runtime_models import AgentRun, ModelCallAdmission, TaskBudget
from .execution_models import QueryAnswerSnapshot, RuntimeJournal, ToolInvocation
from .runtime_gate import create_local_profile
from .security import hash_password
from .services import save_upload
from .test_services import _activate_fixture_index, _ready_fixture, fixture_db
from .testing_governance import seed_verified_approval, seed_verified_version
from .worker import run_one, runtime_artifacts
from .test_execution import execution_http, _create, _headers
from .test_runtime_gate import local_upstream, runtime_db

STARTUP = "DEMO-1 SYNTHETIC startup: connect power and check the status indicator."
WARNING = "WARNING: SYNTHETIC ONLY. Disconnect power before maintenance."


def synthetic_pdf():
    lines = [STARTUP, WARNING]
    stream = "BT /F1 10 Tf 40 740 Td " + " 0 -20 Td ".join(f"({line}) Tj" for line in lines) + " ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>", f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream".encode()]
    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for n, item in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{n} 0 obj\n".encode() + item + b"\nendobj\n")
    start = len(body)
    body.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode())
    for offset in offsets[1:]:
        body.extend(f"{offset:010} 00000 n \n".encode())
    body.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(body)


@pytest.fixture()
def local_stack(fixture_db):
    db, config = fixture_db
    owner, kb, document, _, _ = _ready_fixture(db, config)
    file = save_upload([synthetic_pdf()], "SYNTHETIC-DEMO-1.pdf", "application/pdf", owner, config)
    db.add(file)
    db.flush()
    version = DocumentVersion(document_id=document.id, version_no=2, source_file_id=file.id, created_by=owner.id,
        model_scope=["DEMO-1"], revision_label="SYNTHETIC-1")
    db.add(version)
    db.flush()
    run = ParseRun(document_version_id=version.id, origin="manual", status="needs_review")
    db.add(run)
    db.flush()
    for index, (text, kind) in enumerate(((STARTUP, "paragraph"), (WARNING, "warning")), 1):
        db.add(ContentBlock(parse_run_id=run.id, ordinal=index, kind=kind, text_content=text, locator={"file_page": 1, "section": f"synthetic paragraph {index}"}))
    chunk = ChunkSet(parse_run_id=run.id)
    db.add(chunk)
    db.flush()
    seed_verified_version(db, owner, document, version, run, chunk.id)
    db.flush()
    _activate_fixture_index(db, kb, owner)
    employee = User(username="synthetic-worker-employee", display_name="Synthetic employee", status="active", credential_state="active",
        credential_hash=hash_password("synthetic employee password"), roles=["employee"])
    administrator = User(username="synthetic-worker-admin", display_name="Synthetic system administrator", status="active", credential_state="active",
        credential_hash=hash_password("synthetic admin password"), roles=["system_admin"])
    db.add_all([employee, administrator])
    db.flush()
    for operation in ("document.read", "knowledge.query"):
        approval = BusinessApproval(applicant=employee.id, recorded_by=owner.id, scope={"knowledge_base_id": kb.id, "document_id": None,
            "level": document.access_level, "category": document.category, "need_class": document.need_class}, operations=[operation])
        db.add(approval)
        db.flush()
        seed_verified_approval(db, approval)
        db.add(AuthorizationGrant(subject_user_id=employee.id, knowledge_base_id=kb.id, operation=operation,
            level=document.access_level, category=document.category, need_class=document.need_class,
            approval_id=approval.id, granted_by=owner.id, valid_until=approval.valid_until))
    db.commit()
    requests = []
    mode = {"value": "tools"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append({"payload": payload, "has_private_token": bool(self.headers.get("X-Runtime-Token"))})
            if mode["value"] == "failure":
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            content = payload["messages"][-1]["content"]
            if isinstance(content, list):
                content = "".join(x.get("text", "") for x in content)
            data = json.loads(content)
            searched = any(x["tool_name"] == "dc_evidence_search" for x in data["completed_tool_results"])
            delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "synthetic-search", "type": "function",
                "function": {"name": "dc_evidence_search", "arguments": "{}"}}]} if not searched else {"role": "assistant", "content": "SYNTHETIC MODEL PROSE IS NOT THE BUSINESS ANSWER"}
            base = {"id": "synthetic-http", "object": "chat.completion.chunk", "created": 1, "model": "pi-local-synthetic"}
            chunks = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop" if searched else "tool_calls"}]},
                {**base, "choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}]
            body = ("".join("data: " + json.dumps(x) + "\n\n" for x in chunks) + "data: [DONE]\n\n").encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()
    artifact = runtime_artifacts()
    create_local_profile(db, upstream_port=upstream.server_port, artifact_manifest_hash=artifact["artifact_manifest_hash"],
                         adapter_fingerprint=artifact["adapter_fingerprint"], artifact_manifest=artifact["manifest"])
    app = create_app(config)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    server_thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    server_thread.start()
    deadline = time.monotonic() + 10
    while not server.started and server_thread.is_alive() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.started
    address = f"http://127.0.0.1:{listener.getsockname()[1]}/"
    with httpx.Client(base_url=address, timeout=10) as client:
        login = client.post("/auth/login", json={"username": employee.username, "password": "synthetic employee password"})
        assert login.status_code == 200, login.text
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        try:
            yield db, config, client, address, employee, administrator, kb, document, version, requests, mode
        finally:
            server.should_exit = True
            server_thread.join(10)
            app.state.engine.dispose()
            upstream.shutdown()
            upstream.server_close()
            upstream_thread.join(5)
            listener.close()


def submit(client, kb, model="DEMO-1"):
    result = client.post("/knowledge/queries", json={"query": "DEMO-1 startup", "model": model, "knowledge_base_ids": [kb.id]}, headers={"Idempotency-Key": uuid4().hex})
    assert result.status_code == 202, result.text
    return result.json()["task_id"]


def test_real_sdk_worker_query_preserves_pdf_warning_and_enforces_final_answer(local_stack):
    db, config, client, address, employee, admin, kb, document, version, requests, _ = local_stack
    task_id = submit(client, kb)
    assert client.get(f"/tasks/{task_id}").json()["status"] == "pending"
    assert not requests
    outcome = run_one(config, gateway_url=address)
    assert outcome["state"] == "succeeded", outcome
    response = client.get(f"/tasks/{task_id}/result")
    assert response.status_code == 200, response.text
    answer = response.json()
    assert STARTUP in answer["answer"] and WARNING in answer["answer"]
    assert "MODEL PROSE" not in answer["answer"]
    assert {x["version"]["id"] for x in answer["evidence"]} == {version.id}
    assert all(x["locator"]["file_page"] == 1 for x in answer["evidence"])
    assert len(requests) == 2 and not any(x["has_private_token"] for x in requests)
    assert "EXTERNAL WITNESS" not in json.dumps(requests)
    db.expire_all()
    calls = db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.task_id == task_id)).all()
    assert len(calls) == 2 and all(x.state == "received" for x in calls)
    assert db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task_id))
    assert db.scalar(select(RuntimeJournal).join(AgentRun, AgentRun.id == RuntimeJournal.run_id).where(AgentRun.task_id == task_id))
    assert len(db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == task_id)).all()) == 2
    file = client.get(f"/documents/{document.id}/versions/{version.id}/file")
    assert file.status_code == 200 and file.content == synthetic_pdf()
    assert file.headers["content-disposition"].startswith("inline")
    assert run_one(config, gateway_url=address)["state"] == "idle"
    assert len(requests) == 2
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == employee.id, AuthorizationGrant.operation == "document.read"))
    grant.revoked_at = __import__("src.server.models", fromlist=["utc_now"]).utc_now()
    db.commit()
    assert client.get(f"/tasks/{task_id}/result").status_code == 403
    assert client.get(f"/documents/{document.id}/versions/{version.id}/file").status_code == 404
    with httpx.Client(base_url=address) as system:
        assert system.post("/auth/login", json={"username": admin.username, "password": "synthetic admin password"}).status_code == 200
        assert system.get(f"/documents/{document.id}/versions/{version.id}/file").status_code == 404


def test_missing_model_resumes_original_task_through_real_worker(local_stack):
    db, config, client, address, _, _, kb, _, _, requests, _ = local_stack
    task_id = submit(client, kb, None)
    assert run_one(config, gateway_url=address)["state"] == "waiting_input"
    assert not requests
    request = client.get(f"/tasks/{task_id}/input-requests").json()["items"][0]
    result = client.post(f"/tasks/{task_id}/input-requests/{request['id']}/responses", json={"model": "DEMO-1"},
        headers={"Idempotency-Key": uuid4().hex, "If-Match": f'"{request["revision"]}"'})
    assert result.status_code == 200, result.text
    assert result.json()["task_id"] == task_id
    assert run_one(config, gateway_url=address)["state"] == "succeeded"
    db.expire_all()
    task = db.get(QueryTask, task_id)
    assert task.input_snapshot["model"] is None and task.effective_input["model"] == "DEMO-1" and task.execution_revision == 2
    assert client.get(f"/tasks/{task_id}/result").status_code == 200


def test_real_sdk_upstream_failure_remains_visible_and_is_not_retried(local_stack):
    db, config, client, address, _, _, kb, _, _, requests, mode = local_stack
    mode["value"] = "failure"
    task_id = submit(client, kb)
    outcome = run_one(config, gateway_url=address)
    assert outcome["state"] == "blocked" and outcome["error_code"] == "RUNTIME_MODEL_FAILED", outcome
    assert len(requests) == 1
    assert client.get(f"/tasks/{task_id}/result").status_code == 409
    assert run_one(config, gateway_url=address)["state"] == "idle"
    db.expire_all()
    assert not db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task_id))
    # The owner's explicit recovery stays on the same task and its old budget;
    # it never reuses or silently resends the failed physical model call.
    task = db.get(QueryTask, task_id)
    mode["value"] = "tools"
    response = client.post(f"/tasks/{task_id}/retry", json={"reason": "synthetic service recovered"},
        headers={"Idempotency-Key": uuid4().hex, "If-Match": f'"{task.revision}"'})
    assert response.status_code == 202 and response.json()["task_id"] == task_id
    assert run_one(config, gateway_url=address)["state"] == "succeeded"
    db.expire_all()
    assert len(requests) == 3
    assert db.get(TaskBudget, task_id).reserved_calls == 3
    journals = db.scalars(select(RuntimeJournal).join(AgentRun, AgentRun.id == RuntimeJournal.run_id).where(AgentRun.task_id == task_id)).all()
    # A new L3 execution revision gets a fresh SDK generation while retaining
    # the original model-call receipts and task budget.
    assert len(journals) == 2 and len({x.pi_session_id for x in journals}) == 2


def test_worker_http_cancel_after_selection_prevents_run_admission_and_node_start(execution_http, monkeypatch):
    from . import worker
    from .services import SafeError
    f = execution_http
    task = _create(f)
    observed = {"cancel_committed": False, "runs_admitted": 0, "node_started": 0}
    original_open = worker.open_run

    def inspect_artifacts_then_cancel():
        current = f.client.get(f"/tasks/{task.id}")
        response = f.client.post(f"/tasks/{task.id}/cancel", json={"reason": "SYNTHETIC cancellation after worker selection"},
                                 headers=_headers(current.json()["revision"]))
        assert response.status_code == 200 and response.json()["status"] == "cancelled", response.text
        observed["cancel_committed"] = True
        return {"artifact_manifest_hash": f.profile.artifact_manifest_hash,
                "adapter_fingerprint": f.profile.adapter_fingerprint, "node": "SYNTHETIC-NOT-LAUNCHED", "manifest": {}}

    def observe_admission(*args, **kwargs):
        result = original_open(*args, **kwargs)
        observed["runs_admitted"] += 1
        return result

    def forbid_node(*args, **kwargs):
        observed["node_started"] += 1
        raise SafeError("SYNTHETIC_NODE_FORBIDDEN", "The cancellation regression must never launch Node", 409)

    monkeypatch.setattr(worker, "runtime_artifacts", inspect_artifacts_then_cancel)
    monkeypatch.setattr(worker, "open_run", observe_admission)
    monkeypatch.setattr(worker, "_node_run", forbid_node)
    outcome = worker.run_one(f.app.state.settings, gateway_url="http://127.0.0.1:1/")
    current = f.db.get(QueryTask, task.id, populate_existing=True)
    rows = f.db.scalars(select(AgentRun).where(AgentRun.task_id == task.id)).all()
    assert observed["cancel_committed"] and observed["runs_admitted"] == 0 and observed["node_started"] == 0 and not rows and current.status == "cancelled", {
        **observed, "persisted_runs": len(rows), "final_task_status": current.status, "worker_outcome": outcome}


def test_worker_refuses_replaced_installed_bytes_before_admission(local_stack, monkeypatch):
    from . import worker
    from .runtime_models import RuntimeProfile
    db, config, client, address, _, _, kb, _, _, requests, _ = local_stack
    profile = db.get(RuntimeProfile, ("local-pi", 1))
    manifest = dict(profile.configuration["artifact_manifest"], installed_tree_sha256="e" * 64)
    monkeypatch.setattr(worker, "runtime_artifacts", lambda: {
        "artifact_manifest_hash": hashlib.sha256(worker.canonical(manifest).encode()).hexdigest(),
        "adapter_fingerprint": profile.adapter_fingerprint, "manifest": manifest})
    task_id = submit(client, kb)
    assert run_one(config, gateway_url=address, task_id=task_id)["error_code"] == "RUNTIME_PROFILE_REQUIRED"
    assert not db.scalar(select(AgentRun.id).where(AgentRun.task_id == task_id))
    assert not requests


def test_installed_tree_hashes_changed_bytes_of_same_length(tmp_path):
    from .worker import _installed_tree
    package = tmp_path / "package"
    package.mkdir()
    module = package / "index.js"
    module.write_bytes(b"export const flag = true; ")
    original_stat = module.stat()
    original = _installed_tree(tmp_path)
    module.write_bytes(b"export const flag = false;")
    import os
    os.utime(module, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert module.stat().st_size == original_stat.st_size
    assert _installed_tree(tmp_path) != original
