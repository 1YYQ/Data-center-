"""Installed Chrome over the built React workspace and isolated synthetic PG.

These are local role workflow checks, not enterprise confirmation or A-F signoff.
"""
from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import io
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from . import governance as g
from .config import Settings
from .db import build_engine, init_db
from .governance_models import ApprovalRevocation, AuthorityBasis, BusinessConfirmation, GovernanceEvidence
from .models import AuthorizationGrant, BusinessApproval, ContentBlock, DocumentVersion, IndexGeneration, KnowledgeBase, ParseRun, QueryTask, User, utc_now
from .services import seed_reference_data
from .testing_governance import _authority, _proof, seed_confirmation
from .test_worker import local_stack, synthetic_pdf, STARTUP, WARNING
from .worker import run_one

OUTPUT = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/local-browser")) / "workspace-browser"
pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
    reason="React workflow verification requires an explicit local PostgreSQL URL")


def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.fixture
def workspace(request):
    import psycopg
    from psycopg import sql
    from playwright.sync_api import sync_playwright
    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    assert chrome.is_file(), "Installed Chrome required for this explicit browser run"
    assert (Path("src/web/dist/index.html")).is_file(), "Build the formal React workspace first"
    base = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    assert base.drivername == "postgresql+psycopg" and base.host == "127.0.0.1"
    assert base.database.startswith("dc_local_")
    name = "dc_local_workspace_" + uuid4().hex[:16]
    admin = base.set(database="postgres").render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://", 1)
    config = Settings(database_url=base.set(database=name).render_as_string(hide_password=False),
        storage_dir=OUTPUT / name, bootstrap_token="synthetic-test-only")
    role = request.node.name.removeprefix("test_react_").split("_")[0]
    out = OUTPUT / os.environ.get("DATACENTER_WORKSPACE_EVIDENCE_LABEL", "initial") / role
    out.mkdir(parents=True, exist_ok=True)
    default = Path("outputs/data-center.db")
    before = _hash(default)
    lifecycle = {"database": name, "created": False, "dropped": False, "default_sqlite_before": before}
    with psycopg.connect(admin, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    lifecycle["created"] = True
    engine = build_engine(config.database_url)
    stack = None
    errors, checks, failed_http = [], [], []
    try:
        init_db(engine, config, migrate=True)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_database()")) == name
            lifecycle["migration"] = connection.scalar(text("SELECT version_num FROM alembic_version"))
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            seed_reference_data(db)
            stack = local_stack.__wrapped__((db, config))
            values = next(stack)
            _, _, client, address, employee, administrator, kb, document, version, upstream, mode = values
            manager = db.get(User, version.created_by)
            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, executable_path=str(chrome),
                    args=["--disable-background-networking", "--disable-component-update"])
                context = browser.new_context(viewport={"width": 1440, "height": 900}, service_workers="block")
                # Intercept external requests only. Installed Chrome may be newer
                # than Playwright's protocol; relaying local multipart through a
                # catch-all route can corrupt its boundary/body on that pairing.
                context.route(re.compile(r"^https?://(?!" + re.escape(address.removeprefix("http://")) + r")"),
                    lambda route: route.abort())
                page = context.new_page()
                page.set_default_timeout(15000)
                page.on("pageerror", lambda error: errors.append(str(error)))
                def observe_response(response):
                    if response.status >= 400:
                        try:
                            detail = response.json()
                        except Exception:
                            detail = {"body": "non-JSON error"}
                        failed_http.append({"path": response.url.removeprefix(address), "status": response.status,
                            "request_content_type": response.request.headers.get("content-type"), "detail": detail})
                page.on("response", observe_response)
                f = SimpleNamespace(db=db, config=config, client=client, address=address, employee=employee,
                    administrator=administrator, manager=manager, kb=kb, document=document, version=version,
                    upstream=upstream, mode=mode, browser=browser, context=context, page=page, out=out, checks=checks)
                try:
                    yield f
                finally:
                    page.screenshot(path=str(out / "last-state.png"), full_page=True)
                    (out / "last-dom.html").write_text(page.content(), encoding="utf-8")
                    (out / "observations.json").write_text(json.dumps({"browser": browser.version,
                        "checks": checks, "page_errors": errors, "failed_http": failed_http,
                        "scope": "built React; real local PostgreSQL; synthetic controlled trust fixtures and loopback model only"},
                        ensure_ascii=False, indent=2), encoding="utf-8")
                    context.close(); browser.close()
                    assert not errors, errors
            next(stack, None)
            stack = None
    finally:
        if stack is not None:
            stack.close()
        engine.dispose()
        with psycopg.connect(admin, autocommit=True) as connection:
            assert name.startswith("dc_local_workspace_") and name != base.database
            connection.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))
        lifecycle["dropped"] = True
        lifecycle["default_sqlite_after"] = _hash(default)
        (out / "lifecycle.json").write_text(json.dumps(lifecycle, indent=2), encoding="utf-8")
        assert lifecycle["default_sqlite_after"] == before


