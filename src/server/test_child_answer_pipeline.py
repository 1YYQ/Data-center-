"""Synthetic PDF through production source gates, pgvector, and the installed Pi SDK."""
from contextlib import contextmanager
import io
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
import uvicorn

from .app import create_app
from .embedding_jobs import accept_child_embedding_batch, child_batch_payload, run_child_embedding_batch
from .execution import invoke_tool
from .execution_models import QueryAnswerSnapshot
from .models import (AuthorizationGrant, BusinessApproval, ContentBlock, Document, DocumentVersion, FileObject,
                     QueryTask, TaskArtifact, TaskEvidence, User, utc_now)
from .parsing.ir_projection import normalize_parse_run
from .parsing.processing import run_source_qc, structure_parse_run
from .parse_jobs import run_local_parse_job
from .runtime_gate import close_run, create_local_profile, open_run, project_context
from .runtime_models import ModelCallAdmission, ModelInputProjection
from .security import hash_password
from .service_admission import LocalEndpoint
from .services import SafeError, save_upload, seed_reference_data
from .synthetic_provider import run_synthetic_provider
from .test_child_index import _build, _switch
from .test_chunk_processing import build, publish, review_candidate, verify_source
from .test_creation_recovery import PASSWORD, recovery_pg
from .test_embedding_service_use import _ENV, _SECRET, _login
from .test_execution import _headers
from .test_local_parse_pipeline import accept
from .test_service_use import activate, select_purpose
from .testing_governance import seed_verified_approval
from .testing_service_use import attest_synthetic_input
from .worker import run_one, runtime_artifacts


pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="isolated PostgreSQL required")
MODEL = "SYNTHETIC-1"
QUERY = f"{MODEL} startup and pressure"
STEP_ONE = "Step 1: Check the synthetic readiness indicator."
STEP_TWO = "Step 2: Start the synthetic training console."
WARNING = "Warning for step 2: Keep the synthetic shield closed."
PRESSURE = "Synthetic operating pressure: 5 Pa."
REJECTED_PROSE = "Invented setting: 93 Pa."
LINES = [("paragraph_title", "SYNTHETIC startup procedure"), ("paragraph", STEP_ONE), ("paragraph", STEP_TWO),
         ("paragraph", WARNING), ("paragraph_title", "SYNTHETIC operating limits"), ("paragraph", PRESSURE)]


def _source():
    import pymupdf

    blocks = []
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=595, height=842)
        for index, (kind, literal) in enumerate(LINES):
            y = 84 + 39 * index
            page.insert_text((67, y), literal)
            heading_level = {"level": 1} if kind == "paragraph_title" else {}
            blocks.append({"type": kind, **heading_level, "index": index, "bbox": [.10, (y - 16) / 842, .91, (y + 8) / 842],
                           "content": [{"type": "text", "content": literal}]})
        pdf.new_page(width=595, height=842)
        original = pdf.tobytes()
    pages = [{"page_idx": 0, "blocks": blocks}, {"page_idx": 1, "blocks": []}]
    payload = {"schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": True,
               "metadata": {"file_suffix": "pdf", "producer": {"name": "MinerU", "version": "4.2.3"}},
               "extensions": {"mineru": {"tier": "standard", "parse_mode": "txt"},
                              "docvortex_layout": {"version": 1, "pages": [
                                  {"page_idx": n, "width_pt": 595, "height_pt": 842} for n in range(2)]}},
               "pages": pages}
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, body in (("middle_json.json", json.dumps(payload)), ("structured_content.json", json.dumps({"pages": pages})),
                           ("markdown.md", "\n".join(text for _, text in LINES))):
            archive.writestr(ZipInfo(name, date_time=(2026, 11, 19, 0, 0, 0)), body)
    return original, buffer.getvalue()


def _publish_source(f, bundle):
    run_id = accept(f)

    def recorded_local_output(source, workspace, executable, home, heartbeat):
        result = workspace / "result.zip"
        result.write_bytes(bundle)
        return result

    with f.app.state.session_factory() as db:
        parsed = run_local_parse_job(db, run_id, f.config, runner=recorded_local_output)
        assert parsed.stage_checkpoint["stage_state"] == "parsed", parsed.error_code
        normalized = normalize_parse_run(db, run_id, f.config)
        assert normalized.stage_checkpoint["stage_state"] == "normalized", normalized.error_code
        structured = structure_parse_run(db, run_id, f.config)
        assert structured.stage_checkpoint["stage_state"] == "structured", structured.error_code
        checked = run_source_qc(db, run_id, f.config)
        assert checked.stage_checkpoint["source_qc"]["status"] == "passed", checked.stage_checkpoint["source_qc"]["issues"]
    set_id = build(f, run_id)
    review = review_candidate(f, run_id, set_id, verify_source(f, run_id))
    publish(f, set_id, review)


