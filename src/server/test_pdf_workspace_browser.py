"""Built React on installed Chrome and an isolated PostgreSQL database."""
from dataclasses import replace
from datetime import timedelta
import io
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import select
import uvicorn

from . import governance as g
from .app import create_app
from .chunk_jobs import run_chunk_job
from .governance_models import AuthorityBasis
from .models import (AuthorizationGrant, Chunk, ChunkSet, ContentBlock, Document, DocumentVersion,
                     KnowledgeBase, ParseRun, QueryTask, TaskEvidence, User, utc_now)
from .parse_jobs import run_local_parse_job, run_parse_job
from .parsing.artifacts import archive_bytes
from .parsing.derivation import derive_raw_run
from .parsing.ir_projection import normalize_parse_run
from .parsing.processing import run_source_qc, structure_parse_run
from .security import hash_password
from .services import save_upload, seed_reference_data
from .test_chunk_processing import TOKENIZER, build, publish, review_candidate, verify_source
from .test_governance import synthetic_draft
from .test_local_parse_pipeline import PASSWORD, SYNTHETIC_TEXT, accept, saved_bundle
from .test_pdf_postgres import pdf_pg
from .test_query_evidence_assets import _PASSWORD as EMPLOYEE_PASSWORD, _employee
from .test_source_processing import source_ready
from .test_workspace_browser import _login, _screenshot, _select
from .testing_governance import seed_verified_version

pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
                                reason="Requires an explicit isolated local PostgreSQL URL")
CHROME = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
RESULTS = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/pdf-implementation/2026-11-19/browser"))
ADMIN_PASSWORD = "SYNTHETIC isolated administrator password"


@pytest.fixture
def pdf_browser(pdf_pg, tmp_path, request):
    from playwright.sync_api import sync_playwright

    assert CHROME.is_file(), "Installed Chrome required"
    assert Path("src/web/dist/index.html").is_file(), "Build React first"
    config = replace(pdf_pg.settings, storage_dir=tmp_path / "storage", local_bge_tokenizer=TOKENIZER.resolve())
    app = create_app(config)
    app.state.service_endpoints = {}
    out = RESULTS / request.node.name
    out.mkdir(parents=True, exist_ok=True)
    with app.state.session_factory() as db:
        seed_reference_data(db)
        db.commit()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(32)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    address = f"http://127.0.0.1:{listener.getsockname()[1]}/"
    errors, responses, checks = [], [], []
    try:
        deadline = time.monotonic() + 10
        while not server.started and thread.is_alive() and time.monotonic() < deadline:
            time.sleep(.01)
        assert server.started
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True, executable_path=str(CHROME),
                args=["--disable-background-networking", "--disable-component-update", "--no-first-run"])
            context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
            context.route(re.compile(r"^https?://(?!" + re.escape(address.removeprefix("http://")) + r")"), lambda route: route.abort())
            page = context.new_page()
            page.set_default_timeout(15000)
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("response", lambda response: responses.append({
                "method": response.request.method, "path": response.url.removeprefix(address), "status": response.status,
            }))
            try:
                yield SimpleNamespace(app=app, config=config, address=address, context=context, page=page, out=out, checks=checks)
            finally:
                (out / "observations.json").write_text(json.dumps({
                    "browser": browser.version, "database_receipt": str(pdf_pg.report / "lifecycle.json"),
                    "served_bundle": page.locator('script[src^="/workspace/assets/"]').first.get_attribute("src") if page.locator('script[src^="/workspace/assets/"]').count() else None,
                    "checks": checks, "page_errors": errors, "responses": responses,
                    "scope": "installed Chrome; built React; local HTTP and PG; generated PDFs and synthetic witnesses only",
                }, ensure_ascii=False, indent=2), encoding="utf-8")
                (out / "last-dom.html").write_text(page.content(), encoding="utf-8")
                page.screenshot(path=str(out / "last-state.png"), full_page=True)
                context.close()
                browser.close()
                assert not errors, errors
    finally:
        server.should_exit = True
        thread.join(10)
        listener.close()
        app.state.engine.dispose()


