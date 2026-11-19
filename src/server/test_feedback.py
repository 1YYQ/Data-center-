"""Verify feedback on isolated synthetic data through HTTP and the React workspace."""
from concurrent.futures import ThreadPoolExecutor
import os
import re
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .app import create_app
from .execution_models import QueryAnswerSnapshot
from .feedback_models import DocumentFeedback
from .models import (AuditEvent, AuthorizationGrant, BusinessApproval, ChunkSet, ContentBlock,
                     DocumentVersion, ParseRun, QueryTask, User, utc_now)
from .security import hash_password
from .services import save_upload, seed_reference_data
from .test_creation_recovery import recovery_pg
from .test_services import _activate_fixture_index, _ready_fixture, fixture_db
from .test_worker import STARTUP, WARNING, local_stack, submit, synthetic_pdf
from .testing_governance import seed_verified_approval, seed_verified_version
from .worker import run_one


def headers(key=None, revision=None):
    result = {"Idempotency-Key": key or uuid4().hex}
    if revision is not None:
        result["If-Match"] = f'"{revision}"'
    return result


def grant_document_operation(db, actor, kb, doc, operation):
    scope = {"knowledge_base_id": kb.id, "document_id": doc.id, "level": doc.access_level,
             "category": doc.category, "need_class": doc.need_class}
    approval = BusinessApproval(applicant=actor.id, recorded_by=actor.id, scope=scope, operations=[operation])
    db.add(approval)
    db.flush()
    seed_verified_approval(db, approval)
    grant = AuthorizationGrant(subject_user_id=actor.id, operation=operation,
        approval_id=approval.id, granted_by=actor.id, valid_until=approval.valid_until, **scope)
    db.add(grant)
    db.commit()
    return grant


def authenticate(client, user, password):
    logged_in = client.post("/auth/login", json={"username": user.username, "password": password})
    assert logged_in.status_code == 200, logged_in.text
    client.headers["X-CSRF-Token"] = logged_in.json()["csrf_token"]


def test_feedback_scope_duplicate_recovery_and_revocation(local_stack):
    db, config, employee_client, address, employee, administrator, kb, doc, version, _, _ = local_stack
    manager = db.get(User, version.created_by)
    with httpx.Client(base_url=address) as management, httpx.Client(base_url=address) as system:
        authenticate(system, administrator, "synthetic admin password")
        grant = grant_document_operation(db, manager, kb, doc, "feedback.handle")
        authenticate(management, manager, "manager password")
        assert system.get("/knowledge/feedback").status_code == 404
        task_id = submit(employee_client, kb)
        assert run_one(config, gateway_url=address)["state"] == "succeeded"
        query_feedback = employee_client.post("/document-feedback",
            json={"task_id": task_id, "text": "SYNTHETIC question about answer evidence"}, headers=headers())
        assert query_feedback.status_code == 201, query_feedback.text
        foreign = management.post("/document-feedback", json={"task_id": task_id, "text": "SYNTHETIC foreign task"}, headers=headers())
        assert foreign.status_code == 404
        no_evidence_task = submit(employee_client, kb, model=None)
        unsupported = employee_client.post("/document-feedback",
            json={"task_id": no_evidence_task, "text": "SYNTHETIC missing model"}, headers=headers())
        assert unsupported.json()["status"] == "awaiting_routing"
        body = {"document_version_id": version.id, "text": "SYNTHETIC manual feedback"}
        key = headers()
        saved = employee_client.post("/document-feedback", json=body, headers=key)
        feedback_id = saved.json()["id"]
        path = f"/knowledge/feedback/{feedback_id}"
        assert saved.status_code == 201
        assert employee_client.post("/document-feedback", json=body, headers=key).json()["id"] == feedback_id
        assert employee_client.post("/document-feedback", json={**body, "text": "different"}, headers=key).status_code == 409
        assert management.get(f"/document-feedback/{feedback_id}").status_code == 404
        listed = management.get("/knowledge/feedback").json()["items"]
        assert {r["id"] for r in listed} == {feedback_id, query_feedback.json()["id"]}
        assert all("task_id" not in row and "reporter_id" not in row for row in listed)
        stale = management.patch(path, json={"status": "processing"}, headers=headers(revision=9))
        assert stale.status_code == 412
        claim_headers = headers(revision=0)
        claimed = management.patch(path, json={"status": "processing"}, headers=claim_headers)
        assert claimed.status_code == 200, claimed.text
        assert management.patch(path, json={"status": "processing"}, headers=claim_headers).json() == claimed.json()
        reply = management.patch(path, json={"status": "replied", "reply": "SYNTHETIC reply"}, headers=headers(revision=1))
        assert reply.status_code == 200, reply.text
        assert employee_client.get(f"/document-feedback/{feedback_id}").json()["reply"] == "SYNTHETIC reply"
        assert employee_client.post(f"/document-feedback/{feedback_id}/requery", json={"query": "startup"},
            headers=headers()).status_code == 409
        grant.revoked_at = utc_now()
        dr = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == employee.id,
                                                         AuthorizationGrant.operation == "document.read"))
        dr.revoked_at = utc_now()
        db.commit()
        assert management.get("/knowledge/feedback").json()["items"] == []
        assert employee_client.get(f"/document-feedback/{feedback_id}").json() == {"id": feedback_id, "status": "access_limited"}
        assert employee_client.post("/document-feedback", json=body, headers=key).json() == {"id": feedback_id, "status": "access_limited"}
        assert management.patch(path, json={"status": "processing"}, headers=claim_headers).status_code == 404
        assert db.scalar(select(func.count()).select_from(DocumentFeedback)) == 3
        events = db.scalars(select(AuditEvent).where(AuditEvent.resource_id == feedback_id)).all()
        assert {x.action for x in events} == {"feedback.submitted", "feedback.processing", "feedback.replied"}
        assert all(not x.metadata_json for x in events)


