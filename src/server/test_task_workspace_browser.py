"""Approved task-workbench on installed Chrome, production SDK/HTTP/PG gates.

All identities, signatures, PDFs, vectors and provider responses are synthetic.
No enterprise data or paid provider is consumed by these browser checks.
"""
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from .models import AuthorizationGrant, BusinessApproval, Document, QueryTask, User, utc_now
from .security import hash_password
from .test_child_answer_pipeline import (MODEL, QUERY, STEP_ONE, STEP_TWO, WARNING, PRESSURE,
                                         child_answer_pipeline, recovery_pg, PASSWORD)
from .testing_governance import seed_verified_approval
from .worker import run_one
from .worker import runtime_artifacts
from .runtime_gate import create_local_profile
from .runtime_models import RuntimeProfile
from .test_query_endurance_recovery import _recovery_proxy, _ledger

pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="Explicit isolated local PG required")


def test_task_workbench_query_share_revoke_sources_and_recovery(child_answer_pipeline):
    from playwright.sync_api import expect, sync_playwright
    f = child_answer_pipeline
    out = Path(os.environ["DATACENTER_TEST_EVIDENCE_ROOT"]) / "task-workbench-browser"
    out.mkdir(parents=True, exist_ok=True)
    username = "synthetic-workbench-" + uuid4().hex
    with f.app.state.session_factory() as db:
        document = db.get(Document, f.document_id)
        employee = User(username=username, display_name="模拟试点员工", roles=["employee"],
                        status="active", credential_state="active", credential_hash=hash_password(PASSWORD))
        db.add(employee); db.flush()
        employee_id = employee.id
        scope = {"knowledge_base_id": f.kb_id, "document_id": None, "level": document.access_level,
                 "category": document.category, "need_class": document.need_class}
        for operation in ("document.read", "knowledge.query"):
            approval = BusinessApproval(applicant=employee.id, recorded_by=f.actor_id, scope=scope, operations=[operation])
            db.add(approval); db.flush(); seed_verified_approval(db, approval)
            db.add(AuthorizationGrant(subject_user_id=employee.id, operation=operation, approval_id=approval.id,
                                      valid_until=approval.valid_until, granted_by=f.actor_id, **scope))
        db.commit()
    checks, errors, posts = [], [], []
    ledger_fixture = SimpleNamespace(app=f.app, actor_id=employee_id)
    chrome = "C:/Program Files/Google/Chrome/Application/chrome.exe"
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path=chrome,
            args=["--disable-background-networking", "--disable-component-update", "--no-first-run"])
        contexts = []
        def context():
            item = browser.new_context(viewport={"width":1440,"height":900}, service_workers="block")
            item.route("**/*", lambda route: route.continue_() if route.request.url.startswith(f.gateway + "/")
                       or not route.request.url.startswith(("http:", "https:")) else route.abort())
            contexts.append(item)
            return item
        owner = context(); page = owner.new_page(); page.set_default_timeout(20000)
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("request", lambda req: posts.append(req.headers.get("idempotency-key"))
                if req.method == "POST" and req.url.endswith("/knowledge/queries") else None)
        def login(target, name):
            target.goto(f.gateway + "/workspace/query")
            target.get_by_label("账号", exact=True).fill(name)
            target.get_by_label("口令", exact=True).fill(PASSWORD)
            target.get_by_role("button", name="登录", exact=True).click()
            target.get_by_role("button", name="退出", exact=True).wait_for()
        try:
            login(page, username)
            for width in (1440,1366):
                page.set_viewport_size({"width":width,"height":900})
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(out/f"employee-home-{width}.png"), full_page=True)
            def lose_reply(route):
                assert route.fetch().status == 202
                route.abort("failed")
            page.route("**/knowledge/queries", lose_reply, times=1)
            page.get_by_label("问题", exact=True).fill(QUERY)
            page.get_by_role("button", name="查询资料", exact=False).click()
            page.get_by_text("原操作结果待核对", exact=True).wait_for()
            with f.app.state.session_factory() as db:
                task = db.scalar(select(QueryTask).where(QueryTask.owner_user_id == employee_id))
                assert task and task.status == "pending"; task_id = task.id
            page.reload(wait_until="networkidle")
            assert QUERY not in page.evaluate("JSON.stringify(Object.values(sessionStorage))")
            page.get_by_role("button", name="恢复原提交", exact=True).click()
            expect(page.locator(".badge")).to_have_text("排队中")
            assert len(posts) == 1
            with f.app.state.session_factory() as db:
                assert db.scalar(select(func.count()).select_from(QueryTask).where(QueryTask.owner_user_id == employee_id)) == 1
            assert run_one(f.config, gateway_url=f.gateway, task_id=task_id)["state"] == "waiting_input"
            page.get_by_role("button", name="刷新原查询状态", exact=True).click()
            page.get_by_label("补充设备型号", exact=True).fill(MODEL)
            page.screenshot(path=str(out/"employee-waiting-input.png"), full_page=True)
            page.get_by_role("button", name="补充并继续原任务", exact=True).click()
            expect(page.locator(".badge")).to_have_text("排队中")
            result = run_one(f.config, gateway_url=f.gateway, task_id=task_id)
            assert result["state"] == "succeeded", result
            page.get_by_role("button", name="刷新原查询状态", exact=True).click()
            expect(page.locator(".badge")).to_have_text("已完成",timeout=25000)
            for passage in (STEP_ONE, STEP_TWO, WARNING, PRESSURE):
                expect(page.locator(".answer")).to_contain_text(passage)
            page.get_by_label("问题", exact=True).fill("临时追问草稿，不应因查证丢失")
            page.locator(".result-sources > summary").click()
            page.get_by_role("link", name="核对原文 · PDF 第 1 页", exact=True).first.click()
            expect(page.locator("dialog[open] iframe")).to_be_visible()
            response = owner.request.get(f.gateway + f"/documents/{f.document_id}/versions/{f.version_id}/file")
            assert response.status == 200 and response.body() == f.original
            page.screenshot(path=str(out/"employee-source-reader.png"), full_page=True)
            page.screenshot(path=str(out/"employee-source-reader-viewport.png"))
            page.get_by_role("button", name="返回原工作", exact=True).click()
            expect(page.get_by_label("问题", exact=True)).to_have_value("临时追问草稿，不应因查证丢失")
            page.get_by_label("问题", exact=True).fill("")
            page.screenshot(path=str(out/"employee-formal-result-1366.png"), full_page=True)
            # A temporary identity-read failure hides protected views, but is
            # not treated as proof of logout or permission to repeat a write.
            page.route("**/me", lambda route: route.fulfill(status=503, content_type="application/json",
                body='{"detail":{"message":"SYNTHETIC temporary identity-read failure"}}'), times=1)
            page.evaluate("window.dispatchEvent(new Event('focus'))")
            expect(page.get_by_role("heading", name="暂时无法核对会话", exact=True)).to_be_visible()
            assert page.locator(".answer, iframe").count() == 0
            assert len(posts) == 1
            page.get_by_role("button", name="重新核对会话", exact=True).click()
            expect(page.locator(".answer")).to_contain_text(WARNING, timeout=25000)
            checks.append("temporary identity-read failure hides protected views; read-only recheck restores original work with no extra POST")
            assert len(posts) == 1
            page.get_by_role("button", name="分享这条回答", exact=True).click()
            sharing = page.get_by_role("region", name="单条回答分享", exact=True)
            sharing.get_by_label("接收者用户编号", exact=True).fill(f.recipient_id)
            sharing.get_by_role("button", name="确认分享", exact=True).click()
            expect(sharing.get_by_role("button", name=f"撤回对 {f.recipient_id} 的分享", exact=True)).to_be_visible()
            recipient = context(); received = recipient.new_page(); received.set_default_timeout(25000)
            login(received, f.recipient_name)
            received.get_by_role("button", name="查看结果", exact=False).first.click()
            expect(received.locator(".answer")).to_contain_text(STEP_ONE)
            assert received.get_by_label("问题", exact=True).count() == 0
            received.locator(".result-sources > summary").click()
            received.get_by_role("link", name="核对原文 · PDF 第 1 页", exact=True).first.click()
            expect(received.locator("dialog[open] iframe")).to_be_visible()
            sharing.get_by_role("button", name=f"撤回对 {f.recipient_id} 的分享", exact=True).click()
            expect(received.locator(".answer")).to_have_count(0, timeout=25000)
            expect(received.locator("iframe")).to_have_count(0)
            assert recipient.request.get(f.gateway + f"/tasks/{task_id}/result").status == 404
            received.screenshot(path=str(out/"recipient-share-revoked.png"), full_page=True)
            checks += ["committed submit response lost: GET receipt only, one POST and one original task",
                       "missing model resumes original execution through actual SDK/HTTP/PGvector source gates",
                       "formal six-source result preserves exact procedure, warning, pressure and original PDF bytes",
                       "controlled original reader returns to same draft without navigation loss",
                       "single-answer recipient cannot query or read owner input; revoke clears answer and open original"]
            # A same-conversation follow-up freezes current conditions and creates
            # a later execution without overwriting the original answer/share.
            page.get_by_label("问题", exact=True).fill(QUERY)
            page.get_by_role("button", name="继续查询", exact=True).click()
            expect(page.locator(".badge")).to_have_text("排队中")
            with f.app.state.session_factory() as db:
                tasks = db.scalars(select(QueryTask).where(QueryTask.owner_user_id == employee_id).order_by(QueryTask.created_at)).all()
                assert len(tasks) == 2 and tasks[0].conversation_id == tasks[1].conversation_id
                assert tasks[1].effective_input["model"] == MODEL
                followup = tasks[1].id
            assert run_one(f.config, gateway_url=f.gateway, task_id=followup)["state"] == "succeeded"
            page.get_by_role("button", name="刷新原查询状态", exact=True).click()
            expect(page.locator(".badge")).to_have_text("已完成",timeout=25000)
            page.reload(wait_until="networkidle")
            expect(page.locator(".answer")).to_contain_text(WARNING)
            owner.set_offline(True)
            expect(page.locator(".answer")).to_have_count(0)
            owner.set_offline(False)
            page.get_by_role("button", name="刷新原查询状态", exact=True).click()
            expect(page.locator(".answer")).to_contain_text(WARNING)
            checks.append("same-conversation follow-up inherits confirmed scope, preserves old task, refresh/offline safely restores")
            page.get_by_text("本项工作的历史执行与结果", exact=True).click()
            page.locator(f'a[href="/workspace/query?task={task_id}&history=1"]').click()
            expect(page.get_by_text("正在只读查看历史执行。", exact=False)).to_be_visible()
            assert page.locator(".work-composer").count() == 0
            assert page.evaluate("(owner)=>JSON.parse(sessionStorage.getItem('dc-workbench:'+owner)).task",employee_id) == followup
            page.get_by_role("link", name="返回当前工作", exact=True).click()
            expect(page.locator(".answer")).to_contain_text(WARNING)
            page.goto(f.gateway + "/workspace/query?manual=" + f.version_id)
            expect(page.locator("iframe")).to_be_visible()
            assert page.get_by_role("heading", name="资料查阅", exact=True).count() == 1
            checks.append("historical execution is isolated read-only and preserves latest work; legacy exact-version manual link still resolves")
            assert len(posts) == 2 and not errors
            recovery = {}
            with _recovery_proxy(f.model.port) as proxy:
                profile_id = "workspace-recovery"
                with f.app.state.session_factory() as db:
                    artifact = runtime_artifacts()
                    original_profile = db.get(RuntimeProfile,("local-pi",1))
                    profile = create_local_profile(db, profile_id=profile_id, upstream_port=proxy.port,
                        artifact_manifest_hash=artifact["artifact_manifest_hash"], adapter_fingerprint=artifact["adapter_fingerprint"], artifact_manifest=artifact["manifest"],
                        synthetic_context_window=original_profile.configuration["generation"]["context_window"],
                        max_calls=original_profile.configuration["max_calls"],max_reserved_tokens=original_profile.configuration["max_reserved_tokens"])
                    assert profile.configuration["gateway_policy"]==original_profile.configuration["gateway_policy"]
                    assert profile.configuration["generation"]["context_window"]==original_profile.configuration["generation"]["context_window"]
                def submit_recovery():
                    page.goto(f.gateway + "/workspace/query?new=1")
                    page.get_by_label("问题",exact=True).fill(QUERY)
                    page.get_by_label("设备型号（可选）",exact=True).fill(MODEL)
                    page.get_by_role("button",name="查询资料",exact=False).click()
                    expect(page.locator(".badge")).to_have_text("排队中")
                    with f.app.state.session_factory() as db:
                        return db.scalar(select(QueryTask.id).where(QueryTask.owner_user_id==employee_id).order_by(QueryTask.created_at.desc()).limit(1))
                proxy.mode = "reject"
                failed_id = submit_recovery()
                assert run_one(f.config,gateway_url=f.gateway,profile_id=profile_id,task_id=failed_id)["state"] == "blocked"
                failed = _ledger(ledger_fixture,failed_id)
                page.get_by_role("button",name="刷新原查询状态",exact=True).click(timeout=35000)
                expect(page.get_by_role("button",name="重试原查询",exact=True)).to_be_visible()
                page.screenshot(path=str(out/"employee-known-service-failure.png"),full_page=True)
                system_context = context(); system_page = system_context.new_page()
                login(system_page,f.admin_name)
                system_page.goto(f.gateway+"/workspace/operations")
                expect(system_page.get_by_role("table")).to_contain_text(failed_id)
                assert QUERY not in system_page.locator("body").inner_text()
                assert system_context.request.get(f.gateway+f"/tasks/{failed_id}/result").status==404
                system_page.screenshot(path=str(out/"system-recovery-1440.png"),full_page=True)
                system_page.set_viewport_size({"width":1366,"height":900})
                assert system_page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                system_page.screenshot(path=str(out/"system-recovery-1366.png"),full_page=True)
                proxy.mode = "forward"  # Test-controlled upstream service recovery.
                page.get_by_role("button",name="重试原查询",exact=True).click()
                expect(page.locator(".badge")).to_have_text("排队中")
                recovery_outcome=run_one(f.config,gateway_url=f.gateway,profile_id=profile_id,task_id=failed_id)
                (out/"recovery-checkpoint.json").write_text(json.dumps({"outcome":recovery_outcome,"ledger":_ledger(ledger_fixture,failed_id),"proxy_requests":proxy.requests},ensure_ascii=False,indent=2),encoding="utf-8")
                assert recovery_outcome["state"]=="succeeded",recovery_outcome
                page.get_by_role("button",name="刷新原查询状态",exact=True).click(timeout=35000)
                expect(page.locator(".badge")).to_have_text("已完成",timeout=25000)
                repaired=_ledger(ledger_fixture,failed_id)
                assert repaired["step_id"]==failed["step_id"] and repaired["binding_id"]==failed["binding_id"]
                assert repaired["calls"][0]==failed["calls"][0] and len(repaired["calls"])==4
                proxy.mode="truncate"
                unknown_id=submit_recovery()
                assert run_one(f.config,gateway_url=f.gateway,profile_id=profile_id,task_id=unknown_id)["state"]=="reconciliation_required"
                unknown_ledger=_ledger(ledger_fixture,unknown_id)
                page.get_by_role("button",name="刷新原查询状态",exact=True).click()
                assert page.get_by_role("button",name="重试原查询",exact=True).count()==0
                count=len(proxy.requests)
                page.get_by_role("button",name="刷新原查询状态",exact=True).click()
                assert len(proxy.requests)==count and not unknown_ledger["snapshots"]
                assert unknown_ledger["calls"][0]["state"]=="unknown"
                page.screenshot(path=str(out/"employee-unknown-no-resend.png"),full_page=True)
                recovery={"known_task_id":failed_id,"unknown_task_id":unknown_id,"same_step_and_budget_binding":True,
                          "known_call_states":[row["state"] for row in repaired["calls"]],"unknown_call_states":[row["state"] for row in unknown_ledger["calls"]],
                          "upstream_recovery":"test-controlled local proxy; not production provider repair"}
            checks += ["known HTTP503 shown in employee and sanitized admin view; owner resumes same task/step/budget after local upstream recovery",
                       "truncated successful transport remains unknown, no retry button, no snapshot and status refresh never resends"]
            assert len(posts)==4 and not errors
            page.goto(f.gateway+"/workspace/query?task="+followup)
            expect(page.locator(".answer")).to_contain_text(WARNING,timeout=25000)
            page.locator(".result-sources > summary").click()
            page.get_by_role("link",name="核对原文 · PDF 第 1 页",exact=True).first.click()
            expect(page.locator("dialog[open] iframe")).to_be_visible()
            with f.app.state.session_factory() as db:
                grant=db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id==employee_id,
                    AuthorizationGrant.operation=="document.read"))
                grant.revoked_at=utc_now();db.commit()
            expect(page.locator(".answer")).to_have_count(0,timeout=20000)
            expect(page.locator("iframe")).to_have_count(0)
            assert owner.request.get(f.gateway+f"/documents/{f.document_id}/versions/{f.version_id}/file").status==404
            checks.append("document-read revocation independently clears an already open formal answer and PDF; raw original becomes 404")
            (out/"report.json").write_text(json.dumps({"scope":"local synthetic, built React / Chrome / actual SDK / PGvector / loopback model and synthetic embeddings",
                "checks":checks,"task_ids":[task_id,followup],"query_posts":len(posts),"source_sha256":hashlib.sha256(f.original).hexdigest(),
                "model_calls":len(f.model.requests),"recovery":recovery,"page_errors":errors,"browser":browser.version},ensure_ascii=False,indent=2),encoding="utf-8")
        finally:
            page.screenshot(path=str(out/"last-state.png"),full_page=True)
            (out/"last-dom.html").write_text(page.content(),encoding="utf-8")
            for item in contexts: item.close()
            browser.close()