def _new_source(f, title, revision, content):
    with f.app.state.session_factory() as db:
        actor, kb, document, version, _, _ = synthetic_draft(db)
        actor.credential_state, actor.credential_hash = "active", hash_password(PASSWORD)
        document.title, version.revision_label = title, revision
        stored = save_upload([content], "SYNTHETIC.pdf", "application/pdf", actor, f.config)
        db.add(stored)
        db.flush()
        version.source_file_id = stored.id
        db.commit()
        return SimpleNamespace(app=f.app, config=f.config, actor_id=actor.id, actor=actor, kb_id=kb.id,
            document_id=document.id, version_id=version.id, path=f"/documents/{document.id}/versions/{version.id}/parse-runs")


def _synthetic_pdf(with_image=False):
    import pymupdf

    with pymupdf.open() as pdf:
        first = pdf.new_page()
        first.insert_text((72, 72 if with_image else 112), SYNTHETIC_TEXT)
        area = pymupdf.Rect(72, 104, 170, 160)
        if with_image:
            first.draw_rect(area, color=(.25, .35, .65), fill=(.82, .87, 1))
            first.insert_text((76, 130), "SYNTHETIC image")
        pdf.new_page()
        image = pdf[0].get_pixmap(clip=area).tobytes("png") if with_image else None
        return pdf.tobytes(), image


def _manager_login(client, source):
    response = client.post("/auth/login", json={"username": source.actor.username, "password": PASSWORD})
    assert response.status_code == 200, response.text
    client.headers["X-CSRF-Token"] = response.json()["csrf_token"]


def _replace_raw_text(_source, workspace, _executable, _home, _heartbeat):
    with ZipFile(io.BytesIO(saved_bundle())) as original:
        payload = json.loads(original.read("middle_json.json"))
    payload["pages"][0]["blocks"][0]["content"][0]["content"] = "SYNTHETIC recognized line with missing original values."
    with ZipFile(workspace / "result.zip", "w") as archive:
        archive.writestr("middle_json.json", json.dumps(payload))
        archive.writestr("structured_content.json", json.dumps({"pages": payload["pages"]}))
        archive.writestr("markdown.md", "SYNTHETIC risk-only package")


