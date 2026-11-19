"""Installed Chrome, synthetic PDF and isolated PostgreSQL lifecycle checks.

Controlled fixture approvals/confirmations are local test facts, never enterprise
signoff, provider authorization, production deployment or complete A-F acceptance.
"""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path
import re
from types import SimpleNamespace
from uuid import uuid4
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session
from . import governance as g
from .config import Settings
from .db import build_engine, init_db
from .governance_models import BusinessConfirmation, DocumentUseHold, ReviewSet
from .models import (ApiIdempotencyRecord, AuditEvent, AuthorizationGrant, BusinessApproval, ChunkSet,
    ContentBlock, Document, DocumentVersion, FileObject, KnowledgeBase, ParseRun, QueryTask, User, utc_now)
from .services import seed_reference_data
from .testing_governance import seed_confirmation, seed_verified_approval
from .test_worker import local_stack, synthetic_pdf, submit, STARTUP, WARNING
from .worker import run_one
OUTPUT = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/local-browser")) / "lifecycle-browser"
pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
    reason="Lifecycle Chrome verification requires an explicit isolated local PostgreSQL URL")

def _hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.fixture
def lifecycle_workspace(request):
    import psycopg
    from psycopg import sql
    from playwright.sync_api import sync_playwright
    chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
    assert chrome.is_file(), "Installed Chrome required for this explicit browser run"
    assert (Path("src/web/dist/index.html")).is_file(), "Build the formal React workspace first"
    base = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    assert base.drivername == "postgresql+psycopg" and base.host == "127.0.0.1"
    assert base.database.startswith("dc_local_")
    name = "dc_local_lifecycle_" + uuid4().hex[:16]
    admin = base.set(database="postgres").render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://", 1)
    config = Settings(database_url=base.set(database=name).render_as_string(hide_password=False),
        storage_dir=OUTPUT / name, bootstrap_token="synthetic-test-only")
    role = request.node.name.removeprefix("test_lifecycle_")
    out = OUTPUT / os.environ.get("DATACENTER_LIFECYCLE_EVIDENCE_LABEL", "initial") / role
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
                context = browser.new_context(viewport={"width": 1440, "height": 1000}, service_workers="block")
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
            assert name.startswith("dc_local_lifecycle_") and name != base.database
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
    f.page.screenshot(path=str(f.out / (name + ".png")), full_page=True)


def _csrf(f):
    return f.page.evaluate("JSON.parse(sessionStorage.getItem('dc-workspace-session')).csrf")


def _select(scope, label):
    # The selected option may be included in an implicit label's text by this
    # installed browser/protocol pairing. Identify its visible label and select
    # explicitly, preserving semantic intent without depending on old DOM IDs.
    return scope.locator("label").filter(has_text=re.compile("^" + re.escape(label))).locator("select")


def _request(f, method, path, payload=None, match=None):
    headers = {"X-CSRF-Token": _csrf(f), "Idempotency-Key": str(uuid4())}
    if match is not None:
        headers["If-Match"] = match
    return f.context.request.fetch(f.address.rstrip("/") + path, method=method, data=payload, headers=headers)


def _scope_new_kb(f, kb_id):
    """Explicit controlled synthetic fixture, never inferred from creation."""
    scope = {"knowledge_base_id": kb_id, "document_id": None, "level": "test-level",
        "category": "test-category", "need_class": "test-need"}
    approval = BusinessApproval(applicant=f.manager.id, recorded_by=f.manager.id,
        scope=scope, operations=["knowledge.manage"])
    f.db.add(approval); f.db.flush(); seed_verified_approval(f.db, approval)
    f.db.add(AuthorizationGrant(subject_user_id=f.manager.id, knowledge_base_id=kb_id,
        operation="knowledge.manage", level="test-level", category="test-category", need_class="test-need",
        approval_id=approval.id, granted_by=f.manager.id, valid_until=approval.valid_until))
    f.db.commit()


def _open_lifecycle(f, document_id):
    page = f.page
    page.get_by_role("button", name="生命周期", exact=True).click()
    page.get_by_role("button", name="加载 / 刷新列表", exact=True).click()
    f.db.expire_all()
    title = f.db.get(Document, document_id).title
    page.get_by_role("button", name=title + " · 在用", exact=True).click()
    page.get_by_role("heading", name=title + " · 在用", exact=True).wait_for()