def _build_and_activate(f, previous=None):
    with f.app.state.session_factory() as db:
        generation = _build(db, f)
        generation_id = generation.id
        assert generation.build_report["integrity"]["status"] == "pending", generation.build_report["integrity"]
        manifest = generation.build_report["manifest"]
        jobs = []
        for batch in manifest["batches"]:
            number = batch["batch_no"]
            _, _, payload = child_batch_payload(db, manifest, number)
            attest_synthetic_input(db, f.actor_id, "document.embed", f"index:{generation_id}:{number}", payload)
            db.flush()
            jobs.append(accept_child_embedding_batch(db, generation_id, number, db.get(User, f.actor_id),
                                                      f.app.state.service_endpoints).id)
        db.commit()
        for job_id in jobs:
            job = run_child_embedding_batch(db, job_id, f.app.state.service_endpoints)
            assert job.state == "succeeded", job.error_code
        _switch(db, f, generation_id, previous)
        db.commit()
    return generation_id


def _plan(contract, *, mode="good"):
    groups = {group["id"]: group for group in contract["support_groups"]}
    selected = dict.fromkeys(item["matched_group_refs"][0] for item in contract["coverage"]["items"])
    sections = {}
    for ref in selected:
        group = groups[ref]
        refs = [member["evidence_ref"] for member in group["members"]
                if mode != "missing_warning" or member["role"] != "warning"]
        section = group["allowed_headings"][0]
        item = {"kind": group["kind"], "renderer_key": group["renderer_key"], "support_group_refs": [ref], "evidence_refs": refs}
        sections.setdefault(section, []).append(item)
    plan = {"schema_version": "answer-plan/1", "sections": [{"heading_code": key, "items": items}
                                                           for key, items in sections.items()]}
    return plan


def _model_response(tool, args, *, model_id="pi-local-synthetic"):
    if tool:
        delta = {"role": "assistant", "tool_calls": [{"index": 0, "id": "call_" + tool, "type": "function",
                 "function": {"name": tool, "arguments": json.dumps(args)}}]}
    else:
        delta = {"role": "assistant", "content": REJECTED_PROSE}
    base = {"id": "synthetic-child-answer", "object": "chat.completion.chunk", "created": 1, "model": model_id}
    terminal_delta = {"tool_calls": None} if model_id == "qwen3.8-flash" else {}
    events = [{**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]},
              {**base, "choices": [{"index": 0, "delta": terminal_delta, "finish_reason": "tool_calls" if tool else "stop"}]},
              {**base, "choices": [], "usage": {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}}]
    return ("".join("data: " + json.dumps(event) + "\n\n" for event in events) + "data: [DONE]\n\n").encode()


