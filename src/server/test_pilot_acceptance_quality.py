"""Scenario E on built React, installed Chrome, and disposable PostgreSQL."""
from dataclasses import replace
from datetime import timedelta
import json
import os
from uuid import uuid4
from zipfile import ZipFile

import pytest
from sqlalchemy import select

from . import governance as g
from .chunk_jobs import run_chunk_job
from .governance_models import ReviewSet
from .models import AuthorizationGrant, ChunkSet, ContentBlock, Document, DocumentVersion, KnowledgeBase, ParseArtifact, ParseRun, User, utc_now
from .parse_jobs import run_local_parse_job, run_parse_job
from .parsing.artifacts import read_artifact
from .parsing.local_pipeline import source_preflight
from .parsing.processing import source_confirmation_scope, source_verification
from .security import hash_password
from .services import save_upload
from .test_chunk_processing import TOKENIZER
from .test_local_parse_pipeline import PASSWORD, accept, local_parse
from .test_pdf_workspace_browser import pdf_browser, pdf_pg
from .test_pilot_acceptance_quality_support import NATIVE_ZIP_SHA, OLD_EVIDENCE, QUALITY_ROOT, digest, write_json
from .test_workspace_browser import _csrf, _login, _screenshot, _select
from .testing_governance import _authority, _proof, seed_confirmation, seed_verified_approval


WARNING = "Warning for step 1: Keep the training shield closed."
FOOTNOTE = "Table 1 note: Apply these limits only in demo mode."
TABLE = [["Check", "Training value", "Condition"], ["Pressure", "5 Pa", "Demo mode"],
         ["Hold time", "8 s", "Indicator green"]]


def _inputs():
    native = OLD_EVIDENCE / "attempt-01"
    prior = json.loads((OLD_EVIDENCE / "replay-01/technical-bundle-manifest.json").read_bytes())
    cases = {"native": (native / "SYNTHETIC-two-pages.pdf", native / "result.zip", prior["source_sha256"], NATIVE_ZIP_SHA)}
    for mode in ("mixed", "scan", "scan-positive"):
        folder = QUALITY_ROOT / "native" / mode
        assert (folder / "capture.json").is_file(), f"Capture the actual offline Standard package for {mode} first"
        receipt = json.loads((folder / "capture.json").read_bytes())
        assert receipt["state"] == "passed" and receipt["producer"]["version"] == "4.2.3"
        cases[mode] = (folder / f"SYNTHETIC-E-{mode.upper()}.pdf", folder / "result.zip",
                       receipt["source_sha256"], receipt["bundle_sha256"])
    for path, archive, source_sha, bundle_sha in cases.values():
        assert digest(path) == source_sha and digest(archive) == bundle_sha
    return cases


def _manager(f):
    with f.app.state.session_factory() as db:
        user = User(username=f"synthetic-quality-{uuid4().hex}", display_name="SYNTHETIC quality manager",
                    status="active", roles=["knowledge_manager"], credential_state="active", credential_hash=hash_password(PASSWORD))
        kb = KnowledgeBase(name="SYNTHETIC E quality acceptance")
        db.add_all((user, kb))
        db.flush()
        dims = {"level": "internal", "category": "manual", "need_class": "maintenance"}
        op = "knowledge.manage"
        scope = {**dims, "knowledge_base_id": kb.id, "document_id": None}
        approval = g.register_approval(db, user, {"applicant": user.id, "scope": scope, "operations": [op]})
        seed_verified_approval(db, approval)
        db.add(AuthorizationGrant(subject_user_id=user.id, knowledge_base_id=kb.id, operation=op,
                                  approval_id=approval.id, valid_until=approval.valid_until, **dims))
        db.commit()
        return user, kb.id