def test_lifecycle_manager_registration_new_pdf_and_explicit_hold_resolution(lifecycle_workspace):
    from playwright.sync_api import expect
    f, page, db = lifecycle_workspace, lifecycle_workspace.page, lifecycle_workspace.db
    _login(f, "manage", f.manager, "manager password")
    grants = db.scalar(select(func.count()).select_from(AuthorizationGrant))
    name = "SYNTHETIC registered KB " + uuid4().hex[:8]
    page.get_by_text("新建知识库 / 我的创建登记", exact=True).click()
    page.get_by_label("知识库名称", exact=True).fill(name)
    page.get_by_role("button", name="登记新知识库", exact=True).click()
    page.get_by_text("知识库已登记：" + name, exact=False).wait_for()
    db.expire_all(); kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
    assert kb and db.scalar(select(func.count()).select_from(AuthorizationGrant)) == grants
    expect(page.locator(".locator").filter(has_text=kb.id)).to_contain_text("待配置管理授权")
    assert _select(page, "知识库").locator(f'option[value="{kb.id}"]').count() == 0
    assert f.context.request.get(f.address + f"knowledge-bases/{kb.id}/document-creation-options").status == 404
    registrations = f.context.request.get(f.address + "admin/knowledge-bases/registrations").json()["items"]
    assert {r["id"] for r in registrations} == {kb.id}
    _screenshot(f, "manager-created-kb-awaiting-separate-authorization")
    f.checks.append("manager KB registration has own metadata receipt, no new grants or automatic upload scope")
    _scope_new_kb(f, kb.id)
    f.checks.append("subsequent upload scope established only by explicit SYNTHETIC controlled fixture approval")
    page.get_by_role("button", name="退出", exact=True).click()
    _login(f, "manage", f.manager, "manager password")
    _select(page, "知识库").select_option(kb.id)
    page.get_by_text("上传新文档 PDF", exact=True).click()
    upload = page.locator("details").filter(has=page.get_by_text("上传新文档 PDF", exact=True))
    upload.get_by_label("文档名称", exact=True).fill("SYNTHETIC new lifecycle manual")
    upload.get_by_label("适用型号（逗号分隔，未知留空）", exact=True).fill("DEMO-1")
    upload.get_by_label("版本标识（未知留空）", exact=True).fill("SYNTHETIC-LIFECYCLE-1")
    upload.get_by_label("来源渠道", exact=True).fill("synthetic-controlled-local")
    description = "SYNTHETIC generated PDF; local lifecycle Chrome verification only"
    upload.get_by_label("来源说明", exact=True).fill(description)
    upload.get_by_label("可追溯编号（可选）", exact=True).fill("synthetic://lifecycle/chrome/new-document")
    upload.get_by_label("获准处理的 PDF", exact=True).set_input_files({"name": "SYNTHETIC-LIFECYCLE.pdf",
        "mimeType": "application/pdf", "buffer": synthetic_pdf()})
    with page.expect_response(lambda response: response.request.method == "POST" and response.url.endswith(f"/knowledge-bases/{kb.id}/documents")) as uploaded:
        upload.get_by_role("button", name="保存新文档草稿", exact=True).click()
    assert uploaded.value.status == 201, uploaded.value.text()
    identity = uploaded.value.json(); doc_id, version_id = identity["document"]["id"], identity["version"]["id"]
    page.get_by_text("新文档草稿已保存：", exact=False).wait_for()
    assert _select(page, "文档").input_value() == doc_id and _select(page, "版本").input_value() == version_id
    db.expire_all(); document, version = db.get(Document, doc_id), db.get(DocumentVersion, version_id)
    original = db.get(FileObject, version.source_file_id)
    assert document.category is None and document.access_level is None and document.need_class is None
    assert version.publish_status == "draft" and version.applicability_status == "pending"
    assert version.source_metadata["source_description"] == description
    assert version.source_metadata["traceable_ref"] == "synthetic://lifecycle/chrome/new-document"
    assert original.sha256 == hashlib.sha256(synthetic_pdf()).hexdigest()
    (f.out / "input-source-binding.json").write_text(json.dumps({"scope": "SYNTHETIC ONLY",
        "knowledge_base_id": kb.id, "document_id": doc_id, "version_id": version_id,
        "source_sha256": original.sha256, "metadata": version.source_metadata}, indent=2), encoding="utf-8")
    assert f.client.get(f"/documents/{doc_id}/versions/{version_id}/file").status_code == 404
    page.get_by_role("button", name="业务依据", exact=True).click()
    expect(page.locator(".source-form").get_by_label("来源说明", exact=True)).to_have_value(description)
    expect(page.locator("body")).to_contain_text("本版本尚无业务确认登记")
    page.get_by_role("button", name="发布检查", exact=True).click()
    page.get_by_role("button", name="检查发布条件", exact=True).click()
    page.get_by_role("heading", name="仍有阻断项", exact=True).wait_for()
    assert page.get_by_role("button", name="按此预检发布", exact=True).count() == 0
    _screenshot(f, "manager-new-draft-source-and-publication-blocked")
    page.get_by_role("button", name="原文核验", exact=True).click()
    page.get_by_role("button", name="请求本地解析此版本", exact=True).click()
    page.get_by_text("解析请求回执：", exact=False).wait_for()
    expect(page.locator(".blocks")).to_contain_text(STARTUP)
    expect(page.locator(".blocks")).to_contain_text(WARNING)
    _open_lifecycle(f, doc_id)
    for kind in ("quality", "conflict"):
        _select(page, "保护性暂停类型").select_option(kind)
        page.get_by_label("暂停原因", exact=True).fill("SYNTHETIC " + kind + " requires current resolution")
        page.get_by_role("button", name="登记保护性暂停", exact=True).click()
        page.get_by_text("尚无真实可用消解依据，不能解除暂停。", exact=True).first.wait_for()
        expect(page.get_by_role("button", name="登记解除暂停", exact=True)).to_have_count(1 if kind == "quality" else 2)
    for button in page.get_by_role("button", name="登记解除暂停", exact=True).all():
        expect(button).to_be_disabled()
    db.expire_all(); holds = db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == version_id)).all()
    assert len(holds) == 2
    for hold in holds:
        refused = _request(f, "POST", f"/documents/{doc_id}/versions/{version_id}/holds/{hold.id}/release",
            {"reason": "SYNTHETIC invalid resolution", "resolution_refs": ["not-an-existing-record"]}, f'"{hold.revision}"')
        assert refused.status == 409, refused.text()
    _screenshot(f, "manager-holds-with-no-current-resolution")
    # Create current controlled synthetic facts after both holds. Their references
    # are obtained from the real lifecycle projection and explicitly selected.
    db.expire_all(); version = db.get(DocumentVersion, version_id)
    run = db.scalar(select(ParseRun).where(ParseRun.document_version_id == version_id))
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)).all()
    for block in blocks:
        g.review_content_block(db, f.manager, run, block, "approved", "SYNTHETIC source checked")
    binding = g.source_binding(db, version)
    seed_confirmation(db, f.manager, version, "classification", "classified", {**binding,
        "access_level": "test-level", "category": "test-category", "need_class": "test-need", "rule_version": "synthetic-test-v1"})
    binding = g.source_binding(db, version)
    applicability = seed_confirmation(db, f.manager, version, "applicability", "applicable", binding)
    professional = seed_confirmation(db, f.manager, version, "key_content", "accepted", {**binding,
        "parse_run_id": run.id, "block_bindings": {b.id: g.block_binding(b) for b in blocks}})
    chunk = db.scalar(select(ChunkSet).where(ChunkSet.parse_run_id == run.id))
    review = g.create_review_set(db, f.manager, version, {"parse_run_id": run.id, "chunk_set_id": chunk.id,
        "reviewed_block_refs": [{"id": b.id, "revision": b.revision, "content_hash": g.block_binding(b)} for b in blocks],
        "professional_confirmation_refs": [professional.id], "reason": "SYNTHETIC complete exact source coverage"})
    db.commit()
    page.get_by_role("button", name="加载 / 刷新列表", exact=True).click()
    page.get_by_role("button", name=document.title + " · 在用", exact=True).click()
    for kind, reference in (("quality", review.id), ("conflict", applicability.id)):
        card = page.locator("div.block").filter(has_text="SYNTHETIC " + kind + " requires current resolution")
        _select(card, "当前可用消解依据").select_option(reference)
        card.get_by_label("解除说明", exact=True).fill("SYNTHETIC explicit current source resolution")
        card.get_by_role("button", name="登记解除暂停", exact=True).click()
        expect(card).to_contain_text("已解除")
    db.expire_all(); assert db.get(DocumentVersion, version_id).publish_status == "draft"
    assert all(h.released_at for h in db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == version_id)))
    assert f.client.get(f"/documents/{doc_id}/versions/{version_id}/file").status_code == 404
    history = f.context.request.get(f.address + f"documents/{doc_id}/lifecycle").json()["history"]
    assert all("input" not in row and "result" not in row and "query" not in row for row in history)
    _screenshot(f, "manager-explicit-hold-release-still-draft")
    f.checks.extend(["real multipart new document exact IDs, source hash and unknown classification; employee file refused",
        "new PDF flows into existing source, parse and blocked publication UI with complete synthetic warning",
        "quality/conflict holds block with no current materials and server rejects invented resolution refs",
        "explicit current controlled synthetic ReviewSet/applicability refs release holds; draft remains unpublished"])