def _login(f, path, user, password):
    page = f.page
    page.goto(f.address + "workspace/" + path)
    page.wait_for_load_state("networkidle")
    assert page.locator('script[src^="/workspace/assets/"]').count() > 0
    page.get_by_label("账号", exact=True).fill(user.username)
    page.get_by_label("口令", exact=True).fill(password)
    page.get_by_role("button", name="登录", exact=True).click()
    page.get_by_role("button", name="退出", exact=True).wait_for()


def _screenshot(f, name):
    f.page.evaluate("window.scrollTo(0,0)")
    f.page.screenshot(path=str(f.out / (name + ".png")), full_page=True)


def _csrf(f):
    return f.page.evaluate("JSON.parse(sessionStorage.getItem('dc-workspace-session')).csrf")


def _select(scope, label):
    # The selected option may be included in an implicit label's text by this
    # installed browser/protocol pairing. Identify its visible label and select
    # explicitly, preserving semantic intent without depending on old DOM IDs.
    return scope.locator("label").filter(has_text=re.compile("^" + re.escape(label))).locator("select")


# The former query flow is now covered by test_task_workspace_browser.py with
# formal child evidence, GET-only receipt recovery, controlled PDF and share revocation.


@pytest.fixture
def manager_embedding_workspace(request, monkeypatch):
    import httpx

    from .service_admission import load_local_endpoints
    from .synthetic_provider import run_synthetic_provider
    from .test_embedding_service_use import _ENV, _SECRET
    from .test_service_use import activate, select_purpose

    monkeypatch.setenv(_ENV, _SECRET)
    with run_synthetic_provider(credential=_SECRET) as provider:
        monkeypatch.setenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", json.dumps({"synthetic": {
            "port": provider.port, "credential_env": _ENV, "allowed_models": ["BAAI/bge-m3"]}}))
        f = request.getfixturevalue("workspace")
        with httpx.Client(base_url=f.address, timeout=10, trust_env=False) as admin:
            login = admin.post("/auth/login", json={"username": f.administrator.username, "password": "synthetic admin password"})
            assert login.status_code == 200, login.text
            admin.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            _, _, revision_id = activate(admin, "embedding")
            select_purpose(admin, "document.embed", revision_id)
        provider.requests.clear()
        yield f, revision_id, provider, load_local_endpoints()


def _synthetic_manager_manual():
    import pymupdf

    from .test_local_parse_pipeline import saved_bundle

    lines = [("doc_title", "SYNTHETIC Operating instructions"), ("paragraph", "Step 1: " + STARTUP),
             ("paragraph", "Warning for step 1: " + WARNING)]
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        for ordinal, (_, line) in enumerate(lines):
            page.insert_text((72, 112 + ordinal * 28), line)
        width, height = page.rect.width, page.rect.height
        blocks = []
        for ordinal, (kind, line) in enumerate(lines):
            areas = page.search_for(line)
            assert len(areas) == 1, line
            rect = areas[0]
            blocks.append({"index": ordinal, "type": kind,
                "bbox": [rect.x0 / width, rect.y0 / height, rect.x1 / width, rect.y1 / height],
                "content": [{"type": "text", "content": line}], **({"level": 1} if kind == "doc_title" else {})})
        pdf.new_page()
        original = pdf.tobytes()
    with ZipFile(io.BytesIO(saved_bundle())) as template:
        payload = json.loads(template.read("middle_json.json"))
    payload["pages"][0]["blocks"] = blocks

    def standard_output(source, workspace, _executable, _home, _heartbeat):
        assert source.read_bytes() == original
        path = workspace / "result.zip"
        with ZipFile(path, "w") as archive:
            archive.writestr("middle_json.json", json.dumps(payload))
            archive.writestr("structured_content.json", json.dumps({"pages": payload["pages"]}))
            archive.writestr("markdown.md", "\n\n".join(line for _, line in lines))
        return path

    return original, standard_output


