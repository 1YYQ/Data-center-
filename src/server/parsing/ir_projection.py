"""Freeze normalized IR and atomically import its checked relational projection."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import ContentBlock, DocumentPage, DocumentVersion, FileObject, ParseArtifact, ParseRun
from ..provider_transport import ProviderError
from .artifacts import ArtifactError, bind_workspace, prepare_artifact_set, read_artifact
from .ir import normalize_ir, RENDERER
from .local_pipeline import ParseContractError, fingerprint, read_bundle


PAGE_FIELDS = ("file_page", "media_box_pt", "crop_box_pt", "rotation", "user_unit", "width_pt", "height_pt",
               "printed_page", "printed_page_basis", "page_class", "state", "coverage", "issue_refs", "transforms", "raw_ref")
BLOCK_FIELDS = ("id", "ordinal", "kind", "text_content", "structured_content", "locator", "file_page", "source_kind", "source_block_ref",
                "raw_ref", "content_hash", "parent_block_id", "reading_order", "heading_level", "structure_role")


def _read_json(db: Session, artifact: ParseArtifact, settings: Settings) -> dict:
    try:
        value = json.loads(read_artifact(db, artifact, settings).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED") from None


def _artifact(db: Session, run_id: str, artifact_id: str) -> ParseArtifact:
    row = db.get(ParseArtifact, artifact_id)
    if not row or row.parse_run_id != run_id:
        raise ParseContractError("PROCESSING_SOURCE_CHANGED")
    return row


def _source_binding(db, run, job):
    version = db.get(DocumentVersion, run.document_version_id, populate_existing=True)
    file = db.get(FileObject, job.source_file_id, populate_existing=True)
    if (not version or version.source_file_id != job.source_file_id or not file or file.status != "available"
            or file.sha256 != job.source_sha256 or file.mime_type != "application/pdf"
            or fingerprint({"version_id": version.id, "source_file_id": file.id, "source_sha256": file.sha256,
                            "request_config": run.request_config}) != run.input_fingerprint):
        raise ParseContractError("PROCESSING_SOURCE_CHANGED")
    return file


def _raw_inputs(db, run, job, settings):
    """Resolve exact manifest members, then parse the very bytes we verified."""
    outputs = run.stage_checkpoint.get("outputs", {})
    manifest_artifact = _artifact(db, run.id, outputs["raw_manifest"])
    manifest_file = db.get(FileObject, manifest_artifact.file_id)
    if outputs.get("raw_manifest_sha256") != manifest_file.sha256:
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    manifest = _read_json(db, manifest_artifact, settings)
    if manifest.get("parse_run_id") != run.id or manifest.get("complete") is not True:
        raise ParseContractError("PARSE_RAW_NOT_READY")
    archived = manifest.get("artifacts", [])
    raw_ref = next((row for row in archived if row["kind"] == "raw_archive" and row["logical_name"].endswith("/result.zip")), None)
    preflight_ref = next((row for row in archived if row["kind"] == "preflight"), None)
    if not raw_ref or not preflight_ref:
        raise ParseContractError("PARSE_RAW_NOT_READY")
    def exact_member(row):
        artifact = _artifact(db, run.id, row["artifact_id"])
        file = db.get(FileObject, artifact.file_id)
        if (not file or artifact.file_id != row["file_id"] or artifact.logical_name != row["logical_name"]
                or artifact.kind != row["kind"] or file.sha256 != row["sha256"] or file.byte_size != row["byte_size"]):
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        return artifact, file, read_artifact(db, artifact, settings)
    raw_artifact, raw_file, body = exact_member(raw_ref)
    _, _, preflight_body = exact_member(preflight_ref)
    try:
        preflight = json.loads(preflight_body)
    except (ValueError, UnicodeError):
        raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED") from None
    middle, members = read_bundle(body, preflight, run.request_config["profile"])
    if preflight.get("source_sha256") != job.source_sha256:
        raise ParseContractError("PROCESSING_SOURCE_CHANGED")
    selections = run.stage_checkpoint.get("page_selection", {})
    if set(selections) != {str(page["page_idx"] + 1) for page in middle["pages"]} or any(
        row.get("raw_artifact_id") != raw_artifact.id or row.get("raw_member_name") != "middle_json.json"
        or row.get("profile_hash") != run.input_fingerprint
        or row.get("json_pointer") != f"/pages/{index}"
        for index, page in enumerate(middle["pages"]) for row in [selections[str(page["page_idx"] + 1)]]):
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    return raw_artifact, raw_file, preflight, middle, members, preflight_ref["artifact_id"]


def load_normalized_ir(db, run, job, settings, *, check_projection=True):
    """Validate the frozen artifact, current source and its exact raw lineage."""
    from ..parse_jobs import _fence_authority
    outputs = run.stage_checkpoint.get("outputs", {})
    artifact = _artifact(db, run.id, outputs["normalized_ir"])
    file = db.get(FileObject, artifact.file_id)
    if not file or outputs.get("normalized_ir_sha256") != file.sha256:
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    ir = _read_json(db, artifact, settings)
    raw_artifact, raw_file, preflight, middle, members, preflight_id = _raw_inputs(db, run, job, settings)
    _source_binding(db, run, job)
    if (ir.get("phase") != "normalized" or ir.get("schema") != "dc.document-ir" or ir.get("schema_version") != "1.0"
            or ir.get("parse_run_id") != run.id or ir.get("document_version_id") != run.document_version_id
            or ir.get("source_file_id") != job.source_file_id or ir.get("source_sha256") != job.source_sha256
            or ir.get("raw_ref", {}).get("artifact_id") != raw_artifact.id
            or artifact.derived_from_artifact_id != raw_artifact.id
            or run.stage_checkpoint.get("normalization_fingerprint") != fingerprint(
                {"artifact": file.sha256, "raw": raw_file.sha256, "profile": run.input_fingerprint})):
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    assets = {}
    for row in ir.get("artifact_refs", []):
        if row.get("kind") != "image" or not row.get("raw_member_name"):
            continue
        artifact_row = _artifact(db, run.id, row["artifact_id"])
        file_row = db.get(FileObject, artifact_row.file_id)
        if (not file_row or row.get("file_id") != file_row.id or row.get("sha256") != file_row.sha256
                or read_artifact(db, artifact_row, settings) != members.get(row["raw_member_name"])):
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        assets[row["raw_member_name"]] = {**row, "parse_run_id": run.id}
    reconstructed = normalize_ir(run_id=run.id, version_id=run.document_version_id, source_file_id=job.source_file_id,
        source_sha256=job.source_sha256, profile=run.request_config["profile"], preflight=preflight, middle=middle,
        raw_artifact_id=raw_artifact.id, asset_refs=assets, preflight_artifact_id=preflight_id)
    reconstructed = _apply_corrections(db, run, reconstructed, settings)
    if fingerprint(reconstructed) != fingerprint(ir):
        raise ParseContractError("IR_RAW_CONTENT_MISMATCH")
    _fence_authority(db, run, job)
    if check_projection:
        assert_projection(db, run.id, ir)
    return ir


def _apply_corrections(db, run, ir, settings):
    from .corrections import apply_manual_corrections, inherit_manual_corrections
    outputs = run.stage_checkpoint.get("outputs", {})
    parent_ir = None
    parent_ref = outputs.get("parent_normalized_ir")
    if parent_ref:
        artifact = _artifact(db, run.id, parent_ref)
        file = db.get(FileObject, artifact.file_id)
        if file.sha256 != outputs.get("parent_normalized_ir_sha256"):
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        parent_ir = _read_json(db, artifact, settings)
        if parent_ir.get("parse_run_id") != run.parent_run_id:
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        ir = inherit_manual_corrections(ir, parent_ir=parent_ir,
            artifact_map=run.stage_checkpoint.get("inherited_artifact_map", {}), parent_normalized_alias_id=parent_ref)
    correction_ref = outputs.get("manual_corrections")
    if correction_ref:
        artifact = _artifact(db, run.id, correction_ref)
        file = db.get(FileObject, artifact.file_id)
        if file.sha256 != outputs.get("manual_corrections_sha256") or not parent_ir:
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        ir = apply_manual_corrections(ir, parent_ir=parent_ir, correction_artifact_id=correction_ref,
                                     payload=_read_json(db, artifact, settings))
    return ir


def assert_projection(db: Session, run_id: str, ir: dict) -> None:
    pages = {page.file_page: page for page in db.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == run_id))}
    blocks = {block.id: block for block in db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id))}
    if set(pages) != {row["file_page"] for row in ir["pages"]} or set(blocks) != {row["id"] for row in ir["blocks"]}:
        raise ParseContractError("IR_PROJECTION_MISMATCH")
    for row in ir["pages"]:
        if any(getattr(pages[row["file_page"]], key) != row.get(key) for key in PAGE_FIELDS):
            raise ParseContractError("IR_PROJECTION_MISMATCH")
    for row in ir["blocks"]:
        if any(getattr(blocks[row["id"]], key) != row.get(key) for key in BLOCK_FIELDS):
            raise ParseContractError("IR_PROJECTION_MISMATCH")


def _import_projection(db: Session, run: ParseRun, ir: dict) -> None:
    if db.scalar(select(ContentBlock.id).where(ContentBlock.parse_run_id == run.id).limit(1)) is not None:
        assert_projection(db, run.id, ir)
        return
    if db.scalar(select(DocumentPage.file_page).where(DocumentPage.parse_run_id == run.id).limit(1)) is not None:
        raise ParseContractError("IR_PROJECTION_MISMATCH")
    db.add_all(DocumentPage(parse_run_id=run.id, **{key: row.get(key) for key in PAGE_FIELDS}) for row in ir["pages"])
    db.flush()
    # Physical containers precede their children in the frozen normalizer order.
    # Flush by hierarchy level so SQLite's same-run parent guard also holds.
    remaining, installed = list(ir["blocks"]), set()
    while remaining:
        ready = [row for row in remaining if row.get("parent_block_id") is None or row["parent_block_id"] in installed]
        if not ready:
            raise ParseContractError("IR_PROJECTION_MISMATCH")
        db.add_all(ContentBlock(parse_run_id=run.id, review_status="unreviewed",
                               **{key: row.get(key) for key in BLOCK_FIELDS}) for row in ready)
        db.flush()
        installed.update(row["id"] for row in ready)
        remaining = [row for row in remaining if row["id"] not in installed]
    assert_projection(db, run.id, ir)


def normalize_parse_run(db: Session, run_id: str, settings: Settings) -> ParseRun:
    from ..parse_jobs import ParseServiceJob, _claim_job, _fence_attempt, _local_guard, _local_heartbeat
    from ..services import SafeError, audit, file_path

    run, job = db.get(ParseRun, run_id), db.get(ParseServiceJob, run_id)
    if not run or not job or job.mode != "local":
        raise SafeError("NOT_FOUND", "未找到本地处理任务", 404)
    outputs = (run.stage_checkpoint or {}).get("outputs", {})
    if outputs.get("normalized_ir"):
        load_normalized_ir(db, run, job, settings, check_projection=not outputs.get("structured_ir"))
        db.rollback()
        return run
    if not outputs.get("raw_manifest"):
        raise SafeError("PARSE_RAW_NOT_READY", "原始产物尚未归档", 409)
    attempt = _claim_job(db, job, run, allow_review=True)
    if attempt is None:
        return run
    try:
        _local_guard(db, run, job, attempt, settings, require_runtime=False)
        version = db.get(DocumentVersion, run.document_version_id)
        source_file_id = version.source_file_id
        run.processing_stage = "ir"
        db.commit()
        raw_artifact, raw_file, preflight, middle, members, preflight_artifact_id = _raw_inputs(db, run, job, settings)
        _local_heartbeat(db, run, job, attempt, settings, require_runtime=False)
        asset_specs = []
        inherited_assets = {row["raw_member_name"]: row for row in run.stage_checkpoint.get("inherited_asset_refs", [])}
        reused_assets = {}
        for name, body in members.items():
            suffix = Path(name).suffix.lower()
            if suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif"}:
                previous = inherited_assets.get(name)
                if previous:
                    artifact = _artifact(db, run.id, previous["artifact_id"])
                    if read_artifact(db, artifact, settings) != body:
                        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
                    reused_assets[name] = previous
                    continue
                asset_specs.append({"raw": body, "kind": "image", "logical_name": "assets/" + raw_artifact.id + "/" + hashlib.sha256(name.encode()).hexdigest() + suffix,
                    "producer": {"phase": "normalized", "role": "package_member"},
                    "source_locator": {"raw_artifact_id": raw_artifact.id, "raw_member_name": name},
                    "derived_from_artifact_id": raw_artifact.id})
        db.commit()
        prepared_assets = prepare_artifact_set(run.id, job.actor_id, settings, asset_specs)
        assets = {**reused_assets, **{spec["source_locator"]["raw_member_name"]: {**row, "parse_run_id": run.id,
                  "raw_member_name": spec["source_locator"]["raw_member_name"], "derived_from_artifact_id": raw_artifact.id}
                  for spec, row in zip(asset_specs, prepared_assets.manifest["artifacts"])}}
        ir = normalize_ir(run_id=run.id, version_id=run.document_version_id, source_file_id=source_file_id,
            source_sha256=job.source_sha256, profile=run.request_config["profile"], preflight=preflight, middle=middle,
            raw_artifact_id=raw_artifact.id, asset_refs=assets, preflight_artifact_id=preflight_artifact_id)
        ir = _apply_corrections(db, run, ir, settings)
        body = json.dumps(ir, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        module_hash = hashlib.sha256(Path(__file__).with_name("ir.py").read_bytes()).hexdigest()
        prepared_ir = prepare_artifact_set(run.id, job.actor_id, settings, [{"raw": body, "kind": "document_ir",
            "logical_name": "ir/normalized.json", "producer": {"phase": "normalized", "renderer": RENDERER, "normalizer_sha256": module_hash},
            "derived_from_artifact_id": raw_artifact.id}])
        _local_guard(db, run, job, attempt, settings, require_runtime=False)
        bind_workspace(db, prepared_assets)
        frozen = bind_workspace(db, prepared_ir)["artifacts"][0]
        _import_projection(db, run, ir)
        run.ir_schema_version = "1.0"
        run.stage_checkpoint = {**run.stage_checkpoint, "stage_state": "normalized",
            "outputs": {**outputs, "normalized_ir": frozen["artifact_id"], "normalized_ir_sha256": frozen["sha256"]},
            "normalizer_sha256": module_hash, "normalization_issues": ir["issues"],
            "normalization_fingerprint": fingerprint({"artifact": frozen["sha256"], "raw": raw_file.sha256, "profile": run.input_fingerprint})}
        run.status, run.error_code, job.lease_until = "needs_review", None, None
        audit(db, job.actor_id, "parse.normalized", "parse_run", run.id, page_count=len(ir["pages"]), block_count=len(ir["blocks"]))
        db.commit()
    except (ArtifactError, ParseContractError, ProviderError, SafeError, SQLAlchemyError, OSError) as error:
        db.rollback()
        try:
            _fence_attempt(db, run.id, attempt)
        except (ProviderError, SQLAlchemyError):
            db.rollback()
            return db.get(ParseRun, run_id, populate_existing=True)
        db.refresh(run)
        db.refresh(job)
        code = "IR_PERSISTENCE_UNAVAILABLE" if isinstance(error, (SQLAlchemyError, OSError)) else (
            error.detail["code"] if isinstance(error, SafeError) else error.code)
        run.status = "cancelled" if job.cancel_requested else (
            "waiting_external" if code in {"IR_PERSISTENCE_UNAVAILABLE", "PARSE_ARTIFACT_STORAGE_UNAVAILABLE"} else "failed")
        run.error_code, job.lease_until = code, None
        run.stage_checkpoint = {**run.stage_checkpoint, "stage_state": run.status, "normalization_error": code}
        audit(db, job.actor_id, "parse.normalization_stopped", "parse_run", run.id, error_code=code)
        db.commit()
    return run