def test_pdf_manager_checks_pages_and_switches_only_reviewed_source(pdf_browser):
    from playwright.sync_api import expect

    f, page = pdf_browser, pdf_browser.page
    if not TOKENIZER.is_file():
        pytest.skip("Fixed local BGE-M3 tokenizer is unavailable; no download")
    original, _ = _synthetic_pdf()
    source = _new_source(f, "SYNTHETIC structured manual", "SYNTHETIC PDF R1", original)
    with httpx.Client(base_url=f.address, timeout=10, trust_env=False) as manager:
        source.client = manager
        _manager_login(manager, source)
        first = source_ready(source)
        _login(f, "manage", source.actor, PASSWORD)
        _select(page, "知识库").select_option(source.kb_id)
        batches = _select(page, "解析批次")
        batches.select_option(first)
        expect(page.get_by_text("已对齐原页", exact=True)).to_be_visible()
        expect(page.get_by_text("已恢复", exact=True)).to_be_visible()
        milestones = page.locator(".pdf-milestones")
        expect(milestones.locator("div").filter(has_text="源质量检查").get_by_text("通过", exact=True)).to_be_visible()
        expect(milestones.locator("div").filter(has_text="源人工核验").get_by_text("待核验", exact=True)).to_be_visible()
        expect(page.locator(".pdf-relation").first).to_contain_text("待业务确认")
        nav = page.get_by_role("navigation", name="原页清单")
        nav.get_by_role("button", name=re.compile("第 2 页")).click()
        expect(page.get_by_text("此页有空白判定依据", exact=False)).to_be_visible()
        assert page.get_by_role("link", name="打开原文第 2 页", exact=True).get_attribute("href").endswith("#page=2")
        _screenshot(f, "01-blank-page")
        nav.get_by_role("button", name=re.compile("第 1 页")).click()
        article = page.locator("article[data-block-id]").filter(has_text=SYNTHETIC_TEXT)
        with page.expect_response(lambda result: "/source-image?region=0" in result.url and first in result.url) as image:
            article.locator("summary").filter(has_text="核对原页区域").click()
        assert image.value.status == 200 and image.value.headers["cache-control"] == "private, no-store"
        import pymupdf
        assert min(pymupdf.Pixmap(image.value.body()).samples) < 100, "Synthetic source crop must contain the printed line"
        expect(article.locator("img")).to_be_visible()
        _screenshot(f, "02-private-region-and-pending-review")
        f.checks.append("Structured and source QC passed while source business review remains pending; blank page and private source crop work")

        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/chunk-sets")) as accepted:
            page.get_by_role("button", name="请求生成切分集合", exact=True).click()
        assert accepted.value.status == 202
        set_id = accepted.value.json()["id"]
        with f.app.state.session_factory() as db:
            assert db.get(ChunkSet, set_id).status == "building"
            assert db.get(DocumentVersion, source.version_id).selected_chunk_set_id is None
            built = run_chunk_job(db, set_id, f.config)
            assert built.status == "ready", built.stage_checkpoint
            assert db.scalar(select(Chunk).where(Chunk.chunk_set_id == set_id, Chunk.level == "child", Chunk.token_count > 0))
        refresh = page.get_by_role("button", name="刷新解析批次", exact=True)
        refresh.click()
        candidate = page.get_by_role("button", name="创建完整核验候选", exact=True)
        expect(candidate).to_be_disabled()
        expect(page.get_by_text("源人工核验和集合质量检查都有效后才能登记候选。", exact=True)).to_be_visible()
        _screenshot(f, "03-collection-waits-for-proof")
        proof = verify_source(source, first)
        refresh.click()
        _select(page, "匹配本批次且当前有效的专业确认").select_option(proof)
        page.get_by_label("本次核验登记说明", exact=True).fill("SYNTHETIC source and collection reviewed")
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/review-sets")) as created:
            candidate.click()
        assert created.value.status == 201
        candidate_id = created.value.json()["id"]
        with f.app.state.session_factory() as db:
            current = db.get(DocumentVersion, source.version_id)
            assert current.selected_chunk_set_id is None and current.selected_review_set_id is None
        f.checks.append("browser accepts durable chunk job; installed tokenizer produces child; human proof gate and candidate independence verified")

        publish(source, set_id, candidate_id)
        with f.app.state.session_factory() as db:
            derived = derive_raw_run(db, db.get(User, source.actor_id), db.get(ParseRun, first), f.config)
            derived_id = derived.id
            db.commit()
            assert run_parse_job(db, derived_id, f.config, {}).stage_checkpoint["source_qc"]["status"] == "passed"
        refresh.click()
        batches.select_option(derived_id)
        expect(article.get_by_role("button", name="保存核验", exact=True)).to_be_visible()
        _screenshot(f, "04-effective-version-new-batch-editable")
        new_set = build(source, derived_id)
        new_review = review_candidate(source, derived_id, new_set, verify_source(source, derived_id))
        refresh.click()
        page.get_by_role("button", name="发布检查", exact=True).click()
        _select(page, "待选核验候选").select_option(new_review)
        page.get_by_role("button", name="检查同版更换来源", exact=True).click()
        page.get_by_role("button", name="按此预检更换现行来源", exact=True).click()
        expect(page.get_by_text("更换现行来源回执", exact=False)).to_be_visible()
        with f.app.state.session_factory() as db:
            current = db.get(DocumentVersion, source.version_id)
            assert current.publish_status == "effective" and current.selected_chunk_set_id == new_set
        page.get_by_role("button", name="原文核验", exact=True).click()
        batches.select_option(first)
        expect(page.get_by_text("此批次曾用于正式发布", exact=False)).to_be_visible()
        expect(article.get_by_role("button", name="保存核验", exact=True)).to_have_count(0)
        _screenshot(f, "05-former-published-source-readonly")
        f.checks.append("new source editable under effective version; old published source remains read-only after browser rebind")

        risk_id = accept(source)
        with f.app.state.session_factory() as db:
            run_local_parse_job(db, risk_id, f.config, runner=_replace_raw_text)
            normalize_parse_run(db, risk_id, f.config)
            structure_parse_run(db, risk_id, f.config)
            risk = run_source_qc(db, risk_id, f.config)
            assert risk.stage_checkpoint["source_qc"]["status"] == "needs_review", risk.stage_checkpoint
        refresh.click()
        batches.select_option(risk_id)
        expect(page.get_by_text("原生文字与识别结果不一致，请对照整页。", exact=False).last).to_be_visible()
        _screenshot(f, "06-visible-source-risk")
        f.checks.append("genuine technical native-text discrepancy visible with original page location")