def test_lifecycle_admin_registration_and_employee_refusal(lifecycle_workspace):
    from playwright.sync_api import expect
    f, page, db = lifecycle_workspace, lifecycle_workspace.page, lifecycle_workspace.db
    _login(f, "access-review", f.administrator, "synthetic admin password")
    grants = db.scalar(select(func.count()).select_from(AuthorizationGrant))
    page.get_by_text("新建知识库 / 我的创建登记", exact=True).click()
    name = "SYNTHETIC system registered " + uuid4().hex[:8]
    page.get_by_label("知识库名称", exact=True).fill(name)
    page.get_by_role("button", name="登记新知识库", exact=True).click()
    page.get_by_text("知识库已登记：" + name, exact=False).wait_for()
    db.expire_all(); kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == name))
    assert kb and db.scalar(select(func.count()).select_from(AuthorizationGrant)) == grants
    expect(page.locator(".locator").filter(has_text=kb.id)).to_contain_text("待配置管理授权")
    assert f.context.request.get(f.address + "knowledge-bases").json()["items"] == []
    _screenshot(f, "system-admin-registration-with-no-data-authority")
    page.get_by_role("button", name="退出", exact=True).click()
    _login(f, "manage", f.employee, "synthetic employee password")
    expect(page.get_by_role("alert")).to_contain_text("当前账号没有进入本工作区所需的角色")
    denied = _request(f, "POST", "/admin/knowledge-bases", {"name": "SYNTHETIC denied employee KB"})
    assert denied.status == 403
    assert f.context.request.get(f.address + "admin/knowledge-bases/registrations").status == 403
    assert f.context.request.get(f.address + f"documents/{f.document.id}/lifecycle").status == 404
    assert f.context.request.get(f.address + f"knowledge-bases/{f.kb.id}/document-lifecycle?state=deleted").status == 404
    _screenshot(f, "employee-management-and-metadata-refused")
    f.checks.extend(["system admin registration receipt creates no data grant and exposes no managed knowledge bases",
        "ordinary employee cannot enter management, create KB, read registrations or lifecycle metadata"])


