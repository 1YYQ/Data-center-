"""Observed browser states for the local synthetic reference client only."""
import hashlib
import json
import os
from uuid import uuid4
from pathlib import Path

import pytest
from sqlalchemy import select

from .models import AuthorizationGrant, QueryTask, User, utc_now
from .test_worker import STARTUP, WARNING, local_stack, synthetic_pdf
from .test_services import fixture_db
from .worker import run_one


def test_local_browser_wait_resume_restore_citations_and_roles(local_stack):
    playwright = pytest.importorskip("playwright.sync_api")
    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    if not chrome.exists():
        pytest.skip("Explicit local Chrome unavailable; browser acceptance remains unverified")
    db, config, api_client, address, employee, admin, kb, document, version, upstream, _ = local_stack
    out = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/local-browser")) / "static-query" / uuid4().hex
    out.mkdir(parents=True, exist_ok=True)
    posts, errors = [], []

    def login(page, username, password):
        page.goto(address)
        page.wait_for_load_state("networkidle")
        page.get_by_label("账号", exact=True).fill(username)
        page.get_by_label("口令", exact=True).fill(password)
        page.get_by_role("button", name="登录", exact=True).click()
        page.wait_for_selector("#workspace:not(.hidden)")

    with playwright.sync_playwright() as p:
        browser = p.chromium.launch(headless=True, executable_path=str(chrome), args=["--disable-background-networking", "--disable-component-update"])
        context = browser.new_context(viewport={"width": 1366, "height": 900}, service_workers="block")
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(address) or not route.request.url.startswith(("http:", "https:")) else route.abort())
        page = context.new_page()
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: posts.append(request.url) if request.method == "POST" and ("/knowledge/queries" in request.url or "/responses" in request.url) else None)
        login(page, employee.username, "synthetic employee password")
        page.get_by_label("问题", exact=True).fill("DEMO-1 startup")
        page.get_by_role("button", name="提交查询", exact=True).click()
        page.wait_for_function("document.querySelector('#result-badge').textContent === '正在查询'")
        task_id = db.scalar(select(QueryTask.id).where(QueryTask.owner_user_id == employee.id))
        assert task_id and run_one(config, gateway_url=address)["state"] == "waiting_input"
        page.get_by_role("button", name="刷新原查询状态").click()
        page.wait_for_selector("#input-form:not(.hidden)")
        page.screenshot(path=str(out / "waiting-input.png"), full_page=True)
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_selector("#input-form:not(.hidden)")
        page.get_by_label("补充设备型号", exact=True).fill("DEMO-1")
        page.get_by_role("button", name="补充并继续原任务").click()
        page.wait_for_function("document.querySelector('#result-badge').textContent === '正在查询'")
        outcome = run_one(config, gateway_url=address)
        assert outcome["state"] == "succeeded", outcome
        page.wait_for_function("document.querySelector('#result-badge').textContent === '已找到可核对依据'", timeout=15000)
        assert STARTUP in page.locator("#result").inner_text() and WARNING in page.locator("#result").inner_text()
        assert "MODEL PROSE" not in page.locator("#result").inner_text()
        page.screenshot(path=str(out / "query-result.png"), full_page=True)
        href = page.locator("#result a").first.get_attribute("href")
        assert href.endswith("#page=1")
        source = context.request.get(address.rstrip("/") + href.split("#")[0])
        assert source.status == 200 and source.body() == synthetic_pdf()
        (out / "SYNTHETIC-source.pdf").write_bytes(source.body())
        import fitz
        with fitz.open(stream=source.body(), filetype="pdf") as pdf:
            assert STARTUP in pdf[0].get_text() and WARNING in pdf[0].get_text()
            pdf[0].get_pixmap(dpi=120).save(out / "synthetic-source-page.png")
        context.set_offline(True)
        page.get_by_role("button", name="刷新原查询状态").click()
        page.wait_for_function("document.querySelector('#query-state').textContent.includes('网络连接异常')")
        assert STARTUP not in page.locator("#result").inner_text()
        context.set_offline(False)
        page.get_by_role("button", name="刷新原查询状态").click()
        page.wait_for_function("document.querySelector('#result-badge').textContent === '已找到可核对依据'")
        page.reload()
        page.wait_for_load_state("networkidle")
        page.wait_for_function("document.querySelector('#result-badge').textContent === '已找到可核对依据'")
        assert len([url for url in posts if "/knowledge/queries" in url]) == 1
        assert len([url for url in posts if "/responses" in url]) == 1
        db.expire_all()
        grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == employee.id, AuthorizationGrant.operation == "document.read"))
        grant.revoked_at = utc_now()
        db.commit()
        page.get_by_role("button", name="刷新原查询状态").click()
        page.wait_for_function("document.querySelector('#result-badge').textContent === '查询尚未完成'")
        assert STARTUP not in page.locator("#result").inner_text()
        assert context.request.get(address.rstrip("/") + href.split("#")[0]).status == 404
        page.screenshot(path=str(out / "access-limited.png"), full_page=True)
        system_context = browser.new_context(viewport={"width": 1366, "height": 900})
        system_page = system_context.new_page()
        login(system_page, admin.username, "synthetic admin password")
        assert system_page.locator("#query-form").is_hidden()
        assert "没有资料查询权限" in system_page.locator("#query-state").inner_text()
        assert system_context.request.get(address.rstrip("/") + href.split("#")[0]).status == 404
        system_page.screenshot(path=str(out / "system-role-boundary.png"), full_page=True)
        manager = db.get(User, document.created_by) if hasattr(document, "created_by") else db.get(User, version.created_by)
        manager_context = browser.new_context(viewport={"width": 1366, "height": 900})
        manager_page = manager_context.new_page()
        login(manager_page, manager.username, "manager password")
        assert manager_page.locator("#query-form").is_visible()
        assert manager_context.request.get(address.rstrip("/") + href.split("#")[0]).status == 200
        assert not errors, errors
        report = {"scope": "synthetic local browser/reference client; not real enterprise A-F", "browser": browser.version,
            "viewport": {"width": 1366, "height": 900}, "query_posts": 1, "input_response_posts": 1,
            "same_task_resumed": task_id, "source_sha256": hashlib.sha256(synthetic_pdf()).hexdigest(),
            "source_pdf_text_and_warning_checked": True, "refresh_and_offline_recovery": True, "source_revoke_clears_view": True,
            "employee_manager_system_role_checks": True, "page_errors": errors,
            "manager_and_system_full_management_ui": "NOT_IMPLEMENTED", "real_business_acceptance": "NOT_RUN"}
        (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        manager_context.close()
        system_context.close()
        context.close()
        browser.close()
