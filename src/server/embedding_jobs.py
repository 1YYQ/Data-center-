"""Exact published-source embedding jobs for the selected synthetic service."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import json

from sqlalchemy import DateTime, ForeignKey, JSON, String, UniqueConstraint, event, inspect, or_, select, update
from sqlalchemy.exc import DataError, IntegrityError
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import local_bge
from .config import Settings
from .governance import block_binding, can_read_version, qualification_snapshot
from .inference_adapters import BgeM3Adapter
from .models import Base, Chunk, ContentBlock, Document, DocumentVersion, KnowledgeBase, User, new_id, utc_now
from .provider_transport import ProviderError
from .service_admission import digest
from .service_use import BusinessTransport, LOCAL_EMBEDDING_REQUEST, pin_use, prove_input
from .service_use_models import ServiceUseCall, ServiceUsePin
from .services import SafeError, audit, can_access_document, fence_management_authority, selected_parse, version_is_formal
from .vector_store import ChildVectorBase, upsert_embedding


PURPOSE = "document.embed"
_LEASE = timedelta(minutes=2)
_MAX_BLOCKS = 32
_MAX_BATCH_BYTES = 32 * 1024


class EmbeddingServiceJob(Base):
    __tablename__ = "embedding_service_jobs"
    __table_args__ = (UniqueConstraint("version_id", "chunk_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"))
    chunk_id: Mapped[str] = mapped_column(ForeignKey("chunk_sets.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    pin_id: Mapped[str] = mapped_column(ForeignKey("service_use_pins.id"), unique=True)
    source_snapshot: Mapped[dict] = mapped_column(JSON)
    input_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ChildEmbeddingBatch(ChildVectorBase):
    __tablename__ = "index_embedding_batches"
    __table_args__ = (UniqueConstraint("generation_id", "batch_no", name="uq_index_embedding_batch"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    generation_id: Mapped[str] = mapped_column(String(36), nullable=False)
    batch_no: Mapped[int] = mapped_column(nullable=False)
    actor_id: Mapped[str] = mapped_column(String(36), nullable=False)
    pin_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    state: Mapped[str] = mapped_column(String(30), default="pending")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt: Mapped[int] = mapped_column(default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


@event.listens_for(ChildEmbeddingBatch, "before_update")
def _immutable_batch(mapper, connection, job):
    state = inspect(job)
    if any(state.attrs[field].history.has_changes() for field in
            ("generation_id", "batch_no", "actor_id", "pin_id", "input_hash")):
        raise ValueError("Accepted child index embedding bindings are immutable")


@event.listens_for(EmbeddingServiceJob, "before_update")
def _immutable_source(mapper, connection, job):
    state = inspect(job)
    if any(state.attrs[field].history.has_changes() for field in
            ("version_id", "run_id", "chunk_id", "actor_id", "pin_id", "source_snapshot", "input_hash")):
        raise ValueError("Accepted embedding source bindings are immutable")


@dataclass(frozen=True)
class _Source:
    run_id: str
    chunk_id: str
    snapshot: dict
    block_ids: tuple[str, ...]
    texts: list[str]
    payload: bytes


def canonical_embedding_input(texts: list[str]) -> bytes:
    """Encode ordered text exactly as the private fixture attestation expects."""
    return json.dumps(texts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _require_vector_database(db: Session) -> None:
    if db.get_bind().dialect.name != "postgresql":
        raise ProviderError("EMBEDDING_VECTOR_STORE_REQUIRED")


def _lock_document(db: Session, version_id: str) -> tuple[Document, DocumentVersion]:
    version = db.get(DocumentVersion, version_id, populate_existing=True)
    if not version:
        raise SafeError("NOT_FOUND", "未找到资料版本", 404)
    document = db.scalar(select(Document).where(Document.id == version.document_id, Document.deleted_at.is_(None))
        .with_for_update().execution_options(populate_existing=True))
    if not document:
        raise SafeError("NOT_FOUND", "未找到资料版本", 404)
    db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.id == document.knowledge_base_id)
        .with_for_update(read=True))
    db.refresh(version)
    return document, version


def _require_management(db: Session, actor: User, document: Document, *, request=None) -> None:
    current = fence_management_authority(db, actor, document.knowledge_base_id,
        document_id=document.id, request=request)
    if not can_access_document(db, current, document, "knowledge.manage"):
        raise SafeError("FORBIDDEN", "当前账号没有管理该资料范围的权限", 403)


def _read_source(db: Session, actor: User, document: Document, version: DocumentVersion) -> _Source:
    selected = selected_parse(db, version)
    if not selected or not version_is_formal(version):
        raise ProviderError("EMBEDDING_FORMAL_SOURCE_REQUIRED")
    run, chunk = selected
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)
        .order_by(ContentBlock.ordinal, ContentBlock.id).limit(_MAX_BLOCKS + 1)
        .execution_options(populate_existing=True)).all()
    if len(blocks) > _MAX_BLOCKS:
        raise ProviderError("EMBEDDING_BATCH_LIMIT")
    if not blocks or any(block.review_status != "approved" or not block.locator
                         or not block.text_content.strip() for block in blocks):
        raise ProviderError("EMBEDDING_FORMAL_SOURCE_REQUIRED")
    # Includes exact-version classification and current publication qualifications.
    if not can_read_version(db, actor, version, "knowledge.manage"):
        raise ProviderError("EMBEDDING_FORMAL_SOURCE_REQUIRED")
    texts = [block.text_content for block in blocks]
    try:
        if any(len(value.encode("utf-8")) > 8192 for value in texts):
            raise ProviderError("EMBEDDING_BATCH_LIMIT")
        payload = canonical_embedding_input(texts)
    except UnicodeEncodeError:
        raise ProviderError("EMBEDDING_SOURCE_INVALID") from None
    if len(payload) > _MAX_BATCH_BYTES:
        raise ProviderError("EMBEDDING_BATCH_LIMIT")
    snapshot = {
        "document_id": document.id,
        "knowledge_base_id": document.knowledge_base_id,
        "file_id": version.source_file_id,
        "qualification_hash": digest(qualification_snapshot(db, version)),
        "blocks": [[block.id, block_binding(block)] for block in blocks],
    }
    return _Source(run.id, chunk.id, snapshot, tuple(block.id for block in blocks), texts, payload)


def _match_source(job: EmbeddingServiceJob, source: _Source) -> None:
    if (job.run_id != source.run_id or job.chunk_id != source.chunk_id or job.source_snapshot != source.snapshot
            or job.input_hash != digest(source.payload)):
        raise ProviderError("EMBEDDING_SOURCE_CHANGED")


def accept_selected_embedding(db: Session, actor: User, version: DocumentVersion,
                              endpoints: dict, *, request=None) -> EmbeddingServiceJob:
    """Accept one job for the selected chunk without sending provider requests."""
    _require_vector_database(db)
    document, version = _lock_document(db, version.id)
    if not can_access_document(db, actor, document, "knowledge.manage"):
        raise SafeError("FORBIDDEN", "当前账号没有管理该资料范围的权限", 403)
    existing = db.scalar(select(EmbeddingServiceJob).where(EmbeddingServiceJob.version_id == version.id,
        EmbeddingServiceJob.chunk_id == version.selected_chunk_set_id).execution_options(populate_existing=True))
    if existing:
        _require_management(db, actor, document, request=request)
        if existing.actor_id != actor.id:
            raise SafeError("EMBEDDING_JOB_CONFLICT", "选用的资料批次已有嵌入任务", 409)
        _match_source(existing, _read_source(db, actor, document, version))
        return existing
    pin = pin_use(db, actor.id, PURPOSE, "version:" + version.id, endpoints)
    _require_management(db, actor, document, request=request)
    source = _read_source(db, actor, document, version)
    prove_input(db, pin, source.payload)
    job = EmbeddingServiceJob(version_id=version.id, run_id=source.run_id, chunk_id=source.chunk_id,
        actor_id=actor.id, pin_id=pin.id, source_snapshot=source.snapshot, input_hash=digest(source.payload))
    db.add(job)
    db.flush()
    audit(db, actor.id, "embedding.accepted", "embedding_job", job.id, document_version_id=version.id)
    return job


def _submission(db: Session, pin_id: str) -> ServiceUseCall | None:
    return db.scalar(select(ServiceUseCall).where(ServiceUseCall.pin_id == pin_id,
        ServiceUseCall.request_key.in_(("POST:api", LOCAL_EMBEDDING_REQUEST))).with_for_update()
        .execution_options(populate_existing=True))


def _stop(db: Session, job: EmbeddingServiceJob, state: str, code: str) -> None:
    job.state, job.error_code, job.lease_until, job.finished_at = state, code, None, utc_now()
    audit(db, job.actor_id, "embedding.execution_stopped", "embedding_job", job.id, state=state, error_code=code)
    db.commit()


def _claim(db: Session, job) -> int | None:
    now = utc_now()
    attempt = job.attempt + 1
    model = type(job)
    claimed = db.execute(update(model).where(model.id == job.id,
        model.attempt == job.attempt, model.state.in_(("pending", "running")),
        or_(model.lease_until.is_(None), model.lease_until <= now))
        .values(attempt=attempt, lease_until=now + _LEASE, state="running")
        .execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        db.rollback()
        return None
    db.refresh(job)
    prior = _submission(db, job.pin_id)
    if prior:
        if prior.state == "dispatching":
            prior.state, prior.safe_error = "unknown", "SERVICE_OUTCOME_UNKNOWN"
        if prior.state == "failed":
            _stop(db, job, "failed", prior.safe_error or "EMBEDDING_PROVIDER_FAILED")
        else:
            _stop(db, job, "reconciliation_required", "SERVICE_RECONCILIATION_REQUIRED")
        return None
    db.commit()
    return attempt


def _fence_attempt(db: Session, job, attempt: int) -> None:
    model = type(job)
    owned = db.scalar(select(model.id).where(model.id == job.id,
        model.attempt == attempt, model.state == "running", model.lease_until > utc_now()).with_for_update())
    if not owned:
        raise ProviderError("EMBEDDING_ATTEMPT_STALE")


def _record_failure(db: Session, job, attempt: int, code: str) -> None:
    db.rollback()
    try:
        _fence_attempt(db, job, attempt)
    except ProviderError:
        db.rollback()
        return
    prior = _submission(db, job.pin_id)
    if prior and prior.state == "dispatching":
        prior.state, prior.safe_error = "unknown", "SERVICE_OUTCOME_UNKNOWN"
    unresolved = prior and prior.state == "unknown"
    _stop(db, job, "reconciliation_required" if unresolved else "failed", code)


def _embed(transport, revision, texts):
    if revision.adapter == local_bge.ADAPTER:
        return transport.embed_local()
    if revision.adapter != "bge-m3-v1":
        raise ProviderError("SERVICE_ADMISSION_STALE")
    return BgeM3Adapter(transport, revision.model).embed(texts)


def run_embedding_job(db: Session, job_id: str, _settings: Settings, endpoints: dict) -> EmbeddingServiceJob:
    """Run one accepted job from the trusted worker and commit the batch atomically."""
    _require_vector_database(db)
    job = db.get(EmbeddingServiceJob, job_id, populate_existing=True)
    if not job:
        return run_child_embedding_batch(db, job_id, endpoints)
    attempt = _claim(db, job)
    if attempt is None:
        return job

    def source_guard():
        nonlocal document, version, source
        document, version = _lock_document(db, job.version_id)
        _fence_attempt(db, job, attempt)
        source = None

    def authority_check():
        _require_management(db, actor, document)

    def source_bytes():
        nonlocal source
        if source is None:
            source = _read_source(db, actor, document, version)
            _match_source(job, source)
        return source.payload

    document = version = source = None
    try:
        actor = db.get(User, job.actor_id, populate_existing=True)
        if not actor:
            raise ProviderError("SERVICE_ACTOR_REVOKED")
        pin = db.get(ServiceUsePin, job.pin_id, populate_existing=True)
        if (not pin or pin.actor_id != job.actor_id or pin.purpose != PURPOSE
                or pin.resource_key != "version:" + job.version_id):
            raise ProviderError("SERVICE_ADMISSION_STALE")
        transport = BusinessTransport(db, pin, endpoints, authority_check, source_bytes, source_guard=source_guard)
        revision, *_ = transport.check()
        result = _embed(transport, revision, source.texts)
        revision, *_ = transport.check()
        try:
            for block_id, values in zip(source.block_ids, result["vectors"], strict=True):
                upsert_embedding(db, block_id=block_id, version_id=job.version_id, parse_run_id=source.run_id,
                    chunk_set_id=source.chunk_id, provider=revision.adapter, model_revision=revision.id, values=values)
        except (ValueError, IntegrityError, DataError):
            raise ProviderError("EMBEDDING_VECTOR_PERSISTENCE_REJECTED") from None
        call = _submission(db, pin.id)
        if not call or call.state != "received":
            raise ProviderError("SERVICE_RECONCILIATION_REQUIRED")
        call.usage = result["usage"]
        job.result = {"count": len(source.block_ids), "dimensions": 1024, "service_revision_id": revision.id,
                      "model": revision.model, "usage": result["usage"]}
        job.state, job.error_code, job.lease_until, job.finished_at = "succeeded", None, None, utc_now()
        audit(db, job.actor_id, "embedding.completed", "embedding_job", job.id, count=len(source.block_ids))
        db.commit()
    except (ProviderError, SafeError) as error:
        _record_failure(db, job, attempt, error.code)
    return job


def child_batch_payload(db, manifest, batch_no):
    if type(batch_no) is not int or not 1 <= batch_no <= len(manifest["batches"]):
        raise SafeError("INDEX_BATCH_NOT_FOUND", "未找到代际嵌入批次", 404)
    batch = manifest["batches"][batch_no - 1]
    by_id = {member["id"]: member for member in manifest["members"]}
    entries = [by_id[identifier] for identifier in batch["child_ids"]]
    texts = []
    for entry in entries:
        chunk = db.get(Chunk, entry["id"], populate_existing=True)
        if (not chunk or chunk.level != "child" or chunk.eligibility != "candidate" or not chunk.content
                or chunk.content_hash != entry["content_hash"] or digest(chunk.content.encode("utf-8")) != entry["text_hash"]):
            raise ProviderError("EMBEDDING_SOURCE_CHANGED")
        texts.append(chunk.content)
    payload = canonical_embedding_input(texts)
    if batch["batch_no"] != batch_no or batch["input_hash"] != digest(payload):
        raise ProviderError("EMBEDDING_SOURCE_CHANGED")
    return entries, texts, payload


def _check_child_batch_authority(db, actor, entries):
    for version_id in {entry["version_id"] for entry in entries}:
        version = db.get(DocumentVersion, version_id, populate_existing=True)
        if not version or not can_read_version(db, actor, version, "knowledge.manage"):
            raise ProviderError("EMBEDDING_FORMAL_SOURCE_REQUIRED")


def _child_batch_resource(generation_id, batch_no):
    return f"index:{generation_id}:{batch_no}"


def accept_child_embedding_batch(db, generation_id, batch_no, actor, endpoints, *, request=None):
    """Accept one frozen array of child texts through the existing selected service."""
    from .indexing import current_child_manifest, lock_child_generation

    _require_vector_database(db)
    kb, generation = lock_child_generation(db, generation_id, actor, request=request)
    if generation.build_report["actor_id"] != actor.id or generation.status != "building":
        raise SafeError("INDEX_BATCH_NOT_ACCEPTABLE", "代际已变化或当前账号不是原发起人", 409)
    manifest = current_child_manifest(db, kb, generation)
    entries, _, payload = child_batch_payload(db, manifest, batch_no)
    _check_child_batch_authority(db, actor, entries)
    existing = db.scalar(select(ChildEmbeddingBatch).where(ChildEmbeddingBatch.generation_id == generation.id,
        ChildEmbeddingBatch.batch_no == batch_no).execution_options(populate_existing=True))
    if existing:
        if existing.input_hash != digest(payload) or existing.actor_id != actor.id:
            raise ProviderError("EMBEDDING_SOURCE_CHANGED")
        return existing
    pin = pin_use(db, actor.id, PURPOSE, _child_batch_resource(generation.id, batch_no), endpoints)
    profile = manifest["profile"]
    if (pin.revision_id, pin.binding_hash) != (profile["service_revision_id"], profile["service_binding_hash"]):
        raise ProviderError("INDEX_EMBEDDING_PROFILE_CHANGED")
    prove_input(db, pin, payload)
    job = ChildEmbeddingBatch(generation_id=generation.id, batch_no=batch_no, actor_id=actor.id,
                              pin_id=pin.id, input_hash=digest(payload))
    db.add(job)
    db.flush()
    audit(db, actor.id, "embedding.index_batch_accepted", "embedding_job", job.id, index_generation_id=generation_id)
    return job


def run_child_embedding_batch(db, job_id, endpoints):
    from .indexing import current_child_manifest, finalize_child_generation, lock_child_generation, persist_child_embeddings

    _require_vector_database(db)
    job = db.get(ChildEmbeddingBatch, job_id, populate_existing=True)
    if not job:
        raise SafeError("NOT_FOUND", "未找到嵌入任务", 404)
    attempt = _claim(db, job)
    if attempt is None:
        return job

    def source_guard():
        nonlocal generation, profile, entries, texts, payload
        kb, generation = lock_child_generation(db, job.generation_id, actor)
        if generation.status != "building" or generation.build_report["actor_id"] != job.actor_id:
            raise ProviderError("INDEX_BATCH_NOT_ACCEPTABLE")
        _fence_attempt(db, job, attempt)
        manifest = current_child_manifest(db, kb, generation)
        profile = manifest["profile"]
        entries, texts, payload = child_batch_payload(db, manifest, job.batch_no)
        if digest(payload) != job.input_hash or (pin.revision_id, pin.binding_hash) != (
                profile["service_revision_id"], profile["service_binding_hash"]):
            raise ProviderError("INDEX_EMBEDDING_PROFILE_CHANGED")

    generation = profile = entries = texts = payload = None
    try:
        actor = db.get(User, job.actor_id, populate_existing=True)
        pin = db.get(ServiceUsePin, job.pin_id, populate_existing=True)
        if (not actor or not pin or pin.actor_id != job.actor_id or pin.purpose != PURPOSE
                or pin.resource_key != _child_batch_resource(job.generation_id, job.batch_no)):
            raise ProviderError("SERVICE_ADMISSION_STALE")
        transport = BusinessTransport(db, pin, endpoints, lambda: _check_child_batch_authority(db, actor, entries),
                                      lambda: payload, source_guard=source_guard)
        revision, *_ = transport.check()
        result = _embed(transport, revision, texts)
        revision, *_ = transport.check()
        try:
            persist_child_embeddings(db, generation, entries, result["vectors"])
        except (ValueError, IntegrityError, DataError):
            raise ProviderError("EMBEDDING_VECTOR_PERSISTENCE_REJECTED") from None
        call = _submission(db, pin.id)
        if not call or call.state != "received":
            raise ProviderError("SERVICE_RECONCILIATION_REQUIRED")
        call.usage = result["usage"]
        job.result = {"count": len(entries), "dimensions": 1024, "service_revision_id": revision.id,
                      "model": revision.model, "usage": result["usage"]}
        job.state, job.error_code, job.lease_until, job.finished_at = "succeeded", None, None, utc_now()
        audit(db, job.actor_id, "embedding.index_batch_completed", "embedding_job", job.id, count=len(entries))
        db.flush()
        succeeded = db.scalars(select(ChildEmbeddingBatch.batch_no).where(ChildEmbeddingBatch.generation_id == generation.id,
                               ChildEmbeddingBatch.state == "succeeded")).all()
        if len(succeeded) == len(generation.build_report["manifest"]["batches"]):
            finalize_child_generation(db, generation, actor)
        db.commit()
    except (ProviderError, SafeError) as error:
        _record_failure(db, job, attempt, error.code)
    return job
