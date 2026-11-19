"""Persistent structure/QC stages and current, source-level verification gates."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from ..models import BlockRelation, ContentBlock, DocumentPage, DocumentVersion, FileObject, ParseArtifact, ParseRun
from ..provider_transport import ProviderError
from .artifacts import ArtifactError, bind_workspace, prepare_artifact_set
from .ir_projection import (BLOCK_FIELDS, PAGE_FIELDS, _artifact, _read_json, _source_binding,
                            assert_projection, load_normalized_ir)
from .local_pipeline import ParseContractError, fingerprint
from .structure import RECOVERY_VERSION, recover_structure

RELATION_FIELDS = ("id", "parse_run_id", "from_block_id", "from_part", "relation_type", "to_block_id", "to_part", "basis")


def relation_binding(row):
    return fingerprint({key: getattr(row, key) for key in RELATION_FIELDS})


def page_binding(row):
    return fingerprint({key: getattr(row, key) for key in PAGE_FIELDS})


def projection_fingerprint(db, run_id):
    pages = db.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == run_id).order_by(DocumentPage.file_page)).all()
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id).order_by(ContentBlock.ordinal)).all()
    relations = db.scalars(select(BlockRelation).where(BlockRelation.parse_run_id == run_id).order_by(BlockRelation.id)).all()
    return fingerprint({"pages": [{key: getattr(row, key) for key in PAGE_FIELDS} for row in pages],
        "blocks": [{key: getattr(row, key) for key in BLOCK_FIELDS} for row in blocks],
        "relations": [{key: getattr(row, key) for key in RELATION_FIELDS} for row in relations]})


def checker_signature():
    return fingerprint({name: hashlib.sha256(Path(__file__).with_name(name).read_bytes()).hexdigest()
                        for name in ("quality.py", "structure.py", "ir.py")})


def assert_structured_projection(db, run, ir):
    assert_projection(db, run.id, ir)
    relations = {row.id: row for row in db.scalars(select(BlockRelation).where(BlockRelation.parse_run_id == run.id))}
    if set(relations) != {row["id"] for row in ir["relations"]} or any(
        any(getattr(relations[row["id"]], key) != row.get(key) for key in RELATION_FIELDS) for row in ir["relations"]):
        raise ParseContractError("IR_PROJECTION_MISMATCH")


def load_structured_ir(db, run, job, settings):
    outputs = run.stage_checkpoint.get("outputs", {})
    normalized = load_normalized_ir(db, run, job, settings, check_projection=False)
    artifact = _artifact(db, run.id, outputs["structured_ir"])
    file = db.get(FileObject, artifact.file_id)
    ir = _read_json(db, artifact, settings)
    if (not file or file.sha256 != outputs.get("structured_ir_sha256") or run.ir_artifact_id != artifact.id
            or artifact.derived_from_artifact_id != outputs["normalized_ir"] or ir.get("phase") != "structured"
            or ir.get("normalized_input") != {"artifact_id": outputs["normalized_ir"], "content_hash": fingerprint(normalized)}
            or any(ir.get(key) != normalized.get(key) for key in ("schema", "schema_version", "parse_run_id",
                       "document_version_id", "source_file_id", "source_sha256", "profile", "raw_ref"))):
        raise ParseContractError("IR_SOURCE_BINDING_MISMATCH")
    assert_structured_projection(db, run, ir)
    return ir


def _project_structure(db, run, normalized, structured):
    assert_projection(db, run.id, normalized)
    old = {row.id: row for row in db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id))}
    if any(row.review_status != "unreviewed" for row in old.values()):
        raise ParseContractError("IR_REVIEW_BEFORE_STRUCTURE")
    pending = [row for row in structured["blocks"] if row["id"] not in old]
    installed = set(old)
    while pending:
        ready = [row for row in pending if not row.get("parent_block_id") or row["parent_block_id"] in installed]
        if not ready:
            raise ParseContractError("IR_STRUCTURE_INVALID")
        db.add_all(ContentBlock(parse_run_id=run.id, **{key: row.get(key) for key in BLOCK_FIELDS}) for row in ready)
        db.flush()
        installed.update(row["id"] for row in ready)
        pending = [row for row in pending if row["id"] not in installed]
    for row in structured["blocks"]:
        if row["id"] in old:
            for key in BLOCK_FIELDS:
                setattr(old[row["id"]], key, row.get(key))
    for row in structured["pages"]:
        page = db.get(DocumentPage, (run.id, row["file_page"]))
        for key in PAGE_FIELDS:
            setattr(page, key, row.get(key))
    db.flush()
    db.add_all(BlockRelation(**{key: row[key] for key in RELATION_FIELDS}) for row in structured["relations"])
    db.flush()
    assert_structured_projection(db, run, structured)


def structure_parse_run(db, run_id, settings):
    from ..parse_jobs import ParseServiceJob, _claim_job, _fence_attempt, _local_guard, _local_heartbeat
    from ..services import SafeError, audit
    run, job = db.get(ParseRun, run_id), db.get(ParseServiceJob, run_id)
    if not run or not job or not run.stage_checkpoint.get("outputs", {}).get("normalized_ir"):
        raise SafeError("IR_NOT_READY", "规范化来源尚未就绪", 409)
    outputs = run.stage_checkpoint["outputs"]
    if outputs.get("structured_ir"):
        load_structured_ir(db, run, job, settings)
        db.rollback()
        return run
    attempt = _claim_job(db, job, run, allow_review=True)
    if attempt is None:
        return run
    try:
        _local_guard(db, run, job, attempt, settings, require_runtime=False)
        run.processing_stage = "recovery"
        db.commit()
        normalized = load_normalized_ir(db, run, job, settings)
        _local_heartbeat(db, run, job, attempt, settings, require_runtime=False)
        db.commit()
        ir = recover_structure(normalized, normalized_artifact_id=outputs["normalized_ir"])
        raw = json.dumps(ir, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        prepared = prepare_artifact_set(run.id, job.actor_id, settings, [{"raw": raw, "kind": "document_ir",
            "logical_name": "ir/structured.json", "producer": {"phase": "structured", "recovery_version": RECOVERY_VERSION,
                "recovery_sha256": hashlib.sha256(Path(__file__).with_name("structure.py").read_bytes()).hexdigest()},
            "derived_from_artifact_id": outputs["normalized_ir"]}])
        _local_guard(db, run, job, attempt, settings, require_runtime=False)
        frozen = bind_workspace(db, prepared)["artifacts"][0]
        _project_structure(db, run, normalized, ir)
        run.ir_artifact_id, run.recovery_version = frozen["artifact_id"], RECOVERY_VERSION
        run.stage_checkpoint = {**run.stage_checkpoint, "stage_state": "structured",
            "outputs": {**outputs, "structured_ir": frozen["artifact_id"], "structured_ir_sha256": frozen["sha256"]},
            "structure_issues": ir["issues"], "source_projection_hash": projection_fingerprint(db, run.id)}
        run.status, run.error_code, job.lease_until = "needs_review", None, None
        audit(db, job.actor_id, "parse.structured", "parse_run", run.id, relation_count=len(ir["relations"]))
        db.commit()
    except (ArtifactError, ParseContractError, ProviderError, SafeError, SQLAlchemyError, OSError) as error:
        _stage_failure(db, run, job, attempt, error, "structure", _fence_attempt)
    return run


def _stage_failure(db, run, job, attempt, error, phase, fence_attempt):
    from ..services import SafeError, audit
    db.rollback()
    try:
        fence_attempt(db, run.id, attempt)
    except (ProviderError, SQLAlchemyError):
        db.rollback()
        return
    db.refresh(run)
    db.refresh(job)
    code = "IR_PERSISTENCE_UNAVAILABLE" if isinstance(error, (SQLAlchemyError, OSError)) else (
        error.detail["code"] if isinstance(error, SafeError) else error.code)
    run.status = "cancelled" if job.cancel_requested else (
        "waiting_external" if code in {"IR_PERSISTENCE_UNAVAILABLE", "PARSE_ARTIFACT_STORAGE_UNAVAILABLE"} else "failed")
    run.error_code, job.lease_until = code, None
    run.stage_checkpoint = {**run.stage_checkpoint, "stage_state": run.status, phase + "_error": code}
    audit(db, job.actor_id, "parse.stage_stopped", "parse_run", run.id, phase=phase, error_code=code)
    db.commit()


def source_qc_is_current(db, run):
    """A technical source gate, deliberately independent of human/ChunkSet review."""
    from .quality import CHECKER_REVISION
    if not run or not run.ir_schema_version:
        return False
    report = run.stage_checkpoint.get("source_qc", {})
    outputs = run.stage_checkpoint.get("outputs", {})
    if (report.get("status") != "passed" or report.get("checker_revision") != CHECKER_REVISION
            or report.get("checker_code_sha256") != checker_signature()
            or report.get("projection_fingerprint") != projection_fingerprint(db, run.id)):
        return False
    expected = {"normalized_ir": "normalized_ir_sha256", "structured_ir": "structured_ir_sha256", "raw_manifest": "raw_manifest_sha256",
                "source_qc": "source_qc_sha256"}
    for name, sha_name in expected.items():
        artifact = db.get(ParseArtifact, outputs.get(name)) if outputs.get(name) else None
        file = db.get(FileObject, artifact.file_id) if artifact else None
        if not artifact or artifact.parse_run_id != run.id or not file or file.status != "available" or file.sha256 != outputs.get(sha_name):
            return False
        if name == "source_qc" and fingerprint(report) != file.sha256:
            return False
    version = db.get(DocumentVersion, run.document_version_id)
    file = db.get(FileObject, version.source_file_id) if version else None
    return bool(version and file and file.status == "available" and file.id == report.get("source_file_id")
                and file.sha256 == report.get("source_sha256") and run.ir_artifact_id == outputs["structured_ir"])


def source_confirmation_scope(db, run):
    """Exact, read-only options for recording an actual person's statement."""
    from ..governance import block_binding
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)).all()
    relations = db.scalars(select(BlockRelation).where(BlockRelation.parse_run_id == run.id)).all()
    pages = db.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == run.id)).all()
    return {"parse_run_id": run.id, "ir_artifact_id": run.ir_artifact_id,
        "source_qc_ref": run.stage_checkpoint.get("outputs", {}).get("source_qc"),
        "source_projection_hash": projection_fingerprint(db, run.id),
        "block_bindings": {row.id: block_binding(row) for row in blocks if row.structure_role == "leaf"},
        "container_bindings": {row.id: row.content_hash for row in blocks if row.structure_role == "container"},
        "observation_bindings": {row.id: row.content_hash for row in blocks if row.structure_role == "observation"},
        "page_bindings": {str(row.file_page): page_binding(row) for row in pages},
        "relation_bindings": {row.id: relation_binding(row) for row in relations}}


