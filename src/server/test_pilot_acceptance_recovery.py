"""Scenario F against installed Chrome, formal React, isolated PostgreSQL and Pi SDK."""

import json
import os
import re
from functools import partial
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from . import runtime_gate as gate, test_workspace_browser
from .config import Settings
from .execution import ENABLED_TOOLS
from .execution_models import QueryAnswerSnapshot
from .models import KnowledgeBase, QueryTask, TaskAttempt, TaskOutbox, TaskStep
from .runtime_models import ModelCallAdmission, RuntimeProfile, TaskBudget
from .service_use_models import ServiceTaskSelection, ServiceUseCall, ServiceUsePin
from .synthetic_provider import CHAT_PATH, _Handler, run_synthetic_provider
from .test_child_answer_pipeline import _model_response
from .test_execution import execution_http
from .test_runtime_gate import local_upstream, runtime_db
from .test_workspace_browser import _login, _screenshot, _select, workspace
from .test_worker import STARTUP, WARNING, synthetic_pdf
from .testing_service_use import attest_synthetic_input
from .worker import run_one


PROFILE = "synthetic-af-recovery"
PURPOSE = "query.answer"
PROVIDER_REF = "af_recovery"
CREDENTIAL_ENV = "DATACENTER_SYNTHETIC_AF_RECOVERY_KEY"
SERVICE_LABEL = "SYNTHETIC A–F generation recovery"


@pytest.fixture
def failure_workspace(monkeypatch, request):
    base = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    assert (base.drivername, base.host, base.port) == ("postgresql+psycopg", "127.0.0.1", 55440)
    assert base.database.startswith("dc_local_") and not base.query
    monkeypatch.setattr(test_workspace_browser, "Settings", partial(Settings, query_service_mode="selected_service"))
    original_inference = _Handler._inference

    def answer_request(handler, path, payload):
        if path == CHAT_PATH and payload.get("model") == handler.server.model:
            if handler.server.mode == "unavailable":
                return handler._reply(503)
            if payload.get("stream") is True:
                reply = _model_response(None, {}, model_id=payload["model"])
                if handler.server.mode == "incomplete-stream":
                    reply = reply.removesuffix(b"data: [DONE]\n\n")
                return handler._reply(200, reply, content_type="text/event-stream")
        return original_inference(handler, path, payload)

    monkeypatch.setattr(_Handler, "_inference", answer_request)
    credential = "SYNTHETIC loopback-only scenario F"
    monkeypatch.setenv(CREDENTIAL_ENV, credential)
    monkeypatch.delenv("DATACENTER_BAILIAN_SERVICE_ENDPOINT", raising=False)
    with run_synthetic_provider(credential=credential) as provider:
        monkeypatch.setenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", json.dumps({
            PROVIDER_REF: {"port": provider.port, "credential_env": CREDENTIAL_ENV},
        }))
        f = request.getfixturevalue("workspace")
        legacy = f.db.get(RuntimeProfile, ("local-pi", 1))
        gate.create_local_profile(f.db, profile_id=PROFILE, artifact_manifest_hash=legacy.artifact_manifest_hash,
                                  adapter_fingerprint=legacy.adapter_fingerprint, artifact_manifest=legacy.configuration["artifact_manifest"])
        yield f, provider


def _run_self_test(admin, expected):
    from playwright.sync_api import expect

    receipt = admin.get_by_role("region", name="自检回执", exact=True)
    admin.get_by_role("button", name="运行合成自检", exact=True).click()
    expect(receipt.get_by_role("heading", name=f"自检回执 · {expected}", exact=True)).to_be_visible()
    return receipt


def _confirm_answer_selection(admin, address, revision_id, reason):
    from playwright.sync_api import expect

    use = admin.get_by_role("region", name="按用途选用服务", exact=True)
    _select(use, "用途").select_option(PURPOSE)
    _select(use, "目标配置修订").select_option(revision_id)
    use.get_by_label("选用变更理由", exact=True).fill(reason)
    use.get_by_role("button", name="预检用途变更", exact=True).click()
    actual_path = address + f"admin/service-selections/{PURPOSE}/select"
    with admin.expect_response(lambda response: response.request.method == "POST" and response.url == actual_path) as applied:
        use.get_by_role("button", name="确认选用", exact=True).click()
    assert applied.value.status == 200
    selected = applied.value.json()
    assert selected["enabled"] and selected["revision_id"] == revision_id
    expect(use.get_by_text(selected["test_run_id"], exact=True)).to_be_visible()
    return selected["test_run_id"]