def test_lifecycle_delete_unknown_response_metadata_retention_and_revocation(lifecycle_workspace):
    from playwright.sync_api import expect
    f, page, db = lifecycle_workspace, lifecycle_workspace.page, lifecycle_workspace.db
    task_id = submit(f.client, f.kb)
    assert run_one(f.config, gateway_url=f.address, task_id=task_id)["state"] == "succeeded"
    assert f.client.get(f"/tasks/{task_id}/result").status_code == 200
    _login(f, "manage", f.manager, "manager password")
    _open_lifecycle(f, f.document.id)
    body = page.locator("body")
    expect(body).not_to_contain_text(task_id)
    page.get_by_role("button", name="查看删除影响", exact=True).click()
    page.get_by_text("涉及 2 个版本", exact=False).wait_for()
    expect(page.get_by_label("删除原因", exact=True)).to_have_attribute("required", "")
    expect(page.get_by_label("我已核对影响，确认逻辑删除此文档", exact=True)).to_have_attribute("required", "")
    _screenshot(f, "manager-delete-impact-reason-and-confirmation")
    db.expire_all(); revision_before = db.get(KnowledgeBase, f.kb.id).content_revision
    originals = [db.get(FileObject, version.source_file_id) for version in db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == f.document.id))]
    before = {file.id: hashlib.sha256((f.config.storage_dir / file.object_key).read_bytes()).hexdigest() for file in originals}
    keys = []
    page.on("request", lambda request: keys.append(request.headers.get("idempotency-key"))
        if request.method == "DELETE" and request.url.endswith(f"/documents/{f.document.id}") else None)
    def lose_response(route):
        response = route.fetch()
        assert response.status == 204
        route.abort("failed")
    page.route("**/documents/" + f.document.id, lose_response, times=1)
    page.get_by_label("删除原因", exact=True).fill("SYNTHETIC controlled logical deletion")
    page.get_by_label("我已核对影响，确认逻辑删除此文档", exact=True).check()
    page.get_by_role("button", name="确认逻辑删除", exact=True).click()
    page.get_by_text("上次操作结果未知。", exact=False).wait_for()
    db.expire_all(); assert db.get(Document, f.document.id).deleted_at
    page.reload(); page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="恢复原操作回执", exact=True).click()
    page.get_by_text("原操作回执已读取，请核对当前状态。", exact=True).wait_for()
    assert len(keys) == 2 and keys[0] and keys[0] == keys[1]
    db.expire_all(); assert db.get(KnowledgeBase, f.kb.id).content_revision == revision_before + 1
    assert db.scalar(select(func.count()).select_from(AuditEvent).where(AuditEvent.action == "document.deleted", AuditEvent.resource_id == f.document.id)) == 1
    assert db.scalar(select(func.count()).select_from(ApiIdempotencyRecord).where(ApiIdempotencyRecord.actor_user_id == f.manager.id,
        ApiIdempotencyRecord.idempotency_key == keys[0], ApiIdempotencyRecord.status == "completed")) == 1
    page.get_by_role("button", name="生命周期", exact=True).click()
    _select(page, "文档范围").select_option("deleted")
    page.get_by_role("button", name=f.document.title + " · 已逻辑删除", exact=True).click()
    page.get_by_role("heading", name=f.document.title + " · 已逻辑删除", exact=True).wait_for()
    expect(body).to_contain_text("原件按策略保留")
    assert page.get_by_role("button", name="登记保护性暂停", exact=True).count() == 0
    assert page.get_by_role("button", name="确认逻辑删除", exact=True).count() == 0
    assert "永久清理" in body.inner_text()
    for file in originals:
        assert db.get(FileObject, file.id).status == "available"
        assert hashlib.sha256((f.config.storage_dir / file.object_key).read_bytes()).hexdigest() == before[file.id]
    for client in (f.context.request, f.client):
        if client is f.client:
            result = client.get(f"/tasks/{task_id}/result")
            assert result.status_code == 403
            assert client.get(f"/documents/{f.document.id}/versions/{f.version.id}/file").status_code == 404
        else:
            assert client.get(f.address + f"documents/{f.document.id}/versions/{f.version.id}/file").status == 404
    assert f.client.get(f"/documents/{f.document.id}/lifecycle").status_code == 404
    detail = f.context.request.get(f.address + f"documents/{f.document.id}/lifecycle")
    assert detail.status == 200 and detail.json()["purge_allowed"] is False
    assert all(v["original"]["status"] == "available" for v in detail.json()["versions"])
    refused = _request(f, "POST", f"/documents/{f.document.id}/versions/{f.version.id}/holds",
        {"kind": "quality", "reason": "SYNTHETIC deleted data cannot be written"}, f'"{f.version.revision}"')
    assert refused.status in {403, 404}
    _screenshot(f, "manager-recycle-metadata-originals-retained")
    page.get_by_role("button", name="原文核验", exact=True).click()
    assert page.get_by_role("link", name="打开此版本原文 PDF", exact=True).count() == 0
    expect(body).not_to_contain_text(STARTUP)
    expect(body).not_to_contain_text(WARNING)
    page.reload(); page.wait_for_load_state("networkidle")
    page.get_by_role("button", name="生命周期", exact=True).click()
    _select(page, "文档范围").select_option("deleted")
    page.get_by_role("button", name=f.document.title + " · 已逻辑删除", exact=True).click()
    page.get_by_role("heading", name=f.document.title + " · 已逻辑删除", exact=True).wait_for()
    db.expire_all(); grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.manager.id,
        AuthorizationGrant.operation == "knowledge.manage"))
    grant.revoked_at = utc_now(); db.commit()
    page.get_by_role("button", name=f.document.title + " · 已逻辑删除", exact=True).click()
    page.get_by_role("heading", name="登录工作区", exact=True).wait_for()
    expect(body).not_to_contain_text(f.document.title)
    assert page.get_by_role("link", name="打开此版本原文 PDF", exact=True).count() == 0
    _screenshot(f, "manager-revocation-clears-deleted-metadata")
    f.checks.extend(["real synthetic employee query result exists before deletion; governance projection contains no employee task",
        "deletion requires impact, reason and explicit checkbox; committed response loss recovers same key after reload",
        "exactly one deletion event, one idempotency receipt and one KB content increment",
        "all file bytes retained with status available; employee saved result and both roles file access blocked",
        "recycle metadata available only to manager; deleted holds rejected, no old review content or links after tab/refresh",
        "current management revocation clears recycle metadata and links"])