def confirmation_scope_matches(db, run, scope):
    if not source_qc_is_current(db, run):
        return False
    expected = source_confirmation_scope(db, run)
    for key in ("ir_artifact_id", "source_qc_ref", "source_projection_hash"):
        if scope.get(key) != expected[key]:
            return False
    for key in ("block_bindings", "container_bindings", "observation_bindings", "page_bindings", "relation_bindings"):
        actual = scope.get(key, {})
        if not isinstance(actual, dict) or any(expected[key].get(id) != value for id, value in actual.items()):
            return False
    # An omission does not establish coverage; source_verification checks the
    # combined required set. It is valid to record a partial person's statement.
    dispositions = scope.get("observation_dispositions", {})
    if not isinstance(dispositions, dict) or set(dispositions) - set(scope.get("observation_bindings", {})):
        return False
    for identifier, value in dispositions.items():
        if not isinstance(value, dict) or not str(value.get("reason", "")).strip():
            return False
        if value.get("decision") in {"decorative", "out_of_scope"}:
            continue
        if value.get("decision") == "image_only_accepted" and str(value.get("policy_ref", "")).strip():
            continue
        if value.get("decision") == "covered_by":
            ids = value.get("block_refs")
            if isinstance(ids, list) and ids and set(ids) <= set(expected["block_bindings"]):
                continue
        return False
    repairs = scope.get("repair_of_hold_refs", [])
    if (not isinstance(repairs, list) or any(not isinstance(value, str) for value in repairs)
            or len(set(repairs)) != len(repairs)):
        return False
    from ..governance_models import DocumentUseHold
    if any(not (row := db.get(DocumentUseHold, id)) or row.document_version_id != run.document_version_id or row.kind != "quality" for id in repairs):
        return False
    return True


