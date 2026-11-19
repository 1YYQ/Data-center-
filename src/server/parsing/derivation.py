"""Create a local candidate from verified, immutable parent parse output."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import os
from pathlib import PurePosixPath

from sqlalchemy import update
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import Document, DocumentVersion, FileObject, ParseArtifact, ParseRun, User, new_id
from ..provider_transport import ProviderError
from ..services import SafeError, audit, can_access_document, fence_management_authority, file_path
from .artifacts import (ArtifactError, bind_artifact_alias, bind_workspace, prepare_artifact_alias,
                        prepare_artifact_set, validate_raw_lineage)
from .ir_projection import _artifact, _raw_inputs, _read_json, _source_binding
from .local_pipeline import ParseContractError, fingerprint


def _parent_snapshot(parent: ParseRun) -> str:
    checkpoint = parent.stage_checkpoint or {}
    outputs = checkpoint.get("outputs", {})
    return fingerprint({
        "input": parent.input_fingerprint, "parent": parent.parent_run_id,
        "raw_manifest": outputs.get("raw_manifest"), "raw_manifest_sha256": outputs.get("raw_manifest_sha256"),
        "raw_epoch": outputs.get("raw_epoch"), "page_selection": checkpoint.get("page_selection"),
        "actual_profile": parent.actual_profile, "raw_contract": parent.raw_contract,
        "normalized_ir": outputs.get("normalized_ir"), "normalized_ir_sha256": outputs.get("normalized_ir_sha256"),
        "provider": parent.provider, "engine_version": parent.engine_version, "model_revision": parent.model_revision,
        "adapter_version": parent.adapter_version, "recovery_version": parent.recovery_version,
    })


def _authorized_document(db: Session, actor: User, parent: ParseRun) -> Document:
    version = db.get(DocumentVersion, parent.document_version_id, populate_existing=True)
    document = db.get(Document, version.document_id, populate_existing=True) if version else None
    if not document or not can_access_document(db, actor, document, "knowledge.manage"):
        raise SafeError("FORBIDDEN", "当前账号没有管理该资料范围的权限", 403)
    return document


def _verify_original(file: FileObject, settings: Settings) -> None:
    try:
        with file_path(file, settings).open("rb") as stream:
            if os.fstat(stream.fileno()).st_size == file.byte_size and hashlib.file_digest(stream, "sha256").hexdigest() == file.sha256:
                return
    except (OSError, SafeError):
        pass
    raise ParseContractError("PROCESSING_SOURCE_CHANGED")


def _prepare_parent(db: Session, parent: ParseRun, job, settings: Settings) -> tuple[ParseArtifact, ParseArtifact, dict, dict, dict]:
    try:
        raw, _, preflight, middle, members, preflight_id = _raw_inputs(db, parent, job, settings)
        return raw, _artifact(db, parent.id, preflight_id), preflight, middle, members
    except (KeyError, TypeError, ValueError, AttributeError):
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH") from None


def _prepare_normalized_aliases(db, parent, run, settings, source_file, raw, preflight, members, child_raw_id):
    outputs = parent.stage_checkpoint["outputs"]
    if not outputs.get("normalized_ir"):
        return [], {}, [], {}
    artifact = _artifact(db, parent.id, outputs["normalized_ir"])
    file = db.get(FileObject, artifact.file_id)
    if (artifact.kind != "document_ir" or not file or file.sha256 != outputs.get("normalized_ir_sha256")
            or artifact.derived_from_artifact_id != raw.id):
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    ir = _read_json(db, artifact, settings)
    refs, raw_ref = ir.get("artifact_refs"), ir.get("raw_ref")
    if ((ir.get("schema"), ir.get("schema_version"), ir.get("phase")) != ("dc.document-ir", "1.0", "normalized")
            or ir.get("parse_run_id") != parent.id or ir.get("document_version_id") != parent.document_version_id
            or ir.get("source_file_id") != source_file.id or ir.get("source_sha256") != source_file.sha256
            or ir.get("profile") != parent.request_config.get("profile") or not isinstance(raw_ref, dict)
            or raw_ref.get("artifact_id") != raw.id
            or not isinstance(refs, list)):
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    normalized = prepare_artifact_alias(db, artifact, settings, run_id=run.id, logical_name="reused/parent-normalized.json")
    aliases, mapping, asset_refs, seen = [normalized], {artifact.id: normalized.reference["artifact_id"]}, [], set()
    for row in refs:
        identifier = row.get("artifact_id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in seen:
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        seen.add(identifier)
        if identifier in {raw.id, preflight.id} or row.get("role") == "parent_normalized_ir":
            continue
        source = _artifact(db, parent.id, identifier)
        source_file = db.get(FileObject, source.file_id)
        if not source_file or source.kind != row.get("kind"):
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        if source.kind == "image":
            member = row.get("raw_member_name")
            data = members.get(member) if isinstance(member, str) else None
            if (data is None or row.get("file_id") != source_file.id or row.get("sha256") != source_file.sha256
                    or source_file.byte_size != len(data) or source_file.sha256 != hashlib.sha256(data).hexdigest()):
                raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
            name = f"reused/images/{identifier}{PurePosixPath(member).suffix.lower()}"
        elif source.kind == "structured" and row.get("role") == "manual_correction":
            name = f"reused/corrections/{identifier}.json"
        else:
            raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
        alias = prepare_artifact_alias(db, source, settings, run_id=run.id, logical_name=name)
        aliases.append(alias)
        mapping[identifier] = alias.reference["artifact_id"]
        if source.kind == "image":
            asset_refs.append({**alias.reference, "raw_member_name": member, "parse_run_id": run.id,
                "derived_from_artifact_id": identifier, "mime_type": source_file.mime_type,
                "source_locator": {"raw_artifact_id": child_raw_id, "raw_member_name": member}})
    if not {raw.id, preflight.id} <= seen:
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    return aliases, mapping, asset_refs, {"parent_normalized_ir": normalized.reference["artifact_id"],
                                           "parent_normalized_ir_sha256": normalized.reference["sha256"]}


def _accepted_target(db, target_id, actor, parent, parent_job, attempt):
    from ..parse_jobs import ParseServiceJob

    run = db.get(ParseRun, target_id, populate_existing=True)
    job = db.get(ParseServiceJob, target_id, populate_existing=True)
    if not run or not job or job.mode != "local" or job.actor_id != actor.id:
        raise ProviderError("PARSE_AUTHORITY_REVOKED")
    if job.cancel_requested or run.status == "cancelled":
        raise ProviderError("PARSE_CANCELLED")
    if run.status != "running" or job.attempt != attempt or job.lease_until is None:
        raise ProviderError("PARSE_ATTEMPT_STALE")
    checkpoint = run.stage_checkpoint or {}
    outputs = checkpoint.get("outputs")
    if (run.parent_run_id != parent.id or run.document_version_id != parent.document_version_id
            or run.origin not in {"manual", "reprocess"} or run.input_fingerprint != parent.input_fingerprint
            or run.request_config != parent.request_config or run.actual_profile != parent.actual_profile
            or job.source_file_id != parent_job.source_file_id or job.source_sha256 != parent_job.source_sha256
            or checkpoint.get("source_sha256") != job.source_sha256 or checkpoint.get("derivation") is not True
            or not isinstance(checkpoint.get("correction_payload"), dict) or not isinstance(outputs, dict) or outputs):
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    return run, job


def _request_bytes(payload: dict) -> bytes:
    try:
        return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (ValueError, TypeError):
        raise ParseContractError("IR_CORRECTION_INVALID") from None


def derive_raw_run(db: Session, actor: User, parent: ParseRun, settings: Settings, *,
                   target_run: ParseRun | None = None, attempt: int | None = None) -> ParseRun:
    """Prepare the manifest, then bind parent files and metadata under current DM.

    The caller owns commit/rollback and may use the candidate as an intermediate
    step in a wider command. A failed commit can leave a private manifest object;
    shared parent files and old project selections are never written here.
    """
    from ..parse_jobs import ParseServiceJob, _local_guard

    target_id = target_run.id if target_run is not None else None
    if (target_run is None and attempt is not None
            or target_run is not None and (not target_id or type(attempt) is not int or attempt < 1)):
        raise SafeError("INVALID_INPUT", "派生作业或尝试身份无效", 422)

    parent = db.get(ParseRun, parent.id, populate_existing=True)
    if parent is None:
        raise SafeError("NOT_FOUND", "未找到原始解析批次", 404)
    document = _authorized_document(db, actor, parent)
    job = db.get(ParseServiceJob, parent.id, populate_existing=True)
    if not job or job.mode != "local" or parent.status not in {"needs_review", "succeeded"}:
        raise SafeError("PARSE_RAW_NOT_READY", "原始解析产物尚未归档或无法复用", 409)

    try:
        request = None
        if target_id is not None:
            run, child_job = _accepted_target(db, target_id, actor, parent, job, attempt)
            request = _request_bytes(run.stage_checkpoint["correction_payload"])
        else:
            run = ParseRun(id=new_id(), document_version_id=parent.document_version_id, origin="reprocess",
                parent_run_id=parent.id, status="needs_review", provider=parent.provider, engine_version=parent.engine_version,
                model_revision=parent.model_revision, request_config=deepcopy(parent.request_config),
                actual_profile=deepcopy(parent.actual_profile), raw_contract=deepcopy(parent.raw_contract),
                adapter_version=parent.adapter_version, recovery_version=parent.recovery_version,
                output_revision=parent.output_revision, input_fingerprint=parent.input_fingerprint, processing_stage="parsing")

        lineage = validate_raw_lineage(db, parent.id)
        if len(lineage) >= 32:
            raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")
        source = _source_binding(db, parent, job)
        source_identity = (source.id, source.sha256, source.byte_size, source.object_key)
        _verify_original(source, settings)
        snapshot = _parent_snapshot(parent)
        raw, preflight_artifact, preflight, middle, members = _prepare_parent(db, parent, job, settings)
        parent_outputs = parent.stage_checkpoint["outputs"]
        raw_epoch = parent_outputs.get("raw_epoch")
        page_selection = parent.stage_checkpoint["page_selection"]
        if (type(raw_epoch) is not int or raw_epoch < 1
                or any(row.get("attempt_epoch") != raw_epoch or row.get("region_overrides") for row in page_selection.values())):
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")

        raw_alias = prepare_artifact_alias(db, raw, settings, run_id=run.id, logical_name="reused/result.zip")
        preflight_alias = prepare_artifact_alias(db, preflight_artifact, settings, run_id=run.id, logical_name="reused/preflight.json")
        raw_ref = raw_alias.reference
        inherited, artifact_map, asset_refs, inherited_outputs = _prepare_normalized_aliases(
            db, parent, run, settings, source, raw, preflight_artifact, members, raw_ref["artifact_id"])
        artifact_map.update({raw.id: raw_ref["artifact_id"], preflight_artifact.id: preflight_alias.reference["artifact_id"]})
        manifest = {"schema": "dc.parse-attempt-manifest", "schema_version": "1.0", "parse_run_id": run.id,
            "epoch": raw_epoch, "complete": True, "output_state": "complete", "safe_error": None,
            "source_parse_run_id": parent.id, "source_manifest_artifact_id": parent_outputs["raw_manifest"],
            "source_manifest_sha256": parent_outputs["raw_manifest_sha256"],
            "artifacts": [raw_ref, preflight_alias.reference]}
        body = json.dumps(manifest, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        items = [{"raw": body, "kind": "structured", "logical_name": "reused/manifest.json",
                  "producer": {"role": "derived_raw_manifest", "parent_run_id": parent.id}}]
        if request is not None:
            items.append({"raw": request, "kind": "structured", "logical_name": "corrections/input.json",
                          "producer": {"role": "manual_correction", "parent_run_id": parent.id}})
        prepared = prepare_artifact_set(run.id, actor.id, settings, items)

        # Serialize with document maintenance only after all large byte reads
        # and the child manifest's installation have finished.
        if target_id is None:
            locked = db.execute(update(Document).where(Document.id == document.id, Document.deleted_at.is_(None))
                                .values(revision=Document.revision).execution_options(synchronize_session=False))
            if locked.rowcount != 1:
                raise SafeError("NOT_FOUND", "原件当前不可用", 404)
            fence_management_authority(db, actor, document.knowledge_base_id, document_id=document.id)
        else:
            _local_guard(db, run, child_job, attempt, settings, require_runtime=False)
        parent = db.get(ParseRun, parent.id, populate_existing=True)
        if not parent or parent.status not in {"needs_review", "succeeded"} or _parent_snapshot(parent) != snapshot:
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        _authorized_document(db, actor, parent)
        job = db.get(ParseServiceJob, parent.id, populate_existing=True)
        if job is None:
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        source_manifest = _artifact(db, parent.id, parent_outputs["raw_manifest"])
        source_manifest_file = db.get(FileObject, source_manifest.file_id, populate_existing=True)
        if (not source_manifest_file or source_manifest_file.status != "available"
                or source_manifest_file.storage_name != "local"
                or source_manifest_file.sha256 != parent_outputs["raw_manifest_sha256"]):
            raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
        source = _source_binding(db, parent, job)
        if (source.id, source.sha256, source.byte_size, source.object_key) != source_identity:
            raise ParseContractError("PROCESSING_SOURCE_CHANGED")

        if target_id is None:
            db.add(run)
            db.flush()
            child_job = ParseServiceJob(parse_run_id=run.id, mode="local", actor_id=actor.id,
                source_file_id=source.id, source_sha256=source.sha256)
            db.add(child_job)
            db.flush()
        else:
            run, child_job = _accepted_target(db, target_id, actor, parent, job, attempt)
            if _request_bytes(run.stage_checkpoint["correction_payload"]) != request:
                raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
            # These attributes describe the frozen parser output, not a new
            # invocation in the current machine environment.
            for field in ("provider", "engine_version", "model_revision", "adapter_version", "recovery_version"):
                setattr(run, field, getattr(parent, field))
            run.raw_contract = deepcopy(parent.raw_contract)
            db.flush()
        bind_artifact_alias(db, raw_alias)
        bind_artifact_alias(db, preflight_alias)
        for alias in inherited:
            bind_artifact_alias(db, alias)
        bound = bind_workspace(db, prepared)["artifacts"]
        frozen = bound[0]
        outputs = {"raw_manifest": frozen["artifact_id"], "raw_epoch": raw_epoch, "raw_manifest_sha256": frozen["sha256"],
                   **inherited_outputs}
        attempts = list((run.stage_checkpoint or {}).get("attempts", []))
        if request is not None:
            outputs.update(manual_corrections=bound[1]["artifact_id"], manual_corrections_sha256=bound[1]["sha256"])
            attempts.append({"epoch": attempt, "output_epoch": raw_epoch, "state": "succeeded",
                             "manifest_artifact_id": frozen["artifact_id"], "phase": "derivation"})
        run.stage_checkpoint = {**(run.stage_checkpoint or {}), "source_sha256": source.sha256, "stage_state": "parsed", "attempts": attempts,
            "total_page_count": preflight["page_count"], "processed_page_count": len(middle["pages"]),
            "outputs": outputs, "inherited_artifact_map": artifact_map, "inherited_asset_refs": asset_refs,
            "page_selection": {page: {**selection, "raw_artifact_id": raw_ref["artifact_id"], "source_parse_run_id": parent.id}
                               for page, selection in page_selection.items()}}
        run.status, run.error_code, run.processing_stage = "needs_review", None, "parsing"
        child_job.raw_result_file_id, child_job.lease_until = raw_ref["file_id"], None
        child_job.checkpoint = {**(child_job.checkpoint or {}), "manifest_artifact_id": frozen["artifact_id"], "output_epoch": raw_epoch,
                                "output_state": "complete", "last_attempt": attempt or 0, "source_parse_run_id": parent.id}
        audit(db, actor.id, "parse.raw_derived", "parse_run", run.id, parent_run_id=parent.id,
              source_manifest_artifact_id=parent_outputs["raw_manifest"])
        db.flush()
        return run
    except (ArtifactError, ParseContractError) as error:
        raise SafeError(error.code, "原始解析产物的来源或完整性无法核验", 409) from None