def _configure_answer(admin, address):
    from playwright.sync_api import expect

    admin.get_by_label("服务类型").select_option("generation")
    admin.get_by_label("配置名称", exact=True).fill(SERVICE_LABEL)
    admin.get_by_role("button", name="创建配置", exact=True).click()
    expect(admin.get_by_role("heading", name=SERVICE_LABEL, exact=True)).to_be_visible()
    admin.get_by_role("button", name="保存修订", exact=True).click()
    _run_self_test(admin, "测试通过")
    admin.get_by_label("变更理由", exact=True).fill("SYNTHETIC generation fixture")
    admin.get_by_role("button", name="预检启用", exact=True).click()
    admin.get_by_role("button", name="确认启用", exact=True).click()
    expect(admin.get_by_text("本地已启用修订 1", exact=True)).to_be_visible()
    listed = admin.request.get(address + "admin/service-profiles").json()["items"]
    profile = next(item for item in listed if item["label"] == SERVICE_LABEL)
    revision_id = profile["active_revision_id"]
    return revision_id, _confirm_answer_selection(admin, address, revision_id, "SYNTHETIC scenario F task pin")


def _submit_in_browser(f, question):
    page = f.page
    page.get_by_label("设备型号（可选）", exact=True).fill("DEMO-1")
    page.get_by_label("问题", exact=True).fill(question)
    with page.expect_response(lambda response: response.request.method == "POST"
                              and response.url == f.address + "knowledge/queries") as accepted:
        page.get_by_role("button", name="提交查询", exact=True).click()
    assert accepted.value.status == 202
    task_id = accepted.value.json()["task_id"]
    f.db.expire_all()
    task = f.db.get(QueryTask, task_id)
    assert task.owner_user_id == f.employee.id and task.input_snapshot["service_mode"] == "selected_service"
    envelope = gate.canonical_json({"systemPrompt": gate.SYSTEM, "tools": ENABLED_TOOLS, "messages": [
        {"role": "user", "timestamp": 0, "content": [{"type": "text", "text": gate.canonical_json({
            "business_purpose": "answer", "operation": "answer", "task_input": task.effective_input,
            "authorized_sources": [], "completed_tool_results": [],
        })}]},
    ]})
    attest_synthetic_input(f.db, f.employee.id, PURPOSE, "task:" + task.id, envelope.encode("utf-8"))
    f.db.commit()
    page.get_by_text("正在查询", exact=True).wait_for()
    return task_id


def _receipts(f, task_id, revision_id):
    f.db.expire_all()
    pin_id = f.db.get(ServiceTaskSelection, task_id).pin_ids[PURPOSE]
    pin = f.db.get(ServiceUsePin, pin_id)
    models = f.db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.task_id == task_id)
                          .order_by(ModelCallAdmission.created_at)).all()
    assert pin.revision_id == revision_id and pin.calls_used == len(models)
    for model in models:
        service = f.db.scalar(select(ServiceUseCall).where(ServiceUseCall.pin_id == pin_id,
                              ServiceUseCall.request_key == model.id))
        assert (service.state, service.response_status) == (model.state, model.response_status)
        assert service.payload_hash == model.payload_digest
        if service.state == "unknown":
            assert service.usage is None and service.settled_cost is None and model.usage_state == "unknown"
    budget = f.db.get(TaskBudget, task_id)
    assert budget.reserved_calls == len(models) and budget.reserved_tokens > 0
    return {"pin_id": pin_id, "reserved_calls": budget.reserved_calls, "reserved_tokens": budget.reserved_tokens,
            "calls": [{"id": item.id, "state": item.state, "http_status": item.response_status} for item in models]}


def _status(f, task_id):
    response = f.page.request.get(f.address + f"tasks/{task_id}")
    assert response.status == 200
    view = response.json()
    match = response.headers["etag"]
    assert match == f'"{view["revision"]}"'
    return view, match


def _worker(f, task_id):
    return run_one(f.config, gateway_url=f.address, task_id=task_id, profile_id=PROFILE)


@pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
                    reason="Requires the explicit isolated local PostgreSQL container")
