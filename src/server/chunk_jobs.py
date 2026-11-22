"""Durable, independent chunk builds over a frozen source; no parser invocation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from .models import Chunk, ChunkBlock, ChunkSet, ContentBlock, Document, DocumentVersion, FileObject, KnowledgeBase, ParseArtifact, ParseRun
from .parsing.artifacts import ArtifactError, bind_workspace, prepare_artifact_set
from .parsing.ir_projection import _artifact, _read_json
from .parsing.local_pipeline import ParseContractError, fingerprint
from .parsing.processing import load_structured_ir, projection_fingerprint, source_qc_is_current
from .services import SafeError, audit, can_access_document, fence_management_authority

CHUNK_FIELDS = ("id", "chunk_set_id", "ordinal", "content", "content_hash", "token_count", "level", "parent_chunk_id",
                "structure_root_id", "kind", "eligibility", "required_relation_ids", "assembly", "support_group_refs")
MAPPING_FIELDS = ("chunk_id", "block_id", "chunk_set_id", "parse_run_id", "ordinal", "segments")


def _profile(revision):
    from .parsing.chunking import PROFILE, SPLITTER_VERSION
    if revision != SPLITTER_VERSION:
        raise SafeError("SPLITTER_PROFILE_UNSUPPORTED", "请选择已登记的切分配置", 422)
    return {**PROFILE, "splitter_code_sha256": hashlib.sha256(Path(__file__).with_name("parsing").joinpath("chunking.py").read_bytes()).hexdigest()}


def accept_chunk_set(db, actor, run, revision):
    version = db.get(DocumentVersion, run.document_version_id)
    document = db.get(Document, version.document_id)
    if not can_access_document(db, actor, document, "knowledge.manage"):
        raise SafeError("FORBIDDEN", "没有管理该资料范围的权限", 403)
    outputs = run.stage_checkpoint.get("outputs", {})
    if not run.ir_artifact_id or not outputs.get("source_qc") or run.status not in {"needs_review", "succeeded"}:
        raise SafeError("SOURCE_NOT_READY", "请先完成结构来源和源技术检查", 409)
    profile = {**_profile(revision), "source_qc_ref": outputs["source_qc"], "source_qc_sha256": outputs["source_qc_sha256"],
               "structured_ir_sha256": outputs["structured_ir_sha256"]}
    identity = fingerprint({"parse_run_id": run.id, "profile": profile})
    previous = db.scalar(select(ChunkSet).where(ChunkSet.parse_run_id == run.id, ChunkSet.config_hash == identity))
    if previous:
        return previous
    result = ChunkSet(parse_run_id=run.id, splitter_version=revision, status="building", splitter_config=profile,
        config_hash=identity, ir_content_hash=outputs["structured_ir_sha256"], tokenizer_revision=profile["tokenizer_revision"],
        stage_checkpoint={"actor_id": actor.id, "attempt": 0, "phase": "accepted", "outputs": {}})
    db.add(result)
    db.flush()
    from .governance import _changed
    _changed(db, actor, version, "chunk.candidate_accepted", affects_formal=False)
    audit(db, actor.id, "chunk.accepted", "chunk_set", result.id, parse_run_id=run.id, profile=revision)
    return result


def _guard(db, chunk_set, token=None):
    from .models import User, utc_now
    run = db.get(ParseRun, chunk_set.parse_run_id, populate_existing=True)
    version = db.get(DocumentVersion, run.document_version_id, populate_existing=True)
    document = db.get(Document, version.document_id, populate_existing=True)
    result = db.execute(update(Document).where(Document.id == document.id, Document.deleted_at.is_(None))
                        .values(revision=Document.revision).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise SafeError("NOT_FOUND", "资料当前不可用", 404)
    db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.id == document.knowledge_base_id).with_for_update(read=True))
    db.scalar(select(ChunkSet.id).where(ChunkSet.id == chunk_set.id).with_for_update())
    db.refresh(chunk_set)
    checkpoint = chunk_set.stage_checkpoint
    actor = db.get(User, checkpoint["actor_id"], populate_existing=True)
    if not actor:
        raise SafeError("CHUNK_AUTHORITY_REVOKED", "原处理账号已不可用", 409)
    fence_management_authority(db, actor, document.knowledge_base_id, document_id=document.id)
    if not can_access_document(db, actor, document, "knowledge.manage"):
        raise SafeError("CHUNK_AUTHORITY_REVOKED", "管理范围已变化", 409)
    outputs = run.stage_checkpoint.get("outputs", {})
    source = db.get(FileObject, version.source_file_id)
    if (not source or source.status != "available" or source.id != (run.stage_checkpoint.get("source_qc") or {}).get("source_file_id")
            or source.sha256 != (run.stage_checkpoint.get("source_qc") or {}).get("source_sha256")
            or chunk_set.ir_content_hash != outputs.get("structured_ir_sha256")
            or chunk_set.splitter_config.get("source_qc_ref") != outputs.get("source_qc")
            or chunk_set.splitter_config.get("source_qc_sha256") != outputs.get("source_qc_sha256")
            or run.stage_checkpoint.get("source_projection_hash") != projection_fingerprint(db, run.id)):
        raise SafeError("CHUNK_SOURCE_CHANGED", "源内容或技术检查已变化，请生成新的候选集合", 409)
    if chunk_set.config_hash != fingerprint({"parse_run_id": run.id, "profile": chunk_set.splitter_config}):
        raise SafeError("CHUNK_PROFILE_CHANGED", "切分配置身份已变化", 409)
    if token is not None:
        until = checkpoint.get("lease_until")
        if checkpoint.get("lease_token") != token or not until or datetime.fromisoformat(until) <= utc_now():
            raise SafeError("CHUNK_ATTEMPT_STALE", "处理租约已失效", 409)
    return actor, run


def chunk_projection_fingerprint(db, chunk_set):
    chunks = db.scalars(select(Chunk).where(Chunk.chunk_set_id == chunk_set.id).order_by(Chunk.ordinal)).all()
    mappings = db.scalars(select(ChunkBlock).where(ChunkBlock.chunk_set_id == chunk_set.id).order_by(ChunkBlock.chunk_id, ChunkBlock.ordinal, ChunkBlock.block_id)).all()
    return fingerprint({"chunks": [{key: getattr(row, key) for key in CHUNK_FIELDS} for row in chunks],
        "mappings": [{key: getattr(row, key) for key in MAPPING_FIELDS} for row in mappings],
        "support_groups": chunk_set.stage_checkpoint.get("support_groups", [])})


def chunk_set_qc_is_current(db, chunk_set):
    if not chunk_set or not chunk_set.config_hash or chunk_set.status != "ready":
        return False
    run = db.get(ParseRun, chunk_set.parse_run_id, populate_existing=True)
    if not source_qc_is_current(db, run):
        return False
    checkpoint, outputs = chunk_set.stage_checkpoint, chunk_set.stage_checkpoint.get("outputs", {})
    quality = checkpoint.get("quality", {})
    if (quality.get("status") != "passed" or checkpoint.get("projection_fingerprint") != chunk_projection_fingerprint(db, chunk_set)
            or chunk_set.ir_content_hash != run.stage_checkpoint["outputs"].get("structured_ir_sha256")
            or chunk_set.splitter_config.get("source_qc_ref") != run.stage_checkpoint["outputs"].get("source_qc")
            or chunk_set.splitter_config.get("source_qc_sha256") != run.stage_checkpoint["outputs"].get("source_qc_sha256")):
        return False
    try:
        registered = _profile(chunk_set.splitter_version)
    except SafeError:
        return False
    if any(chunk_set.splitter_config.get(key) != value for key, value in registered.items()):
        return False
    from .parsing.processing import _artifact_identities
    identities = _artifact_identities(db, (outputs.get(key) for key in ("manifest", "quality")))
    for key in ("manifest", "quality"):
        item = identities.get(outputs.get(key))
        if (not item or item.parse_run_id != run.id or not item.file_id or item.file_status != "available"
                or item.sha256 != outputs.get(key + "_sha256")):
            return False
        if key == "quality" and fingerprint(quality) != item.sha256:
            return False
    return True


def chunk_set_view(db, row):
    from .governance_models import ReviewSet
    version = db.get(DocumentVersion, db.get(ParseRun, row.parse_run_id).document_version_id)
    reviews = db.scalars(select(ReviewSet.id).where(ReviewSet.chunk_set_id == row.id)).all()
    return {"id": row.id, "parse_run_id": row.parse_run_id, "status": row.status, "splitter_version": row.splitter_version,
        "config_hash": row.config_hash, "tokenizer_revision": row.tokenizer_revision, "ir_content_hash": row.ir_content_hash,
        "quality_status": row.stage_checkpoint.get("quality", {}).get("status", "not_run"),
        "quality_current": chunk_set_qc_is_current(db, row) if row.config_hash else None,
        "quality_issues": row.stage_checkpoint.get("quality", {}).get("issues", []),
        "phase": row.stage_checkpoint.get("phase"), "error_code": row.stage_checkpoint.get("error_code"),
        "selected": version.selected_chunk_set_id == row.id, "review_set_ids": reviews,
        "child_count": row.stage_checkpoint.get("child_count", 0), "parent_count": row.stage_checkpoint.get("parent_count", 0)}


def run_chunk_job(db, chunk_set_id, settings, *, accepted_only=False):
    from .models import utc_now
    from .parse_jobs import ParseServiceJob
    from .parsing.chunking import build_chunks, load_tokenizer
    row = db.get(ChunkSet, chunk_set_id)
    if not row or not row.config_hash:
        raise SafeError("NOT_FOUND", "未找到切分候选任务", 404)
    if row.status == "ready":
        return row
    token = str(uuid4())
    try:
        actor, run = _guard(db, row)
        checkpoint = row.stage_checkpoint
        if row.status == "ready" or (accepted_only and (row.status != "building" or checkpoint.get("phase") not in {"accepted", "building"})):
            db.rollback()
            return row
        until = checkpoint.get("lease_until")
        if until and datetime.fromisoformat(until) > utc_now():
            db.rollback()
            return row
        profile = row.splitter_config
        registered = _profile(row.splitter_version)
        if any(profile.get(key) != value for key, value in registered.items()):
            raise SafeError("CHUNK_PROFILE_CHANGED", "切分规则已变化，请创建新候选", 409)
        row.stage_checkpoint = {**checkpoint, "attempt": checkpoint.get("attempt", 0) + 1,
            "lease_token": token, "lease_until": (utc_now() + timedelta(seconds=120)).isoformat(), "phase": "building"}
        row.status = "building"
        db.commit()
        job = db.get(ParseServiceJob, run.id)
        ir = load_structured_ir(db, run, job, settings)
        db.commit()
        if settings.local_bge_tokenizer is None:
            raise SafeError("CHUNK_TOKENIZER_NOT_READY", "固定BGE-M3 tokenizer尚未配置，候选已保留", 409)
        tokenizer = load_tokenizer(settings.local_bge_tokenizer)
        result = build_chunks(ir, chunk_set_id=row.id, profile=profile, tokenizer=tokenizer)
        result["quality"]["source_qc_status"] = run.stage_checkpoint["source_qc"]["status"]
        if run.stage_checkpoint["source_qc"]["status"] != "passed":
            result["quality"]["status"] = "needs_review"
        body = json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        quality_body = json.dumps(result["quality"], ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        prepared = prepare_artifact_set(run.id, actor.id, settings, [
            {"raw": body, "kind": "structured", "logical_name": f"chunks/{row.id}/manifest.json",
             "producer": {"chunk_set_id": row.id, "profile": row.splitter_version}, "derived_from_artifact_id": run.ir_artifact_id},
            {"raw": quality_body, "kind": "qc_report", "logical_name": f"chunks/{row.id}/qc.json",
             "producer": {"chunk_set_id": row.id, "phase": "collection_qc"}, "derived_from_artifact_id": run.ir_artifact_id}])
        _guard(db, row, token)
        manifest, quality = bind_workspace(db, prepared)["artifacts"]
        existing = db.scalars(select(Chunk.id).where(Chunk.chunk_set_id == row.id)).all()
        if existing:
            raise SafeError("CHUNK_PROJECTION_MISMATCH", "候选成员存在未完成的漂移，请核对原回执", 409)
        for level in ("parent", "child"):
            db.add_all(Chunk(parse_run_id=run.id, **{key: chunk.get(key) for key in CHUNK_FIELDS})
                       for chunk in result["chunks"] if chunk["level"] == level)
            db.flush()
        db.add_all(ChunkBlock(**{key: mapping[key] for key in MAPPING_FIELDS}) for mapping in result["chunk_blocks"])
        db.flush()
        row.quality_artifact_id = quality["artifact_id"]
        row.stage_checkpoint = {**row.stage_checkpoint, "lease_until": None, "phase": "ready", "error_code": None,
            "quality": result["quality"], "support_groups": result["support_groups"],
            "child_count": sum(chunk["level"] == "child" for chunk in result["chunks"]),
            "parent_count": sum(chunk["level"] == "parent" for chunk in result["chunks"]),
            "outputs": {"manifest": manifest["artifact_id"], "manifest_sha256": manifest["sha256"],
                        "quality": quality["artifact_id"], "quality_sha256": quality["sha256"]}}
        row.stage_checkpoint = {**row.stage_checkpoint, "projection_fingerprint": chunk_projection_fingerprint(db, row)}
        row.status = "ready"
        audit(db, actor.id, "chunk.completed", "chunk_set", row.id, source_run_id=run.id,
              child_count=row.stage_checkpoint["child_count"], quality=result["quality"]["status"])
        db.commit()
    except (SafeError, ArtifactError, ParseContractError, SQLAlchemyError, OSError) as error:
        db.rollback()
        row = db.get(ChunkSet, chunk_set_id, populate_existing=True)
        if row.status == "ready":
            return row  # Commit acknowledgement lost: inspect before retrying.
        if row.stage_checkpoint.get("lease_token") not in {None, token}:
            return row
        code = "CHUNK_PERSISTENCE_UNAVAILABLE" if isinstance(error, (SQLAlchemyError, OSError)) else (
            error.detail["code"] if isinstance(error, SafeError) else error.code)
        row.status = "building" if code in {"CHUNK_PERSISTENCE_UNAVAILABLE", "CHUNK_TOKENIZER_NOT_READY", "PARSE_ARTIFACT_STORAGE_UNAVAILABLE"} else "failed"
        row.stage_checkpoint = {**row.stage_checkpoint, "lease_until": None, "phase": "blocked" if row.status == "building" else "failed", "error_code": code}
        db.commit()
    return row