@contextmanager
def _scripted_model():
    """Actual loopback streaming transport, SDK and receipts with scripted answers."""
    model = SimpleNamespace(mode="good", requests=[], request_bytes=[], errors=[], bailian_fixture=False, wire=[])

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            payload = self.rfile.read(int(self.headers["Content-Length"]))
            body = json.loads(payload)
            if model.bailian_fixture:
                from .service_admission import BAILIAN_CHAT_PATH, BAILIAN_MODEL
                model.wire.append((self.path, payload, body, self.headers.get("X-Runtime-Token")))
                if (self.path != BAILIAN_CHAT_PATH or body.get("model") != BAILIAN_MODEL
                        or body.get("enable_thinking") is not False or body.get("preserve_thinking") is not False
                        or not 1 <= body.get("max_tokens", 0) <= 2048 or "max_completion_tokens" in body
                        or "store" in body or self.headers.get("Authorization") != "Bearer synthetic-bailian-key"):
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if body.get("tool_choice", {}).get("function", {}).get("name") == "record_probe":
                    self.send_sse(_model_response("record_probe", {"value": 7}, model_id=BAILIAN_MODEL))
                    return
            source = body["messages"][-1]["content"]
            if isinstance(source, list):
                source = "".join(part.get("text", "") for part in source)
            projection = json.loads(source)
            completed = {item["tool_name"]: item["data"] for item in projection["completed_tool_results"]}
            model.requests.append(projection)
            model.request_bytes.append(len(payload))
            try:
                if "dc_evidence_search" not in completed:
                    tool, args = "dc_evidence_search", {"query": QUERY, "top_k": "10"} if model.bailian_fixture else {}
                elif "dc_artifacts_validate" not in completed:
                    tool, args = "dc_artifacts_validate", {"evidence_ids": ["invented-ref"]} if model.bailian_fixture else {}
                elif completed["dc_artifacts_validate"]["ready"]:
                    tool = None if model.mode == "free_prose" else "dc_artifacts_deliver"
                    args = {"answer_plan": _plan(projection["answer_contract"], mode=model.mode)} if tool else {}
                elif model.mode == "no_evidence_stop":
                    # A well-behaved provider ends without inventing evidence.
                    # Production finalization must still expose the precise
                    # missing/conflict state rather than trusting this prose.
                    tool, args = None, {}
                else:
                    raise AssertionError(projection["answer_contract"]["coverage"])
            except (AssertionError, IndexError, KeyError) as error:
                model.errors.append(str(error))
                self.send_response(503)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            answer = _model_response(tool, args, model_id=body["model"])
            if model.mode == "validate_and_deliver" and tool == "dc_artifacts_validate":
                first, *tail = answer.splitlines(keepends=True)
                frame = json.loads(first[5:])
                frame["choices"][0]["delta"]["tool_calls"].append({"index": 1, "id": "call_early_delivery", "type": "function",
                    "function": {"name": "dc_artifacts_deliver", "arguments": json.dumps({"answer_plan": _plan(projection["answer_contract"])})}})
                answer = b"data: " + json.dumps(frame).encode() + b"\n" + b"".join(tail)
            self.send_sse(answer)

        def send_sse(self, response):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as upstream:
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        model.port = upstream.server_port
        try:
            yield model
        finally:
            upstream.shutdown()
            thread.join(5)


@contextmanager
def _api_gateway(app):
    trace = []

    async def traced_app(scope, receive, send):
        path = scope.get("path", "")
        if not path.startswith("/private/runtime/runs/"):
            await app(scope, receive, send)
            return
        started = time.perf_counter()
        event = {"route": path.split("/", 5)[-1], "status": None, "bytes": 0, "has_sse_finish": False}

        async def observed(message):
            if message["type"] == "http.response.start":
                event["status"] = message["status"]
            elif message["type"] == "http.response.body":
                body = message.get("body", b"")
                event["bytes"] += len(body)
                event["has_sse_finish"] |= b'"finish_reason"' in body and b"[DONE]" in body
                if event["status"] >= 400 and body.startswith(b"{"):
                    event["safe_error"] = json.loads(body).get("code")
            await send(message)

        try:
            await app(scope, receive, observed)
        finally:
            trace.append({**event, "seconds": round(time.perf_counter() - started, 3)})

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        server = uvicorn.Server(uvicorn.Config(traced_app, log_level="error", access_log=False))
        thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(.01)
            assert server.started
            yield f"http://127.0.0.1:{listener.getsockname()[1]}", trace
        finally:
            server.should_exit = True
            thread.join(10)