def test_react_service_failure_retry_and_unknown_original_task_recovery(failure_workspace):
    from playwright.sync_api import expect

    f, provider = failure_workspace
    with f.browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block") as admins:
        admins.route(re.compile(r"^https?://(?!" + re.escape(f.address.removeprefix("http://")) + r")"),
                     lambda route: route.abort())
        admin = admins.new_page()
        admin_errors = []
        admin.on("pageerror", lambda error: admin_errors.append(str(error)))
        _login(SimpleNamespace(page=admin, address=f.address), "services", f.administrator, "synthetic admin password")
        revision_id, original_self_test = _configure_answer(admin, f.address)
        _login(f, "query", f.employee, "synthetic employee password")

        page = f.page
        employee_submits = []
        page.on("request", lambda request: employee_submits.append(request.headers.get("idempotency-key"))
                if request.method == "POST" and request.url == f.address + "knowledge/queries" else None)
        retry = page.get_by_role("button", name="重试原查询", exact=True)
        refresh = page.get_by_role("button", name="刷新原查询状态", exact=True)
        task_id = _submit_in_browser(f, "DEMO-1 startup")
        provider.mode = "unavailable"
        failure = _worker(f, task_id)
        assert failure == {"task_id": task_id, "state": "blocked", "error_code": "RUNTIME_MODEL_FAILED"}
        refresh.click()
        expect(page.get_by_text("查询服务暂不可用，请联系管理员确认恢复后重试原查询。", exact=True)).to_be_visible()
        expect(retry).to_be_visible()
        expect(page.get_by_role("link", name="核对原文", exact=False)).to_have_count(0)
        failed_view, first_etag = _status(f, task_id)
        assert failed_view["retry_eligible"] is True and failed_view["execution_revision"] == 1
        failure_receipt = _receipts(f, task_id, revision_id)
        assert failure_receipt["calls"][0]["http_status"] == 503 and failure_receipt["calls"][0]["state"] == "failed"
        assert _worker(f, task_id)["state"] == "idle"
        _screenshot(f, "employee-query-service-unavailable")

        admin.reload(wait_until="networkidle")
        admin.get_by_role("button", name=SERVICE_LABEL, exact=False).click()
        failed_self_test = _run_self_test(admin, "测试未通过")
        expect(failed_self_test.get_by_text("HTTP 503", exact=False)).to_be_visible()
        admin.screenshot(path=str(f.out / "admin-service-failed-self-test.png"), full_page=True)
        # Repair the controlled upstream, then bind the successful recheck for
        # new submissions without replacing the original task's accepted pin.
        provider.mode = "normal"
        _run_self_test(admin, "测试通过")
        restored_self_test = _confirm_answer_selection(admin, f.address, revision_id, "受控测试上游已恢复，确认同一修订的新自检回执")
        assert restored_self_test != original_self_test
        admin.screenshot(path=str(f.out / "admin-same-revision-rechecked.png"), full_page=True)
        assert not admin_errors, admin_errors

        retry_url = f.address + f"tasks/{task_id}/retry"
        retry_requests = []

        def lose_retry_response(route):
            request = route.request
            retry_requests.append((request.headers["idempotency-key"], request.headers["if-match"], request.post_data))
            if len(retry_requests) == 1:
                response = route.fetch()
                assert response.status == 202 and response.json()["task_id"] == task_id
                route.abort()
            else:
                route.continue_()

        page.route(retry_url, lose_retry_response)
        retry.click()
        recover = page.get_by_role("button", name="恢复原重试回执", exact=True)
        expect(recover).to_be_visible()
        page.reload(wait_until="networkidle")
        expect(recover).to_be_visible()
        _screenshot(f, "employee-original-retry-receipt-pending")
        recover.click()
        expect(recover).to_have_count(0)
        assert len(retry_requests) == 2 and retry_requests[0] == retry_requests[1]
        assert retry_requests[0][0] and retry_requests[0][1] == first_etag
        page.unroute(retry_url)
        recovered, _ = _status(f, task_id)
        assert recovered["execution_revision"] == 2 and recovered["retry_eligible"] is False
        assert recovered["effective_input"] == failed_view["effective_input"]
        assert recovered["conversation_id"] == failed_view["conversation_id"]
        assert _worker(f, task_id)["state"] == "succeeded"
        expect(page.get_by_text("已找到可核对依据", exact=True)).to_be_visible(timeout=30000)
        result = page.locator(".result-panel")
        expect(result).to_contain_text(STARTUP)
        expect(result).to_contain_text(WARNING)
        link = result.get_by_role("link", name="核对原文", exact=False).first.get_attribute("href")
        assert link.endswith("#page=1")
        source = f.context.request.get(f.address.rstrip("/") + link.split("#")[0])
        assert source.status == 200 and source.body() == synthetic_pdf()
        retried_receipt = _receipts(f, task_id, revision_id)
        assert retried_receipt["pin_id"] == failure_receipt["pin_id"]
        assert [call["state"] for call in retried_receipt["calls"]] == ["failed", "received"]
        assert retried_receipt["calls"][0] == failure_receipt["calls"][0]
        assert retried_receipt["reserved_calls"] == 2 and retried_receipt["reserved_tokens"] > failure_receipt["reserved_tokens"]
        assert len(employee_submits) == 1
        _screenshot(f, "employee-same-query-recovered")
        f.checks.append({"scenario": "F known HTTP 503", "task_id": task_id, "synthetic_version": f.version.id,
                         "react_bundle": page.locator('script[src^="/workspace/assets/"]').first.get_attribute("src"),
                         "service_revision": revision_id, "failed": failure_receipt, "recovered": retried_receipt,
                         "retry_receipt_loss_replayed_with_same_key_etag_and_body": True, "query_submits": 1,
                         "original_admission_self_test": original_self_test, "system_admin_reselected_self_test": restored_self_test,
                         "upstream_repair": "controlled loopback",
                         "source_warning_and_exact_pdf": True})

        unknown_id = _submit_in_browser(f, "DEMO-1 startup service receipt check")
        provider.mode = "incomplete-stream"
        assert _worker(f, unknown_id)["state"] == "reconciliation_required"
        page.reload(wait_until="networkidle")
        expect(page.get_by_text("原查询的服务请求结果待核对，请联系管理员查看原任务。", exact=True)).to_be_visible()
        expect(retry).to_have_count(0)
        expect(page.get_by_role("link", name="核对原文", exact=False)).to_have_count(0)
        unknown, unknown_etag = _status(f, unknown_id)
        assert unknown["retry_eligible"] is False and unknown["execution_revision"] == 1
        unknown_receipt = _receipts(f, unknown_id, revision_id)
        assert unknown_receipt["calls"][0]["http_status"] == 200 and unknown_receipt["calls"][0]["state"] == "unknown"
        physical_calls = len(provider.requests)
        provider.mode = "normal"
        for _ in range(2):
            denied = f.client.post(f"/tasks/{unknown_id}/retry", json={"reason": "SYNTHETIC probe after an incomplete stream"},
                                   headers={"Idempotency-Key": uuid4().hex, "If-Match": unknown_etag})
            assert denied.status_code == 409 and denied.json()["code"] == "MODEL_RECONCILIATION_REQUIRED"
        assert _worker(f, unknown_id)["state"] == "idle" and len(provider.requests) == physical_calls
        assert _receipts(f, unknown_id, revision_id) == unknown_receipt
        assert f.client.get(f"/tasks/{unknown_id}/result").status_code == 409
        assert f.db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == unknown_id)) is None
        _screenshot(f, "employee-unknown-remote-outcome")
        f.checks.append({"scenario": "F incomplete physical stream", "task_id": unknown_id, "receipt": unknown_receipt,
                         "fresh_keys_denied": 2, "extra_physical_requests": 0, "result_published": False})
        assert len(employee_submits) == len(set(employee_submits)) == 2
        assert not f.upstream  # All business generation used the explicitly selected loopback revision.
        assert len([request for request in provider.requests if json.loads(request.body).get("stream") is True]) == 3


@pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
                    reason="Requires the explicit isolated local PostgreSQL container")
def test_react_index_unavailable_keeps_original_query_blocked(failure_workspace):
    from playwright.sync_api import expect

    f, provider = failure_workspace
    page = f.page
    _login(f, "services", f.administrator, "synthetic admin password")
    revision_id, _ = _configure_answer(page, f.address)
    page.get_by_role("button", name="退出", exact=True).click()
    page.get_by_role("button", name="登录", exact=True).wait_for()
    _login(f, "query", f.employee, "synthetic employee password")
    kb = f.db.get(KnowledgeBase, f.kb.id)
    kb.active_generation_id = None  # Simulate the authorized corpus awaiting its first active index.
    f.db.commit()
    task_id = _submit_in_browser(f, "DEMO-1 startup pending index check")
    assert _worker(f, task_id) == {"task_id": task_id, "state": "blocked", "error_code": "INDEX_NOT_READY"}
    page.get_by_role("button", name="刷新原查询状态", exact=True).click()
    expect(page.get_by_text("资料尚未准备好查询。请联系知识管理员确认索引可用，再重试原查询。", exact=True)).to_be_visible()
    expect(page.get_by_role("link", name="核对原文", exact=False)).to_have_count(0)
    assert f.client.get(f"/tasks/{task_id}/result").status_code == 409
    view, _ = _status(f, task_id)
    assert "index_unavailable" in view["warnings"] and view["retry_eligible"] is True
    assert not f.upstream
    assert len([request for request in provider.requests if json.loads(request.body).get("stream") is True]) == 1
    _screenshot(f, "employee-index-not-ready")
    f.checks.append({"scenario": "F index unavailable", "task_id": task_id, "warning": "index_unavailable",
                    "react_bundle": page.locator('script[src^="/workspace/assets/"]').first.get_attribute("src"),
                    "receipt": _receipts(f, task_id, revision_id), "result_published": False})