def _build_and_enable_manager_index(f, revision_id, provider, endpoints):
    from playwright.sync_api import expect

    from .embedding_jobs import child_batch_payload, run_child_embedding_batch
    from .testing_service_use import attest_synthetic_input

    db, page = f.db, f.page
    db.expire_all()
    previous = db.get(KnowledgeBase, f.kb.id).active_generation_id
    _select(page, "索引构建配置").select_option(revision_id)
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/index-generations")) as accepted:
        page.get_by_role("button", name="请求构建候选代际", exact=True).click()
    assert accepted.value.status == 202, accepted.value.text()
    generation_id = accepted.value.json()["index_generation_id"]
    db.expire_all()
    candidate = db.get(IndexGeneration, generation_id)
    assert candidate.status == "building" and db.get(KnowledgeBase, f.kb.id).active_generation_id == previous
    manifest = candidate.build_report["manifest"]
    batches = [batch["batch_no"] for batch in manifest["batches"]]
    assert batches and not provider.requests
    for number in batches:
        _, _, payload = child_batch_payload(db, manifest, number)
        attest_synthetic_input(db, f.manager.id, "document.embed", f"index:{generation_id}:{number}", payload)
    db.commit()
    refresh = page.get_by_role("button", name="刷新索引状态", exact=True)
    refresh.click()
    page.get_by_text("查看并受理嵌入批次", exact=False).click()
    for number in batches:
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/embedding-batches/{number}")) as accepted:
            page.get_by_role("button", name=f"受理第 {number} 批", exact=True).click()
        assert accepted.value.status == 202, accepted.value.text()
        receipt = accepted.value.json()
        assert receipt["state"] == "pending"
        job = run_child_embedding_batch(db, receipt["embedding_job_id"], endpoints)
        assert job.state == "succeeded", job.error_code
    assert len(provider.requests) == len(batches)
    refresh.click()
    expect(page.get_by_role("region", name="所选索引代际")).to_contain_text("构建已就绪")
    page.get_by_text("索引状态 来源已变化", exact=True).wait_for()
    db.expire_all()
    assert db.get(KnowledgeBase, f.kb.id).active_generation_id == previous
    page.get_by_role("button", name="启用此代际", exact=True).click()
    page.get_by_text("此版本已纳入可用索引。", exact=True).wait_for()
    db.expire_all()
    assert db.get(KnowledgeBase, f.kb.id).active_generation_id == generation_id