def _upload_parse(f, kb, mode, evidence):
    from playwright.sync_api import expect

    page = f.page
    path, archive, source_sha, bundle_sha = evidence
    creation = page.locator("details").filter(has=page.locator("summary").filter(has_text="上传新文档 PDF"))
    if creation.get_attribute("open") is None:
        creation.locator("summary").click()
    creation.get_by_label("文档名称", exact=True).fill(f"SYNTHETIC E {mode}")
    creation.get_by_label("业务分类（未知留空）", exact=True).fill("manual")
    creation.get_by_label("适用型号（逗号分隔，未知留空）", exact=True).fill(f"SYNTHETIC-E-{mode.upper()}")
    creation.get_by_label("版本标识（未知留空）", exact=True).fill(f"SYNTHETIC-E-{mode.upper()}-R1")
    creation.get_by_label("来源渠道", exact=True).fill("synthetic acceptance")
    creation.get_by_label("来源说明", exact=True).fill("Locally authored training sample; no enterprise material")
    creation.get_by_label("获准处理的 PDF", exact=True).set_input_files(path)
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/knowledge-bases/{kb}/documents")) as upload:
        creation.get_by_role("button", name="保存新文档草稿", exact=True).click()
    assert upload.value.status == 201, upload.value.text()
    uploaded = upload.value.json()
    version = uploaded["version"]["id"]
    document = uploaded["document"]["id"]
    base = f"documents/{document}/versions/{version}"
    expect(_select(page, "版本")).to_have_value(version)
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(base + "/parse-runs")) as accepted:
        page.get_by_role("button", name="请求本地解析此版本", exact=True).click()
    assert accepted.value.status == 202, accepted.value.text()
    run_id = accepted.value.json()["parse_run_id"]
    expect(page.get_by_text("已收到本地解析请求", exact=False)).to_be_visible()
    bundle = archive.read_bytes()

    def replay(actual_source, workspace, _executable, _home, heartbeat):
        assert digest(actual_source) == source_sha
        heartbeat()
        (workspace / "result.zip").write_bytes(bundle)

    with f.app.state.session_factory() as db:
        assert db.get(ParseRun, run_id).status == "pending"
        raw = run_local_parse_job(db, run_id, f.config, runner=replay)
        assert raw.stage_checkpoint["stage_state"] == "parsed", raw.error_code
        run = run_parse_job(db, run_id, f.config, {})
        assert run.stage_checkpoint["stage_state"] in {"qc_passed", "qc_needs_review"}, run.error_code
        assert _artifact(db, f, run, "raw_archive") == bundle
        assert run.engine_version == "4.2.3" and run.actual_profile["tier"] == "standard"
        assert db.get(DocumentVersion, version).selected_chunk_set_id is None
    _refresh(page)
    response = f.context.request.get(f.address + base + "/file")
    assert response.status == 200 and response.body() == path.read_bytes()
    qc = _blocks(f, run_id)["source_qc"]
    f.checks.append({"sample": mode, "source_sha256": source_sha, "real_saved_zip_sha256": bundle_sha,
                     "version_id": version, "run_id": run_id, "source_qc_status": qc["status"],
                     "source_qc_issue_codes": sorted({item["code"] for item in qc["issues"]})})
    return run_id, version, base


def _artifact(db, f, run, kind):
    artifact = db.scalar(select(ParseArtifact).where(ParseArtifact.parse_run_id == run.id, ParseArtifact.kind == kind))
    return read_artifact(db, artifact, f.config)


def _blocks(f, run_id):
    response = f.context.request.get(f.address + f"parse-runs/{run_id}/blocks")
    assert response.status == 200
    return response.json()


def _refresh(page):
    page.get_by_role("button", name="刷新解析批次", exact=True).click()
    page.get_by_text("已读取最新批次状态。", exact=True).wait_for()


def _crop(page, article):
    import pymupdf

    with page.expect_response(lambda r: "/source-image?region=0" in r.url) as crop:
        article.locator("summary").filter(has_text="核对原页区域").first.click()
    assert crop.value.status == 200 and crop.value.headers.get("cache-control") == "private, no-store"
    assert min(pymupdf.Pixmap(crop.value.body()).samples) < 120
    article.locator("img").first.wait_for()


def _chunk(f, run_id):
    page = f.page
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/parse-runs/{run_id}/chunk-sets")) as accepted:
        page.get_by_role("button", name="请求生成切分集合", exact=True).click()
    assert accepted.value.status == 202, accepted.value.text()
    set_id = accepted.value.json()["id"]
    with f.app.state.session_factory() as db:
        assert db.get(ChunkSet, set_id).status == "building"
        result = run_chunk_job(db, set_id, f.config)
        status, quality = result.status, result.stage_checkpoint["quality"]
    _refresh(page)
    return set_id, status, quality


