"""Managed index generations, explicit active-pointer changes, and batch admission."""
from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Path, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from .db import get_db
from .embedding_jobs import ChildEmbeddingBatch, accept_child_embedding_batch
from .indexing import (KIND, PURPOSE, _selected_profile, child_generation, child_generation_view,
                       create_child_generation, current_child_manifest, lock_child_library, switch_child_generation)
from .models import ContentBlock, Document, DocumentVersion, IndexGeneration, IndexSource, KnowledgeBase
from .parsing.local_pipeline import fingerprint
from .provider_transport import ProviderError
from .service_use_models import ServiceSelection
from .services import (SafeError, _legacy_index_manifest, can_access_document, can_manage_kb, complete_idempotency,
                       create_index, current_session, document_creation_scopes, idempotency_record,
                       legacy_index_is_current, require_csrf, switch_legacy_index)
from .vector_store import ChunkEmbedding


PREFIX = "/knowledge-bases/{kb_id}"


class BuildIndexInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    embedding_profile_id: Literal["local-keyword"] | UUID = "local-keyword"


class SwitchIndexInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    generation_id: UUID
    expected_active_id: UUID | None
    expected_source_revision: Annotated[int, Field(strict=True, ge=0)]
    action: Literal["activate", "rollback"] = "activate"


class EmptyBatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _knowledge_base(db, kb_id):
    kb = db.get(KnowledgeBase, kb_id, populate_existing=True)
    if not kb or kb.status != "active":
        raise SafeError("NOT_FOUND", "资源不存在", 404)
    documents = db.scalars(select(Document).where(Document.knowledge_base_id == kb.id, Document.deleted_at.is_(None))
                           .order_by(Document.id).execution_options(populate_existing=True)).all()
    return kb, documents


def _full_access(db, actor, kb, documents):
    return bool(document_creation_scopes(db, actor, kb.id)
                and all(can_access_document(db, actor, item, "knowledge.manage") for item in documents))


def _management_scope(db, actor, kb_id):
    kb, documents = _knowledge_base(db, kb_id)
    if not _full_access(db, actor, kb, documents):
        raise SafeError("INDEX_MANAGEMENT_SCOPE_REQUIRED", "需要知识库和全库资料管理权限", 403)
    return kb


def _generation(db, kb_id, identifier):
    generation = db.get(IndexGeneration, identifier, populate_existing=True)
    if not generation or generation.knowledge_base_id != kb_id:
        raise SafeError("NOT_FOUND", "索引代际不存在", 404)
    return generation


def _is_current(db, kb, generation):
    if not generation or generation.status != "ready" or not generation.ready_at:
        return False
    if child_generation(generation):
        if generation.build_report.get("integrity", {}).get("status") != "passed":
            return False
        try:
            current_child_manifest(db, kb, generation)
        except (SafeError, ProviderError):
            return False
        return True
    return legacy_index_is_current(db, kb, generation)


def _status(db, actor, kb, *, can_manage):
    active = db.get(IndexGeneration, kb.active_generation_id, populate_existing=True) if kb.active_generation_id else None
    if active and active.knowledge_base_id != kb.id:
        active = None
    current = _is_current(db, kb, active)
    state = "ready" if current else "stale" if kb.active_generation_id else "not_ready"
    manifest_hash = active.build_report.get("manifest_hash") if active and can_manage else None
    tag = '"' + fingerprint({"kb": kb.id, "revision": kb.content_revision, "acl_revision": kb.acl_revision,
        "active": kb.active_generation_id, "source_revision": active.source_revision if active else None,
        "profile": active.embedding_profile_id if active else None,
        "manifest": active.build_report.get("manifest_hash") if active else None,
        "activation_count": active.build_report.get("activation_count", 0) if active else 0, "status": state}) + '"'
    version_ids = []
    if current:
        versions = db.execute(select(DocumentVersion.id, Document).join(Document, Document.id == DocumentVersion.document_id)
            .join(IndexSource, IndexSource.document_version_id == DocumentVersion.id)
            .where(IndexSource.index_generation_id == active.id).order_by(DocumentVersion.id)).all()
        version_ids = [identifier for identifier, document in versions
                       if can_access_document(db, actor, document, "knowledge.manage")]
    return {"knowledge_base_id": kb.id, "active_generation_id": active.id if active else None, "status": state,
            "revision": kb.content_revision, "content_revision": kb.content_revision,
            "source_revision": active.source_revision if active else None, "active_source_revision": active.source_revision if active else None,
            "embedding_profile_id": active.embedding_profile_id if active and can_manage else None,
            "manifest_hash": manifest_hash, "included_version_ids": version_ids, "etag": tag, "can_manage_index": can_manage}