def test_pdf_employee_exact_saved_quote_images_and_revocation(pdf_browser):
    from playwright.sync_api import expect

    f, page = pdf_browser, pdf_browser.page
    original, png = _synthetic_pdf(with_image=True)
    source = _new_source(f, "SYNTHETIC evidence manual", "SYNTHETIC EVIDENCE R1", original)
    quote = "合成 😀 引文𠀀" + chr(10) + "原样保留"
    with f.app.state.session_factory() as db:
        run = db.scalar(select(ParseRun).where(ParseRun.document_version_id == source.version_id))
        manager, version = db.get(User, source.actor_id), db.get(DocumentVersion, source.version_id)
        document, kb = db.get(Document, source.document_id), db.get(KnowledgeBase, source.kb_id)
        chunk_set = db.scalar(select(ChunkSet).where(ChunkSet.parse_run_id == run.id))
        text_block, _warning, picture = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id).order_by(ContentBlock.ordinal)).all()
        prefix = "SYNTHETIC prefix "
        text_block.text_content = prefix + quote + " END"
        region = {"file_page": 1, "printed_page": "iv", "geometry_status": "exact", "bbox_normalized": [0.11, 0.12, 0.29, 0.20]}
        blank = {"file_page": 2, "printed_page": "v", "geometry_status": "page_only"}
        picture.kind, picture.text_content = "image", ""
        picture.locator = {"regions": [region, blank]}
        version.revision_label = None
        seed_verified_version(db, manager, document, version, run, chunk_set.id)
        png_record = archive_bytes(db, run.id, manager.id, f.config, png, kind="image", logical_name="images/synthetic.png")
        employee = _employee(db, manager, kb, document, "browser")
        task = QueryTask(owner_user_id=employee.id, idempotency_key="synthetic-browser-" + uuid4().hex, status="succeeded",
            accepted_scope={"operation": "knowledge.query"},
            result_summary={"answer": None, "result_status": "partial",
                "coverage": {"missing_count": 1, "missing_item_refs": ["synthetic-private-ref"],
                    "items": [{"label": "合成参数的适用条件", "state": "missing"}]}})
        db.add(task)
        db.flush()
        text_item = TaskEvidence(task_id=task.id, document_version_id=version.id, block_id=text_block.id, quote_snapshot=quote,
            locator={"file_page": 1, "printed_page": "iv"}, evidence_kind="text",
            source_spans=[{"block_start": len(prefix), "block_end": len(prefix) + len(quote)}], review_at_run="approved")
        visual_item = TaskEvidence(task_id=task.id, document_version_id=version.id, block_id=picture.id, quote_snapshot="",
            locator={"regions": [region, blank]}, evidence_kind="region", asset_refs=[png_record.id], review_at_run="approved")
        db.add_all([text_item, visual_item])
        db.commit()
        task_id, asset_id, image_id, run_id = task.id, png_record.id, visual_item.id, run.id
    _login(f, f"query?task={task_id}", employee, EMPLOYEE_PASSWORD)
    expect(page.get_by_text("依据部分覆盖", exact=True)).to_be_visible()
    expect(page.locator("article[data-evidence-kind=text] h3")).to_contain_text("版本标识待确认")
    expect(page.get_by_role("region", name="依据缺项")).to_contain_text("合成参数的适用条件")
    assert page.locator("article[data-evidence-kind=text] blockquote").text_content() == quote
    image_card = page.locator("article[data-evidence-kind=region]")
    expect(image_card.locator("blockquote")).to_have_count(0)
    expect(image_card).to_contain_text("印刷页码 iv")
    expect(image_card).to_contain_text("印刷页码 v")
    expect(image_card).to_contain_text("仅定位至原页")
    hrefs = image_card.get_by_role("link", name=re.compile("核对原文")).evaluate_all("(links) => links.map(link => link.getAttribute('href'))")
    assert hrefs == [f"/documents/{source.document_id}/versions/{source.version_id}/file#page={page_no}" for page_no in (1, 2)]
    assert not page.get_by_text("synthetic-private-ref", exact=False).count()
    evidence_path = f"/tasks/{task_id}/evidence/{image_id}"
    with page.expect_response(lambda response: response.url.endswith(evidence_path + "/source-image?region=0")) as crop:
        image_card.locator("summary").filter(has_text="查看原页裁图").click()
    assert crop.value.status == 200 and crop.value.headers["cache-control"] == "private, no-store"
    with page.expect_response(lambda response: response.url.endswith(evidence_path + "/assets/" + asset_id)) as asset:
        image_card.locator("summary").filter(has_text="查看图像证据").click()
    assert asset.value.status == 200 and asset.value.body() == png
    expect(image_card.locator("img")).to_have_count(2)
    assert f.context.request.get(f.address.rstrip("/") + evidence_path + "/source-image?region=1").status == 404
    assert f.context.request.get(f.address + f"parse-runs/{run_id}/assets/{asset_id}").status in (403, 404)
    _screenshot(f, "01-saved-exact-quote-and-authorized-images")
    f.checks.append("saved synthetic result: exact Unicode, no quote on image, two exact-version page links and readable partial coverage")
    f.checks.append("private task crop and asset available; page-only crop and direct manager resource denied")
    with f.app.state.session_factory() as db:
        grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == employee.id,
                                                          AuthorizationGrant.operation == "document.read"))
        grant.revoked_at = utc_now()
        db.commit()
    page.get_by_role("button", name="刷新原查询状态", exact=True).click()
    expect(page.locator("article[data-evidence-kind]")).to_have_count(0)
    denied = f.context.request.get(f.address.rstrip("/") + evidence_path + "/assets/" + asset_id)
    assert denied.status in (401, 403, 404)
    _screenshot(f, "02-evidence-cleared-after-revocation")
    f.checks.append("current document grant revocation clears employee evidence and blocks saved image URL")