def _assert_publication_blocked(f, base):
    from playwright.sync_api import expect

    page = f.page
    page.get_by_role("button", name="发布检查", exact=True).click()
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(base + "/publication-preview")) as result:
        page.get_by_role("button", name="检查发布条件", exact=True).click()
    preview = result.value.json()
    assert result.value.ok and preview["allowed"] is False
    expect(page.get_by_role("heading", name="仍有阻断项", exact=True)).to_be_visible()
    expect(page.get_by_role("button", name="按此预检发布", exact=True)).to_have_count(0)
    body = f.context.request.post(f.address + base + "/publish", data={"preview_id": preview["preview_id"]},
        headers={"X-CSRF-Token": _csrf(f), "Idempotency-Key": str(uuid4()), "If-Match": preview["document_etag"]})
    assert body.status == 409, body.text()
    return preview["blocked_reasons"], body.json()["code"]


def test_pdf_metadata_label_does_not_attest_conflicting_printed_footer(tmp_path):
    pymupdf = pytest.importorskip("pymupdf")
    source = tmp_path / "SYNTHETIC-conflicting-page-label.pdf"
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((250, 795), "PRINT-37")
        pdf.set_page_labels([{"startpage": 0, "prefix": "META-", "style": "D", "firstpagenum": 8}])
        pdf.save(source)
    page, = source_preflight(source, digest(source), source.stat().st_size)["pages"]
    assert page["file_page"] == 1 and page["native_text"] == "PRINT-37"
    assert page["printed_page"] is None and page["printed_page_basis"] == {"kind": "pdf_page_labels", "label": "META-8"}