def publish_replacement(db, config, manager, kb, document, version):
    import fitz

    with fitz.open(stream=synthetic_pdf(), filetype="pdf") as pdf:
        pdf[0].insert_text((40, 110), "SYNTHETIC replacement revision")
        original = save_upload([pdf.tobytes()], "synthetic-replacement.pdf", "application/pdf", manager, config)
    db.add(original)
    db.flush()
    replacement = DocumentVersion(document_id=document.id, version_no=version.version_no + 1,
        revision_label="SYNTHETIC replacement", model_scope=version.model_scope, source_file_id=original.id, created_by=manager.id)
    db.add(replacement)
    db.flush()
    run = ParseRun(document_version_id=replacement.id, origin="manual", status="needs_review")
    db.add(run)
    db.flush()
    chunk = ChunkSet(parse_run_id=run.id)
    db.add(chunk)
    for ordinal, (body, kind) in enumerate(((STARTUP, "paragraph"), (WARNING, "warning")), 1):
        db.add(ContentBlock(parse_run_id=run.id, ordinal=ordinal, kind=kind, text_content=body,
                            locator={"file_page": 1, "section": "Synthetic replacement"}))
    db.flush()
    seed_verified_version(db, manager, document, replacement, run, chunk.id)
    _activate_fixture_index(db, kb, manager)
    db.commit()


