"""Opt-in task-workbench paging, full queue, recovery and cancellation."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import func, select

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
              "max_inflight": CAPACITY, "history_requests": [], "execution_requests": [], "browser_errors": [], "external_page_requests": []}
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
                context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
                page = context.new_page()
                page.on("pageerror", lambda error: report["browser_errors"].append(str(error)))
                submitted = []

                def observe(request):
                    location = urlsplit(request.url)
                    if location.scheme not in {"http", "https"}:
                        return
                    if not request.url.startswith(gateway + "/"):
                        report["external_page_requests"].append(request.url)
                    elif request.method == "GET" and location.path == "/workspace-data/works":
                        report["history_requests"].append(parse_qs(location.query))
                    elif request.method == "GET" and location.path.endswith("/executions"):
                        report["execution_requests"].append(request.url)
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
                table = page.get_by_role("region", name="我的工作", exact=True)
                expect(table.locator("tbody tr")).to_have_count(10)
                assert report["history_requests"] and not report["execution_requests"]
                page.get_by_role("button", name="加载更早工作", exact=True).click()
                expect(table.locator("tbody tr")).to_have_count(1)
                assert table.locator("tbody").inner_text().find(names[-1]) >= 0
                screenshot(page, "01-bounded-work-pages")

                def submit(question):
                    page.goto(gateway + "/workspace/query?new=1")
                    page.get_by_label("问题", exact=True).fill(question)
                    page.get_by_label("设备型号（可选）", exact=True).fill(MODEL)
                    with page.expect_response(lambda response: response.url.endswith("/knowledge/queries") and response.request.method == "POST") as reply:
                        page.get_by_role("button", name="提交工作", exact=False).click()
                    return reply.value

                accepted = submit("SYNTHETIC fourth admitted work")
                assert accepted.status == 202
                original_id = accepted.json()["task_id"]
                expect(page.locator(".badge")).to_have_text("排队中")
                refused = submit("SYNTHETIC fifth refused work")
                assert refused.status == 429
                expect(page.get_by_text("执行未受理", exact=True)).to_be_visible()
                expect(page.locator(".task-workbench")).to_contain_text("本次执行未受理")
                assert len(submitted) == 2
                screenshot(page, "02-full-queue-refuses-without-new-task")
                with app.state.session_factory() as db:
                    saved = list(db.scalars(select(QueryTask).where(QueryTask.owner_user_id == employee_id)))
                    assert len(saved) == len(names) + 1
                    assert {task.id for task in saved if task.status == "pending"} == {*pending, original_id}

                page.goto(gateway + "/workspace/query?task=" + original_id)
                expect(page.locator(".badge")).to_have_text("排队中")
                page.reload()
                expect(page.locator(".badge")).to_have_text("排队中")
                assert len(submitted) == 2
                screenshot(page, "03-restored-original-work")
                page.get_by_role("button", name="取消本次执行", exact=True).click()
                expect(page.locator(".badge")).to_have_text("已取消")
                with app.state.session_factory() as db:
                    assert db.get(QueryTask, original_id).status == "cancelled"
                    assert db.scalar(select(func.count()).select_from(QueryTask).where(QueryTask.status == "pending")) == CAPACITY - 1
                page.reload()
                expect(page.locator(".badge")).to_have_text("已取消")
                assert len(submitted) == 2
                screenshot(page, "04-server-confirmed-cancellation")
                report["original_reopened"] = {"task_id": original_id, "state": "cancelled", "browser_submission_count": len(submitted)}
                report["full_queue_pending_count"] = CAPACITY
                report["post_cancellation_pending_count"] = CAPACITY - 1
                assert not report["browser_errors"] and not report["external_page_requests"]
                report["status"] = "PASS"
                record_testsuite_property("served_react_bundle", bundle)
            finally:
                browser.close()
    finally:
        app.state.engine.dispose()
        # Only this fixture's exact newly created database and SQLite sidecars.
        for target in (fresh, Path(str(fresh) + "-wal"), Path(str(fresh) + "-shm")):
            assert target.parent.resolve() == tmp_path.resolve() and target.name.startswith("query-capacity.db")
            target.unlink(missing_ok=True)
        report["temporary_sqlite_removed"] = not fresh.exists()
        (output / "observations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
