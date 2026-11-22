"""Query recovery with the installed Pi SDK, isolated PG and loopback generation."""

from contextlib import contextmanager
from functools import partial
import gc
from hashlib import sha256
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from threading import Thread
import time
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4
from weakref import ref

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from . import local_bge
from .execution_models import QueryAnswerSnapshot
from .indexing import current_child_manifest
from .models import ContentBlock, DocumentVersion, IndexGeneration, KnowledgeBase, QueryTask, TaskAttempt, TaskOutbox, TaskStep, User
from .runtime_gate import create_local_profile
from .runtime_models import AgentRun, AgentSessionBinding, ModelCallAdmission, RuntimeProfile, TaskBudget
from .service_admission import CHAT_COMPLETIONS
from .service_models import ServiceRevision
from .services import SafeError
from .test_child_answer_pipeline import MODEL, PRESSURE, QUERY, STEP_ONE, STEP_TWO, WARNING, child_answer_pipeline, recovery_pg
from .test_execution import _headers
from .worker import run_one


PAID_ENV = ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DATACENTER_BAILIAN_SERVICE_ENDPOINT")


@contextmanager
def _recovery_proxy(origin_port):
    proxy = SimpleNamespace(mode="forward", requests=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if (self.path != CHAT_COMPLETIONS or not 0 < size <= 16384
                    or self.headers.get("Authorization") is not None):
                self.send_error(400)
                return
            request = self.rfile.read(size)
            mode = proxy.mode
            trace = {"mode": mode, "request_sha256": sha256(request).hexdigest(), "request_bytes": len(request),
                     "origin_reached": False, "http_status": 503}
            if mode == "reject":
                proxy.requests.append(trace)
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            connection = http.client.HTTPConnection("127.0.0.1", origin_port, timeout=5)
            try:
                connection.request("POST", CHAT_COMPLETIONS, body=request, headers={"Content-Type": "application/json"})
                response = connection.getresponse()
                body = response.read(1048577)
                content_type = response.getheader("Content-Type")
                trace.update(origin_reached=True, http_status=response.status, origin_response_bytes=len(body),
                             origin_complete_sse=body.endswith(b"data: [DONE]\n\n"))
            except (OSError, http.client.HTTPException) as error:
                trace.update(proxy_error=type(error).__name__)
                proxy.requests.append(trace)
                self.send_error(502)
                return
            finally:
                connection.close()
            if (mode == "truncate" and trace["origin_complete_sse"] and response.status == 200
                    and content_type == "text/event-stream"):
                head = body.split(b"\n\n", 1)[0] + b"\n\n"
                # Send one real SSE event as the start of an incomplete HTTP chunk.
                # An EOF inside the declared chunk must be visible to the HTTP client.
                trace.update(sent_response_bytes=len(head), truncated=True)
                proxy.requests.append(trace)
                self.send_response(200)
                self.send_header("Content-Type", content_type)
                self.send_header("Transfer-Encoding", "chunked")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(f"{len(body):x}\r\n".encode("ascii") + head)
                self.close_connection = True
                return
            proxy.requests.append(trace)
            self.send_response(response.status)
            if content_type:
                self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as listener:
        thread = Thread(target=listener.serve_forever, daemon=True)
        thread.start()
        proxy.port = listener.server_port
        try:
            yield proxy
        finally:
            listener.shutdown()
            thread.join(5)


def _isolated_fixture(f):
    address = urlsplit(f.gateway)
    assert address.scheme == "http" and address.hostname == "127.0.0.1" and address.port
    assert not (address.username or address.password or address.query or address.fragment or address.path not in ("", "/"))
    endpoint = make_url(f.config.database_url)
    assert (endpoint.drivername, endpoint.host, endpoint.port) == ("postgresql+psycopg", "127.0.0.1", 55440)
    assert re.fullmatch(r"dc_local_creation_recovery_[a-f0-9]{20}", endpoint.database or "") and not endpoint.query
    assert f.app.state.engine.url == endpoint and f.config.query_service_mode == "local"
    storage = f.config.storage_dir.resolve()
    assert storage.name == "storage" and storage.parent.name == endpoint.database
    assert type(f.model.port) is int and 1 <= f.model.port <= 65535 and not f.model.bailian_fixture
    assert f.model.mode == "good"
    assert not any(os.environ.get(name) for name in PAID_ENV)
    return endpoint.database


def _submit(client, f):
    response = client.post("/knowledge/queries", headers=_headers(),
        json={"query": QUERY, "model": MODEL, "knowledge_base_ids": [f.kb_id]})
    assert response.status_code == 202, response.text
    return response.json()["task_id"]


def _task_view(client, task_id):
    response = client.get(f"/tasks/{task_id}")
    assert response.status_code == 200, response.text
    view = response.json()
    assert view["id"] == task_id and response.headers["etag"] == f'"{view["revision"]}"'
    return view


def _ledger(f, task_id):
    with f.app.state.session_factory() as db:
        task = db.get(QueryTask, task_id)
        assert task.owner_user_id == f.actor_id and task.input_snapshot.get("service_mode", "local") == "local"
        budget = db.get(TaskBudget, task_id)
        binding = db.scalar(select(AgentSessionBinding).where(AgentSessionBinding.task_id == task_id))
        step = db.scalar(select(TaskStep).where(TaskStep.task_id == task_id, TaskStep.step_key == "query"))
        calls = [{"id": call.id, "run_id": call.run_id, "execution_revision": revision, "state": call.state,
                  "http_status": call.response_status, "error_code": call.error_code, "reserved_tokens": call.reserved_tokens,
                  "usage_state": call.usage_state, "usage": call.usage_json, "has_response": call.response_body is not None,
                  "payload_sha256": call.payload_digest}
                 for call, revision in db.execute(select(ModelCallAdmission, AgentRun.execution_revision)
                     .join(AgentRun, AgentRun.id == ModelCallAdmission.run_id).where(ModelCallAdmission.task_id == task_id)
                     .order_by(ModelCallAdmission.created_at, ModelCallAdmission.id))]
        snapshots = [{"id": row.id, "execution_revision": row.execution_revision, "evidence_ids": row.evidence_ids}
                     for row in db.scalars(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task_id)
                         .order_by(QueryAnswerSnapshot.execution_revision))]
        attempts = [{"id": row.id, "number": row.attempt_no, "state": row.status, "error_code": row.error_code}
                    for row in db.scalars(select(TaskAttempt).where(TaskAttempt.step_id == step.id).order_by(TaskAttempt.attempt_no))]
        outbox = [{"id": row.id, "number": row.dispatch_no, "state": row.status, "publish_attempts": row.publish_attempts}
                  for row in db.scalars(select(TaskOutbox).where(TaskOutbox.step_id == step.id).order_by(TaskOutbox.dispatch_no))]
        limits = ({name: getattr(budget, name) for name in ("max_calls", "max_reserved_tokens", "reserved_calls",
                                                           "reserved_tokens", "revision")} if budget else None)
        return {"task_id": task.id, "conversation_id": task.conversation_id, "execution_revision": task.execution_revision,
                "status": task.status, "step_id": step.id, "step_state": step.status, "binding_id": binding.id if binding else None,
                "budget": limits, "calls": calls, "snapshots": snapshots, "attempts": attempts, "outbox": outbox}


