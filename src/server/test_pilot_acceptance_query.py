"""A/B/C on the built React workspace with three-role synthetic governance.

The PDF contract fixture is explicit, not a fresh MinerU inference. The pinned
Pi SDK, HTTP, SQL source gates, version events and browser are actually run.
"""
from pathlib import Path
from uuid import uuid4
import json
import os

import pytest
from sqlalchemy import select

from .models import (AuthorizationGrant, BusinessApproval, Document, DocumentVersion,
                     IndexGeneration, KnowledgeBase, QueryTask, User)
from .test_child_answer_pipeline import (MODEL, PASSWORD, PRESSURE, QUERY, STEP_ONE, STEP_TWO, WARNING,
                                        child_answer_pipeline, recovery_pg)
from .test_execution import _headers
from .testing_governance import seed_verified_approval
from .worker import run_one

pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="isolated PG required")


@pytest.mark.parametrize("phase", ["input", "negative", "versions"])
def test_pilot_abc_original_input_recovery_draft_reference_and_conflict(child_answer_pipeline, phase):
    from playwright.sync_api import expect, sync_playwright

    f = child_answer_pipeline
    out = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/pilot-af/2026-11-19/query")) / ("abc-" + phase)
    out.mkdir(parents=True, exist_ok=True)
    checks, errors, posts = [], [], []
    with f.app.state.session_factory() as db:
        employee = db.get(User, f.recipient_id)
        document = db.get(Document, f.document_id)
        scope = {"knowledge_base_id": f.kb_id, "document_id": None, "level": document.access_level,
                 "category": document.category, "need_class": document.need_class}
        approval = BusinessApproval(applicant=employee.id, recorded_by=f.actor_id, scope=scope, operations=["knowledge.query"])
        db.add(approval); db.flush(); seed_verified_approval(db, approval)
        db.add(AuthorizationGrant(subject_user_id=employee.id, operation="knowledge.query", approval_id=approval.id,
                                  valid_until=approval.valid_until, granted_by=f.actor_id, **scope))
        db.commit()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True, executable_path="C:/Program Files/Google/Chrome/Application/chrome.exe",
            args=["--disable-background-networking", "--disable-component-update", "--no-first-run"])
        context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
        context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(f.gateway + "/") else route.abort())
        page = context.new_page(); page.set_default_timeout(20000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("request", lambda request: posts.append(request.headers.get("idempotency-key"))
                if request.method == "POST" and request.url.endswith("/knowledge/queries") else None)
        try:
            page.goto(f.gateway + "/workspace/query"); page.wait_for_load_state("networkidle")
            page.get_by_label("账号", exact=True).fill(f.recipient_name)
            page.get_by_label("口令", exact=True).fill(PASSWORD)
            page.get_by_role("button", name="登录", exact=True).click()
            expect(page.get_by_role("button", name="退出", exact=True)).to_be_visible()

            def submit(question, model):
                expect(page.locator(".query-panel fieldset.operation-fields")).to_have_attribute("aria-busy", "false", timeout=20000)
                page.locator(".query-panel form textarea").fill(question)
                page.get_by_label("设备型号（可选）", exact=True).fill(model)
                with page.expect_response(lambda reply: reply.url.endswith("/knowledge/queries") and reply.request.method == "POST") as accepted:
                    page.get_by_role("button", name="提交查询", exact=True).click()
                assert accepted.value.status == 202
                return accepted.value.json()["task_id"]

            def execute(task_id):
                if phase == "versions" and f.model.mode == "good":
                    # Version admission is independent of the native SDK's
                    # total request deadline. Use the same exact child tools,
                    # physical gateway receipt and strict delivery; A separately
                    # runs the SDK and persistent Bailian is checked in Chrome.
                    from .test_pilot_acceptance_permissions import _deliver_child_answer_through_gateway
                    delivery = _deliver_child_answer_through_gateway(f, task_id)
                    outcome = {"task_id": task_id, "state": delivery["state"], "driver": "production-child-gateway"}
                else:
                    outcome = run_one(f.config, task_id=task_id, gateway_url=f.gateway)
                checks.append({"task_id": task_id, "worker_outcome": outcome, "gateway": list(f.gateway_trace)})
                page.get_by_role("button", name="刷新原查询状态", exact=True).click()
                if outcome["state"] == "succeeded":
                    expect(page.get_by_text("已找到可核对依据", exact=True)).to_be_visible(timeout=20000)
                elif outcome["state"] == "blocked":
                    expect(page.get_by_text("查询尚未完成", exact=True)).to_be_visible(timeout=20000)
                return outcome

            def no_answer():
                expect(page.locator(".result-panel .answer")).to_have_count(0)
                expect(page.locator(".result-panel article.query-evidence")).to_have_count(0)

            if phase == "input":
                # B: a missing model is a real user input request, not an inferred default.
                missing = submit(QUERY, "")
                before = len(f.model.requests)
                assert execute(missing)["state"] == "waiting_input"
                expect(page.get_by_label("补充设备型号", exact=True)).to_be_visible()
                assert len(f.model.requests) == before
                page.screenshot(path=str(out / "b-missing-model.png"), full_page=True)
                page.reload(); page.wait_for_load_state("networkidle")
                page.get_by_label("补充设备型号", exact=True).fill(MODEL)
                page.get_by_role("button", name="补充并继续原任务", exact=True).click()
                expect(page.get_by_text("正在查询", exact=True)).to_be_visible()
                assert execute(missing)["state"] == "succeeded"
                expect(page.locator(".answer")).to_contain_text(STEP_ONE)
                expect(page.locator(".answer")).to_contain_text(WARNING)
                expect(page.locator(".answer")).to_contain_text(STEP_TWO)
                expect(page.locator(".answer")).to_contain_text(PRESSURE)
                answer_text = page.locator(".answer").inner_text()
                assert answer_text.index(WARNING) < answer_text.index(STEP_TWO)
                result = context.request.get(f.gateway + f"/tasks/{missing}/result")
                assert result.status == 200 and result.json()["result_status"] == "formal"
                evidence = result.json()["evidence"]; assert len(evidence) == 6
                for item in evidence:
                    assert item["version"]["id"] == f.version_id and item["locator"]["file_page"] == 1
                source = page.get_by_role("link", name="核对原文 · PDF 第 1 页", exact=True).first.get_attribute("href")
                assert context.request.get(f.gateway + source.split("#")[0]).body() == f.original
                assert len(posts) == 1
                page.reload(); page.wait_for_load_state("networkidle")
                expect(page.locator(".answer")).to_contain_text(WARNING)
                page.screenshot(path=str(out / "a-formal-and-source.png"), full_page=True)
                checks.append({"scenario": "A/B", "task_id": missing, "state": "PASS", "same_original_task": True,
                               "formal": True, "complete_evidence": 6, "warning_before_step": True})

            if phase == "negative":
                # B: wrong explicit model and unsupported maintenance never produce a formal fact.
                f.model.mode = "no_evidence_stop"
                for name, question, model in (("wrong-model", QUERY, "SYNTHETIC-UNKNOWN"),
                                               ("missing-material", f"{MODEL} maintenance", MODEL)):
                    task_id = submit(question, model)
                    outcome = execute(task_id)
                    assert outcome["state"] in {"blocked", "waiting_input"}, outcome
                    no_answer()
                    expect(page.locator(".status.muted")).to_contain_text("资料")
                    page.screenshot(path=str(out / f"b-{name}.png"), full_page=True)
                    checks.append({"scenario": "B", "case": name, "task_id": task_id, "state": "PASS"})

            if phase == "versions":
                # C: a freshly uploaded draft must not supersede the effective old version or index.
                with f.app.state.session_factory() as db:
                    kb = db.get(KnowledgeBase, f.kb_id)
                    old_revision, old_generation = kb.content_revision, kb.active_generation_id
                import pymupdf
                with pymupdf.open(stream=f.original, filetype="pdf") as pending_pdf:
                    pending_pdf[0].insert_text((67, 400), "SYNTHETIC pending draft; not approved for operation.")
                    draft_original = pending_pdf.tobytes()
                uploaded = f.client.post(f"/documents/{f.document_id}/versions", params={"model_scope": MODEL},
                    files={"file": ("SYNTHETIC-new-draft.pdf", draft_original, "application/pdf")}, headers=_headers())
                assert uploaded.status_code == 201, uploaded.text
                draft = uploaded.json()["version"]; assert draft["publish_status"] == "draft"
                draft_file = f"/documents/{f.document_id}/versions/{draft['id']}/file"
                assert context.request.get(f.gateway + draft_file).status == 404
                with f.app.state.session_factory() as db:
                    kb = db.get(KnowledgeBase, f.kb_id)
                    assert (kb.content_revision, kb.active_generation_id) == (old_revision, old_generation)
                    assert db.get(IndexGeneration, old_generation).status == "ready"
                f.model.mode = "good"
                old_still_used = submit(QUERY, MODEL)
                assert execute(old_still_used)["state"] == "succeeded"
                result = context.request.get(f.gateway + f"/tasks/{old_still_used}/result").json()
                assert {item["version"]["id"] for item in result["evidence"]} == {f.version_id}
                checks.append({"scenario": "C", "case": "new draft preserves old published answer", "task_id": old_still_used, "state": "PASS"})

                # KM explicitly opens only this draft for reference; formal gates remain intact.
                reference = f.client.put(f"/documents/{f.document_id}/versions/{draft['id']}/reference-access",
                    json={"state": "open", "reason": "SYNTHETIC pending reference only"},
                    headers={**_headers(), "If-Match": f'"{draft["revision"]}"'})
                assert reference.status_code == 200, reference.text
                reference_file = context.request.get(f.gateway + draft_file)
                assert reference_file.status == 200 and reference_file.body() == draft_original
                with f.app.state.session_factory() as db:
                    assert db.get(DocumentVersion, draft["id"]).publish_status == "draft"
                checks.append({"scenario": "C", "case": "explicit reference does not publish", "state": "PASS"})

                # The manager records an actual blocking conflict through the production API.
                version = f.client.get(f"/documents/{f.document_id}/versions/{f.version_id}/source-metadata")
                held = f.client.post(f"/documents/{f.document_id}/versions/{f.version_id}/holds",
                    json={"kind": "conflict", "reason": "SYNTHETIC competing applicability requires business confirmation"},
                    headers={**_headers(), "If-Match": version.headers["etag"]})
                assert held.status_code == 201, held.text
                f.model.mode = "no_evidence_stop"
                conflicted = submit(QUERY, MODEL)
                outcome = execute(conflicted)
                assert outcome["state"] == "blocked", outcome
                with f.app.state.session_factory() as db:
                    task = db.get(QueryTask, conflicted)
                    assert task.error_code == "VERSION_CONFLICT", (task.error_code, task.next_action)
                expect(page.locator(".status.muted")).to_contain_text("冲突")
                no_answer()
                page.screenshot(path=str(out / "c-conflict-visible.png"), full_page=True)
                checks.append({"scenario": "C", "case": "current applicable conflict blocks formal facts", "task_id": conflicted, "state": "PASS"})
            assert not errors, errors
        finally:
            (out / "observations.json").write_text(json.dumps({"checks": checks, "page_errors": errors,
                "query_posts": len(posts), "scope": ("synthetic roles/PDF; actual production gates, PG, HTTP and Chrome; version positive uses a real child gateway receipt, negative uses SDK" if phase == "versions" else "synthetic roles/PDF; actual production gates, PG, pinned SDK, HTTP and Chrome")},
                ensure_ascii=False, indent=2), encoding="utf-8")
            (out / "last-dom.json").write_text(json.dumps({"fields": page.locator(".query-panel fieldset").evaluate_all('elements => elements.map(e => ({disabled:e.disabled, busy:e.getAttribute("aria-busy"), text:e.innerText}))'), "question_count": page.get_by_label("问题", exact=True).count()}, ensure_ascii=False, indent=2), encoding="utf-8")
            page.screenshot(path=str(out / "last-state.png"), full_page=True)
            context.close(); browser.close()
