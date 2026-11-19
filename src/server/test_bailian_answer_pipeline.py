"""Opt-in real Bailian egress from an isolated, closed synthetic query fixture."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
import http.client
import json
import os
from threading import Event
import time
from urllib.parse import urlsplit

import certifi
from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select, text

from .answer_delivery import _candidate_origin
from .app import create_app
from .execution_models import QueryAnswerSnapshot, ToolInvocation
from .models import ChunkSet, DocumentVersion, FileObject, QueryTask, TaskEvidence, User
from .runtime_gate import (ADAPTER, _bailian_stream_call, _selected_model_body, authorize_model, close_run,
                           create_local_profile, dispatch_model, open_run, project_context)
from .runtime_models import AgentRun, ModelCallAdmission, ModelInputProjection, TaskBudget
from .service_admission import (BAILIAN_CHAT_PATH, BAILIAN_ENDPOINT_REF, BAILIAN_ENV, BAILIAN_MODEL,
                                BailianEndpoint, ProviderError, bailian_endpoint, digest, load_local_endpoints)
from .service_use_models import ServiceUseCall
from .services import SafeError
from .test_child_answer_pipeline import (MODEL, PRESSURE, QUERY, STEP_ONE, STEP_TWO, WARNING, _api_gateway,
                                         _model_response, child_answer_pipeline, recovery_pg)
from .test_embedding_service_use import _login
from .test_service_admission import _change, _expect, _headers, _preview, _profile, _test_run
from .test_service_use import select_purpose
from .testing_service_use import install_synthetic_answer_fixture
from .worker import run_one, runtime_artifacts


def test_bailian_endpoint_cannot_change_host_model_key_or_use_fake_dns(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-bailian-key")
    endpoint = bailian_endpoint(peer_address="39.96.213.166", tls_ca_file=certifi.where())
    assert endpoint.chat_path == BAILIAN_CHAT_PATH
    assert endpoint.generation_profiles()[BAILIAN_MODEL]["max_output_tokens"] == 2048
    for change in ({"hostname": "fixture.example"}, {"port": 8443}, {"allowed_models": ("qwen4-max",)},
                   {"credential_env": "DATACENTER_GENERATION_CANDIDATE_KEY"}, {"peer_address": "198.18.0.175"}):
        with pytest.raises(ProviderError):
            replace(endpoint, **change).validate()
    monkeypatch.setenv(BAILIAN_ENV, json.dumps({"peer_address": "39.96.213.166", "tls_ca_file": certifi.where(),
                                             "chat_path": "/unapproved"}))
    with pytest.raises(ProviderError, match="SERVICE_ENDPOINT_CONFIGURATION_INVALID"):
        load_local_endpoints()


def test_bailian_stream_accepts_only_the_projected_tool_and_complete_terminal():
    name = "dc_artifacts_deliver"
    args = {"answer_plan": {"schema_version": "answer-plan/1", "sections": []}}
    raw = _model_response(name, args, model_id=BAILIAN_MODEL)
    assert _bailian_stream_call(raw, BAILIAN_MODEL, name) == {"id": "call_" + name, "name": name, "args": args}
    assert _bailian_stream_call(raw, BAILIAN_MODEL, "dc_artifacts_validate") is None
    assert _bailian_stream_call(raw, BAILIAN_MODEL, None) is None
    for reason in (b"length", b"stop", b"content_filter"):
        incomplete = raw.replace(b'"finish_reason": "tool_calls"', b'"finish_reason": "' + reason + b'"')
        assert _bailian_stream_call(incomplete, BAILIAN_MODEL, name) is None
    first, terminal, usage = [json.loads(line[5:]) for line in raw.splitlines()
                              if line.startswith(b"data:") and line[5:].strip() != b"[DONE]"]

    def stream(*events):
        return b"".join(b"data: " + json.dumps(event).encode() + b"\n\n" for event in events) + b"data: [DONE]\n\n"

    choice = first["choices"][0]
    tool = choice["delta"]["tool_calls"][0]
    tool["function"]["arguments"], tail_args = tool["function"]["arguments"][:12], tool["function"]["arguments"][12:]
    continuation = {**first, "choices": [{**choice, "delta": {"tool_calls": [
        {"index": 0, "id": None, "function": {"name": None, "arguments": tail_args}}]}}]}
    assert _bailian_stream_call(stream(first, continuation, terminal, usage), BAILIAN_MODEL, name)["args"] == args
    assert _bailian_stream_call(stream(first, continuation, terminal, continuation, usage), BAILIAN_MODEL, name) is None
    continuation["choices"][0]["index"] = 1
    assert _bailian_stream_call(stream(first, continuation, terminal, usage), BAILIAN_MODEL, name) is None
    choice["delta"]["tool_calls"].append({"index": 1, "id": "unapproved", "function": {"name": "dc_artifacts_validate", "arguments": "{}"}})
    assert _bailian_stream_call(stream(first, terminal, usage), BAILIAN_MODEL, name) is None
    assert _bailian_stream_call(_model_response(None, {}, model_id=BAILIAN_MODEL), BAILIAN_MODEL, None) == {}


def _assert_wire_edit_refused(app, fixture, gateway, profile_id, task_id):
    with app.state.session_factory() as db:
        run, token = open_run(db, task_id, profile_id=profile_id)
        target = urlsplit(gateway)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
        prefix = f"/private/runtime/runs/{run.id}"
        headers = {"X-Runtime-Token": token, "Content-Type": "application/json"}

        def post(path, body, extra=None):
            connection.request("POST", prefix + path, body=body, headers={**headers, **(extra or {})})
            response = connection.getresponse()
            return response.status, json.loads(response.read())

        before = len(fixture.model.wire)
        try:
            code, projected = post("/context-project", b'{"operation":"answer","business_purpose":"answer"}')
            assert code == 200
            edited = _selected_model_body(projected["context_json"], projected["generation"]) | {"enable_thinking": True}
            payload = json.dumps(edited).encode()
            code, auth = post("/model-authorizations", json.dumps({"projection_id": projected["id"],
                "context_digest": projected["context_digest"], "payload_digest": digest(payload),
                "adapter": ADAPTER, "request_key": "attempt-wire-edit"}).encode())
            assert code == 200
            code, error = post("/models/chat/completions", payload, {"X-Model-Call": auth["id"]})
            assert code == 409 and error["code"] == "RUNTIME_PAYLOAD_MISMATCH"
            assert len(fixture.model.wire) == before
            assert not db.scalar(select(ServiceUseCall.id).where(ServiceUseCall.request_key == auth["id"]))
        finally:
            connection.close()
            close_run(db, run.id, token)


def _assert_postgres_serializes_authorizations(app, fixture, employee, gateway, profile_id, task_id):
    with app.state.session_factory() as db:
        run, token = open_run(db, task_id, profile_id=profile_id)
        run_id = run.id
        server = urlsplit(gateway)
        with closing(http.client.HTTPConnection(server.hostname, server.port, timeout=10)) as connection:
            connection.request("POST", f"/private/runtime/runs/{run_id}/context-project",
                body=b'{"operation":"answer","business_purpose":"answer"}',
                headers={"X-Runtime-Token": token, "Content-Type": "application/json"})
            response = connection.getresponse()
            assert response.status == 200
            response.read()
        projections = [project_context(db, run_id, token) for _ in range(2)]
        assert projections[0].id != projections[1].id
        payload = json.dumps(_selected_model_body(projections[0].context_json, projections[0].generation)).encode()

    first_holds_lock, second_started, release_first = Event(), Event(), Event()
    backend_ids = {}
    engine = app.state.engine

    def pause_first(_conn, _cursor, statement, _parameters, context, _executemany):
        if context.execution_options.get("bailian_race_contender") == 0 and statement.startswith("UPDATE task_budgets"):
            first_holds_lock.set()
            assert release_first.wait(10)

    def contender(position):
        with app.state.session_factory() as db:
            db.connection(execution_options={"bailian_race_contender": position})
            backend_ids[position] = db.scalar(text("SELECT pg_backend_pid()"))
            if position == 1:
                second_started.set()
            try:
                projection = projections[position]
                call = authorize_model(db, run_id, token, projection_id=projection.id, context_digest=projection.context_digest,
                                       adapter=ADAPTER, payload_digest=digest(payload), request_key=f"contestant-{position}")
                return call.state
            except SafeError as error:
                db.rollback()
                return error.code

    before = len(fixture.model.wire)
    event.listen(engine, "after_cursor_execute", pause_first)
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(contender, 0)
            try:
                assert first_holds_lock.wait(10)
                second = pool.submit(contender, 1)
                assert second_started.wait(5)
                with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
                    deadline = time.monotonic() + 5
                    waiting_sql = None
                    while time.monotonic() < deadline:
                        waiting_sql = conn.scalar(text("SELECT query FROM pg_stat_activity WHERE pid=:second AND "
                            ":first = ANY(pg_blocking_pids(:second))"), {"first": backend_ids[0], "second": backend_ids[1]})
                        if waiting_sql:
                            break
                        time.sleep(.02)
                    assert waiting_sql and waiting_sql.startswith("UPDATE query_tasks"), waiting_sql
            finally:
                release_first.set()
            assert first.result(timeout=10) == "reserved"
            assert second.result(timeout=10) == "RUNTIME_CALL_UNSETTLED"
        with app.state.session_factory() as db:
            budget = db.get(TaskBudget, task_id)
            assert budget.reserved_calls == 1
            reserved = db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.task_id == task_id)).all()
            assert len(reserved) == 1
            assert len(fixture.model.wire) == before
            assert dispatch_model(db, run_id, token, reserved[0].id, payload)["state"] == "received"
            assert len(fixture.model.wire) == before + 1
            repeated = projections[1]
            with pytest.raises(SafeError) as denied:
                authorize_model(db, run_id, token, projection_id=repeated.id, context_digest=repeated.context_digest,
                                adapter=ADAPTER, payload_digest=digest(payload), request_key="another-message-no-tool-progress")
            assert denied.value.code == "RUNTIME_PHASE_NOT_ADVANCED"
            db.rollback()
            assert db.get(TaskBudget, task_id).reserved_calls == 1 and len(fixture.model.wire) == before + 1
            task = db.get(QueryTask, task_id)
            task.status, task.error_code = "blocked", "SYNTHETIC_KNOWN_FAILURE"
            task.revision += 1
            db.commit()
            close_run(db, run_id, token, state="interrupted")
        task_url = f"/tasks/{task_id}"
        status = employee.get(task_url)
        _expect(employee.post(task_url + "/retry", json={"reason": "synthetic known outcome"},
                              headers=_headers(status.headers["ETag"])), 202)
        with app.state.session_factory() as db:
            next_run, next_token = open_run(db, task_id, profile_id=profile_id)
            assert next_run.execution_revision == 2
            resumed = project_context(db, next_run.id, next_token)
            assert resumed.context_digest == repeated.context_digest
            next_call = authorize_model(db, next_run.id, next_token, projection_id=resumed.id, context_digest=resumed.context_digest,
                                        adapter=ADAPTER, payload_digest=digest(payload), request_key="manual-retry-known-outcome")
            assert next_call.state == "reserved"
            assert db.get(TaskBudget, task_id).reserved_calls == 2 and len(fixture.model.wire) == before + 1
            close_run(db, next_run.id, next_token)
    finally:
        event.remove(engine, "after_cursor_execute", pause_first)


@pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="isolated PostgreSQL required")
@pytest.mark.parametrize("live", [False, pytest.param(True, marks=pytest.mark.skipif(
    os.environ.get("DATACENTER_TEST_LIVE_BAILIAN") != "1", reason="explicit live Bailian opt-in required"))],
    ids=["scripted", "live"])
def test_bailian_sdk_query_delivers_validated_synthetic_answer(child_answer_pipeline, live, monkeypatch, record_testsuite_property):
    fixture = child_answer_pipeline
    if not live:
        fixture.model.bailian_fixture = True
        monkeypatch.setenv("DASHSCOPE_API_KEY", "synthetic-bailian-key")
        monkeypatch.setenv(BAILIAN_ENV, json.dumps({"peer_address": "39.96.213.166", "tls_ca_file": certifi.where()}))
        monkeypatch.setattr(BailianEndpoint, "connect", lambda _endpoint, timeout, _context:
                            http.client.HTTPConnection("127.0.0.1", fixture.model.port, timeout=timeout))
    assert os.environ.get(BAILIAN_ENV) and os.environ.get("DASHSCOPE_API_KEY"), "pinned TLS endpoint and credential required"
    profile_id = "fixture-bailian-answer"
    app = create_app(fixture.config)
    try:
        endpoints = load_local_endpoints()
        app.state.service_endpoints = endpoints
        with app.state.session_factory() as db:
            version = db.get(DocumentVersion, fixture.version_id)
            parse_run_id = db.get(ChunkSet, version.selected_chunk_set_id).parse_run_id
            source_sha256 = db.get(FileObject, version.source_file_id).sha256
            employee_name = db.get(User, fixture.actor_id).username
        install_synthetic_answer_fixture(app, actor_id=fixture.actor_id, knowledge_base_id=fixture.kb_id,
            version_id=fixture.version_id, parse_run_id=parse_run_id, source_sha256=source_sha256, query=QUERY, model=MODEL)
        with TestClient(app) as admin, TestClient(app) as employee, _api_gateway(app) as (gateway, trace):
            _login(admin, fixture.admin_name)
            _login(employee, employee_name)
            profile, etag = _profile(admin, "generation")
            revision_id = _expect(admin.post(profile + "/revisions", json={"endpoint_ref": BAILIAN_ENDPOINT_REF,
                "adapter": "openai-chat-v1", "model": BAILIAN_MODEL}, headers=_headers(etag)), 201)["id"]
            admission, _ = _test_run(admin, f"{profile}/revisions/{revision_id}")
            assert admission["state"] == "succeeded" and admission["result"]["tool_call_verified"], admission
            preview = _expect(_preview(admin, profile, "activate", revision_id=revision_id), 200)
            _expect(_change(admin, profile, "activate", preview), 200)
            select_purpose(admin, "query.answer", revision_id)
            with app.state.session_factory() as db:
                artifacts = runtime_artifacts()
                create_local_profile(db, profile_id=profile_id, generation_revision_id=revision_id,
                    max_calls=5, max_reserved_tokens=163840, artifact_manifest_hash=artifacts["artifact_manifest_hash"],
                    adapter_fingerprint=artifacts["adapter_fingerprint"], artifact_manifest=artifacts["manifest"])

            def submit(query):
                return _expect(employee.post("/knowledge/queries", json={"query": query, "model": MODEL,
                    "mode": "answer", "knowledge_base_ids": [fixture.kb_id], "service_mode": "selected_service"},
                    headers=_headers()), 202)["task_id"]

            denied_id = submit(QUERY + "?")
            denied_outcome = run_one(fixture.config, task_id=denied_id, gateway_url=gateway, profile_id=profile_id)
            assert denied_outcome["state"] == "blocked", denied_outcome
            assert any(event.get("safe_error") == "SERVICE_SYNTHETIC_INPUT_REQUIRED" for event in trace), (denied_outcome, trace)
            with app.state.session_factory() as db:
                assert not db.scalar(select(ModelCallAdmission.id).where(ModelCallAdmission.task_id == denied_id))

            task_id = submit(QUERY)
            outcome = run_one(fixture.config, task_id=task_id, gateway_url=gateway, profile_id=profile_id)
            with app.state.session_factory() as db:
                calls = list(db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.task_id == task_id)
                                       .order_by(ModelCallAdmission.created_at)))
                receipt_summary = [{"state": call.state, "status": call.response_status, "error": call.error_code,
                                    "usage": call.usage_json} for call in calls]
                assert outcome["state"] == "succeeded", {"outcome": outcome, "receipts": receipt_summary, "trace": trace}
                assert 1 <= len(calls) <= 5
                for call in calls:
                    ledger = db.scalar(select(ServiceUseCall).where(ServiceUseCall.request_key == call.id))
                    projection = db.get(ModelInputProjection, call.projection_id)
                    assert call.state == ledger.state == "received" and call.response_status == 200
                    assert call.payload_digest == ledger.payload_hash and call.context_digest == ledger.input_hash
                    assert ledger.target_hash == digest(BAILIAN_CHAT_PATH.encode())
                    assert projection.dependencies["service_use_pin"]["revision_id"] == revision_id
                if not live:
                    delivery = _bailian_stream_call(calls[-1].response_body.encode(), BAILIAN_MODEL, "dc_artifacts_deliver")
                    plan = delivery["args"]["answer_plan"]
                    run = db.get(AgentRun, calls[-1].run_id)
                    evidence = db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == task_id)).all()
                    calls[-2].response_body = _model_response("dc_artifacts_deliver", {"answer_plan": plan},
                        model_id=BAILIAN_MODEL).decode().replace("call_dc_artifacts_deliver", "call_wrong_phase")
                    db.flush()  # Simulate a historical receipt admitted before the stream gate was tightened.
                    assert _candidate_origin(db, run, "call_wrong_phase", plan, evidence) is None
                    db.rollback()
            result = _expect(employee.get(f"/tasks/{task_id}/result"), 200)
            assert result["result_status"] == "formal"
            assert all(literal in result["answer"] for literal in (STEP_ONE, STEP_TWO, WARNING, PRESSURE))
            if not live:
                assert len(fixture.model.wire) == 4 and len(fixture.model.requests) == 3
                assert all(token is None and b"synthetic-bailian-key" not in raw for _, raw, _, token in fixture.model.wire)
                _assert_wire_edit_refused(app, fixture, gateway, profile_id, submit(QUERY))
                _assert_postgres_serializes_authorizations(app, fixture, employee, gateway, profile_id, submit(QUERY))
                fixture.model.mode = "validate_and_deliver"
                bad_task = submit(QUERY)
                before_bad = len(fixture.model.wire)
                bad_result = run_one(fixture.config, task_id=bad_task, gateway_url=gateway, profile_id=profile_id)
                assert bad_result["state"] == "reconciliation_required", bad_result
                assert len(fixture.model.wire) == before_bad + 2  # Search plus one rejected response without retry.
                with app.state.session_factory() as db:
                    bad_calls = db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.task_id == bad_task)
                                           .order_by(ModelCallAdmission.created_at)).all()
                    assert [call.state for call in bad_calls] == ["received", "unknown"]
                    assert not db.scalar(select(QueryAnswerSnapshot.id).where(QueryAnswerSnapshot.task_id == bad_task))
                    assert db.scalars(select(ToolInvocation.tool_name).where(ToolInvocation.task_id == bad_task)).all() == ["dc_evidence_search"]
            record_testsuite_property("bailian_query_receipts", receipt_summary)
    finally:
        app.state.engine.dispose()
