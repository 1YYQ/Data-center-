"""Opt-in desktop acceptance for lazy history and bounded query admission."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from .app import create_app
from .config import Settings
from .db import init_db
from .models import AuthorizationGrant, BusinessApproval, KnowledgeBase, QueryTask, User
from .security import hash_password
from .test_child_answer_pipeline import _api_gateway
from .testing_governance import seed_verified_approval


CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
USERNAME = "synthetic-capacity-employee"
PASSWORD = "synthetic capacity browser password"
MODEL = "SYNTHETIC-CAPACITY-1"
CAPACITY = 4


def _seed_employee(app):
    with app.state.session_factory() as db:
        employee = User(username=USERNAME, display_name="合成容量验收员工", roles=["employee"], status="active",
                        credential_state="active", credential_hash=hash_password(PASSWORD))
        library = KnowledgeBase(name="SYNTHETIC capacity library")
        db.add_all([employee, library])
        db.flush()
        scope = {"knowledge_base_id": library.id, "document_id": None, "level": "internal",
                 "category": "manual", "need_class": "maintenance"}
        approval = BusinessApproval(applicant=employee.id, recorded_by=employee.id, scope=scope, operations=["knowledge.query"])
        db.add(approval)
        seed_verified_approval(db, approval)
        db.add(AuthorizationGrant(subject_user_id=employee.id, operation="knowledge.query", approval_id=approval.id,
                                  granted_by=employee.id, valid_until=approval.valid_until, **scope))
        db.commit()
        return employee.id


def _create_history(gateway):
    names, pending = [], []
    with httpx.Client(base_url=gateway, trust_env=False) as client:
        login = client.post("/auth/login", json={"username": USERNAME, "password": PASSWORD})
        assert login.status_code == 200, login.text
        client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        for number in range(11):
            question = f"SYNTHETIC capacity history {number:02d}"
            result = client.post("/knowledge/queries", json={"query": question, "model": MODEL},
                                 headers={"Idempotency-Key": str(uuid4())})
            assert result.status_code == 202 and result.json()["status"] == "pending", result.text
            task_id = result.json()["task_id"]
            names.append(question)
            if number < 8:
                task = client.get(f"/tasks/{task_id}")
                cancelled = client.post(f"/tasks/{task_id}/cancel", json={"reason": "SYNTHETIC history fixture"},
                    headers={"Idempotency-Key": str(uuid4()), "If-Match": task.headers["ETag"]})
                assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled", cancelled.text
            else:
                pending.append(task_id)
    return list(reversed(names)), pending


@pytest.mark.skipif(os.environ.get("DATACENTER_TEST_QUERY_CAPACITY_BROWSER") != "1", reason="explicit local desktop acceptance")
def test_query_capacity_browser_history_and_full_queue(tmp_path, record_testsuite_property):
    from playwright.sync_api import expect, sync_playwright

    assert CHROME.is_file(), "Installed Chrome is required for this acceptance run"
    dist = Path("src/web/dist")
    assert (dist / "index.html").is_file(), "Build the formal React workspace first"
    output = Path(os.environ.get("DATACENTER_CAPACITY_BROWSER_EVIDENCE", "outputs/pilot-capacity/2026-11-19/browser"))
    output.mkdir(parents=True, exist_ok=True)
    report = {"status": "INCOMPLETE", "scope": "desktop Chrome, production HTTP and fresh migrated SQLite; synthetic employee and query inputs; no worker or model",
              "max_inflight": CAPACITY, "history_requests": [], "browser_errors": [], "external_page_requests": []}
    fresh = tmp_path / "query-capacity.db"
    assert not fresh.exists()
    settings = Settings(database_url="sqlite:///" + fresh.as_posix(), storage_dir=tmp_path / "storage",
                        query_service_mode="local", query_max_inflight=CAPACITY, secure_cookie=False)
    app = create_app(settings)

    def screenshot(page, name):
        page.screenshot(path=str(output / (name + ".png")), full_page=True)

    try:
        init_db(app.state.engine, settings)
        employee_id = _seed_employee(app)
        with _api_gateway(app) as (gateway, _), sync_playwright() as playwright:
            names, pending = _create_history(gateway)
            browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME), args=[
                "--disable-background-networking", "--disable-component-update", "--no-first-run", "--proxy-server=http://127.0.0.1:1"])
            try:
                report["browser"] = browser.version
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
                page = context.new_page()
                page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                submitted = []

                def observe(request):
                    location = urlsplit(request.url)
                    if location.scheme not in {"http", "https"}:
                        return
                    if not request.url.startswith(gateway + "/"):
                        report["external_page_requests"].append(request.url)
                    elif request.method == "GET" and location.path == "/tasks":
                        report["history_requests"].append(parse_qs(location.query))
                    elif request.method == "POST" and location.path == "/knowledge/queries":
                        submitted.append(request.url)

                page.on("request", observe)
                page.goto(gateway + "/workspace/query", wait_until="networkidle")
                bundle = page.locator('script[src^="/workspace/assets/"]').get_attribute("src")
                report["served_bundle"] = bundle
                report["served_bundle_sha256"] = hashlib.sha256((dist / bundle.removeprefix("/workspace/")).read_bytes()).hexdigest()
                page.get_by_label("账号", exact=True).fill(USERNAME)
                page.get_by_label("口令", exact=True).fill(PASSWORD)
                page.get_by_role("button", name="登录", exact=True).click()
                history = page.locator(".query-panel details")
                summary = history.locator("summary")
                expect(summary).to_have_text("我的查询记录")
                page.wait_for_load_state("networkidle")
                assert not history.evaluate("details => details.open") and report["history_requests"] == []
                report["initial_collapsed_history_requests"] = 0

                def history_page(button, offset, total_visible):
                    uri = f"/tasks?limit=5&offset={offset}"
                    with page.expect_response(lambda response: response.url == gateway + uri) as listed:
                        button.click()
                    assert listed.value.status == 200
                    items = listed.value.json()
                    expected = names[offset:offset + 5]
                    assert [entry["input"]["query"] for entry in items["items"]] == expected
                    assert items["next_offset"] == (offset + 5 if offset + 5 < len(names) else None)
                    rows = history.locator("ul > li button")
                    expect(rows).to_have_text(names[:total_visible])
                    return len(expected)

                load_more = history.get_by_role("button", name="加载更多查询记录", exact=True)
                report["history_page_sizes"] = [history_page(summary, 0, 5), history_page(load_more, 5, 10), history_page(load_more, 10, 11)]
                expect(load_more).to_have_count(0)
                report["history_refresh_size"] = history_page(history.get_by_role("button", name="刷新查询记录", exact=True), 0, 5)
                screenshot(page, "01-history-refresh-first-page")
                assert report["history_requests"] == [{"limit": ["5"], "offset": [str(offset)]} for offset in (0, 5, 10, 0)]
                summary.click()

                form = page.locator(".query-panel form").first
                question = form.locator("textarea")
                form.get_by_label("设备型号（可选）", exact=True).fill(MODEL)
                submit = form.get_by_role("button", name="提交查询", exact=True)
                query_url = gateway + "/knowledge/queries"
                question.fill("SYNTHETIC fourth query accepted from Chrome")
                with page.expect_response(lambda response: response.url == query_url and response.request.method == "POST") as first:
                    submit.click()
                assert first.value.status == 202 and first.value.json()["status"] == "pending"
                original_id = first.value.json()["task_id"]
                expect(page.get_by_text("查询已排队", exact=True)).to_be_visible()
                expect(page.get_by_text("查询已保存，正在等待执行。关闭或刷新不会重复提交。", exact=True)).to_be_visible()
                report["queued_acceptance"] = {"status": first.value.status, "task_id": original_id}
                screenshot(page, "02-pending-queue")

                question.fill("SYNTHETIC fifth query rejected at capacity")
                with page.expect_response(lambda response: response.url == query_url and response.request.method == "POST") as refused:
                    submit.click()
                assert refused.value.status == 429 and refused.value.json()["code"] == "QUERY_CAPACITY_FULL"
                assert "task_id" not in refused.value.json()
                expect(page.get_by_text("尚未提交", exact=True)).to_be_visible()
                expect(page.get_by_text("当前查询较多，本次未创建新任务。请稍后再提交。", exact=True)).to_be_visible()
                expect(page.get_by_role("button", name="恢复原提交", exact=True)).to_have_count(0)
                screenshot(page, "03-capacity-rejection-keeps-original")
                report["capacity_rejection"] = {"status": refused.value.status, "code": refused.value.json()["code"]}

                status_url = gateway + f"/tasks/{original_id}/status"
                with page.expect_response(lambda response: response.url == status_url) as refreshed:
                    page.get_by_role("button", name="刷新原查询状态", exact=True).click()
                assert refreshed.value.json()["id"] == original_id and refreshed.value.json()["status"] == "pending"
                expect(page.get_by_text("查询已排队", exact=True)).to_be_visible()
                with page.expect_response(lambda response: response.url == status_url) as reopened:
                    page.reload()
                assert reopened.value.json()["id"] == original_id and reopened.value.json()["status"] == "pending"
                expect(page.get_by_text("查询已排队", exact=True)).to_be_visible()
                assert not history.evaluate("details => details.open") and len(report["history_requests"]) == 4
                assert len(submitted) == 2
                screenshot(page, "04-reopen-original-pending-task")

                with app.state.session_factory() as db:
                    saved = list(db.scalars(select(QueryTask).where(QueryTask.owner_user_id == employee_id)))
                    assert len(saved) == len(names) + 1
                    assert {task.id for task in saved if task.status == "pending"} == {*pending, original_id}
                report["original_reopened"] = {"task_id": original_id, "state": "pending", "browser_submission_count": len(submitted)}
                report["persisted_pending_count"] = CAPACITY
                assert not report["browser_errors"] and not report["external_page_requests"]
                report["status"] = "PASS"
                record_testsuite_property("served_react_bundle", bundle)
            finally:
                browser.close()
    finally:
        app.state.engine.dispose()
        (output / "observations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
