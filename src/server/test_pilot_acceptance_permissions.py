"""Scenario D on installed Chrome with synthetic decisions and a production answer."""
from contextlib import ExitStack
from datetime import timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
from time import sleep
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.engine import make_url

from . import test_runtime_gate as runtime_fixture_module
from .answer_delivery import _response_tools
from .config import Settings
from .db import build_engine, init_db
from .execution import invoke_tool
from .governance_models import AuthorityBasis
from .models import AuthorizationGrant, BusinessApproval, Document, KnowledgeBase, User, utc_now
from .runtime_gate import ADAPTER, close_run, renew_run
from .runtime_models import ModelCallAdmission
from .security import hash_password
from .test_answer_share import _produce_answer, share_http
from .test_child_answer_pipeline import (
    MODEL, PASSWORD, PRESSURE, QUERY, WARNING, _api_gateway, _manual_validation, _receipt_states, child_answer_pipeline, recovery_pg,
)
from .test_execution import _headers, execution_http
from .test_runtime_gate import local_upstream, runtime_db
from .testing_governance import _authority, _proof, seed_verified_approval
from .worker import run_one


FOLLOWUP = "SYNTHETIC private second-turn question marker"
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
CHROME_ARGS = ["--disable-background-networking", "--disable-component-update", "--no-first-run", "--proxy-server=http://127.0.0.1:1"]


def _prepare(f):
    """Seed only synthetic identities and external trust facts; issue sender grants over HTTP."""
    with f.app.state.session_factory() as db:
        sender, no_dr, other_reader = [User(username=f"synthetic-{role}-{uuid4().hex}", display_name=f"SYNTHETIC {role}",
            roles=["employee"], status="active", credential_state="active", credential_hash=hash_password(PASSWORD))
            for role in ("answer-owner", "ungranted-recipient", "additional-recipient")]
        db.add_all([sender, no_dr, other_reader])
        db.flush()
        document, kb = db.get(Document, f.document_id), db.get(KnowledgeBase, f.kb_id)
        admin = db.scalar(select(User).where(User.username == f.admin_name))
        grant = db.get(AuthorizationGrant, f.recipient_grant_id)
        recipient_approval = db.get(BusinessApproval, grant.approval_id)
        now = utc_now()
        db.add(AuthorityBasis(identity={"user_ref": admin.id, "identity_label": admin.display_name,
            "responsibility": "SYNTHETIC pilot registrar"}, purposes=["access.manage"], scope={"knowledge_base_id": kb.id},
            source_reference="synthetic://pilot-af-registrar", valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=1)))
        revoke_scope = {"approval_id": recipient_approval.id, **recipient_approval.scope}
        _, witness = _authority(db, purpose="access_revocation", scope=revoke_scope)
        proof = _proof(db, admin, "approval", recipient_approval.id, {"kind": "access_revocation", "actual_identity": witness,
            "decision": "revoked", "decided_at": (now - timedelta(minutes=1)).isoformat(), "scope": revoke_scope})
        grant_payloads = []
        for user, operations in ((sender, ["document.read", "knowledge.query"]), (other_reader, ["document.read"])):
            approval = BusinessApproval(applicant=user.id, recorded_by=f.actor_id, scope=dict(recipient_approval.scope), operations=operations)
            db.add(approval)
            seed_verified_approval(db, approval)
            grant_payloads.extend({"user_id": user.id, "approval_id": approval.id, "valid_until": approval.valid_until.isoformat(),
                                   "operation": operation, **approval.scope} for operation in operations)
        facts = {
            "owner": sender.username, "no_dr": no_dr.username, "no_dr_id": no_dr.id, "other_reader_id": other_reader.id,
            "title": document.title, "library": kb.name, "revoke_approval": recipient_approval.id, "revoke_proof": proof.id,
            "grants": grant_payloads,
        }
        db.commit()
        return facts


def _login(page, username, password=PASSWORD):
    from playwright.sync_api import expect

    page.get_by_label("账号", exact=True).fill(username)
    page.get_by_label("口令", exact=True).fill(password)
    page.get_by_role("button", name="登录", exact=True).click()
    expect(page.get_by_role("button", name="退出", exact=True)).to_be_visible()