def _formal_answer(client, f, task_id, ledger):
    result = client.get(f"/tasks/{task_id}/result")
    evidence = client.get(f"/tasks/{task_id}/evidence")
    original = client.get(f"/documents/{f.document_id}/versions/{f.version_id}/file")
    assert (result.status_code, evidence.status_code, original.status_code) == (200, 200, 200)
    data, items = result.json(), evidence.json()["items"]
    snapshot, = ledger["snapshots"]
    digest = sha256(original.content).hexdigest()
    assert original.content == f.original
    assert data["result_status"] == "formal" and data["answer_snapshot_id"] == snapshot["id"]
    assert snapshot["execution_revision"] == ledger["execution_revision"] and len(items) == 6
    assert {item["id"] for item in items} == set(snapshot["evidence_ids"]) == {item["id"] for item in data["evidence"]}
    assert all(item["document_version_id"] == f.version_id and item["locator"]["file_page"] == 1
               and item["locator"]["source_sha256"] == digest for item in items)
    assert all(literal in data["answer"] for literal in (STEP_ONE, STEP_TWO, WARNING, PRESSURE))
    assert data["answer"].index(WARNING) < data["answer"].index(STEP_TWO)
    return {"formal_snapshot_id": snapshot["id"], "evidence_ids": [item["id"] for item in items],
            "source_version_id": f.version_id, "original_sha256": digest, "original_bytes": len(original.content)}