def source_verification(db, run, confirmation_refs=None):
    from ..governance import confirmation_is_current
    from ..governance_models import BusinessConfirmation
    if not source_qc_is_current(db, run):
        return {"state": "pending", "reasons": ["SOURCE_QC_NOT_CURRENT"], "missing_leaf_refs": [],
                "missing_critical_refs": [], "missing_relation_refs": [], "missing_observation_refs": [], "confirmation_refs": []}
    report = run.stage_checkpoint["source_qc"]
    blocks = {row.id: row for row in db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id))}
    required = set(report["required_leaf_refs"])
    missing_leaf = {id for id in required if id not in blocks or blocks[id].review_status != "approved"}
    query = select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == run.document_version_id,
                BusinessConfirmation.kind == "key_content")
    if confirmation_refs is not None:
        query = query.where(BusinessConfirmation.id.in_(confirmation_refs))
    accepted, rejected, relations, observations, proofs = set(), set(), set(), set(), []
    for item in db.scalars(query):
        if item.scope.get("parse_run_id") != run.id or not confirmation_is_current(db, item):
            continue
        if item.conclusion == "rejected":
            rejected.update(item.scope.get("block_bindings", {}))
            rejected.update(item.scope.get("relation_bindings", {}))
            continue
        if item.conclusion != "accepted":
            continue
        accepted.update(item.scope.get("block_bindings", {}))
        relations.update(item.scope.get("relation_bindings", {}))
        observations.update(item.scope.get("observation_dispositions", {}))
        proofs.append(item.id)
    missing_critical = set(report["critical_refs"]) - accepted
    missing_relations = set(report["required_relation_refs"]) - relations
    missing_observations = set(report["observation_refs"]) - observations
    reasons = []
    for condition, reason in ((missing_leaf, "LEAF_REVIEW_INCOMPLETE"), (missing_critical, "CRITICAL_CONFIRMATION_MISSING"),
            (missing_relations, "RELATION_CONFIRMATION_MISSING"), (missing_observations, "OBSERVATION_DISPOSITION_MISSING"),
            (rejected, "SOURCE_REJECTED")):
        if condition:
            reasons.append(reason)
    return {"state": "rejected" if rejected else "pending" if reasons else "verified", "reasons": reasons,
        "missing_leaf_refs": sorted(missing_leaf), "missing_critical_refs": sorted(missing_critical),
        "missing_relation_refs": sorted(missing_relations), "missing_observation_refs": sorted(missing_observations),
        "confirmation_refs": sorted(proofs)}