def test_pdf_system_role_cannot_read_source_or_self_attest(pdf_browser):
    from playwright.sync_api import expect

    f, page = pdf_browser, pdf_browser.page
    original, _ = _synthetic_pdf()
    source = _new_source(f, "SYNTHETIC restricted source", "SYNTHETIC ADMIN BOUNDARY", original)
    with f.app.state.session_factory() as db:
        admin = User(username="synthetic-browser-admin-" + uuid4().hex, display_name="SYNTHETIC system administrator",
            roles=["system_admin"], status="active", credential_state="active", credential_hash=hash_password(ADMIN_PASSWORD))
        db.add(admin)
        db.flush()
        now = utc_now()
        db.add(AuthorityBasis(identity={"user_ref": admin.id, "identity_label": admin.display_name, "responsibility": "SYNTHETIC registrar"},
            purposes=["access.manage"], scope={"knowledge_base_id": source.kb_id}, source_reference="synthetic://browser-role",
            valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=1)))
        db.flush()
        approval = g.register_approval(db, admin, {"applicant": admin.id, "operations": ["document.read"],
            "actual_approver": {"user_ref": admin.id, "identity_label": admin.display_name, "responsibility": "SYNTHETIC self claim"},
            "decision": "approved", "approved_at": now - timedelta(minutes=1), "valid_from": now - timedelta(minutes=1),
            "valid_until": now + timedelta(hours=1),
            "scope": {"knowledge_base_id": source.kb_id, "document_id": source.document_id, "level": "internal",
                "category": "manual", "need_class": "maintenance"}})
        db.commit()
        approval_id = approval.id
    _login(f, "access-review", admin, ADMIN_PASSWORD)
    expect(page.get_by_text("核查不等于批准", exact=True)).to_be_visible()
    claim = page.locator("button.approval-item").filter(has_text=approval_id)
    claim.click()
    expect(claim.get_by_text("待核查", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="提交核查记录", exact=True)).to_be_disabled()
    _screenshot(f, "01-admin-pending-proof")
    original_path = f"documents/{source.document_id}/versions/{source.version_id}/file"
    assert f.context.request.get(f.address + original_path).status in (403, 404)
    page.goto(f.address + "workspace/manage")
    expect(page.get_by_text("当前账号没有进入本工作区所需的角色", exact=False)).to_be_visible()
    expect(page.locator("body")).not_to_contain_text(SYNTHETIC_TEXT)
    _screenshot(f, "02-admin-source-denied")
    f.checks.append("system-only role sees pending proof; no self-attestation or access to original and management workspace")