def _known_recovery(client, f, profile_id, proxy, report):
    proxy.mode = "reject"
    offset = len(proxy.requests)
    task_id = report["task_id"] = _submit(client, f)
    report["failure_outcome"] = run_one(f.config, gateway_url=f.gateway, profile_id=profile_id, task_id=task_id)
    failed = report["failed_ledger"] = _ledger(f, task_id)
    view = _task_view(client, task_id)
    assert report["failure_outcome"] == {"task_id": task_id, "state": "blocked", "error_code": "RUNTIME_MODEL_FAILED"}
    assert view["retry_eligible"] is True and failed["execution_revision"] == 1 and not failed["snapshots"]
    failure, = failed["calls"]
    assert (failure["state"], failure["http_status"], failure["has_response"]) == ("failed", 503, False)
    assert failed["budget"]["reserved_calls"] == 1 and failed["budget"]["reserved_tokens"] == failure["reserved_tokens"]
    assert len(proxy.requests) == offset + 1 and proxy.requests[-1]["request_sha256"] == failure["payload_sha256"]
    assert not proxy.requests[-1]["origin_reached"]

    proxy.mode = "forward"
    retry = client.post(f"/tasks/{task_id}/retry", headers=_headers(view["revision"]),
                        json={"reason": "SYNTHETIC loopback HTTP 503 recovered"})
    report["retry_http"] = retry.status_code
    assert retry.status_code == 202 and retry.json() == {"task_id": task_id, "status": "pending"}, retry.text
    pending = report["pending_ledger"] = _ledger(f, task_id)
    assert pending["execution_revision"] == 2 and pending["conversation_id"] == failed["conversation_id"]
    assert _task_view(client, task_id)["effective_input"] == view["effective_input"]
    assert pending["budget"] == failed["budget"] and pending["calls"] == failed["calls"]
    report["recovery_outcome"] = run_one(f.config, gateway_url=f.gateway, profile_id=profile_id, task_id=task_id)
    settled = report["recovered_ledger"] = _ledger(f, task_id)
    assert report["recovery_outcome"] == {"task_id": task_id, "state": "succeeded", "error_code": None}
    assert settled["step_id"] == failed["step_id"] and settled["binding_id"] == failed["binding_id"]
    assert settled["calls"][0] == failure and len(settled["calls"]) == 4
    assert all((row["state"], row["http_status"], row["execution_revision"]) == ("received", 200, 2)
               for row in settled["calls"][1:])
    assert settled["budget"]["reserved_calls"] == 4
    assert settled["budget"]["reserved_tokens"] == sum(row["reserved_tokens"] for row in settled["calls"])
    assert [row["request_sha256"] for row in proxy.requests[offset:]] == [row["payload_sha256"] for row in settled["calls"]]
    report["answer"] = _formal_answer(client, f, task_id, settled)


def _unknown_remains_fenced(client, f, profile_id, proxy, report):
    proxy.mode = "truncate"
    offset = len(proxy.requests)
    task_id = report["task_id"] = _submit(client, f)
    report["outcome"] = run_one(f.config, gateway_url=f.gateway, profile_id=profile_id, task_id=task_id)
    unknown = report["ledger"] = _ledger(f, task_id)
    view = _task_view(client, task_id)
    assert report["outcome"]["state"] == "reconciliation_required"
    assert view["retry_eligible"] is False and unknown["execution_revision"] == 1 and not unknown["snapshots"]
    call, = unknown["calls"]
    assert (call["state"], call["http_status"], call["error_code"]) == ("unknown", 200, "RUNTIME_UPSTREAM_OUTCOME_UNKNOWN")
    assert call["usage_state"] == "unknown" and call["usage"] is None and not call["has_response"]
    assert unknown["budget"]["reserved_calls"] == 1 and unknown["budget"]["reserved_tokens"] == call["reserved_tokens"]
    assert len(proxy.requests) == offset + 1 and proxy.requests[-1]["request_sha256"] == call["payload_sha256"]
    assert proxy.requests[-1]["truncated"] and proxy.requests[-1]["origin_reached"]
    proxy.mode = "forward"
    report["retry_denials"] = []
    for _ in range(2):
        retry = client.post(f"/tasks/{task_id}/retry", headers=_headers(view["revision"]),
                            json={"reason": "SYNTHETIC retry after genuinely incomplete SSE transfer"})
        report["retry_denials"].append({"http": retry.status_code, "code": retry.json().get("code")})
    assert report["retry_denials"] == [{"http": 409, "code": "MODEL_RECONCILIATION_REQUIRED"}] * 2
    assert run_one(f.config, gateway_url=f.gateway, profile_id=profile_id, task_id=task_id) == {"state": "idle"}
    report["after_retry_ledger"] = _ledger(f, task_id)
    assert report["after_retry_ledger"] == unknown and len(proxy.requests) == offset + 1
    report["result_http"] = client.get(f"/tasks/{task_id}/result").status_code
    assert report["result_http"] == 409