def synchronize_source_status(db, run):
    if run and run.ir_schema_version and run.ir_artifact_id:
        run.status = "succeeded" if source_verification(db, run)["state"] == "verified" else "needs_review"
    return run


def processing_view(db, run):
    from ..governance import parse_run_was_published
    outputs = run.stage_checkpoint.get("outputs", {})
    report = run.stage_checkpoint.get("source_qc", {})
    current = source_qc_is_current(db, run) if report else False
    verified = source_verification(db, run) if report else {"state": "pending", "reasons": ["SOURCE_QC_NOT_READY"]}
    return {"ir_schema_version": run.ir_schema_version, "was_published": parse_run_was_published(db, run),
        "source_qc": {"artifact_ref": outputs.get("source_qc"), "status": ("stale" if report.get("status") == "passed" and not current
            else report.get("status", "not_run")), "current": current, "checker_revision": report.get("checker_revision"),
            "issues": report.get("issues", []), "checks": report.get("checks", {}),
            "critical_refs": report.get("critical_refs", {}), "required_relation_refs": report.get("required_relation_refs", []),
            "observation_refs": report.get("observation_refs", [])}, "source_verification": verified,
        "milestones": {"parsed": bool(outputs.get("raw_manifest")), "normalized": bool(outputs.get("normalized_ir")),
            "structured": bool(outputs.get("structured_ir")), "source_qc_passed": current, "source_verified": verified["state"] == "verified"}}