def _credentials(page):
    return {"X-CSRF-Token": page.evaluate("JSON.parse(sessionStorage.getItem('dc-workspace-session')).csrf"),
            "Idempotency-Key": str(uuid4())}


def _answer_share(page, path, recipient):
    from playwright.sync_api import expect

    page.get_by_label("接收者用户编号", exact=True).fill(recipient)
    with page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith(path)) as response:
        page.get_by_role("button", name="确认分享", exact=True).click()
    assert response.value.status == 200, response.value.text()
    expect(page.get_by_text("已保存分享。接收者需通过资料权限检查。", exact=True)).to_be_visible(timeout=30_000)
    return response.value


def _assert_no_source(page, markers):
    from playwright.sync_api import expect

    expect(page.locator(".result-panel .answer, article.query-evidence")).to_have_count(0)
    expect(page.get_by_role("link", name="核对原文 · PDF 第 1 页", exact=True)).to_have_count(0)
    for marker in markers:
        expect(page.locator("body")).not_to_contain_text(marker)
    browser_store = page.evaluate("async () => [JSON.stringify(Object.entries(sessionStorage)), JSON.stringify(Object.entries(localStorage)), await caches.keys()]")
    assert browser_store[2] == []
    assert all(marker not in browser_store[0] + browser_store[1] for marker in markers)


@pytest.mark.skipif(os.environ.get("DATACENTER_TEST_PILOT_D_NATIVE") != "1" or not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
                    reason="requires an explicit native SDK contention run with isolated PostgreSQL")
def test_pilot_permissions_with_real_share_and_source_revocations(child_answer_pipeline, record_testsuite_property):
    _observe_permissions(child_answer_pipeline, record_testsuite_property)


@pytest.mark.skipif(os.environ.get("DATACENTER_TEST_PILOT_D_BROWSER") != "1" or not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
                    reason="requires explicit Chrome and isolated PostgreSQL")
def test_pilot_permissions_with_production_child_answer_delivery(child_answer_pipeline, record_testsuite_property):
    _observe_permissions(child_answer_pipeline, record_testsuite_property, direct_bridge=True)


def _deliver_child_answer_through_gateway(f, task_id):
    with f.app.state.session_factory() as db:
        run, token, expected_plan = _manual_validation(f, db, task_id)
        renew_run(db, run.id, token)

    path = f.gateway + f"/private/runtime/runs/{run.id}"
    with httpx.Client(timeout=60, trust_env=False, headers={"X-Runtime-Token": token}) as client:
        projection_reply = client.post(path + "/context-project", json={"operation": "answer", "business_purpose": "answer"})
        assert projection_reply.status_code == 200, projection_reply.text
        projection = projection_reply.json()
        generation = projection["generation"]
        projected = json.loads(projection["context_json"])
        body = json.dumps({"model": generation["model"], "max_completion_tokens": generation["max_output_tokens"], "stream": True,
            "messages": [{"role": "user", "content": projected["messages"][-1]["content"]}]}, separators=(",", ":")).encode()
        authorization = client.post(path + "/model-authorizations", json={"projection_id": projection["id"],
            "context_digest": projection["context_digest"], "adapter": ADAPTER,
            "payload_digest": hashlib.sha256(body).hexdigest(), "request_key": str(uuid4())})
        assert authorization.status_code == 200 and authorization.json()["state"] == "reserved", authorization.text
        response = client.post(path + "/models/chat/completions", content=body,
            headers={"Content-Type": "application/json", "X-Model-Call": authorization.json()["id"]})
        assert response.status_code == 200 and "[DONE]" in response.text, response.text
        tool, = _response_tools(response.text)
        assert tool["name"] == "dc_artifacts_deliver" and tool["args"] == {"answer_plan": expected_plan}

    with f.app.state.session_factory() as db:
        delivered = invoke_tool(db, run.id, token, tool["id"], tool["name"], tool["args"], settings=f.config)
        db.commit()
        assert delivered["state"] == "succeeded", delivered
        close_run(db, run.id, token)
    return {"call_receipt": authorization.json()["id"], "tool": tool["name"], "state": delivered["state"]}