_CONSUMER = """
import json
import os
from pathlib import Path
import sys
import time
from src.server.config import Settings
from src.server.worker import run_one

inputs = json.load(sys.stdin)
config = Settings(database_url=inputs['database_url'], storage_dir=Path(inputs['storage_dir']), query_service_mode='local')

if inputs['measure_bge']:
    import psutil
    from src.server import local_bge

    def model_state():
        return {'rss_bytes': psutil.Process().memory_info().rss,
                'available_bytes': psutil.virtual_memory().available,
                'model_cache_entries': local_bge._load.cache_info().currsize}

    before_preparation = model_state()
    preparation_started = time.monotonic()
    endpoint = local_bge.read_model_endpoint()
    if not isinstance(endpoint, local_bge.LocalBgeEndpoint):
        raise RuntimeError('Consumer requires the approved local embedding endpoint')
    # Match pilot.serve: integrity-check the offline weights before claiming work.
    endpoint.preload()
    preparation_seconds = time.monotonic() - preparation_started
    before_claim = model_state()
query_started = time.monotonic()
outcome = run_one(config, gateway_url=inputs['gateway'], profile_id=inputs['profile_id'])
query_seconds = time.monotonic() - query_started
result = {'pid': os.getpid(), 'outcome': outcome}
if inputs['measure_bge']:
    result['bge'] = {'before_preparation': before_preparation, 'before_claim': before_claim, 'before_exit': model_state(),
                     'preparation_seconds': preparation_seconds, 'query_seconds': query_seconds}
print(json.dumps(result), flush=True)
"""


def _consumer(f, profile_id, *, measure_bge=False):
    env = {key: value for key, value in os.environ.items() if key not in PAID_ENV}
    inputs = {"database_url": f.config.database_url, "storage_dir": str(f.config.storage_dir.resolve()),
              "gateway": f.gateway, "profile_id": profile_id, "measure_bge": measure_bge}
    started = time.monotonic()
    # Native model progress on Windows may use the console code page on stderr.
    # Keep diagnostics as bytes; only the JSON stdout is the UTF-8 protocol.
    result = subprocess.run([sys.executable, "-c", _CONSUMER], cwd=Path(__file__).resolve().parents[2], env=env,
                            input=json.dumps(inputs).encode("utf-8"), capture_output=True, timeout=100)
    assert result.returncode == 0, f"isolated consumer exited with status {result.returncode}"
    return {**json.loads(result.stdout.decode("utf-8").splitlines()[-1]), "exit_code": result.returncode,
            "duration_seconds": time.monotonic() - started, "stderr_bytes": len(result.stderr),
            "stderr_sha256": sha256(result.stderr).hexdigest()}


@contextmanager
def _handoff_bge_to_consumer(report):
    import psutil

    def parent_state():
        return {"rss_bytes": psutil.Process().memory_info().rss, "available_bytes": psutil.virtual_memory().available,
                "model_cache_entries": local_bge._load.cache_info().currsize}

    # Guard the in-process loader until the separate one-shot consumer exits.
    # The API remains live, but it does not embed the restarted worker's query.
    with local_bge._MODEL_LOCK:
        before = report["parent_before_release"] = parent_state()
        model_type = sys.modules["sentence_transformers"].SentenceTransformer
        models = [ref(item) for item in gc.get_objects() if type(item) is model_type]
        report["parent_model_instances_before_release"] = len(models)
        assert before["model_cache_entries"] == len(models) == 1
        local_bge._load.cache_clear()
        gc.collect()
        after = report["parent_after_release"] = parent_state()
        report["parent_model_instances_retained"] = sum(item() is not None for item in models)
        assert after["model_cache_entries"] == report["parent_model_instances_retained"] == 0
        # Use the measured size of the former loaded process as a conservative
        # estimate for the fresh process and retain the run's normal headroom.
        headroom = int(os.environ.get("DATACENTER_ENDURANCE_FREE_MIB", "512")) * 1024 ** 2
        required = report["minimum_available_before_restart_bytes"] = before["rss_bytes"] + headroom
        assert after["available_bytes"] >= required, "Insufficient memory for one BGE consumer after releasing the parent model"
        try:
            yield
        finally:
            report["parent_after_consumer_exit"] = parent_state()
            assert report["parent_after_consumer_exit"]["model_cache_entries"] == 0