def _generation_view(db, actor, kb, generation):
    report = generation.build_report
    child = child_generation(generation)
    integrity = report.get("integrity", {})
    blockers = [{"document_version_id": item.get("version_id"), "code": item["reason"]}
                for item in integrity.get("blockers", [])]
    stale = generation.source_revision != kb.content_revision
    batches = []
    if child:
        view = child_generation_view(db, generation)
        accepted = {row["batch_no"]: row for row in view["embedding_batches"]}
        allow_accept = (generation.status == "building" and generation.source_revision == kb.content_revision
                        and integrity.get("status") in {"pending", "incomplete"} and report.get("actor_id") == actor.id)
        if allow_accept:
            try:
                current_child_manifest(db, kb, generation)
            except (SafeError, ProviderError):
                stale, allow_accept = True, False
        for batch in report.get("manifest", {}).get("batches", []):
            number = batch["batch_no"]
            row = accepted.get(number)
            batches.append({**(row or {"id": None, "batch_no": number, "state": "not_accepted", "error_code": None}),
                            "can_accept": allow_accept and row is None})
        actual = db.scalar(select(func.count()).select_from(ChunkEmbedding)
                           .where(ChunkEmbedding.generation_id == generation.id)) if db.get_bind().dialect.name == "postgresql" else 0
    else:
        actual = db.scalar(select(func.count(ContentBlock.id)).join(IndexSource, IndexSource.parse_run_id == ContentBlock.parse_run_id)
                           .where(IndexSource.index_generation_id == generation.id))
    return {"id": generation.id, "knowledge_base_id": kb.id, "kind": KIND if child else "legacy",
            "embedding_profile_id": generation.embedding_profile_id,
            "status": "failed" if generation.status == "ready_partial" else generation.status,
            "stale": stale or generation.status == "ready" and not _is_current(db, kb, generation),
            "active": kb.active_generation_id == generation.id, "source_revision": generation.source_revision,
            "expected_chunk_count": generation.expected_chunk_count, "actual_chunk_count": actual or 0,
            "exclusions": blockers,
            "integrity": {**{key: integrity[key] for key in ("status", "error_code", "missing_child_refs",
                            "unexpected_child_refs", "invalid_child_refs") if key in integrity}, "blockers": blockers},
            "manifest_hash": report.get("manifest_hash"), "created_at": generation.created_at, "ready_at": generation.ready_at,
            "activation_count": report.get("activation_count", 0), "batch_count": len(batches), "embedding_batches": batches}


def _provider_denied(error):
    return SafeError(error.code, "当前嵌入服务、用途绑定或资料许可不满足受理条件", 409)