def _observe_permissions(f, record_testsuite_property, *, direct_bridge=False):
    from playwright.sync_api import expect, sync_playwright

    facts = _prepare(f)
    assert CHROME.is_file(), "Installed Chrome required"
    assert Path("src/web/dist/index.html").is_file(), "Build the formal React workspace first"
    output = Path(os.environ.get("DATACENTER_PILOT_D_EVIDENCE", "outputs/pilot-af/2026-11-19/permissions"))
    output.mkdir(parents=True, exist_ok=True)
    scope = ("installed Chrome and formal React, isolated PostgreSQL, production child tools and private model gateway with an actual loopback receipt; direct bridge driver without the Pi SDK"
             if direct_bridge else "installed Chrome and formal React, isolated PostgreSQL, real Pi SDK and loopback scripted model")
    report = {"scope": scope + "; synthetic accounts, source, external decisions and proof",
              "status": "INCOMPLETE", "checkpoints": {}, "page_errors": [], "external_page_requests": [], "private_gateway_trace": f.gateway_trace}
    checks = report["checkpoints"]
    query_page = "/workspace/query"
    pdf = f"/documents/{f.document_id}/versions/{f.version_id}/file"
    concealed = (facts["title"], facts["library"], WARNING, PRESSURE, QUERY, FOLLOWUP)

    def screenshot(page, filename):
        page.screenshot(path=str(output / f"{filename}.png"), full_page=True)

    def deny(context, paths, *, status=(403, 404)):
        result = {}
        for name, path in paths.items():
            response = context.request.get(f.gateway + path)
            assert response.status in status, (name, response.status)
            assert all(marker not in response.text() for marker in concealed), name
            result[name] = response.status
        return result

    try:
        with sync_playwright() as playwright, ExitStack() as cleanup:
            expect.set_options(timeout=45_000)
            cleanup.callback(expect.set_options, timeout=5_000)
            # Chrome's implicit loopback bypass keeps local requests direct.
            # Other traffic has no reachable proxy; Playwright routing remains
            # off so the browser's normal HTTP cache behavior is tested.
            browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME), args=CHROME_ARGS)
            cleanup.callback(browser.close)
            report["browser"] = browser.version

            def open_workspace(path, name):
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block", timezone_id="Asia/Shanghai")
                cleanup.callback(context.close)
                context.set_default_timeout(45_000)
                context.on("request", lambda request: report["external_page_requests"].append(request.url)
                           if request.url.startswith(("http:", "https:")) and not request.url.startswith(f.gateway + "/") else None)
                page = context.new_page()
                page.on("pageerror", lambda error: report["page_errors"].append(str(error)))
                page.goto(f.gateway + path, wait_until="networkidle")
                _login(page, name)
                return context, page

            admin, admin_page = open_workspace("/workspace/access-review", f.admin_name)
            expect(admin_page.get_by_text("核查不等于批准", exact=True)).to_be_visible()
            created = [admin.request.post(f.gateway + "/admin/grants", data=grant, headers=_credentials(admin_page))
                       for grant in facts["grants"]]
            assert all(item.status == 201 for item in created), [(item.status, item.text()) for item in created]
            checks["grants_issued"] = [item.json()["operation"] for item in created]

            sender, sender_page = open_workspace(query_page, facts["owner"])
            bundle = sender_page.locator('script[src^="/workspace/assets/"]').get_attribute("src")
            report["served_bundle"] = bundle
            report["served_bundle_sha256"] = hashlib.sha256((Path("src/web/dist") / bundle.rsplit("/workspace/", 1)[-1]).read_bytes()).hexdigest()
            sender_page.get_by_label("设备型号（可选）", exact=True).fill(MODEL)
            sender_page.locator(".query-panel form textarea").fill(QUERY)
            with sender_page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/knowledge/queries")) as submitted:
                sender_page.get_by_role("button", name="提交查询", exact=True).click()
            assert submitted.value.status == 202, submitted.value.text()
            task = f"/tasks/{submitted.value.json()['task_id']}"
            task_id = submitted.value.json()["task_id"]
            if direct_bridge:
                checks["tool_delivery"] = _deliver_child_answer_through_gateway(f, task_id)
            else:
                outcome = run_one(f.config, task_id=task_id, gateway_url=f.gateway)
                checks["worker_outcome"] = outcome
                if outcome["state"] != "succeeded" or f.model.errors:
                    pytest.fail(json.dumps({"outcome": outcome, "receipts": _receipt_states(f, task_id), "errors": f.model.errors}, default=str))
            expect(sender_page.get_by_text("已找到可核对依据", exact=True)).to_be_visible()
            owner_result = sender.request.get(f.gateway + task + "/result").json()
            snapshot_id = owner_result["answer_snapshot_id"]
            assert snapshot_id and owner_result["result_status"] == "formal"
            assert owner_result["task"]["result"]["answer_contract"] == "answer-plan/1"
            assert len(owner_result["evidence"]) == 6 and all(item["source_binding_hash"] for item in owner_result["evidence"])
            checks["exact_source_members"] = len(owner_result["evidence"])
            with f.app.state.session_factory() as db:
                receipts = [tuple(row) for row in db.execute(select(ModelCallAdmission.state,
                    ModelCallAdmission.response_status, ModelCallAdmission.error_code).where(ModelCallAdmission.task_id == task_id)
                    .order_by(ModelCallAdmission.created_at))]
            checks["model_received"] = receipts
            assert receipts == [("received", 200, None)] * (1 if direct_bridge else 3)
            conversation = owner_result["task"]["conversation_id"]
            followup = sender.request.post(f.gateway + "/knowledge/queries", headers=_credentials(sender_page),
                data={"query": FOLLOWUP, "model": "SYNTHETIC-PRIVATE-FOLLOWUP", "conversation_id": conversation})
            assert followup.status == 202, followup.text()
            private_task = f"/tasks/{followup.json()['task_id']}"
            paths = {"answer": task + "/result", "evidence": task + "/evidence", "pdf": pdf}

            sender_page.get_by_role("button", name="分享这条回答", exact=True).click()
            deadline_local = sender_page.evaluate("""() => {const d = new Date(Date.now() + 2 * 3600_000); const p = n => String(n).padStart(2, '0');
                return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;}""")
            sender_page.get_by_label("分享截止时间（可选）", exact=True).fill(deadline_local)
            first_share = _answer_share(sender_page, task + "/shares", facts["other_reader_id"])
            assert first_share.request.post_data_json["answer_snapshot_id"] == snapshot_id
            assert first_share.request.header_value("if-match") and first_share.request.header_value("idempotency-key")
            incumbent = first_share.json()["items"][0]
            assert incumbent["expires_at"].endswith("+00:00")
            sender_page.get_by_label("接收者用户编号", exact=True).fill(facts["no_dr_id"])
            with sender_page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith(task + "/shares")) as unauthorized_share:
                sender_page.get_by_role("button", name="确认分享", exact=True).click()
            assert unauthorized_share.value.status == 409, unauthorized_share.value.text()
            expect(sender_page.get_by_text("接收者当前不能读取这条回答所需的资料，分享未保存", exact=True)).to_be_visible()
            expect(sender_page.get_by_role("button", name="退出", exact=True)).to_be_visible()
            assert [item["user_id"] for item in sender.request.get(f.gateway + task + "/shares").json()["items"]] == [facts["other_reader_id"]]
            checks["share_without_read_grant"] = unauthorized_share.value.status
            addition = _answer_share(sender_page, task + "/shares", f.recipient_id).json()["items"]
            assert {item["user_id"] for item in addition} == {facts["other_reader_id"], f.recipient_id}
            assert next(item for item in addition if item["user_id"] == facts["other_reader_id"])["expires_at"] == incumbent["expires_at"]
            screenshot(sender_page, "01-employee-shares-exact-answer")

            ungranted, ungranted_page = open_workspace(query_page, facts["no_dr"])
            ungranted_page.get_by_text("我的查询记录", exact=True).click()
            expect(ungranted_page.get_by_text("暂无查询记录。", exact=True)).to_be_visible()
            checks["ungranted_recipient"] = deny(ungranted, paths)
            assert ungranted.request.get(f.gateway + "/knowledge-bases").json()["items"] == []
            assert ungranted.request.get(f.gateway + "/tasks").json()["items"] == []
            _assert_no_source(ungranted_page, concealed)
            screenshot(ungranted_page, "02-share-does-not-grant-source-access")

            recipient, recipient_page = open_workspace(query_page, f.recipient_name)
            recipient_posts = []
            recipient_page.on("request", lambda req: recipient_posts.append(req.url)
                              if req.method == "POST" and req.url.endswith("/knowledge/queries") else None)
            recipient_page.get_by_text("我的查询记录", exact=True).click()
            history_entry = recipient_page.get_by_role("button", name="已保存的查询", exact=True)
            expect(history_entry).to_have_count(1)
            with recipient_page.expect_response(lambda response: response.url.endswith(paths["answer"])) as shared:
                history_entry.click()
            shared_value = shared.value.json()
            assert shared.value.status == 200 and shared_value["answer_snapshot_id"] == snapshot_id
            assert shared_value["answer"] == owner_result["answer"] and shared_value["task"]["input"] is None
            assert shared_value["task"]["conversation_id"] is None
            assert shared.value.headers["cache-control"] == "private, no-store"
            expect(recipient_page.locator(".result-panel .answer")).to_contain_text(WARNING)
            expect(recipient_page.get_by_text(f"本次采用的设备型号 {MODEL}", exact=True)).to_be_visible()
            expect(recipient_page.get_by_role("button", name="分享这条回答", exact=True)).to_have_count(0)
            for hidden in (QUERY, FOLLOWUP, "SYNTHETIC-PRIVATE-FOLLOWUP"):
                expect(recipient_page.locator("body")).not_to_contain_text(hidden)
            warning = recipient_page.locator("article.query-evidence").filter(has_text=WARNING)
            source_link = warning.get_by_role("link", name="核对原文 · PDF 第 1 页", exact=True)
            assert source_link.get_attribute("href") == pdf + "#page=1"
            warning_ref = next(item["id"] for item in shared_value["evidence"] if item["quote"] == WARNING)
            paths["evidence_detail"] = task + f"/evidence/{warning_ref}"
            paths["source_crop"] = paths["evidence_detail"] + "/source-image?region=0"
            with recipient_page.expect_response(lambda response: response.url.endswith(paths["source_crop"])) as crop:
                warning.get_by_text("查看原页裁图 · 第 1 页", exact=True).click()
            assert crop.value.status == 200 and crop.value.headers["cache-control"] == "private, no-store"
            expect(warning.get_by_role("img", name="PDF 第 1 页的引用区域裁图", exact=True)).to_be_visible()
            original = recipient.request.get(f.gateway + pdf)
            assert original.status == 200 and original.headers["cache-control"] == "private, no-store" and original.body() == f.original
            authorized = recipient.request.get(f.gateway + paths["evidence"])
            assert authorized.status == 200 and authorized.headers["cache-control"] == "private, no-store"
            assert [item["id"] for item in authorized.json()["items"]] == [item["id"] for item in shared_value["evidence"]]
            checks["shared_snapshot"] = {"id": snapshot_id, "answer": shared.value.status, "evidence": authorized.status, "pdf": original.status, "source_crop": crop.value.status,
                "private_paths": deny(recipient, {"conversation": f"/conversations/{conversation}/conditions", "second_turn": private_task, "share_edit": task + "/shares"})}
            screenshot(recipient_page, "03-recipient-exact-answer-warning-and-source")

            with sender_page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith(task + "/shares")) as removed:
                sender_page.get_by_role("button", name=f"撤回对 {f.recipient_id} 的分享", exact=True).click()
            assert removed.value.status == 200, removed.value.text()
            assert {item["user_id"]: item["status"] for item in removed.value.json()["items"]} == {f.recipient_id: "revoked", facts["other_reader_id"]: "active"}
            assert next(item for item in removed.value.json()["items"] if item["user_id"] == facts["other_reader_id"])["expires_at"] == incumbent["expires_at"]
            checks["incumbent_share_preserved"] = incumbent["expires_at"]
            expect(sender_page.get_by_text("已撤回这条回答的分享。", exact=True)).to_be_visible(timeout=30_000)
            recipient_page.get_by_role("button", name="刷新原查询状态", exact=True).click()
            expect(recipient_page.get_by_text("当前登录或资料访问条件已变化", exact=False)).to_be_visible()
            _assert_no_source(recipient_page, concealed)
            checks["share_revoked"] = deny(recipient, {name: path for name, path in paths.items() if name != "pdf"})
            # The independent document grant still allows a direct original.
            independent = recipient.request.get(f.gateway + pdf)
            assert independent.status == 200 and independent.body() == f.original
            checks["share_revoked"]["independent_pdf"] = independent.status
            screenshot(recipient_page, "04-share-revoked-clears-open-answer")

            _answer_share(sender_page, task + "/shares", f.recipient_id)
            _login(recipient_page, f.recipient_name)
            recipient_page.goto(f.gateway + query_page + f"?task={task_id}")
            expect(recipient_page.locator(".result-panel .answer")).to_contain_text(WARNING)
            admin_page.get_by_role("button").filter(has_text=facts["revoke_approval"]).click()
            admin_page.get_by_label("已核实撤销证据引用", exact=True).fill(facts["revoke_proof"])
            admin_page.get_by_label("登记说明", exact=True).fill("SYNTHETIC pilot source approval revoked by scoped registrar")
            with admin_page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith("/revocations")) as revoked:
                admin_page.get_by_role("button", name="登记外部撤销", exact=True).click()
            assert revoked.value.status == 201, revoked.value.text()
            expect(admin_page.get_by_text("撤销登记回执", exact=False)).to_be_visible()
            checks["source_revocation_receipt"] = revoked.value.json()["id"]
            screenshot(admin_page, "05-system-admin-records-source-revocation")
            recipient_page.get_by_role("button", name="刷新原查询状态", exact=True).click()
            expect(recipient_page.get_by_text("当前登录或资料访问条件已变化", exact=False)).to_be_visible()
            _assert_no_source(recipient_page, concealed)
            _login(recipient_page, f.recipient_name)
            assert recipient.request.get(f.gateway + "/me").status == 200
            checks["source_revoked_after_reauth"] = deny(recipient, paths)
            assert recipient.request.get(f.gateway + "/knowledge-bases").json()["items"] == []
            assert recipient.request.get(f.gateway + "/tasks").json()["items"] == []
            recipient_page.get_by_text("我的查询记录", exact=True).click()
            expect(recipient_page.get_by_text("暂无查询记录。", exact=True)).to_be_visible()
            _assert_no_source(recipient_page, concealed)
            screenshot(recipient_page, "06-source-revoked-relogin-has-no-cached-answer")
            assert any(item["user_id"] == f.recipient_id and item["status"] == "active"
                       for item in sender.request.get(f.gateway + task + "/shares").json()["items"])

            assert admin.request.get(f.gateway + "/me").status == 200
            checks["system_admin_business_denied"] = deny(admin, paths)
            assert admin.request.get(f.gateway + "/knowledge-bases").json()["items"] == []
            admin_page.goto(f.gateway + "/workspace/manage")
            expect(admin_page.get_by_text("当前账号没有进入本工作区所需的角色，请联系管理员。", exact=True)).to_be_visible()
            _assert_no_source(admin_page, concealed)
            screenshot(admin_page, "07-system-admin-no-business-content")
            assert not recipient_posts and not report["external_page_requests"] and not report["page_errors"], report
            report["recipient_query_submissions"] = len(recipient_posts)
            report["status"] = "PASS"
            record_testsuite_property("served_react_bundle", bundle)
    finally:
        (output / "observations.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


@pytest.fixture
def migrated_share_http(request, monkeypatch, tmp_path):
    def new_sqlite(database_url, **unused):
        database = Path(make_url(database_url).database)
        assert database.parent == tmp_path and not database.exists()
        config = Settings(database_url=database_url, storage_dir=tmp_path / "schema-evidence")
        engine = build_engine(database_url)
        init_db(engine, config)
        return engine

    # The reusable service fixture ordinarily creates a metadata-only schema.
    # Prepare the new temporary file through migrations for real app startup.
    monkeypatch.setattr(runtime_fixture_module, "create_engine", new_sqlite)
    return request.getfixturevalue("share_http")


@pytest.mark.skipif(os.environ.get("DATACENTER_TEST_PILOT_D_BROWSER") != "1", reason="requires an explicit pilot browser run")
def test_pilot_share_expiry_round_trips_sqlite_and_browser_preserves_active_recipient(migrated_share_http, record_testsuite_property):
    from playwright.sync_api import expect, sync_playwright

    assert CHROME.is_file(), "Installed Chrome required"
    f = migrated_share_http
    read_grant = f.db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.reader.id))
    scope = dict(f.db.get(BusinessApproval, read_grant.approval_id).scope)
    approval = BusinessApproval(applicant=f.no_dr.id, recorded_by=f.actor.id, scope=scope, operations=["document.read"])
    f.db.add(approval)
    seed_verified_approval(f.db, approval)
    f.db.add(AuthorizationGrant(subject_user_id=f.no_dr.id, operation="document.read", approval_id=approval.id,
        valid_until=approval.valid_until, granted_by=f.actor.id, **scope))
    f.db.commit()
    task, snapshot, _ = _produce_answer(f)
    path = f"/tasks/{task.id}/shares"
    deadline_utc = utc_now().replace(microsecond=0) + timedelta(hours=2)
    deadline = deadline_utc.isoformat()
    shanghai = timezone(timedelta(hours=8))
    payload = {"answer_snapshot_id": snapshot.id, "entries": [{"user_id": f.reader.id, "expires_at": deadline_utc.astimezone(shanghai).isoformat()}]}
    headers = _headers(task.revision)
    for write in (f.client.put(path, json=payload, headers=headers), f.client.put(path, json=payload, headers=headers)):
        assert write.status_code == 200 and write.json()["items"][0]["expires_at"] == deadline
    initial = f.client.get(path)
    assert initial.json()["items"][0]["expires_at"] == deadline
    assert initial.json()["items"][0]["granted_at"].endswith("+00:00")

    with _api_gateway(f.app) as (gateway, _), sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME), args=CHROME_ARGS)
        try:
            context = browser.new_context(timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            # A bad workstation clock must not omit an incumbent authorization.
            page.add_init_script("const realNow = Date.now.bind(Date); Date.now = () => realNow() + 3 * 3600_000;")
            page.goto(gateway + "/workspace/query?task=" + task.id)
            _login(page, f.actor.username, "synthetic execution password")
            record_testsuite_property("served_react_bundle", page.locator('script[src^="/workspace/assets/"]').get_attribute("src"))
            page.get_by_role("button", name="分享这条回答", exact=True).click()
            expect(page.get_by_role("button", name=f"撤回对 {f.reader.id} 的分享", exact=True)).to_be_visible()
            changed = _answer_share(page, path, f.no_dr.id)
            assert {item["user_id"]: item["expires_at"] for item in changed.request.post_data_json["entries"]} == {
                f.reader.id: deadline, f.no_dr.id: None}
            assert {item["user_id"]: item["status"] for item in changed.json()["items"]} == {f.reader.id: "active", f.no_dr.id: "active"}
            assert f.reader_client.get(f"/tasks/{task.id}/result").status_code == 200
        finally:
            browser.close()

    current_task = f.client.get(f"/tasks/{task.id}")
    short_deadline = utc_now() + timedelta(seconds=3)
    changed = f.client.put(path, json={"answer_snapshot_id": snapshot.id, "entries": [
        {"user_id": f.reader.id, "expires_at": short_deadline.astimezone(shanghai).isoformat()}, {"user_id": f.no_dr.id, "expires_at": None},
    ]}, headers={**_headers(), "If-Match": current_task.headers["ETag"]})
    assert changed.status_code == 200, changed.text
    assert next(item for item in changed.json()["items"] if item["user_id"] == f.reader.id)["expires_at"] == short_deadline.isoformat()
    assert f.reader_client.get(f"/tasks/{task.id}/result").status_code == 200
    sleep(max(0, (short_deadline - utc_now()).total_seconds()) + .1)
    assert f.reader_client.get(f"/tasks/{task.id}/result").status_code == 404
    assert f.reader_client.get(f"/tasks/{task.id}/evidence").status_code == 404
    assert {item["user_id"]: item["status"] for item in f.client.get(path).json()["items"]} == {
        f.reader.id: "expired", f.no_dr.id: "active"}
    record_testsuite_property("sqlite_deadline", "+08:00 stored as the same UTC instant; preserved in Chrome despite clock skew; result and evidence denied after expiry")