@pytest.fixture
def child_answer_pipeline(recovery_pg, monkeypatch):
    _, config, account, _ = recovery_pg
    monkeypatch.setenv(_ENV, _SECRET)
    monkeypatch.delenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", raising=False)
    app = create_app(config)
    f = SimpleNamespace(app=app, config=config, actor_id=account["actor_id"], kb_id=account["kb_id"],
                        document_id=account["doc_id"], version_id=account["version_id"],
                        path=f"/documents/{account['doc_id']}/versions/{account['version_id']}/parse-runs")
    admin_name, recipient_name = ("synthetic-answer-" + uuid4().hex for _ in range(2))
    original, bundle = _source()
    f.original, f.recipient_name, f.admin_name = original, recipient_name, admin_name
    try:
        with app.state.session_factory() as db:
            seed_reference_data(db)
            actor = db.get(User, f.actor_id)
            document = db.get(Document, f.document_id)
            version = db.get(DocumentVersion, f.version_id)
            file = save_upload([original], "synthetic-answer.pdf", "application/pdf", actor, config)
            recipient = User(username=recipient_name, display_name="Synthetic reading-only recipient", roles=["employee"],
                             status="active", credential_state="active", credential_hash=hash_password(PASSWORD))
            db.add_all([file, recipient, User(username=admin_name, display_name="Synthetic service administrator", roles=["system_admin"],
                       status="active", credential_state="active", credential_hash=hash_password(PASSWORD))])
            db.flush()
            version.source_file_id = file.id
            scope = {"knowledge_base_id": f.kb_id, "document_id": None, "level": document.access_level,
                     "category": document.category, "need_class": document.need_class}
            approval = BusinessApproval(applicant=recipient.id, recorded_by=actor.id, scope=scope, operations=["document.read"])
            db.add(approval)
            db.flush()
            seed_verified_approval(db, approval)
            grant = AuthorizationGrant(subject_user_id=recipient.id, operation="document.read", approval_id=approval.id,
                                       valid_until=approval.valid_until, granted_by=actor.id, **scope)
            db.add(grant)
            db.flush()
            f.recipient_id, f.recipient_grant_id = recipient.id, grant.id
            db.commit()
        with (run_synthetic_provider(credential=_SECRET) as provider, _scripted_model() as model, TestClient(app) as manager,
              TestClient(app) as administrator, TestClient(app) as recipient):
            app.state.service_endpoints = {"synthetic": LocalEndpoint(provider.port, _ENV)}
            _login(manager, account["username"])
            _login(administrator, admin_name)
            _login(recipient, recipient_name)
            f.client, f.recipient, f.model = manager, recipient, model
            _publish_source(f, bundle)
            _, _, f.revision_id = activate(administrator, "embedding")
            select_purpose(administrator, "document.embed", f.revision_id)
            f.generation_id = _build_and_activate(f)
            with app.state.session_factory() as db:
                artifacts = runtime_artifacts()
                for profile_id, window in (("local-pi", 16384), ("fixture-narrow-pi", 8192)):
                    profile = create_local_profile(db, profile_id=profile_id, upstream_port=model.port,
                        synthetic_context_window=window, artifact_manifest_hash=artifacts["artifact_manifest_hash"],
                        adapter_fingerprint=artifacts["adapter_fingerprint"], artifact_manifest=artifacts["manifest"])
                    assert profile.configuration["gateway_policy"] == {"native_timeout_ms": 20000, "upstream_timeout_seconds": 5}
                    assert profile.configuration["request_timeout_seconds"] == 5
            with _api_gateway(app) as (gateway, trace):
                f.gateway, f.gateway_trace = gateway, trace
                yield f
    finally:
        app.state.engine.dispose()


def _submit(f):
    result = f.client.post("/knowledge/queries", json={"query": QUERY, "model": MODEL, "knowledge_base_ids": [f.kb_id]},
                           headers=_headers())
    assert result.status_code == 202, result.text
    return result.json()["task_id"]


def _manual_validation(f, db, task_id):
    run, token = open_run(db, task_id)
    task = db.get(QueryTask, task_id)
    task.status = "running"
    task.revision += 1
    db.commit()
    found = invoke_tool(db, run.id, token, "fixture:search", "dc_evidence_search", {}, settings=f.config)
    db.commit()
    checked = invoke_tool(db, run.id, token, "fixture:validate", "dc_artifacts_validate", {}, settings=f.config)
    db.commit()
    assert checked["data"]["ready"], checked["data"]
    return run, token, _plan(found["data"]["answer_contract"])


def _candidate(db, task_id, config):
    artifact = db.scalar(select(TaskArtifact).where(TaskArtifact.task_id == task_id, TaskArtifact.kind == "answer_candidate"))
    assert artifact
    file = db.get(FileObject, artifact.file_object_id)
    return json.loads((config.storage_dir / file.object_key).read_bytes())


def _share(f, task_id, snapshot_id, entries):
    status = f.client.get(f"/tasks/{task_id}")
    changed = f.client.put(f"/tasks/{task_id}/shares", json={"entries": entries, "answer_snapshot_id": snapshot_id},
                           headers={**_headers(), "If-Match": status.headers["ETag"]})
    assert changed.status_code == 200, changed.text