def test_feedback_rendered_flow_retains_original_answer(local_stack):
    from playwright.sync_api import expect, sync_playwright

    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    assert chrome.is_file() and Path("src/web/dist/index.html").is_file()
    db, config, _, address, employee, _, kb, doc, version, _, _ = local_stack
    manager = db.get(User, version.created_by)
    grant_document_operation(db, manager, kb, doc, "feedback.handle")
    screenshots = Path("outputs/readiness-followon/browser") / uuid4().hex
    screenshots.mkdir(parents=True)

    def session(browser, route, user, password):
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        context.route(re.compile(r"^https?://(?!" + re.escape(address.removeprefix("http://")) + r")"),
                      lambda external: external.abort())
        page = context.new_page()
        page.set_default_timeout(12000)
        page.goto(address + route)
        page.get_by_label("账号", exact=True).fill(user.username)
        page.get_by_label("口令", exact=True).fill(password)
        page.get_by_role("button", name="登录", exact=True).click()
        return page

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True, executable_path=str(chrome),
            args=["--disable-background-networking", "--disable-component-update"])
        try:
            page = session(browser, "workspace/query", employee, "synthetic employee password")
            page.get_by_label("设备型号（可选）").fill("DEMO-1")
            page.get_by_label("问题", exact=True).fill("DEMO-1 startup")
            with page.expect_response(lambda r: r.url.endswith("/knowledge/queries") and r.request.method == "POST") as accepted:
                page.get_by_role("button", name="提交查询", exact=True).click()
            original = accepted.value.json()["task_id"]
            assert run_one(config, gateway_url=address)["state"] == "succeeded"
            expect(page.get_by_text("已找到可核对依据", exact=True)).to_be_visible()
            page.get_by_label("遇到的资料问题").fill("SYNTHETIC query feedback")
            page.get_by_role("button", name="提交资料反馈").click()
            expect(page.get_by_text("SYNTHETIC query feedback", exact=True)).to_be_visible()
            page.get_by_role("link", name="查阅手册与反馈").first.click()
            expect(page.get_by_role("link", name="查看本版本原文 PDF")).to_be_visible()
            page.get_by_label("遇到的资料问题").fill("SYNTHETIC manual question")
            page.get_by_role("button", name="提交资料反馈").click()
            expect(page.get_by_text("SYNTHETIC manual question", exact=True)).to_be_visible()
            page.reload()
            expect(page.get_by_text("SYNTHETIC manual question", exact=True)).to_be_visible()

            pending = session(browser, "workspace/manage?feedback=1", manager, "manager password")
            item = pending.locator("article").filter(has_text="SYNTHETIC manual question")
            item.get_by_role("button", name="领取反馈").click()
            item.get_by_label("给员工的答复").fill("SYNTHETIC manager response")
            item.get_by_role("button", name="提交答复").click()
            expect(item.get_by_text("答复：SYNTHETIC manager response")).to_be_visible()
            pending.screenshot(path=str(screenshots / "manager.png"), full_page=True)
            page.get_by_role("button", name="刷新反馈", exact=True).click()
            expect(page.get_by_text("答复：SYNTHETIC manager response")).to_be_visible()
            expect(page.get_by_text("关联手册已有可用的新版本。")).to_have_count(0)

            with page.expect_navigation():
                page.get_by_role("link", name="资料查询", exact=True).click()
            expect(page.get_by_text("SYNTHETIC query feedback", exact=True)).to_be_visible()
            publish_replacement(db, config, manager, kb, doc, version)
            page.get_by_role("button", name="刷新反馈", exact=True).click()
            expect(page.get_by_text("关联手册已有可用的新版本。")).to_be_visible()
            page.screenshot(path=str(screenshots / "employee.png"), full_page=True)
            with page.expect_response(lambda r: r.url.endswith("/requery") and r.request.method == "POST") as refreshed:
                page.get_by_role("button", name="按最新资料重新查询").click()
            new_task = refreshed.value.json()["task_id"]
            assert new_task != original
            assert run_one(config, gateway_url=address)["state"] == "succeeded"
            expect(page.get_by_text("已找到可核对依据", exact=True)).to_be_visible()
            assert db.scalar(select(func.count()).select_from(QueryAnswerSnapshot).where(
                QueryAnswerSnapshot.task_id.in_([original, new_task]))) == 2
            assert db.get(QueryTask, original, populate_existing=True).status == "succeeded"
        finally:
            browser.close()


@pytest.mark.skipif(not os.getenv("DATACENTER_TEST_POSTGRES_URL"), reason="Requires explicit isolated local PostgreSQL")
def test_feedback_concurrent_postgres_claim_is_single_winner(recovery_pg):
    engine, config, _, _ = recovery_pg
    password = "SYNTHETIC feedback account"
    with Session(engine, expire_on_commit=False) as db:
        seed_reference_data(db)
        manager, kb, doc, version, _ = _ready_fixture(db, config)
        reporter = User(username="synthetic-feedback-reporter", display_name="Synthetic reporter",
            roles=["employee"], status="active", credential_state="active", credential_hash=hash_password(password))
        other = User(username="synthetic-feedback-manager", display_name="Synthetic manager",
            roles=["knowledge_manager"], status="active", credential_state="active", credential_hash=hash_password(password))
        db.add_all([reporter, other])
        db.flush()
        grant_document_operation(db, reporter, kb, doc, "document.read")
        grant_document_operation(db, other, kb, doc, "knowledge.manage")
        grant_document_operation(db, manager, kb, doc, "feedback.handle")
        grant_document_operation(db, other, kb, doc, "feedback.handle")
        with TestClient(create_app(config)) as submitter:
            authenticate(submitter, reporter, password)
            created = submitter.post("/document-feedback",
                json={"document_version_id": version.id, "text": "SYNTHETIC concurrent claim"}, headers=headers())
            assert created.status_code == 201, created.text
        feedback_id = created.json()["id"]
        barrier = Barrier(2, timeout=10)

        def claim(actor, credentials):
            app = create_app(config)
            try:
                with TestClient(app) as client:
                    authenticate(client, actor, credentials)
                    barrier.wait()
                    return client.patch(f"/knowledge/feedback/{feedback_id}",
                        json={"status": "processing"}, headers=headers(revision=0)).status_code
            finally:
                app.state.engine.dispose()

        with ThreadPoolExecutor(max_workers=2) as pool:
            claims = [pool.submit(claim, manager, "manager password"), pool.submit(claim, other, password)]
            assert sorted(job.result(timeout=15) for job in claims) == [200, 412]
        row = db.get(DocumentFeedback, feedback_id, populate_existing=True)
        assert row.handler_id in {manager.id, other.id} and row.revision == 1
        assert db.scalar(select(func.count()).select_from(AuditEvent).where(
            AuditEvent.resource_id == feedback_id, AuditEvent.action == "feedback.processing")) == 1