def test_react_manager_review_confirmation_publication_and_access_loss(manager_embedding_workspace):
    from playwright.sync_api import expect

    from .chunk_jobs import run_chunk_job
    from .parse_jobs import run_local_parse_job, run_parse_job
    from .parsing.processing import source_confirmation_scope, source_verification
    from .test_chunk_processing import TOKENIZER

    f, embedding_revision, provider, endpoints = manager_embedding_workspace
    page = f.page
    db, manager, doc = f.db, f.manager, f.document
    assert TOKENIZER.is_file(), "Installed local BGE-M3 tokenizer required for this browser run"
    original, standard_output = _synthetic_manager_manual()
    _login(f, "manage", manager, "manager password")
    _select(page, "文档").select_option(doc.id)
    page.get_by_text("上传此文档新版本 PDF", exact=True).click()
    page.get_by_label("版本标识（可选）", exact=True).fill("SYNTHETIC-REACT-REVISION")
    page.get_by_label("适用型号（逗号分隔，可选）", exact=True).fill("DEMO-1")
    page.get_by_label("新版本 PDF", exact=True).set_input_files({"name": "SYNTHETIC-REACT-DRAFT.pdf", "mimeType": "application/pdf", "buffer": original})
    page.get_by_role("button", name="上传新版本", exact=True).click()
    page.get_by_text("新版本草稿已保存：", exact=False).wait_for()
    db.expire_all()
    draft = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
        DocumentVersion.revision_label == "SYNTHETIC-REACT-REVISION"))
    assert draft and draft.publish_status == "draft" and draft.model_scope == ["DEMO-1"]
    assert _select(page, "版本").input_value() == draft.id
    assert db.get(DocumentVersion, f.version.id).publish_status == "effective"
    page.get_by_role("button", name="业务依据", exact=True).click()
    page.locator("section.panel").get_by_label("来源渠道", exact=True).fill("synthetic-controlled-local")
    page.locator("section.panel").get_by_label("来源说明", exact=True).fill("SYNTHETIC React local source supplied by authorized test initializer")
    page.get_by_role("button", name="保存资料来源", exact=True).click()
    page.get_by_text("资料来源已保存", exact=False).wait_for()
    db.refresh(draft)
    assert draft.source_metadata["channel"] == "synthetic-controlled-local"
    assert draft.source_metadata["source_description"] == "SYNTHETIC React local source supplied by authorized test initializer"
    assert db.scalar(select(func.count()).select_from(BusinessConfirmation).where(BusinessConfirmation.document_version_id == draft.id)) == 0
    page.get_by_role("button", name="原文核验", exact=True).click()
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/parse-runs")) as accepted:
        page.get_by_role("button", name="请求本地解析此版本", exact=True).click()
    assert accepted.value.status == 202, accepted.value.text()
    run_id = accepted.value.json()["parse_run_id"]
    page.get_by_text("已收到本地解析请求", exact=False).wait_for()
    db.expire_all()
    run = db.get(ParseRun, run_id)
    assert run.document_version_id == draft.id and run.origin == "local" and run.status == "pending"
    def refresh_run():
        button = page.get_by_role("button", name="刷新解析批次", exact=True)
        if not button.is_visible(): page.get_by_text("解析批次与处理详情", exact=True).click()
        button.click()
    refresh_run()
    page.get_by_text("已读取最新批次状态。", exact=True).wait_for()
    db.refresh(run)
    assert run.status == "pending"
    raw = run_local_parse_job(db, run_id, f.config, runner=standard_output)
    assert raw.stage_checkpoint.get("outputs", {}).get("raw_manifest"), raw.stage_checkpoint
    run = run_parse_job(db, run_id, f.config, {})
    assert run.status == "needs_review" and run.stage_checkpoint["source_qc"]["status"] == "passed", run.stage_checkpoint
    assert run.stage_checkpoint["source_qc"]["required_relation_refs"], "Synthetic warning must bind to its procedure step"
    leaves = select(ContentBlock).where(ContentBlock.parse_run_id == run.id, ContentBlock.structure_role == "leaf").order_by(ContentBlock.ordinal)
    parsed_blocks = db.scalars(leaves).all()
    assert parsed_blocks and STARTUP in "\n".join(block.text_content for block in parsed_blocks)
    assert WARNING in "\n".join(block.text_content for block in parsed_blocks)
    refresh_run()
    page.get_by_text("查看处理与源核验状态", exact=True).click()
    milestones = page.locator(".pdf-milestones")
    expect(milestones.locator("div").filter(has_text="源质量检查").get_by_text("通过", exact=True)).to_be_visible()
    expect(milestones.locator("div").filter(has_text="源人工核验").get_by_text("待核验", exact=True)).to_be_visible()
    source = page.get_by_role("link", name="打开此版本原文 PDF", exact=True).get_attribute("href")
    assert "version=" + draft.id in source
    source = f"/documents/{doc.id}/versions/{draft.id}/file"
    assert f.context.request.get(f.address + f"documents/{doc.id}/versions/{draft.id}/file").body() == original
    page.get_by_role("button", name="发布检查", exact=True).click()
    page.get_by_role("button", name="检查发布条件", exact=True).click()
    page.get_by_role("heading", name="仍有阻断项", exact=True).wait_for()
    assert page.get_by_role("button", name="按此预检发布", exact=True).count() == 0
    page.get_by_role("button", name="原文核验", exact=True).click()
    for block in parsed_blocks:
        ordinal = block.ordinal
        article = page.locator(f'article[data-block-id="{block.id}"]')
        article.get_by_label(f"内容块 {ordinal} 核验结论", exact=True).select_option("approved")
        article.get_by_label(f"内容块 {ordinal} 核验说明", exact=True).fill("SYNTHETIC exact source and warning checked")
        article.get_by_role("button", name="保存核验", exact=True).click()
        page.get_by_text("逐块核验结果已保存。", exact=True).wait_for()
        expect(article).to_contain_text("核验通过")
    _screenshot(f, "manager-reviewed-blocks")
    page.set_viewport_size({"width":1366,"height":900})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    _screenshot(f, "manager-reviewed-blocks-1366")
    page.set_viewport_size({"width":1440,"height":900})
    db.expire_all()
    assert [(block.revision, block.review_status) for block in db.scalars(leaves)] == [(1, "approved")] * len(parsed_blocks)
    assert source_verification(db, run)["state"] == "pending"
    binding = g.source_binding(db, draft)
    seed_confirmation(db, manager, draft, "applicability", "applicable", binding)
    seed_confirmation(db, manager, draft, "classification", "classified", {**binding, "access_level": doc.access_level,
        "category": doc.category, "need_class": doc.need_class, "rule_version": "synthetic-v1"})
    authority, _ = _authority(db, purpose="key_content", scope={key: binding[key] for key in ("version_id", "source_sha256", "scope_hash")})
    db.commit()
    _select(page, "版本").select_option(draft.id)
    page.get_by_role("button", name="业务依据", exact=True).click()
    _select(page, "确认类型").select_option("key_content")
    _select(page, "已登记职责依据").select_option(authority.id)
    page.get_by_label("确认时间", exact=True).fill((datetime.now() - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M"))
    page.get_by_label("实际确认人的依据覆盖此批次的原页、内容及适用关系", exact=True).check()
    page.get_by_role("button", name="登记真实确认", exact=True).click()
    page.get_by_text("真实确认事实已登记", exact=False).wait_for()
    db.expire_all()
    professional = db.scalar(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == draft.id,
        BusinessConfirmation.kind == "key_content"))
    assert professional and professional.verification_state == "pending"
    assert professional.scope == {**binding, **source_confirmation_scope(db, run), "observation_dispositions": {}}
    assert source_verification(db, run)["state"] == "pending"
    page.get_by_text("补充本版本真实依据材料", exact=True).click()
    page.get_by_label("依据来源说明", exact=True).fill("SYNTHETIC ordinary browser attachment")
    page.get_by_label("材料文件", exact=True).set_input_files({"name": "SYNTHETIC-proof.pdf", "mimeType": "application/pdf", "buffer": synthetic_pdf()})
    page.get_by_role("button", name="上传待核查材料", exact=True).click()
    page.get_by_text("待核查材料已保存", exact=False).wait_for()
    db.expire_all()
    untrusted = db.scalar(select(GovernanceEvidence).where(GovernanceEvidence.owner_id == draft.id, GovernanceEvidence.attested.is_(False)))
    def card():
        return page.locator("article.record").filter(has_text=professional.id)
    card().get_by_role("button", name="查看依据与核查", exact=True).click()
    _select(card(), "本版本依据材料（须逐项核对）").select_option(untrusted.id)
    card().get_by_role("button", name="保存依据关联", exact=True).click()
    page.get_by_text("已保存依据关联，等待受控核查。", exact=True).wait_for()
    card().get_by_role("button", name="查看依据与核查", exact=True).click()
    card().get_by_label("核查原因", exact=True).fill("SYNTHETIC ordinary attachment cannot authorize")
    expect(card().get_by_role("button", name="记录核查通过", exact=True)).to_be_disabled()
    db.expire_all(); db.refresh(professional)
    refused = f.context.request.post(f.address + f"documents/{doc.id}/versions/{draft.id}/business-confirmations/{professional.id}/verification",
        data={"decision": "verified", "reason": "SYNTHETIC reject untrusted proof", "authority_basis_ref": authority.id, "checked_proof_refs": [untrusted.id]},
        headers={"X-CSRF-Token": _csrf(f), "Idempotency-Key": str(uuid4()), "If-Match": f'"{professional.revision}"'})
    assert refused.status == 409 and refused.json()["code"] == "PROOF_UNVERIFIABLE"
    _screenshot(f, "manager-untrusted-proof-blocked")
    db.expire_all(); professional = db.get(BusinessConfirmation, professional.id)
    proof = _proof(db, manager, "version", draft.id, g._confirmation_assertion(professional)); db.commit()
    page.get_by_role("button", name="刷新", exact=True).click()
    _select(page, "版本").select_option(draft.id)
    page.get_by_role("button", name="业务依据", exact=True).click()
    card().get_by_role("button", name="查看依据与核查", exact=True).click()
    _select(card(), "本版本依据材料（须逐项核对）").select_option(proof.id)
    card().get_by_role("button", name="保存依据关联", exact=True).click()
    page.get_by_text("已保存依据关联，等待受控核查。", exact=True).wait_for()
    card().get_by_role("button", name="查看依据与核查", exact=True).click()
    card().get_by_label("核查原因", exact=True).fill("SYNTHETIC controlled witness exact scope matched")
    card().get_by_role("button", name="记录核查通过", exact=True).click()
    page.get_by_text("系统核查结果已读取。", exact=True).wait_for()
    expect(card()).to_contain_text("当前有效")
    db.expire_all()
    assert source_verification(db, run)["state"] == "verified"
    _screenshot(f, "manager-confirmation-verified")
    page.get_by_role("button", name="原文核验", exact=True).click()
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/chunk-sets")) as accepted:
        page.get_by_role("button", name="请求生成切分集合", exact=True).click()
    assert accepted.value.status == 202, accepted.value.text()
    chunk_set_id = accepted.value.json()["id"]
    chunk_set = run_chunk_job(db, chunk_set_id, replace(f.config, local_bge_tokenizer=TOKENIZER.resolve()))
    assert chunk_set.status == "ready" and chunk_set.stage_checkpoint["quality"]["status"] == "passed", chunk_set.stage_checkpoint
    refresh_run()
    _select(page, "匹配本批次且当前有效的专业确认").select_option(professional.id)
    page.get_by_label("本次核验登记说明", exact=True).fill("SYNTHETIC exact procedure and warning checked in the collection")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith("/review-sets")) as accepted:
        page.get_by_role("button", name="创建完整核验候选", exact=True).click()
    assert accepted.value.status == 201, accepted.value.text()
    review_id = accepted.value.json()["id"]
    page.get_by_text("完整核验候选已登记", exact=False).wait_for()
    db.refresh(draft)
    assert draft.selected_chunk_set_id is None and draft.selected_review_set_id is None
    page.get_by_role("button", name="发布检查", exact=True).click()
    _select(page, "待选核验候选").select_option(review_id)
    page.get_by_role("button", name="选用此草稿来源", exact=True).click()
    page.get_by_text("草稿来源已选用", exact=False).wait_for()
    db.refresh(draft)
    assert (draft.selected_chunk_set_id, draft.selected_review_set_id) == (chunk_set_id, review_id)
    page.get_by_label("仅供参考开放此精确版本原文", exact=True).check()
    page.get_by_role("button", name="保存参考状态", exact=True).click()
    page.get_by_text("已保存原文参考状态。", exact=True).wait_for()
    db.expire_all(); assert db.get(DocumentVersion, draft.id).publish_status == "draft"
    page.get_by_role("button", name="检查发布条件", exact=True).click()
    page.get_by_role("heading", name="预检允许发布", exact=True).wait_for()
    page.get_by_role("button", name="按此预检发布", exact=True).click()
    page.get_by_text("发布回执：", exact=False).wait_for()
    page.get_by_text("索引状态 来源已变化", exact=True).wait_for()
    _screenshot(f, "manager-published-stale")
    _build_and_enable_manager_index(f, embedding_revision, provider, endpoints)
    _screenshot(f, "manager-index-ready")
    page.get_by_label("撤回原因", exact=True).fill("SYNTHETIC React withdrawal")
    page.get_by_role("button", name="检查撤回影响", exact=True).click()
    page.get_by_role("heading", name="预检允许撤回", exact=True).wait_for()
    page.get_by_role("button", name="按此预检撤回", exact=True).click()
    page.get_by_text("撤回回执：", exact=False).wait_for()
    page.get_by_text("索引状态 来源已变化", exact=True).wait_for()
    _screenshot(f, "manager-withdrawn")
    # Receipt rendering precedes the awaited version/confirmation refresh. Inject
    # revocation after that operation settles, so the explicit reload below is
    # the request being tested rather than an already-running refresh.
    expect(page.get_by_role("button", name="刷新", exact=True)).to_be_enabled()
    db.expire_all(); assert db.get(DocumentVersion, draft.id).publish_status == "withdrawn"
    grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == manager.id,
        AuthorizationGrant.operation == "knowledge.manage"))
    grant.revoked_at = utc_now(); db.commit()
    page.get_by_role("button", name="刷新", exact=True).click()
    page.get_by_role("heading", name="登录数据中心", exact=True).wait_for()
    expect(page.locator("body")).not_to_contain_text(STARTUP)
    assert page.get_by_role("link", name="打开此版本原文 PDF", exact=True).count() == 0
    _screenshot(f, "manager-access-cleared")
    original_url = f.address.rstrip("/") + source
    historical = f.context.request.get(original_url)
    assert historical.status == 200 and historical.headers["x-document-read-mode"] == "historical" and historical.body() == original
    reader = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == manager.id,
        AuthorizationGrant.knowledge_base_id == f.kb.id, AuthorizationGrant.operation == "document.read"))
    reader.revoked_at = utc_now(); db.commit()
    assert f.context.request.get(original_url).status == 404
    f.checks.extend(["React existing-document new-version upload retains old effective version; durable Standard ZIP processing through IR and QC; source requires independent human review; incomplete publication blocked",
        "source channel and source description recorded through React source-metadata editor", "pending real confirmation registration and owned attachment link do not self-attest",
        "exact source scope and controlled synthetic proof linked and verified through React", "persistent BGE-M3 split, candidate, explicit draft source selection and reference state precede formal publication",
        "actual publish/withdraw preview receipts; child index candidates use real loopback batches and explicit activation for exact-version inclusion",
        "management revocation clears the workspace; the historical original remains available only until the independent document read grant is revoked"])