def relation_review_state(db, run, relation):
    from ..governance import confirmation_is_current
    from ..governance_models import BusinessConfirmation
    accepted = False
    for item in db.scalars(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == run.document_version_id,
                                                              BusinessConfirmation.kind == "key_content")):
        if (item.scope.get("parse_run_id") != run.id or item.scope.get("relation_bindings", {}).get(relation.id) != relation_binding(relation)
                or not confirmation_is_current(db, item)):
            continue
        if item.conclusion == "rejected":
            return "rejected"
        accepted |= item.conclusion == "accepted"
    return "confirmed" if accepted else "candidate"


def run_source_qc(db, run_id, settings):
    from ..parse_jobs import ParseServiceJob, _claim_job, _fence_attempt, _local_guard, _local_heartbeat
    from ..services import SafeError, audit
    from .quality import evaluate_source_qc
    run, job = db.get(ParseRun, run_id), db.get(ParseServiceJob, run_id)
    if not run or not job or not run.ir_artifact_id:
        raise SafeError("IR_NOT_READY", "结构来源尚未就绪", 409)
    if run.stage_checkpoint.get("source_qc"):
        # Reuse a report (including needs_review) only for its exact immutable
        # inputs and checker. Proof additions do not trigger QC or parsing.
        previous = run.stage_checkpoint["source_qc"]
        if (previous.get("checker_code_sha256") == checker_signature()
                and previous.get("projection_fingerprint") == projection_fingerprint(db, run.id)):
            load_structured_ir(db, run, job, settings)
            _read_json(db, _artifact(db, run.id, run.stage_checkpoint["outputs"]["source_qc"]), settings)
            db.rollback()
            return run
    attempt = _claim_job(db, job, run, allow_review=True)
    if attempt is None:
        return run
    try:
        _local_guard(db, run, job, attempt, settings, require_runtime=False)
        run.processing_stage = "qc"
        db.commit()
        ir = load_structured_ir(db, run, job, settings)
        _local_heartbeat(db, run, job, attempt, settings, require_runtime=False)
        outputs = run.stage_checkpoint["outputs"]
        report = evaluate_source_qc(ir, normalized_sha256=outputs["normalized_ir_sha256"],
            structured_sha256=outputs["structured_ir_sha256"], raw_manifest_sha256=outputs["raw_manifest_sha256"])
        report.update(checker_code_sha256=checker_signature(), projection_fingerprint=projection_fingerprint(db, run.id))
        db.commit()
        body = json.dumps(report, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        prepared = prepare_artifact_set(run.id, job.actor_id, settings, [{"raw": body, "kind": "qc_report",
            "logical_name": "qc/source/" + hashlib.sha256(body).hexdigest() + ".json",
            "producer": {"phase": "source_qc", "checker_revision": report["checker_revision"]},
            "derived_from_artifact_id": run.ir_artifact_id}])
        _local_guard(db, run, job, attempt, settings, require_runtime=False)
        frozen = bind_workspace(db, prepared)["artifacts"][0]
        run.stage_checkpoint = {**run.stage_checkpoint, "stage_state": "qc_" + report["status"], "source_qc": report,
            "outputs": {**outputs, "source_qc": frozen["artifact_id"], "source_qc_sha256": frozen["sha256"]}}
        run.status, run.error_code, job.lease_until = "needs_review", None, None
        audit(db, job.actor_id, "parse.source_qc", "parse_run", run.id, status=report["status"])
        db.commit()
    except (ArtifactError, ParseContractError, ProviderError, SafeError, SQLAlchemyError, OSError) as error:
        _stage_failure(db, run, job, attempt, error, "qc", _fence_attempt)
    return run