@pytest.mark.skipif(os.environ.get("DATACENTER_RUN_PILOT_E") != "1", reason="Requires the archived pilot E samples")
@pytest.mark.parametrize("mode", ["mixed", "scan"])
def test_pilot_e_rejects_review_candidate_at_the_specific_quality_gate(local_parse, mode):
    f = local_parse
    path, archive, source_sha, bundle_sha = _inputs()[mode]
    bundle = archive.read_bytes()
    with f.app.state.session_factory() as db:
        actor, version = db.get(User, f.actor_id), db.get(DocumentVersion, f.version_id)
        original = save_upload([path.read_bytes()], path.name, "application/pdf", actor, f.config)
        db.add(original)
        db.flush()
        version.source_file_id = original.id
        db.commit()

    def replay(actual_source, workspace, _executable, _home, heartbeat):
        assert digest(actual_source) == source_sha
        heartbeat()
        (workspace / "result.zip").write_bytes(bundle)

    run_id = accept(f)
    with f.app.state.session_factory() as db:
        run_local_parse_job(db, run_id, f.config, runner=replay)
        run = run_parse_job(db, run_id, f.config, {})
        assert _artifact(db, f, run, "raw_archive") == bundle
    if mode == "scan":
        blocks = f.client.get(f"/parse-runs/{run_id}/blocks").json()["items"]
        warning = next(row for row in blocks if row["text"] == WARNING)
        response = f.client.post(f"/parse-runs/{run_id}/corrections", json={"blocks": [{"block_id": warning["id"],
            "kind": "warning", "reason": "SYNTHETIC bold warning was parsed as body text",
            "basis": {"file_page": 1, "description": "SYNTHETIC original bold warning compared to scan"}}]},
            headers={"Idempotency-Key": str(uuid4())})
        assert response.status_code == 202, response.text
        run_id = response.json()["parse_run_id"]
        with f.app.state.session_factory() as db:
            run = run_parse_job(db, run_id, f.config, {})
            assert run.status == "needs_review" and _artifact(db, f, run, "raw_archive") == bundle

    base = f"/documents/{f.document_id}/versions/{f.version_id}"
    with f.app.state.session_factory() as db:
        actor, version, run = db.get(User, f.actor_id), db.get(DocumentVersion, f.version_id), db.get(ParseRun, run_id)
        doc = db.get(Document, f.document_id)
        binding = g.source_binding(db, version)
        seed_confirmation(db, actor, version, "applicability", "applicable", binding)
        seed_confirmation(db, actor, version, "classification", "classified", {**binding,
            "access_level": doc.access_level, "category": doc.category, "need_class": doc.need_class, "rule_version": "SYNTHETIC E"})
        leaves = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id, ContentBlock.structure_role == "leaf")).all()
        for block in leaves:
            g.review_content_block(db, actor, run, block, "approved", "SYNTHETIC individual leaf checked")
        scope = {**binding, **source_confirmation_scope(db, run)}
        scope["observation_dispositions"] = {ref: {"decision": "covered_by", "block_refs": list(scope["block_bindings"]),
            "reason": "SYNTHETIC comparison of all extracted lines and source regions"} for ref in scope["observation_bindings"]}
        if mode == "scan":
            professional = seed_confirmation(db, actor, version, "key_content", "accepted", scope)
            assert source_verification(db, run)["state"] == "verified"
            proof_ids = [professional.id]
        else:
            authority, identity = _authority(db, purpose="key_content", scope={key: binding[key] for key in ("version_id", "source_sha256", "scope_hash")})
            professional = g.register_confirmation(db, actor, version, {"kind": "key_content", "scope": scope,
                "conclusion": "accepted", "actual_confirmer": identity, "confirmed_at": utc_now() - timedelta(seconds=1)})
            proof = _proof(db, actor, "version", version.id, g._confirmation_assertion(professional))
            professional.proof_refs = [proof.id]
            db.flush()
            assert g._proof_dependencies(db, "version", version.id, professional.proof_refs,
                g._confirmation_assertion(professional), authority.id, utc_now())
            proof_ids = [professional.id]
            verification_request = {"decision": "verified", "reason": "SYNTHETIC controlled witness", "checked_proof_refs": [proof.id],
                                    "authority_basis_ref": authority.id}
            verification_revision = professional.revision
            assert source_verification(db, run)["reasons"] == ["SOURCE_QC_NOT_CURRENT"]
        reviewed = [{"id": block.id, "revision": block.revision, "content_hash": g.block_binding(block)} for block in leaves]
        assert reviewed and all(block.review_status == "approved" for block in leaves)
        version_revision = version.revision
        db.commit()

    evidence = {"sample": mode, "source_sha256": source_sha, "real_saved_zip_sha256": bundle_sha}
    if mode == "mixed":
        refused = f.client.post(base + f"/business-confirmations/{proof_ids[0]}/verification", json=verification_request,
            headers={"Idempotency-Key": str(uuid4()), "If-Match": f'"{verification_revision}"'})
        assert refused.status_code == 409 and refused.json()["code"] == "PROOF_UNVERIFIABLE", refused.text
        evidence["source_professional_verification"] = {"http": 409, "code": refused.json()["code"]}
    built = f.client.post(f"/parse-runs/{run_id}/chunk-sets", json={"splitter_profile_revision_id": "dc.semantic-bge-m3/1"},
        headers={"Idempotency-Key": str(uuid4()), "If-Match": f'"{version_revision}"'})
    assert built.status_code == 202, built.text
    set_id = built.json()["id"]
    with f.app.state.session_factory() as db:
        chunk = run_chunk_job(db, set_id, replace(f.config, local_bge_tokenizer=TOKENIZER.resolve()))
        assert chunk.status == "ready" and chunk.stage_checkpoint["quality"]["status"] == "needs_review"
        collection_codes = {item["code"] for item in chunk.stage_checkpoint["quality"]["issues"]}
        assert ("CHUNK_TABLE_HEADER_UNRESOLVED" if mode == "mixed" else "CHUNK_LEAF_ROLE_UNRESOLVED") in collection_codes
        version = db.get(DocumentVersion, f.version_id)
        version_revision = version.revision
    current = f.client.get(f"/parse-runs/{run_id}/blocks").json()
    source_codes = {item["code"] for item in current["source_qc"]["issues"]}
    if mode == "mixed":
        assert current["source_verification"]["reasons"] == ["SOURCE_QC_NOT_CURRENT"] and "QC_TABLE_HEADER_RELATION_MISSING" in source_codes
    else:
        assert current["source_verification"]["state"] == "verified" and current["source_qc"]["current"] is True
    candidate = f.client.post(base + "/review-sets", json={"parse_run_id": run_id, "chunk_set_id": set_id,
        "reviewed_block_refs": reviewed, "professional_confirmation_refs": proof_ids, "reason": "SYNTHETIC QC gate"},
        headers={"Idempotency-Key": str(uuid4()), "If-Match": f'"{version_revision}"'})
    assert candidate.status_code == 409 and candidate.json()["code"] == "INVALID_REVIEW_BATCH", candidate.text
    preview = f.client.post(base + "/publication-preview", json={"action": "publish"}, headers={"Idempotency-Key": str(uuid4())})
    assert preview.status_code == 200 and preview.json()["blocked_reasons"] == ["REVIEW_COVERAGE_INCOMPLETE"], preview.text
    with f.app.state.session_factory() as db:
        assert db.get(DocumentVersion, f.version_id).selected_review_set_id is None
        assert db.scalar(select(ReviewSet.id).where(ReviewSet.parse_run_id == run_id)) is None
    write_json(QUALITY_ROOT / f"qc-gate-{mode}.json", {**evidence, "source_qc": current["source_qc"]["status"],
        "source_verification": current["source_verification"]["state"], "source_issues": sorted(source_codes),
        "collection_qc": "needs_review", "collection_issues": sorted(collection_codes),
        "candidate_registration": {"http": candidate.status_code, "code": candidate.json()["code"]},
        "publication_preview_blockers": preview.json()["blocked_reasons"],
        "scope": "Production routes on disposable SQLite with explicit synthetic applicability, classification and authority fixtures"})