def test_react_admin_pending_proof_verification_revocation_and_role_refusal(workspace):
    from playwright.sync_api import expect
    f, page, db = workspace, workspace.page, workspace.db
    now = utc_now()
    scope = {"knowledge_base_id": f.kb.id, "document_id": None, "level": f.document.access_level,
        "category": f.document.category, "need_class": f.document.need_class}
    manager_basis = AuthorityBasis(identity={"user_ref": f.administrator.id, "identity_label": f.administrator.display_name,
        "responsibility": "SYNTHETIC administrator"}, purposes=["access.manage"], scope={"knowledge_base_id": f.kb.id},
        source_reference="synthetic://react-test-manager", valid_from=now - timedelta(days=1), valid_until=now + timedelta(days=10))
    approver_basis, identity = _authority(db, purpose="access_approval", scope=scope)
    db.add(manager_basis); db.flush()
    positive = BusinessApproval(applicant=f.employee.id, recorded_by=f.administrator.id, actual_approver_snapshot=identity,
        actual_decision="approved", approved_at=now - timedelta(minutes=5), scope=scope, operations=["document.read"],
        valid_from=now - timedelta(minutes=5), valid_until=now + timedelta(days=2), status="pending", verification_state="unverified",
        evidence_locator="SYNTHETIC external decision")
    db.add(positive); db.flush()
    trusted = _proof(db, f.administrator, "approval", positive.id, g._approval_assertion(positive))
    revoke_basis, revoke_identity = _authority(db, purpose="access_revocation", scope={"approval_id": positive.id, **scope})
    revoke_proof = _proof(db, f.administrator, "approval", positive.id, {"kind": "access_revocation", "actual_identity": revoke_identity,
        "decision": "revoked", "decided_at": (now - timedelta(minutes=1)).isoformat(), "scope": {"approval_id": positive.id, **scope}})
    db.commit()
    before = db.scalar(select(func.count()).select_from(AuthorizationGrant))
    _login(f, "access-review", f.administrator, "synthetic admin password")
    page.goto(f.address + "workspace/accounts")
    page.get_by_text("创建平台账号", exact=True).click()
    username = "synthetic-ui-account-" + uuid4().hex[:12]
    page.get_by_label("登录账号", exact=True).fill(username)
    page.get_by_label("显示名称", exact=True).fill("模拟生命周期账号")
    page.get_by_role("button", name="登记账号", exact=True).click()
    account = page.locator(".account-row").filter(has_text=username)
    account.get_by_text("管理此账号", exact=True).click()
    account.get_by_label("新口令", exact=True).fill("SYNTHETIC workspace account password")
    account.get_by_label("操作原因", exact=True).fill("SYNTHETIC activation only")
    account.get_by_role("button", name="确认更新凭据", exact=True).click()
    expect(account).to_contain_text("已启用")
    page.once("dialog", lambda dialog: dialog.accept())
    account.get_by_role("button", name="停用账号并撤销会话", exact=True).click()
    expect(account).to_contain_text("已停用")
    assert db.scalar(select(User).where(User.username == username)).status == "disabled"
    page.goto(f.address + "workspace/access-review")
    f.checks.append("actual account creation, credential activation and confirmed deactivation use existing user APIs without granting document access")
    page.get_by_role("button", name="＋ 登记外部审批事实", exact=True).click()
    dialog = page.get_by_role("dialog", name="登记真实审批事实", exact=True)
    values = {"被授权用户 ID": f.administrator.id, "实际审批人": approver_basis.identity["identity_label"],
        "审批职责": approver_basis.identity["responsibility"], "知识库 ID": f.kb.id, "资料等级": scope["level"],
        "资料类别": scope["category"], "用途分类": scope["need_class"], "实际审批人的职责依据编号": approver_basis.id,
        "外部审批来源说明": "SYNTHETIC React registration creates no grant"}
    for label, value in values.items():
        dialog.get_by_label(label, exact=True).fill(value)
    for label, value in {"审批时间": now - timedelta(minutes=2), "授权开始": now - timedelta(minutes=2), "授权截止": now + timedelta(days=2)}.items():
        dialog.get_by_label(label, exact=True).fill(value.astimezone().strftime("%Y-%m-%dT%H:%M"))
    _select(dialog, "允许操作").select_option("document.read")
    dialog.get_by_role("button", name="保存为待核查", exact=True).click()
    page.get_by_text("事实已登记为待核查", exact=False).wait_for()
    db.expire_all()
    self_record = db.scalar(select(BusinessApproval).where(BusinessApproval.applicant == f.administrator.id))
    assert self_record.status == "pending" and self_record.verification_state == "unverified" and self_record.approver is None
    assert db.scalar(select(func.count()).select_from(AuthorizationGrant)) == before
    _screenshot(f, "admin-pending-no-grant")
    expect(page.locator(".facts-grid")).to_contain_text("SYNTHETIC React registration creates no grant")
    page.get_by_role("button").filter(has_text=positive.id).click()
    expect(page.locator(".facts-grid")).to_contain_text("SYNTHETIC external decision")
    page.get_by_label("选择 PDF 附件", exact=True).set_input_files({"name": "SYNTHETIC-untrusted.pdf", "mimeType": "application/pdf", "buffer": synthetic_pdf()})
    page.get_by_label("来源说明", exact=True).fill("SYNTHETIC ordinary React attachment")
    page.get_by_role("button", name="登记附件", exact=True).click()
    page.get_by_text("附件已登记，尚未核实", exact=False).wait_for()
    page.get_by_role("checkbox", name="SYNTHETIC ordinary React attachment", exact=False).check()
    page.get_by_role("button", name="更新关联材料", exact=True).click()
    page.get_by_text("材料关联已更新", exact=False).wait_for()
    expect(page.get_by_role("button", name="提交核查记录", exact=True)).to_be_disabled()
    db.expire_all(); db.refresh(positive)
    ordinary = db.scalar(select(GovernanceEvidence).where(GovernanceEvidence.owner_id == positive.id, GovernanceEvidence.attested.is_(False)))
    refused = f.context.request.post(f.address + f"admin/business-approvals/{positive.id}/verification",
        data={"decision": "verified", "reason": "SYNTHETIC reject untrusted proof", "authority_basis_ref": approver_basis.id, "checked_evidence_refs": [ordinary.id]},
        headers={"X-CSRF-Token": _csrf(f), "Idempotency-Key": str(uuid4()), "If-Match": f'"{positive.revision}"'})
    assert refused.status == 409 and refused.json()["code"] == "PROOF_UNVERIFIABLE"
    _screenshot(f, "admin-untrusted-blocked")
    page.get_by_role("checkbox", name="SYNTHETIC ordinary React attachment", exact=False).uncheck()
    page.get_by_role("checkbox", name=trusted.source_reference["source_description"] + " · " + trusted.id, exact=True).check()
    page.get_by_role("button", name="更新关联材料", exact=True).click()
    page.get_by_text("材料关联已更新", exact=False).wait_for()
    _select(page, "实际审批职责依据").select_option(approver_basis.id)
    page.get_by_label("核查说明", exact=True).fill("SYNTHETIC controlled external decision exact scope checked")
    page.get_by_role("button", name="提交核查记录", exact=True).click()
    page.get_by_text("服务端核查结果已读取。", exact=True).wait_for()
    db.expire_all(); assert g.approval_is_current(db, db.get(BusinessApproval, positive.id))
    assert db.scalar(select(func.count()).select_from(AuthorizationGrant)) == before
    _screenshot(f, "admin-verified")
    page.get_by_label("已核实撤销证据引用", exact=True).fill(revoke_proof.id)
    page.get_by_label("登记说明", exact=True).fill("SYNTHETIC external revocation exact proof checked")
    page.get_by_role("button", name="登记外部撤销", exact=True).click()
    page.get_by_text("撤销登记回执：", exact=False).wait_for()
    db.expire_all()
    revocation = db.scalar(select(ApprovalRevocation).where(ApprovalRevocation.approval_id == positive.id))
    assert revocation and not g.approval_is_current(db, db.get(BusinessApproval, positive.id))
    expect(page.locator(".facts-grid")).to_contain_text(revocation.id)
    assert db.scalar(select(func.count()).select_from(AuthorizationGrant)) == before
    _screenshot(f, "admin-revoked")
    page.get_by_role("button", name="退出", exact=True).click()
    _login(f, "access-review", f.employee, "synthetic employee password")
    page.get_by_text("当前账号没有进入本工作区所需的角色，请切换到已获准的工作区。", exact=True).wait_for()
    assert page.get_by_role("button", name="＋ 登记外部审批事实", exact=True).count() == 0
    assert positive.id not in page.locator("body").inner_text()
    assert f.context.request.get(f.address + "admin/business-approvals").status == 403
    _screenshot(f, "admin-employee-refused")
    f.checks.extend(["React registers pending self-beneficiary approval facts without granting access", "ordinary uploaded evidence explicitly linked but cannot pass verification",
        "owned controlled synthetic approval proof linked and checked through React without adding grants", "external revocation exact evidence creates actual receipt and invalidates approval",
        "ordinary employee role rejected by React and server without approval records"])