def _receipt_states(f, task_id, profile_id="local-pi"):
    from .runtime_gate import _assemble
    from .runtime_models import AgentRun, RuntimeProfile

    with f.app.state.session_factory() as db:
        receipts = db.execute(select(ModelCallAdmission.state, ModelCallAdmission.response_status, ModelCallAdmission.error_code)
                              .where(ModelCallAdmission.task_id == task_id).order_by(ModelCallAdmission.created_at)).all()
        run = db.scalar(select(AgentRun).where(AgentRun.task_id == task_id))
        task = db.get(QueryTask, task_id)
        profile = db.get(RuntimeProfile, (profile_id, 1))
        if not run or not profile:
            return {"durable_receipts": receipts, "loopback_payload_bytes": f.model.request_bytes,
                    "projected_context": None, "gateway": f.gateway_trace[-9:]}
        try:
            context, _ = _assemble(db, run, task, db.get(User, f.actor_id), profile, "answer", "answer")
            projected_bytes = len(context.encode("utf-8"))
        except SafeError as error:
            projected_bytes = error.code
        return {"durable_receipts": receipts, "loopback_payload_bytes": f.model.request_bytes,
                "projected_context": projected_bytes, "gateway": f.gateway_trace[-9:]}


def _observe_shared_answer_in_chrome(f, task_id):
    from playwright.sync_api import expect, sync_playwright

    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    assert chrome.is_file(), "Installed Chrome required"
    assert Path("src/web/dist/index.html").is_file(), "Build the React workspace first"
    out = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/pdf-implementation/2026-11-19/browser"))
    out = out / "child-answer-shared-history"
    out.mkdir(parents=True, exist_ok=True)
    errors, query_posts = [], []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(chrome),
            args=["--disable-background-networking", "--disable-component-update", "--no-first-run"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(f.gateway + "/")
                      or not route.request.url.startswith(("http:", "https:")) else route.abort())
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: query_posts.append(request.method)
                if request.method == "POST" and request.url.endswith("/knowledge/queries") else None)
        try:
            page.goto(f.gateway + "/workspace/query")
            page.wait_for_load_state("networkidle")
            page.get_by_label("账号", exact=True).fill(f.recipient_name)
            page.get_by_label("口令", exact=True).fill(PASSWORD)
            page.get_by_role("button", name="登录", exact=True).click()
            page.get_by_text("我的查询记录", exact=True).click()
            entry = page.get_by_role("button", name="已保存的查询", exact=True)
            with page.expect_response(lambda response: response.url.endswith(f"/tasks/{task_id}/result")) as reply:
                entry.click()
            assert reply.value.status == 200 and reply.value.json()["result_status"] == "formal"
            expect(page.get_by_text("已找到可核对依据", exact=True)).to_be_visible()
            answer = page.locator(".result-panel .answer")
            for passage in (STEP_ONE, WARNING, STEP_TWO, PRESSURE):
                expect(answer).to_contain_text(passage)
            evidence = page.locator("article.query-evidence").filter(has_text=WARNING)
            source = evidence.get_by_role("link", name="核对原文 · PDF 第 1 页", exact=True)
            href = source.get_attribute("href")
            assert href == f"/documents/{f.document_id}/versions/{f.version_id}/file#page=1"
            original = context.request.get(f.gateway + href.split("#")[0])
            assert original.status == 200 and original.body() == f.original
            assert not query_posts and not errors, (query_posts, errors)
            bundle = page.locator('script[src^="/workspace/assets/"]').get_attribute("src")
            page.screenshot(path=str(out / "formal-child-answer.png"), full_page=True)
            (out / "observations.json").write_text(json.dumps({
                "scope": "synthetic PDF and local scripted model via installed Pi SDK",
                "browser": browser.version, "view": "employee opened an existing shared answer from query history",
                "task_id": task_id, "formal_result_http_status": reply.value.status, "served_bundle": bundle,
                "warning_and_original_pdf_page": 1, "query_posts": len(query_posts), "page_errors": errors,
            }, ensure_ascii=False, indent=2), encoding="utf-8")
        finally:
            context.close()
            browser.close()