def test_retry_dispatch_survives_late_worker_close(execution_http, monkeypatch):
    f = execution_http
    task_id = f.task.id
    f.task.status = "blocked"
    f.task.revision += 1
    f.db.commit()
    view = f.client.get(f"/tasks/{task_id}")
    assert view.status_code == 200 and view.json()["retry_eligible"] is True
    original_row = gate._row
    accepted = []

    with f.factory(autoflush=False) as closing_db:
        def close_with_an_intervening_retry(db, cls, identity):
            item = original_row(db, cls, identity)
            if db is closing_db and cls is QueryTask and identity == task_id:
                assert item.status == "blocked"
                response = f.client.post(f"/tasks/{task_id}/retry", json={"reason": "SYNTHETIC service recovered"},
                    headers={"Idempotency-Key": uuid4().hex, "If-Match": view.headers["ETag"]})
                assert response.status_code == 202 and response.json()["task_id"] == task_id
                accepted.append(response.json())
            return item

        with monkeypatch.context() as resume_while_closing:
            resume_while_closing.setattr(gate, "_row", close_with_an_intervening_retry)
            gate.close_run(closing_db, f.run.id, f.token, state="interrupted")

    assert len(accepted) == 1
    f.db.expire_all()
    assert f.task.status == "pending" and f.task.execution_revision == 2
    step = f.db.get(TaskStep, f.run.step_id)
    assert step.status == "pending" and step.lease_owner is None and step.lease_until is None
    assert step.lease_epoch > f.run.step_lease_epoch
    old_attempt = f.db.get(TaskAttempt, f.run.attempt_id)
    assert old_attempt.status == "blocked" and old_attempt.finished_at is not None
    pending = f.db.scalars(select(TaskOutbox).where(TaskOutbox.step_id == step.id, TaskOutbox.status == "pending")).all()
    assert len(pending) == 1
    next_run, _ = gate.open_run(f.db, task_id)
    assert next_run.step_id == step.id and next_run.execution_revision == 2 and next_run.attempt_id != f.run.attempt_id


@pytest.mark.parametrize(("restriction", "status", "code"), [
    ("exhausted_budget", 409, "TASK_BUDGET_EXHAUSTED"),
    ("empty_scope", 403, "ACCESS_LIMITED"),
])
def test_retry_visibility_matches_server_gate_for_permanent_blockers(execution_http, restriction, status, code):
    f = execution_http
    f.task.status = "blocked"
    f.task.revision += 1
    budget = f.db.get(TaskBudget, f.task.id)
    if restriction == "exhausted_budget":
        budget.reserved_calls = budget.max_calls
    else:
        f.task.accepted_scope["knowledge_base_ids"] = []
    f.db.commit()
    task_url = f"/tasks/{f.task.id}"
    view = f.client.get(task_url)
    assert view.status_code == 200 and view.json()["retry_eligible"] is False
    denied = f.client.post(task_url + "/retry", json={"reason": "SYNTHETIC recovery gate"},
                           headers={"Idempotency-Key": uuid4().hex, "If-Match": view.headers["ETag"]})
    assert denied.status_code == status and denied.json()["code"] == code
    received = f.client.get(task_url).json()
    assert received["execution_revision"] == view.json()["execution_revision"] and received["status"] == "blocked"
    assert not f.db.scalar(select(TaskOutbox.id).where(TaskOutbox.step_id == f.run.step_id))