@pytest.mark.skipif(not (os.environ.get("DATACENTER_TEST_POSTGRES_URL") and os.environ.get("DATACENTER_RUN_PILOT_E") == "1"),
                    reason="Requires the archived pilot E samples and an explicit disposable local PostgreSQL URL")
def test_pilot_e_actual_mineru_quality_and_manager_correction(pdf_browser):
    from playwright.sync_api import expect

    assert TOKENIZER.is_file(), "Use the installed offline tokenizer; never fetch a fallback"
    f, page = pdf_browser, pdf_browser.page
    cases = _inputs()
    actor, kb = _manager(f)
    _login(f, "manage", actor, PASSWORD)
    expect(_select(page, "知识库")).to_have_value(kb)

    native, _, _ = _upload_parse(f, kb, "native", cases["native"])
    expect(page.locator("blockquote").filter(has_text="Pressure: 5 Pa. Disconnect power before maintenance.")).to_be_visible()
    expect(page.get_by_text("印刷页码待核对", exact=False).first).to_be_visible()
    native_pages = f.context.request.get(f.address + f"parse-runs/{native}/pages").json()["items"]
    assert [(x["file_page"], x["state"], x["printed_page"], x["printed_page_basis"]) for x in native_pages] == [
        (1, "processed", None, None), (2, "blank", None, None)]
    page.get_by_role("navigation", name="原页清单").get_by_role("button", name="第 2 页 有据空白").click()
    expect(page.get_by_text("此页有空白判定依据", exact=False)).to_be_visible()
    assert page.get_by_role("link", name="打开原文第 2 页", exact=True).get_attribute("href").endswith("#page=2")
    _screenshot(f, "01-native-blank-and-original")

    mixed, mixed_version, mixed_base = _upload_parse(f, kb, "mixed", cases["mixed"])
    mixed_data = _blocks(f, mixed)
    assert mixed_data["source_qc"]["status"] == "needs_review"
    assert mixed_data["source_verification"]["state"] == "pending"
    assert "QC_TABLE_HEADER_RELATION_MISSING" in {item["code"] for item in mixed_data["source_qc"]["issues"]}
    table = next(item for item in mixed_data["items"] if item["kind"] == "table")
    assert [[cell["text"] for cell in row] for row in table["structured_content"]["rows"]] == TABLE
    assert not any(cell["header"] for row in table["structured_content"]["rows"] for cell in row)
    with ZipFile(cases["mixed"][1]) as original_zip:
        assert b"<td>Check</td>" in original_zip.read("middle_json.json")
    for value in (FOOTNOTE, WARNING, "Figure 1. Synthetic indicator panel"):
        expect(page.locator("blockquote").filter(has_text=value)).to_be_visible()
    expect(page.get_by_text("PDF 页标签 M-12 · 印刷页码待核对", exact=False)).to_be_visible()
    original_page = f.context.request.get(f.address + f"parse-runs/{mixed}/pages").json()["items"][0]
    assert original_page["file_page"] == 1 and original_page["page_class"] == "mixed"
    assert original_page["printed_page"] is None and original_page["printed_page_basis"] == {"kind": "pdf_page_labels", "label": "M-12"}
    assert any(block["kind"] == "page_number" and block["text"] == "M-12" for block in mixed_data["items"])
    workbench = page.locator(".pdf-page-content")
    expect(workbench.get_by_text("表格缺少表头适用关系。", exact=False).first).to_be_visible()
    article = page.locator(f'article[data-block-id="{table["id"]}"]')
    assert [row.get_by_role("cell").all_text_contents() for row in article.get_by_role("row").all()] == TABLE
    _crop(page, article)
    image = next(item for item in mixed_data["items"] if item["kind"] == "image")
    image_article = page.locator(f'article[data-block-id="{image["id"]}"]')
    with page.expect_response(lambda r: f"/parse-runs/{mixed}/assets/" in r.url) as archived:
        image_article.get_by_text("核对归档图像 1", exact=True).click()
    assert archived.value.ok and archived.value.headers.get("cache-control") == "private, no-store"
    image_article.locator("img").wait_for()
    article.screenshot(path=str(f.out / "02-table-detail.png"))
    _screenshot(f, "02-mixed-table-and-figure")
    mixed_set, _, mixed_quality = _chunk(f, mixed)
    assert mixed_quality["status"] == "needs_review"
    mixed_blockers, mixed_publish_error = _assert_publication_blocked(f, mixed_base)
    _screenshot(f, "03-mixed-publication-blocked")
    with f.app.state.session_factory() as db:
        version = db.get(DocumentVersion, mixed_version)
        assert version.publish_status == "draft" and version.selected_chunk_set_id is None
    f.checks.append({"sample": "mixed", "original_page": 1, "page_label_metadata": "M-12", "table_rows": TABLE,
                     "table_header_state": "unconfirmed", "figure_and_footnote": "returned with source crops",
                     "chunk_set": mixed_set, "publication_preview_blockers": mixed_blockers,
                     "publication_post_error": mixed_publish_error})

    for mode, label, should_qualify in (("scan", "S-37", False), ("scan-positive", "S-38", True)):
        _review_scan(f, kb, mode, label, cases[mode], actor.id, should_qualify)

    write_json(f.out / "coverage.json", {"scenario": "E", "status": "VERIFIED_LOCAL_SYNTHETIC",
               "browser_actor": "knowledge_manager", "source": "real local MinerU 4.2.3 archives replayed byte-for-byte",
               "literality": "no OCR transcription error in these scans; an existing warning was corrected from paragraph to warning",
               "limitations": ["Mixed table content and bounds are retained; header semantics and figure action are unresolved, so formal publication is blocked",
                               "The corrected negative scan passes source review; a detached note stays unresolved in collection QC",
                               "Generic table semantic editing and enterprise professional acceptance remain outside this run"],
               "checks": f.checks})