def test_pdf_local_compatibility_index_build_activate_and_rollback(pdf_browser):
    from playwright.sync_api import expect

    f, page = pdf_browser, pdf_browser.page
    original, _ = _synthetic_pdf()
    source = _new_source(f, "SYNTHETIC legacy index manual", "SYNTHETIC LEGACY R1", original)
    with f.app.state.session_factory() as db:
        document, version, actor = db.get(Document, source.document_id), db.get(DocumentVersion, source.version_id), db.get(User, source.actor_id)
        run = db.scalar(select(ParseRun).where(ParseRun.document_version_id == version.id))
        collection = db.scalar(select(ChunkSet).where(ChunkSet.parse_run_id == run.id))
        seed_verified_version(db, actor, document, version, run, collection.id)
        db.commit()
    _login(f, "manage", source.actor, PASSWORD)
    _select(page, "知识库").select_option(source.kb_id)
    page.get_by_role("button", name="发布检查", exact=True).click()
    _select(page, "索引构建配置").select_option("local-keyword")
    get_status = f.address + f"knowledge-bases/{source.kb_id}/index-status"
    builds = page.get_by_role("button", name="请求构建候选代际", exact=True)
    activate = page.get_by_role("button", name="启用此代际", exact=True)

    def build_candidate():
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/index-generations")) as built:
            builds.click()
        assert built.value.status == 202
        expect(activate).to_be_visible()
        return built.value.json()["index_generation_id"]

    first = build_candidate()
    assert f.context.request.get(get_status).json()["active_generation_id"] is None
    _screenshot(f, "01-ready-candidate-no-active-index")
    activate.click()
    expect(page.get_by_text("此版本已纳入可用索引。", exact=True)).to_be_visible()
    assert f.context.request.get(get_status).json()["active_generation_id"] == first
    second = build_candidate()
    assert second != first and f.context.request.get(get_status).json()["active_generation_id"] == first
    activate.click()
    expect(page.get_by_text("启用回执 · 活动代际 " + second[:8], exact=True)).to_be_visible()
    page.get_by_role("button", name=re.compile(first[:8] + ".*本地关键词")).click()
    page.get_by_role("button", name="回滚至此代际", exact=True).click()
    expect(page.get_by_text("回滚回执 · 活动代际 " + first[:8], exact=True)).to_be_visible()
    assert f.context.request.get(get_status).json()["active_generation_id"] == first
    _screenshot(f, "02-original-active-pointer-restored")
    f.checks.append("legacy compatibility only: real browser build leaves active pointer, explicit activate, and rollback within same source revision")