def test_production_child_answer_preserves_exact_support_rejects_unbound_candidates_and_rechecks_shares(
        child_answer_pipeline, record_testsuite_property):
    f = child_answer_pipeline
    task_id = _submit(f)
    outcome = run_one(f.config, task_id=task_id, gateway_url=f.gateway)
    assert not f.model.errors, f.model.errors
    if outcome["state"] != "succeeded":
        pytest.fail(json.dumps({"outcome": outcome, **_receipt_states(f, task_id)}, default=str))
    record_testsuite_property("wide_profile_loopback_payload_bytes", json.dumps(f.model.request_bytes))
    record_testsuite_property("wide_profile_gateway_trace", json.dumps(f.gateway_trace))
    result_path = f"/tasks/{task_id}/result"
    answer = f.client.get(result_path)
    assert answer.status_code == 200, answer.text
    result = answer.json()
    snapshot_id = result["answer_snapshot_id"]
    assert result["result_status"] == "formal" and WARNING in result["answer"] and PRESSURE in result["answer"]
    assert STEP_ONE in result["answer"] and STEP_TWO in result["answer"]
    assert result["answer"].index(STEP_ONE) < result["answer"].index(WARNING) < result["answer"].index(STEP_TWO)
    assert not any(item["kind"] == "answer_candidate"
                   for item in f.client.get(f"/tasks/{task_id}/artifacts").json()["items"])
    assert len(f.model.requests) == 3
    delivered = f.model.requests[-1]
    coverage = delivered["answer_contract"]["coverage"]
    assert [item["kind"] for item in coverage["items"]] == ["operation", "fact"] and coverage["missing_count"] == 0
    assert len({item["matched_group_refs"][0] for item in coverage["items"]}) == 2
    assert "answer_plan_schema" not in delivered["answer_contract"]
    assert all("text" not in member for group in delivered["answer_contract"]["support_groups"] for member in group["members"])
    projected_sources = {item["evidence_id"]: item for item in delivered["authorized_sources"]}
    assert {item["text"] for item in projected_sources.values()} == {literal for _, literal in LINES}
    assert all(item["locator"]["file_page"] == 1 and item["block_start"] < item["block_end"]
               and "source_spans" not in item for item in projected_sources.values())
    with f.app.state.session_factory() as db:
        from .chunk_retrieval import validate_child_fragment
        from .query_evidence import child_fragment

        snapshot = db.get(QueryAnswerSnapshot, snapshot_id)
        assert snapshot.result["answer_contract"] == "answer-plan/1" and snapshot.result["coverage"]["missing_count"] == 0
        assert {ref for claim in snapshot.result["claims"] for ref in claim["support_group_refs"]} >= {
            item["matched_group_refs"][0] for item in delivered["answer_contract"]["coverage"]["items"]}
        evidence = db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == task_id)).all()
        assert set(projected_sources) == {row.id for row in evidence}
        for row in evidence:
            assert row.chunk_id and row.source_binding_hash and "source_binding_hash" not in projected_sources[row.id]
            assert row.quote_snapshot == projected_sources[row.id]["text"]
            block = db.get(ContentBlock, row.block_id)
            spans = row.source_spans
            assert len(spans) == 1
            span = spans[0]
            assert row.quote_snapshot == block.text_content[span["block_start"]:span["block_end"]]
            assert projected_sources[row.id]["block_id"] == row.block_id
            assert all(projected_sources[row.id][key] == span[key] for key in ("block_start", "block_end"))
            assert not validate_child_fragment(db, db.get(User, f.recipient_id), child_fragment(row), model=MODEL, kb_ids=[f.kb_id])
        receipt = _candidate(db, task_id, f.config)["origin"]
        assert receipt["pi_tool_call_id"] == "call_dc_artifacts_deliver"
        assert db.get(ModelCallAdmission, receipt["model_call_id"]).state == "received"
        projection = db.get(ModelInputProjection, receipt["projection_id"])
        assert {source["evidence"]["id"] for source in projection.dependencies["sources"]} == set(projected_sources)
        assert {source["evidence"]["id"]: source["fragment"]["source_binding_hash"]
                for source in projection.dependencies["sources"]} == {row.id: row.source_binding_hash for row in evidence}

    narrow_id = _submit(f)
    before = len(f.model.requests)
    narrow = run_one(f.config, task_id=narrow_id, gateway_url=f.gateway, profile_id="fixture-narrow-pi")
    narrow_receipts = _receipt_states(f, narrow_id, "fixture-narrow-pi")
    assert narrow["state"] == "blocked" and narrow_receipts["projected_context"] == "RUNTIME_CONTEXT_BUDGET", narrow_receipts
    assert narrow_receipts["durable_receipts"] == [("received", 200, None)] and len(f.model.requests) == before + 1
    record_testsuite_property("narrow_profile_gateway_trace", json.dumps(narrow_receipts["gateway"]))

    assert f.recipient.get(result_path).status_code == 404
    _share(f, task_id, snapshot_id, [{"user_id": f.recipient_id}])
    shared = f.recipient.get(result_path)
    assert shared.status_code == 200 and shared.json()["answer"] == result["answer"], shared.text
    assert QUERY not in shared.text
    assert "coverage" not in shared.json()["task"]["result"]
    if os.environ.get("DATACENTER_TEST_CHILD_ANSWER_BROWSER") == "1":
        _observe_shared_answer_in_chrome(f, task_id)
    _share(f, task_id, snapshot_id, [])
    assert f.recipient.get(result_path).status_code == 404
    _share(f, task_id, snapshot_id, [{"user_id": f.recipient_id}])
    with f.app.state.session_factory() as db:
        db.get(AuthorizationGrant, f.recipient_grant_id).revoked_at = utc_now()
        db.commit()
    assert f.recipient.get(result_path).status_code == 404

    for mode in ("missing_warning", "free_prose"):
        f.model.mode = mode
        rejected_id = _submit(f)
        rejected = run_one(f.config, task_id=rejected_id, gateway_url=f.gateway)
        assert not f.model.errors, f.model.errors
        assert rejected["state"] == "blocked", rejected
        assert f.client.get(f"/tasks/{rejected_id}/result").status_code == 409
        with f.app.state.session_factory() as db:
            assert not db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == rejected_id))
            if mode == "missing_warning":
                candidate = _candidate(db, rejected_id, f.config)
                assert candidate["origin"] and "evidence_membership_mismatch" in {
                    item["code"] for item in candidate["validation"]["reasons"]}
            else:
                assert rejected["error_code"] == "ANSWER_PLAN_REQUIRED"
                assert any(REJECTED_PROSE in (call.response_body or "") for call in db.scalars(
                    select(ModelCallAdmission).where(ModelCallAdmission.task_id == rejected_id, ModelCallAdmission.state == "received")))
                assert not db.scalar(select(TaskArtifact).where(TaskArtifact.task_id == rejected_id,
                                                                TaskArtifact.kind == "answer_candidate"))

    forged_id = _submit(f)
    with f.app.state.session_factory() as db:
        run, token, plan = _manual_validation(f, db, forged_id)
        forged = invoke_tool(db, run.id, token, "fixture:no-model-receipt", "dc_artifacts_deliver", {"answer_plan": plan}, settings=f.config)
        db.commit()
        assert forged["state"] == "blocked"
        candidate = _candidate(db, forged_id, f.config)
        assert candidate["origin"] is None and "candidate_model_receipt_unverified" in {
            item["code"] for item in candidate["validation"]["reasons"]}
        close_run(db, run.id, token)

    stale_id = _submit(f)
    with f.app.state.session_factory() as db:
        run, token, plan = _manual_validation(f, db, stale_id)
        project_context(db, run.id, token)
        new_generation = _build_and_activate(f, f.generation_id)
        assert new_generation != f.generation_id
        with pytest.raises(SafeError) as changed:
            invoke_tool(db, run.id, token, "fixture:stale", "dc_artifacts_deliver", {"answer_plan": plan}, settings=f.config)
        assert changed.value.code in {"ACCESS_LIMITED", "RUNTIME_SOURCE_CHANGED"}
        db.rollback()
        assert not db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == stale_id))
    historical = f.client.get(result_path)
    assert historical.status_code == 200, historical.text
    assert historical.json()["evidence"] == result["evidence"]

    revoked_id = _submit(f)
    with f.app.state.session_factory() as db:
        run, token, plan = _manual_validation(f, db, revoked_id)
        grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.actor_id,
                                                           AuthorizationGrant.operation == "document.read"))
        grant.revoked_at = utc_now()
        db.commit()
        with pytest.raises(SafeError) as changed:
            invoke_tool(db, run.id, token, "fixture:revoked", "dc_artifacts_deliver", {"answer_plan": plan}, settings=f.config)
        assert changed.value.code in {"ACCESS_LIMITED", "RUNTIME_SOURCE_CHANGED"}
        db.rollback()
        assert not db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == revoked_id))
    assert f.client.get(result_path).status_code == 403