def _review_scan(f, kb, mode, label, evidence, actor_id, should_qualify):
    from playwright.sync_api import expect

    page = f.page
    page.get_by_role("button", name="原文核验", exact=True).click()
    scan, scan_version, scan_base = _upload_parse(f, kb, mode, evidence)
    scan_data = _blocks(f, scan)
    scan_pages = f.context.request.get(f.address + f"parse-runs/{scan}/pages").json()["items"]
    assert [(x["file_page"], x["page_class"], x["printed_page"], x["coverage"]["native_text_present"]) for x in scan_pages] == [
        (1, "scan", None, False)]
    assert scan_pages[0]["printed_page_basis"] == {"kind": "pdf_page_labels", "label": label}
    assert any(block["kind"] == "page_number" and block["text"] == label for block in scan_data["items"])
    assert scan_data["source_qc"]["status"] == "passed" and scan_data["source_verification"]["state"] == "pending"
    original_warning = next(item for item in scan_data["items"] if item["text"] == WARNING)
    assert original_warning["kind"] == "paragraph" and original_warning["raw_ref"]["raw_member_name"] == "middle_json.json"
    assert scan_data["source_qc"]["critical_refs"][original_warning["id"]] == ["warning"]
    original_article = page.locator(f'article[data-block-id="{original_warning["id"]}"]')
    _crop(page, original_article)
    expect(page.get_by_text(f"PDF 页标签 {label} · 印刷页码待核对", exact=False)).to_be_visible()
    original_archive = evidence[1].read_bytes()
    with f.app.state.session_factory() as db:
        parent = db.get(ParseRun, scan)
        parent_ir = read_artifact(db, db.get(ParseArtifact, parent.ir_artifact_id), f.config)
    original_article.get_by_text("按原文修正此块", exact=True).click()
    expect(original_article.locator("textarea[name=corrected_text]")).to_have_value(WARNING)
    _select(original_article, "修正内容类型").select_option("warning")
    original_article.get_by_label("修正原因", exact=True).fill("SYNTHETIC bold warning was parsed as body text")
    original_article.get_by_label("原页对照依据", exact=True).fill("SYNTHETIC scanned page 1, bold warning immediately beneath Step 1")
    original_article.screenshot(path=str(f.out / f"04-{mode}-warning-detail.png"))
    _screenshot(f, f"04-{mode}-original-and-proposed-correction")
    with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(f"/parse-runs/{scan}/corrections")) as accepted:
        original_article.get_by_role("button", name="提交修正批次", exact=True).click()
    assert accepted.value.status == 202, accepted.value.text()
    correction = accepted.value.json()["parse_run_id"]
    assert accepted.value.json()["status"] == "pending" and correction != scan
    assert accepted.value.request.post_data_json["blocks"][0]["basis"]["file_page"] == 1
    expect(page.get_by_text("修正已受理。后台生成新批次后，请重新核验。", exact=True)).to_be_visible()
    with f.app.state.session_factory() as db:
        assert db.get(ParseRun, correction).status == "pending"
        assert not db.scalar(select(ParseArtifact.id).where(ParseArtifact.parse_run_id == correction))
        result = run_parse_job(db, correction, f.config, {})
        assert result.status == "needs_review" and result.parent_run_id == scan, result.error_code
        assert result.stage_checkpoint["source_qc"]["status"] == "passed"
        assert _artifact(db, f, result, "raw_archive") == original_archive
        assert read_artifact(db, db.get(ParseArtifact, db.get(ParseRun, scan).ir_artifact_id), f.config) == parent_ir
    _refresh(page)
    revised = _blocks(f, correction)
    warning = next(item for item in revised["items"] if item["text"] == WARNING)
    assert warning["kind"] == "warning" and warning["source_kind"] == "manual"
    assert warning["structured_content"]["manual_correction"]["parent_block_id"] == original_warning["id"]
    original_region, = original_warning["locator"]["regions"]
    corrected_region, = warning["locator"]["regions"]
    assert {key: val for key, val in original_region.items() if key != "raw_geometry"} == {
        key: val for key, val in corrected_region.items() if key != "raw_geometry"}
    original_layout = original_region["raw_geometry"]["layout_raw_ref"]
    corrected_layout = corrected_region["raw_geometry"]["layout_raw_ref"]
    assert original_layout["artifact_id"] == original_warning["raw_ref"]["artifact_id"]
    assert corrected_layout["artifact_id"] == warning["raw_ref"]["artifact_id"] != original_layout["artifact_id"]
    assert original_layout["json_pointer"] == corrected_layout["json_pointer"]
    assert {"warning", "manual_correction"} <= set(revised["source_qc"]["critical_refs"][warning["id"]])
    assert warning["id"] in revised["source_verification"]["missing_critical_refs"]
    expect(page.get_by_text("人工修正内容", exact=False)).to_be_visible()
    corrected_set, scan_chunk_status, scan_chunk_qc = _chunk(f, correction)
    assert scan_chunk_status == "ready"
    assert scan_chunk_qc["status"] == ("passed" if should_qualify else "needs_review"), scan_chunk_qc["issues"]
    if not should_qualify:
        issue, = scan_chunk_qc["issues"]
        assert issue["code"] == "CHUNK_LEAF_ROLE_UNRESOLVED"
        with f.app.state.session_factory() as db:
            group = next(row for row in db.get(ChunkSet, corrected_set).stage_checkpoint["support_groups"] if row["id"] == issue["support_group_id"])
            block, = group["members"]
            leaf = next(row for row in revised["items"] if row["id"] == block["block_id"])
            assert leaf["text"] == "Note: Apply this card only during a simulated exercise."
    candidate = page.get_by_role("button", name="创建完整核验候选", exact=True)
    expect(candidate).to_be_disabled()
    blocked_before_review, scan_publish_error = _assert_publication_blocked(f, scan_base)
    page.get_by_role("button", name="原文核验", exact=True).click()

    for block in (b for b in revised["items"] if b["structure_role"] == "leaf"):
        leaf = page.locator(f'article[data-block-id="{block["id"]}"]')
        leaf.get_by_label(f'内容块 {block["ordinal"]} 核验结论', exact=True).select_option("approved")
        leaf.get_by_label(f'内容块 {block["ordinal"]} 核验说明', exact=True).fill("SYNTHETIC source checked against scanned original")
        leaf.get_by_role("button", name="保存核验", exact=True).click()
        page.get_by_text("逐块核验结果已保存。", exact=True).wait_for()
    expect(candidate).to_be_disabled()
    with f.app.state.session_factory() as db:
        run = db.get(ParseRun, correction)
        version = db.get(DocumentVersion, scan_version)
        scope = {**g.source_binding(db, version), **source_confirmation_scope(db, run)}
        scope["observation_dispositions"] = {ref: {"decision": "covered_by",
            "block_refs": list(scope["block_bindings"]), "reason": "SYNTHETIC witness compared the full raster to every extracted line"}
            for ref in scope["observation_bindings"]}
        # Fixture supplies the explicit synthetic human witness, after all actual
        # React review and production validation. It cannot confer real approval.
        proof = seed_confirmation(db, db.get(User, actor_id), version, "key_content", "accepted", scope).id
        assert source_verification(db, run)["state"] == "verified"
        assert source_verification(db, db.get(ParseRun, scan))["state"] == "pending"
        db.commit()
    _refresh(page)
    expect(page.locator(".pdf-milestones").locator("div").filter(has_text="源人工核验").get_by_text("已核验", exact=True)).to_be_visible()
    assert proof in _blocks(f, correction)["source_verification"]["confirmation_refs"]
    _screenshot(f, f"05-{mode}-source-reviewed")
    if should_qualify:
        _select(page, "匹配本批次且当前有效的专业确认").select_option(proof)
        page.get_by_label("本次核验登记说明", exact=True).fill("SYNTHETIC corrected warning and scanned sources checked")
        with page.expect_response(lambda r: r.request.method == "POST" and r.url.endswith(scan_base + "/review-sets")) as registered:
            candidate.click()
        assert registered.value.status == 201, registered.value.text()
        after_review = {"complete_candidate": registered.value.json()["id"]}
        with f.app.state.session_factory() as db:
            version = db.get(DocumentVersion, scan_version)
            assert version.publish_status == "draft" and version.selected_review_set_id is None and version.selected_chunk_set_id is None
        page.get_by_text("完整核验候选已登记", exact=False).wait_for()
        _screenshot(f, f"06-{mode}-candidate")
    else:
        expect(candidate).to_be_disabled()
        blockers, error = _assert_publication_blocked(f, scan_base)
        after_review = {"publication_preview_blockers": blockers, "publication_post_error": error}
        page.get_by_role("button", name="原文核验", exact=True).click()
    _select(page, "解析批次").select_option(scan)
    expect(page.locator(f'article[data-block-id="{original_warning["id"]}"]')).to_contain_text(WARNING)
    assert _blocks(f, scan)["source_verification"]["state"] == "pending"
    assert f.context.request.get(f.address + scan_base + "/file").body() == evidence[0].read_bytes()
    f.checks.append({"sample": mode, "original_page": 1, "page_label_metadata": label, "warning_block": warning["id"],
                     "correction": correction, "old_run": scan, "old_warning_block": original_warning["id"],
                     "correction_kind": "paragraph→warning", "parent_original_pdf_raw_archive_and_ir_preserved": True,
                     "unreviewed_publish": {"blockers": blocked_before_review, "error": scan_publish_error},
                     "verified_source": proof, "witness": "synthetic", "chunk_set": corrected_set,
                     "collection_qc": scan_chunk_qc["status"], "collection_qc_issues": [item["code"] for item in scan_chunk_qc["issues"]],
                     "after_source_verification": after_review})