def test_consumer_keeps_native_stderr_bytes_outside_json_protocol(monkeypatch, tmp_path):
    monkeypatch.setattr(sys.modules[__name__], "_CONSUMER", """
import json, sys
json.load(sys.stdin)
sys.stderr.buffer.write(bytes([0xa8]))
print(json.dumps({'outcome': {'state': 'idle'}}))
""")
    fixture = SimpleNamespace(config=SimpleNamespace(database_url="unused", storage_dir=tmp_path), gateway="unused")
    result = _consumer(fixture, "unused")
    assert result["exit_code"] == 0 and result["outcome"] == {"state": "idle"}
    assert result["stderr_bytes"] == 1 and result["stderr_sha256"] == sha256(bytes([0xa8])).hexdigest()


def _restart_consumer(client, f, profile_id, proxy, report, *, real_bge=False):
    report["stopped_consumer"] = _consumer(f, profile_id)
    assert report["stopped_consumer"]["outcome"] == {"state": "idle"}
    offset = len(proxy.requests)
    task_id = report["task_id"] = _submit(client, f)
    pending = report["pending_ledger"] = _ledger(f, task_id)
    assert pending["status"] == pending["step_state"] == "pending"
    assert not pending["calls"] and not pending["attempts"] and not pending["snapshots"]
    assert pending["budget"] is None or pending["budget"]["reserved_calls"] == 0
    original_dispatch, = pending["outbox"]
    assert original_dispatch["state"] == "pending" and original_dispatch["publish_attempts"] == 0
    if real_bge:
        handoff = report["bge_handoff"] = {}
        with _handoff_bge_to_consumer(handoff):
            report["restarted_consumer"] = _consumer(f, profile_id, measure_bge=True)
        child_bge = report["restarted_consumer"]["bge"]
        assert child_bge["before_preparation"]["model_cache_entries"] == 0
        assert child_bge["before_claim"]["model_cache_entries"] == 1
        assert child_bge["before_exit"]["model_cache_entries"] == 1
    else:
        report["restarted_consumer"] = _consumer(f, profile_id)
    assert report["restarted_consumer"]["pid"] != report["stopped_consumer"]["pid"]
    assert report["restarted_consumer"]["outcome"] == {"task_id": task_id, "state": "succeeded", "error_code": None}
    settled = report["settled_ledger"] = _ledger(f, task_id)
    assert settled["execution_revision"] == pending["execution_revision"] == 1
    assert settled["step_id"] == pending["step_id"] and len(settled["attempts"]) == 1
    assert settled["outbox"] == [{**original_dispatch, "state": "published", "publish_attempts": 1}]
    assert len(settled["calls"]) == settled["budget"]["reserved_calls"] == 3
    assert settled["budget"]["reserved_tokens"] == sum(row["reserved_tokens"] for row in settled["calls"])
    assert all(row["state"] == "received" and row["http_status"] == 200 for row in settled["calls"])
    assert [row["request_sha256"] for row in proxy.requests[offset:]] == [row["payload_sha256"] for row in settled["calls"]]
    report["answer"] = _formal_answer(client, f, task_id, settled)


def _nonmatching_source_fence(f, report):
    def manifest():
        with f.app.state.session_factory() as db:
            kb, generation = db.get(KnowledgeBase, f.kb_id), db.get(IndexGeneration, f.generation_id)
            return current_child_manifest(db, kb, generation), kb.content_revision

    with f.app.state.session_factory() as db:
        generation = db.get(IndexGeneration, f.generation_id)
        before = generation.build_report["manifest"]
        revision = db.get(KnowledgeBase, f.kb_id).content_revision
        candidates = [source for source in before["sources"]
                      if MODEL not in db.get(DocumentVersion, source["version_id"]).model_scope]
    if not candidates:
        report["status"] = "no_nonmatching_source_in_fixture"
        return
    source = candidates[0]
    report.update(version_id=source["version_id"], generation_id=f.generation_id, content_revision_before=revision)
    with f.app.state.session_factory() as writer:
        block = writer.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == source["run_id"],
            ContentBlock.structure_role == "leaf").order_by(ContentBlock.ordinal, ContentBlock.id)).first()
        block_id, original = block.id, block.text_content
        block.text_content += " SYNTHETIC temporary projection fault"
        writer.commit()
    try:
        with f.app.state.session_factory() as db:
            observed_revision = db.get(KnowledgeBase, f.kb_id).content_revision
            report["content_revision_during_fault"] = observed_revision
            assert observed_revision == revision
        with pytest.raises(SafeError) as error:
            manifest()
        report["rejected_with"] = error.value.code
        assert error.value.code == "INDEX_GENERATION_STALE"
    finally:
        with f.app.state.session_factory() as writer:
            block = writer.get(ContentBlock, block_id)
            assert block.text_content == original + " SYNTHETIC temporary projection fault"
            block.text_content = original
            writer.commit()
        report["fault_injection_cleanup_restored_exact_manifest"] = manifest() == (before, revision)
    assert report["fault_injection_cleanup_restored_exact_manifest"]
    report["status"] = "verified"