def register_index_routes(app: FastAPI) -> None:
    def actor_for(request, db, *, write=False):
        actor, session = current_session(request, db, app.state.settings, persist_heartbeat=False)
        if write:
            require_csrf(request, session)
        return actor

    def endpoints():
        return getattr(app.state, "service_endpoints", {})

    @app.get(PREFIX + "/index-status")
    def status(kb_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
        actor = actor_for(request, db)
        kb, documents = _knowledge_base(db, kb_id)
        manager = _full_access(db, actor, kb, documents)
        if not (manager or can_manage_kb(db, actor, kb.id)
                or any(can_access_document(db, actor, item, "knowledge.query")
                       or can_access_document(db, actor, item, "knowledge.manage") for item in documents)):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        result = _status(db, actor, kb, can_manage=manager)
        response.headers.update({"ETag": result["etag"], "Cache-Control": "private, no-store"})
        return result

    @app.get(PREFIX + "/embedding-profiles")
    def profiles(kb_id: str, request: Request, response: Response, db: Session = Depends(get_db),
                 cursor: UUID | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 25):
        actor = actor_for(request, db)
        kb = _management_scope(db, actor, kb_id)
        entries = []
        if db.get_bind().dialect.name == "postgresql":
            selection = db.get(ServiceSelection, PURPOSE, populate_existing=True)
            if selection and selection.enabled and selection.revision_id and cursor is None:
                try:
                    model = _selected_profile(db, selection.revision_id, endpoints())
                except ProviderError:
                    pass
                else:
                    entries.append({"id": model["service_revision_id"], "model_name": model["model"],
                                    "model_revision": None, "dimension": model["dimensions"],
                                    "distance_metric": model["distance"]})
        legacy = _legacy_index_manifest(db, kb)["expected_sources"]
        response.headers["Cache-Control"] = "private, no-store"
        return {"items": entries[:limit], "next_cursor": None,
                "legacy_mode_available": bool(legacy) and all(row["reason"] is None for row in legacy)}

    @app.get(PREFIX + "/index-generations")
    def generations(kb_id: str, request: Request, response: Response, db: Session = Depends(get_db),
                    cursor: UUID | None = None, limit: Annotated[int, Query(ge=1, le=100)] = 25):
        actor = actor_for(request, db)
        kb = _management_scope(db, actor, kb_id)
        statement = select(IndexGeneration).where(IndexGeneration.knowledge_base_id == kb.id)
        if cursor:
            prior = _generation(db, kb.id, str(cursor))
            statement = statement.where(or_(IndexGeneration.created_at < prior.created_at,
                and_(IndexGeneration.created_at == prior.created_at, IndexGeneration.id < prior.id)))
        items = db.scalars(statement.order_by(IndexGeneration.created_at.desc(), IndexGeneration.id.desc()).limit(limit + 1)).all()
        response.headers["Cache-Control"] = "private, no-store"
        return {"items": [_generation_view(db, actor, kb, row) for row in items[:limit]],
                "next_cursor": items[limit - 1].id if len(items) > limit else None}

    @app.get(PREFIX + "/index-generations/{generation_id}")
    def generation(kb_id: str, generation_id: UUID, request: Request, response: Response, db: Session = Depends(get_db)):
        actor = actor_for(request, db)
        kb = _management_scope(db, actor, kb_id)
        response.headers["Cache-Control"] = "private, no-store"
        return _generation_view(db, actor, kb, _generation(db, kb.id, str(generation_id)))

    @app.post(PREFIX + "/index-generations", status_code=202)
    def build(kb_id: str, payload: BuildIndexInput, request: Request, db: Session = Depends(get_db)):
        actor = actor_for(request, db, write=True)
        kb, _ = lock_child_library(db, kb_id, actor, request=request)
        record, replay = idempotency_record(db, actor, request, request.headers.get("Idempotency-Key", ""),
                                            payload.model_dump_json().encode())
        if replay:
            row = _generation(db, kb.id, record.resource_refs["index_generation_id"])
        else:
            profile = str(payload.embedding_profile_id)
            try:
                row = (create_index(db, kb, actor, request=request) if profile == "local-keyword" else
                       create_child_generation(db, kb, actor, embedding_profile_id=profile, endpoints=endpoints(), request=request))
            except ProviderError as error:
                raise _provider_denied(error) from None
            complete_idempotency(db, record, status_code=202, refs={"index_generation_id": row.id})
        result = {"index_generation_id": row.id, "resource_type": "index_generation", "status": row.status,
                  "embedding_profile_id": row.embedding_profile_id}
        db.commit()
        return result

    @app.put(PREFIX + "/active-index")
    def switch(kb_id: str, payload: SwitchIndexInput, request: Request, response: Response, db: Session = Depends(get_db)):
        actor = actor_for(request, db, write=True)
        kb, _ = lock_child_library(db, kb_id, actor, request=request)
        condition = request.headers.get("If-Match")
        if not condition:
            raise SafeError("PRECONDITION_REQUIRED", "需要 If-Match 并发条件", 428)
        record, replay = idempotency_record(db, actor, request, request.headers.get("Idempotency-Key", ""),
                                            payload.model_dump_json().encode())
        if replay:
            result = record.resource_refs["index_status"]
        else:
            previous = str(payload.expected_active_id) if payload.expected_active_id else None
            before = _status(db, actor, kb, can_manage=True)
            if (condition != before["etag"] or payload.expected_source_revision != kb.content_revision
                    or previous != kb.active_generation_id):
                raise SafeError("PRECONDITION_FAILED", "活动索引或资料修订已变化，请刷新后重试", 412)
            target = _generation(db, kb.id, str(payload.generation_id))
            change = switch_child_generation if child_generation(target) else switch_legacy_index
            try:
                change(db, kb, target.id, actor, expected_active_id=previous, expected_source_revision=payload.expected_source_revision,
                       action=payload.action, request=request)
            except ProviderError as error:
                raise _provider_denied(error) from None
            result = _status(db, actor, kb, can_manage=True)
            complete_idempotency(db, record, status_code=200, refs={"index_status": result})
        db.commit()
        response.headers.update({"ETag": result["etag"], "Cache-Control": "private, no-store"})
        return result

    @app.post(PREFIX + "/index-generations/{generation_id}/embedding-batches/{batch_no}", status_code=202)
    def embedding_batch(kb_id: str, generation_id: UUID, batch_no: Annotated[int, Path(ge=1)],
                        request: Request, payload: EmptyBatchInput | None = None, db: Session = Depends(get_db)):
        actor = actor_for(request, db, write=True)
        kb, _ = lock_child_library(db, kb_id, actor, request=request)
        target = _generation(db, kb.id, str(generation_id))
        if not child_generation(target) or db.get_bind().dialect.name != "postgresql":
            raise SafeError("INDEX_BATCH_NOT_FOUND", "未找到代际嵌入批次", 404)
        record, replay = idempotency_record(db, actor, request, request.headers.get("Idempotency-Key", ""), b"index-embedding-batch/1")
        if replay:
            row = db.get(ChildEmbeddingBatch, record.resource_refs["embedding_job_id"], populate_existing=True)
            if not row or (row.generation_id, row.batch_no, row.actor_id) != (target.id, batch_no, actor.id):
                raise SafeError("NOT_FOUND", "原嵌入批次回执不存在", 404)
        else:
            try:
                row = accept_child_embedding_batch(db, target.id, batch_no, actor, endpoints(), request=request)
            except ProviderError as error:
                raise _provider_denied(error) from None
            complete_idempotency(db, record, status_code=202, refs={"embedding_job_id": row.id})
        result = {"embedding_job_id": row.id, "index_generation_id": target.id, "batch_no": row.batch_no,
                  "state": row.state, "error_code": row.error_code, "data_scope": "synthetic_only"}
        db.commit()
        return result