def run_recovery_scenarios(f, output_path):
    """Use the caller's generation and embedding service with a new local profile."""
    database = _isolated_fixture(f)
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {"scope": "Installed Pi SDK, real loopback HTTP fault injection, isolated PostgreSQL",
              "database": database, "generation_id": f.generation_id, "scenarios": {}, "passed": False}
    started, gateway_offset, error_offset = time.monotonic(), len(f.gateway_trace), len(f.model.errors)
    try:
        with _recovery_proxy(f.model.port) as proxy:
            report["loopback_requests"] = proxy.requests
            profile_id = "synthetic-endurance-recovery-" + uuid4().hex
            with f.app.state.session_factory() as db:
                embed = db.get(ServiceRevision, f.revision_id)
                report["embedding"] = {"revision": embed.id, "adapter": embed.adapter, "real_bge": embed.adapter == local_bge.ADAPTER}
                original = db.get(RuntimeProfile, ("local-pi", 1))
                profile = create_local_profile(db, profile_id=profile_id, upstream_port=proxy.port,
                    artifact_manifest_hash=original.artifact_manifest_hash, adapter_fingerprint=original.adapter_fingerprint,
                    artifact_manifest=original.configuration["artifact_manifest"],
                    synthetic_context_window=original.configuration["generation"]["context_window"],
                    max_calls=original.configuration["max_calls"], max_reserved_tokens=original.configuration["max_reserved_tokens"])
                assert profile.configuration["gateway_policy"] == original.configuration["gateway_policy"]
                report["profile"] = {"id": profile.id, "revision": profile.revision, "digest": profile.configuration_hash,
                                     "gateway_policy": profile.configuration["gateway_policy"]}
            with httpx.Client(base_url=f.gateway, cookies=dict(f.client.cookies), trust_env=False, timeout=35,
                              headers={"X-CSRF-Token": f.client.headers["X-CSRF-Token"]}) as client:
                restart = partial(_restart_consumer, real_bge=report["embedding"]["real_bge"])
                for name, verify in (("known_http_503", _known_recovery), ("incomplete_sse", _unknown_remains_fenced),
                                     ("idle_consumer_restart", restart)):
                    scenario = report["scenarios"][name] = {"passed": False}
                    verify(client, f, profile_id, proxy, scenario)
                    scenario["passed"] = True
            fence = report["scenarios"]["nonmatching_source_fence"] = {}
            _nonmatching_source_fence(f, fence)
            assert len(proxy.requests) == 8 and all("proxy_error" not in row for row in proxy.requests)
            assert len(f.model.errors) == error_offset, f.model.errors[error_offset:]
            report["passed"] = True
        return report
    finally:
        report["gateway_trace"] = f.gateway_trace[gateway_offset:]
        report["duration_seconds"] = time.monotonic() - started
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL") or
                    os.environ.get("DATACENTER_RUN_ENDURANCE_RECOVERY") != "1",
                    reason="Explicit local PostgreSQL and loopback endurance recovery opt-in required")
def test_endurance_recovery_on_isolated_fixture(request, monkeypatch, tmp_path):
    endpoint = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    assert (endpoint.drivername, endpoint.host, endpoint.port) == ("postgresql+psycopg", "127.0.0.1", 55440)
    assert re.fullmatch(r"dc_local_capacity_marker_[a-f0-9]{32}", endpoint.database or "") and not endpoint.query
    for name in PAID_ENV:
        monkeypatch.delenv(name, raising=False)
    f = request.getfixturevalue("child_answer_pipeline")
    output = Path(os.environ.get("DATACENTER_ENDURANCE_RECOVERY_OUTPUT", tmp_path / "recovery.json"))
    report = run_recovery_scenarios(f, output)
    assert report["passed"] and not report["embedding"]["real_bge"]
