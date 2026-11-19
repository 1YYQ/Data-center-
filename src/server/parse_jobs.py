"""Selected-service parsing for an existing document version and its original."""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, and_, or_, select, update
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.types import JSON
from sqlalchemy.orm import Mapped, Session, mapped_column
from sqlalchemy.exc import SQLAlchemyError

from .config import Settings
from .models import Base, ChunkSet, ContentBlock, Document, DocumentVersion, FileObject, KnowledgeBase, ParseRun, User, new_id, utc_now
from .parsing.mineru import MinerUAdapter
from .provider_transport import ProviderError, ProviderTransport


PURPOSE = "document.parse"
_LEASE = timedelta(seconds=120)
_MAX_SOURCE_BYTES = 65_536
_MAX_ARCHIVE_BYTES = 1024 * 1024
_ACTIONABLE = frozenset({"pending", "running", "waiting_external"})
_UNSETTLED = _ACTIONABLE | {"reconciliation_required"}


def _local_runtime_identity(settings: Settings) -> dict:
    """Bind the configured executable and installed model manifests, no secrets."""
    executable = settings.local_mineru_executable
    manifests = []
    if settings.local_mineru_home and settings.local_mineru_home.is_dir():
        home = settings.local_mineru_home.resolve()
        for path in sorted(home.rglob(".mineru_complete"))[:128]:
            resolved = path.resolve()
            if home in resolved.parents and path.stat().st_size <= 1024 * 1024:
                manifests.append({"name": path.relative_to(home).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"executable_sha256": hashlib.sha256(executable.read_bytes()).hexdigest() if executable and executable.is_file() else None,
            "model_installations": manifests,
            "runner_sha256": hashlib.sha256(Path(__file__).with_name("parsing").joinpath("standard_runner.py").read_bytes()).hexdigest()}


def parse_run_view(run: ParseRun, db: Session | None = None) -> dict:
    checkpoint = run.stage_checkpoint or {}
    result = {"id": run.id, "status": run.status, "origin": run.origin, "error_code": run.error_code,
        "processing_stage": run.processing_stage, "stage_state": checkpoint.get("stage_state", run.status),
        "processed_page_count": checkpoint.get("processed_page_count", 0), "total_page_count": checkpoint.get("total_page_count"),
        "quality_state": "unverified", "actual_profile": run.actual_profile, "ir_schema_version": run.ir_schema_version,
        "parent_run_id": run.parent_run_id, "artifact_manifest_ref": checkpoint.get("outputs", {}).get("raw_manifest"),
        "created_at": run.created_at.isoformat() if run.created_at else None}
    if db is not None and run.ir_schema_version:
        from .parsing.processing import processing_view
        state = processing_view(db, run)
        result.update(state, quality_state=state["source_verification"]["state"])
    return result


def accept_local_parse(db: Session, actor: User, version: DocumentVersion, file: FileObject, settings: Settings) -> ParseRun:
    """Only durable acceptance; no parsing, model execution or network in HTTP."""
    from .parsing.local_pipeline import PROFILE, fingerprint
    from .services import SafeError, audit

    if (version.source_file_id != file.id or file.status != "available" or file.storage_name != "local"
            or file.mime_type != "application/pdf" or not 0 < file.byte_size <= settings.max_upload_bytes):
        raise SafeError("PARSE_SOURCE_UNAVAILABLE", "原件当前不可用于解析", 409)
    config = {"profile": dict(PROFILE), "runtime": _local_runtime_identity(settings)}
    prior = db.execute(select(ParseRun, ParseServiceJob).join(ParseServiceJob, ParseServiceJob.parse_run_id == ParseRun.id)
        .where(ParseRun.document_version_id == version.id, ParseServiceJob.source_file_id == file.id,
               ParseServiceJob.source_sha256 == file.sha256, ParseRun.status.in_(_UNSETTLED))).first()
    if prior:
        if prior[1].actor_id == actor.id and prior[1].mode == "local":
            old_run, old_job = prior
            if old_run.request_config == config:
                return old_run
            if old_run.status in {"pending", "waiting_external"} and old_job.lease_until is None:
                old_run.status, old_run.error_code = "failed", "PARSE_PROFILE_CHANGED"
                old_run.stage_checkpoint = {**old_run.stage_checkpoint, "stage_state": "superseded_profile"}
                audit(db, actor.id, "parse.profile_superseded", "parse_run", old_run.id)
            else:
                raise SafeError("PARSE_RECONCILIATION_REQUIRED", "原任务的执行状态须先对账，不能复用已变化的解析配置", 409)
        else:
            raise SafeError("PARSE_RECONCILIATION_REQUIRED", "已有未结束的解析任务，请先核对原任务", 409)
    identity = {"version_id": version.id, "source_file_id": file.id, "source_sha256": file.sha256, "request_config": config}
    run = ParseRun(document_version_id=version.id, origin="local", status="pending", provider="mineru-kit",
        output_revision="dc-pdf-pipeline-v1", adapter_version=PROFILE["adapter_version"], recovery_version="dc-pdf-recovery/1",
        request_config=config, raw_contract={"schema": "docvortex.middle", "schema_version": "2.0", "api_family": "local-sdk-save"},
        input_fingerprint=fingerprint(identity), stage_checkpoint={"source_sha256": file.sha256, "outputs": {}, "attempts": []})
    db.add(run)
    db.flush()
    db.add(ParseServiceJob(parse_run_id=run.id, mode="local", actor_id=actor.id,
        source_file_id=file.id, source_sha256=file.sha256))
    audit(db, actor.id, "parse.accepted", "parse_run", run.id, provider="local", profile=PROFILE["revision"])
    return run


def accept_derived_parse(db, actor, parent, payload):
    """Persist a correction/reprocess request; all archive I/O belongs to Worker."""
    from copy import deepcopy
    from .parsing.ir_projection import _source_binding
    from .services import SafeError, audit
    parent_job = db.get(ParseServiceJob, parent.id)
    if (not parent_job or parent_job.mode != "local" or not parent.ir_schema_version
            or parent.status not in {"needs_review", "succeeded"}):
        raise SafeError("PARSE_RAW_NOT_READY", "请先完成可回查的原始解析和规范化来源", 409)
    source = _source_binding(db, parent, parent_job)
    run = ParseRun(document_version_id=parent.document_version_id, parent_run_id=parent.id,
        origin="manual" if payload.get("blocks") else "reprocess", status="pending", provider=parent.provider,
        request_config=deepcopy(parent.request_config), input_fingerprint=parent.input_fingerprint,
        actual_profile=deepcopy(parent.actual_profile), raw_contract=deepcopy(parent.raw_contract),
        engine_version=parent.engine_version, model_revision=parent.model_revision, output_revision=parent.output_revision,
        adapter_version=parent.adapter_version, recovery_version=parent.recovery_version,
        stage_checkpoint={"source_sha256": source.sha256, "stage_state": "accepted", "derivation": True,
            "correction_payload": deepcopy(payload), "attempts": [], "outputs": {}})
    db.add(run)
    db.flush()
    db.add(ParseServiceJob(parse_run_id=run.id, mode="local", actor_id=actor.id, source_file_id=source.id, source_sha256=source.sha256))
    audit(db, actor.id, "parse.derivation_accepted", "parse_run", run.id, parent_run_id=parent.id)
    return run


class ParseServiceJob(Base):
    __tablename__ = "parse_service_jobs"

    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), primary_key=True)
    pin_id: Mapped[str | None] = mapped_column(ForeignKey("service_use_pins.id"), unique=True, nullable=True)
    mode: Mapped[str] = mapped_column(String(30), default="selected_service", server_default="selected_service")
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    source_file_id: Mapped[str] = mapped_column(ForeignKey("file_objects.id"))
    source_sha256: Mapped[str] = mapped_column(String(64))
    raw_result_file_id: Mapped[str | None] = mapped_column(ForeignKey("file_objects.id"), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpoint: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")


class _ArchiveCapture:
    def __init__(self, transport: ProviderTransport, workspace: Path | None = None, identity=None):
        self.transport = transport
        self.body: bytes | None = None
        self.workspace = workspace
        self.identity = identity

    def persist(self, raw: bytes) -> None:
        if len(raw) > _MAX_ARCHIVE_BYTES:
            raise ProviderError("PARSE_ARCHIVE_LIMIT")
        self.body = raw
        if self.workspace is None:
            return
        try:
            with (self.workspace / "result.zip").open("xb") as output:
                output.write(raw)
                output.flush()
                os.fsync(output.fileno())
            if self.identity is not None:
                _write_receipt(self.workspace / "cloud_receipt.json",
                    {**self.identity(), "sha256": hashlib.sha256(raw).hexdigest(), "byte_size": len(raw)})
        except OSError:
            raise ProviderError("PARSE_ARCHIVE_STORAGE_UNAVAILABLE") from None

    def request(self, method, target, *, body=b"", purpose="api", content_type="application/json") -> bytes:
        response = self.transport.request(method, target, body=body, purpose=purpose, content_type=content_type)
        if purpose == "download" and self.body is None:
            self.persist(response)
        return response


def _attach_archive(db: Session, job: ParseServiceJob, settings: Settings, raw: bytes, *, status: str) -> None:
    """Retain the exact download, including invalid ZIPs in quarantine."""
    from .parsing.artifacts import ArtifactError, archive_bytes
    try:
        artifact = archive_bytes(db, job.parse_run_id, job.actor_id, settings, raw, kind="raw_archive",
            logical_name=f"attempt/{job.attempt}/raw/result.zip", producer={"provider": "mineru-cloud", "epoch": job.attempt}, status=status)
    except ArtifactError:
        raise ProviderError("PARSE_ARCHIVE_STORAGE_UNAVAILABLE") from None
    # If the process stops before DB commit, the unreferenced object remains
    # in private storage. No source or artifact route can resolve its key.
    job.raw_result_file_id = artifact.file_id


def _source_bytes(file: FileObject | None, sha256: str, settings: Settings) -> bytes:
    from .services import SafeError, file_path

    if (not file or file.status != "available" or file.storage_name != "local"
            or file.mime_type != "application/pdf" or file.sha256 != sha256
            or not 0 < file.byte_size <= _MAX_SOURCE_BYTES):
        raise ProviderError("PARSE_SOURCE_UNAVAILABLE")
    try:
        with file_path(file, settings).open("rb") as source:
            body = source.read(_MAX_SOURCE_BYTES + 1)
    except (OSError, SafeError):
        raise ProviderError("PARSE_SOURCE_UNAVAILABLE") from None
    if (len(body) != file.byte_size or not body.startswith(b"%PDF-")
            or hashlib.sha256(body).hexdigest() != sha256):
        raise ProviderError("PARSE_SOURCE_CHANGED")
    return body


def accept_selected_parse(db: Session, actor: User, version: DocumentVersion, file: FileObject,
                          settings: Settings, endpoints: dict) -> ParseRun:
    """Accept one unresolved run per source while the caller holds the parent document lock."""
    from .service_use import pin_use, prove_input
    from .service_use_models import ServiceUseCall
    from .services import audit

    if version.source_file_id != file.id:
        raise ProviderError("PARSE_SOURCE_CHANGED")
    body = _source_bytes(file, file.sha256, settings)
    uncertain_call = select(ServiceUseCall.id).where(ServiceUseCall.pin_id == ParseServiceJob.pin_id,
        ServiceUseCall.state.in_(("unknown", "dispatching"))).exists()
    prior = db.execute(select(ParseRun, ParseServiceJob.actor_id, ParseServiceJob.mode).join(ParseServiceJob,
        ParseServiceJob.parse_run_id == ParseRun.id).where(ParseRun.document_version_id == version.id,
        ParseServiceJob.source_file_id == file.id, ParseServiceJob.source_sha256 == file.sha256,
        or_(ParseRun.status.in_(_UNSETTLED), and_(ParseRun.status == "failed", uncertain_call)))
        .order_by(ParseRun.created_at, ParseRun.id).limit(1)).first()
    if prior:
        run, owner_id, prior_mode = prior
        if owner_id == actor.id and prior_mode == "selected_service":
            return run
        raise ProviderError("PARSE_RECONCILIATION_REQUIRED")
    kb_id = db.scalar(select(Document.knowledge_base_id).where(Document.id == version.document_id))
    db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.id == kb_id).with_for_update(read=True))
    pin = pin_use(db, actor.id, PURPOSE, "version:" + version.id, endpoints)
    prove_input(db, pin, body)
    run = ParseRun(document_version_id=version.id, origin="provider", status="pending",
                   output_revision="mineru-v4-legacy-v1")
    db.add(run)
    db.flush()
    db.add(ParseServiceJob(parse_run_id=run.id, pin_id=pin.id, actor_id=actor.id,
                           source_file_id=file.id, source_sha256=file.sha256))
    audit(db, actor.id, "parse.accepted", "parse_run", run.id, provider="mineru", scope="synthetic_only")
    return run


def _has_submit_intent(db: Session, pin_id: str) -> bool:
    from .service_use_models import ServiceUseCall

    return db.scalar(select(ServiceUseCall.id).where(ServiceUseCall.pin_id == pin_id,
        ServiceUseCall.method == "POST").limit(1)) is not None


def _claim_job(db: Session, job: ParseServiceJob, run: ParseRun, *, allow_review: bool = False) -> int | None:
    from .service_use_models import ServiceUseCall

    actionable = _ACTIONABLE | ({"needs_review"} if allow_review else set())
    if run.status not in actionable:
        db.rollback()
        return None
    now = utc_now()
    attempt = job.attempt + 1
    result = db.execute(update(ParseServiceJob).where(ParseServiceJob.parse_run_id == run.id,
        ParseServiceJob.attempt == job.attempt,
        or_(ParseServiceJob.lease_until.is_(None), ParseServiceJob.lease_until <= now))
        .values(attempt=attempt, lease_until=now + _LEASE).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        db.rollback()
        return None
    db.refresh(run)
    db.refresh(job)
    if run.status not in actionable:
        db.rollback()
        return None
    # A prior worker can die between recording intent and receiving a reply.
    if job.mode == "selected_service":
        db.execute(update(ServiceUseCall).where(ServiceUseCall.pin_id == job.pin_id,
            ServiceUseCall.state == "dispatching").values(state="unknown", safe_error="SERVICE_OUTCOME_UNKNOWN")
            .execution_options(synchronize_session=False))
    if job.mode == "selected_service" and not run.provider_task_id and _has_submit_intent(db, job.pin_id):
        run.status, run.error_code = "reconciliation_required", "SERVICE_RECONCILIATION_REQUIRED"
        job.lease_until = None
        db.commit()
        return None
    run.status, run.error_code = "running", None
    db.commit()
    return attempt


def _fence_attempt(db: Session, run_id: str, attempt: int) -> None:
    result = db.execute(update(ParseServiceJob).where(ParseServiceJob.parse_run_id == run_id,
        ParseServiceJob.attempt == attempt, ParseServiceJob.lease_until > utc_now())
        .values(attempt=ParseServiceJob.attempt).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ProviderError("PARSE_ATTEMPT_STALE")


def _fence_source(db: Session, run: ParseRun, job: ParseServiceJob, attempt: int) -> DocumentVersion:
    version = db.get(DocumentVersion, run.document_version_id, populate_existing=True)
    if version is None:
        raise ProviderError("PARSE_SOURCE_UNAVAILABLE")
    # Match legacy document maintenance: serialize first on the live parent.
    result = db.execute(update(Document).where(Document.id == version.document_id, Document.deleted_at.is_(None))
        .values(revision=Document.revision).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise ProviderError("PARSE_SOURCE_UNAVAILABLE")
    db.refresh(version)
    document = db.get(Document, version.document_id, populate_existing=True)
    db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.id == document.knowledge_base_id).with_for_update(read=True))
    if version.source_file_id != job.source_file_id:
        raise ProviderError("PARSE_SOURCE_CHANGED")
    _fence_attempt(db, run.id, attempt)
    db.refresh(run)
    db.refresh(job)
    if job.cancel_requested or run.status == "cancelled":
        raise ProviderError("PARSE_CANCELLED")
    if run.status != "running":
        raise ProviderError("PARSE_ATTEMPT_STALE")
    return version


def _fence_authority(db: Session, run: ParseRun, job: ParseServiceJob) -> None:
    from .services import can_access_document, fence_management_authority

    version = db.get(DocumentVersion, run.document_version_id)
    document = db.get(Document, version.document_id)
    actor = db.get(User, job.actor_id, populate_existing=True)
    if actor is None:
        raise ProviderError("PARSE_AUTHORITY_REVOKED")
    fence_management_authority(db, actor, document.knowledge_base_id, document_id=document.id)
    if not can_access_document(db, actor, document, "knowledge.manage"):
        raise ProviderError("PARSE_AUTHORITY_REVOKED")


def _record_failure(db: Session, run: ParseRun, job: ParseServiceJob, attempt: int, code: str,
                    settings: Settings, invalid_archive: bytes | None) -> None:
    from .services import audit

    db.rollback()
    try:
        _fence_attempt(db, run.id, attempt)
    except ProviderError:
        db.rollback()
        return
    db.refresh(run)
    db.refresh(job)
    if run.status != "running":
        db.rollback()
        return
    if not run.provider_task_id and _has_submit_intent(db, job.pin_id):
        state = "reconciliation_required"
    elif run.provider_task_id and code in {"SERVICE_OUTCOME_UNKNOWN", "PARSE_ARCHIVE_STORAGE_UNAVAILABLE", "PARSE_PERSISTENCE_UNAVAILABLE"}:
        state = "waiting_external"
    else:
        state = "failed"
    if invalid_archive is not None:
        try:
            _attach_archive(db, job, settings, invalid_archive, status="quarantined")
        except ProviderError as error:
            state, code = "waiting_external", error.code
    run.status, run.error_code, job.lease_until = state, code, None
    audit(db, job.actor_id, "parse.execution_stopped", "parse_run", run.id, state=state, error_code=code)
    db.commit()


def run_parse_job(db: Session, run_id: str, settings: Settings, endpoints: dict) -> ParseRun:
    """Claim and advance an accepted parse once from the trusted worker process."""
    from .service_use import BusinessTransport, check_pin, prove_input
    from .service_use_models import ServiceUsePin
    from .services import SafeError, audit

    job = db.get(ParseServiceJob, run_id, populate_existing=True)
    run = db.get(ParseRun, run_id, populate_existing=True)
    if not job or not run:
        raise SafeError("NOT_FOUND", "未找到选用服务的解析任务", 404)
    if job.mode == "local":
        if (run.stage_checkpoint or {}).get("outputs", {}).get("raw_manifest"):
            raw = run
        elif run.stage_checkpoint.get("derivation") is True:
            raw = run_derived_parse_job(db, run_id, settings)
        else:
            raw = run_local_parse_job(db, run_id, settings)
        if not (raw.stage_checkpoint or {}).get("outputs", {}).get("raw_manifest") or raw.status in {"cancelled", "failed", "reconciliation_required"}:
            return raw
        from .parsing.ir_projection import normalize_parse_run
        from .parsing.processing import structure_parse_run, run_source_qc, synchronize_source_status
        for name, advance in (("normalized_ir", normalize_parse_run), ("structured_ir", structure_parse_run),
                              ("source_qc", run_source_qc)):
            # After a mid-stage interruption the existing stage loaders retain
            # the running status. Let the next unfinished stage claim its own lease.
            if (raw.status in _ACTIONABLE and name != "source_qc"
                    and raw.stage_checkpoint.get("outputs", {}).get(name)):
                continue
            raw = advance(db, run_id, settings)
            if raw.status not in {"needs_review", "succeeded"}:
                return raw
        synchronize_source_status(db, raw)
        db.commit()
        return raw
    attempt = _claim_job(db, job, run)
    if attempt is None:
        return run

    def source_bytes():
        file = db.scalar(select(FileObject).where(FileObject.id == job.source_file_id)
            .with_for_update(read=True).execution_options(populate_existing=True))
        return _source_bytes(file, job.source_sha256, settings)

    def source_guard():
        return _fence_source(db, run, job, attempt)

    def authority_check():
        _fence_authority(db, run, job)

    invalid_archive = None
    from .parsing.artifacts import ArtifactError, work_directory
    try:
        pin = db.get(ServiceUsePin, job.pin_id, populate_existing=True)
        if (not pin or pin.actor_id != job.actor_id or pin.purpose != PURPOSE
                or pin.resource_key != "version:" + run.document_version_id):
            raise ProviderError("SERVICE_ADMISSION_STALE")
        transport = BusinessTransport(db, pin, endpoints, authority_check=authority_check,
                                      source_bytes=source_bytes, source_guard=source_guard)
        archive = _ArchiveCapture(transport, work_directory(settings, run.id, attempt),
            lambda: {"run_id": run.id, "pin_id": job.pin_id, "batch_id": run.provider_task_id,
                     "source_sha256": job.source_sha256})
        transport.download_sink = archive.persist
        provider = MinerUAdapter(archive)

        def checked_source():
            version = source_guard()
            check_pin(db, pin, endpoints)
            authority_check()
            body = source_bytes()
            prove_input(db, pin, body)
            return version, body

        def checkpoint(batch_id: str):
            checked_source()
            if run.provider_task_id is not None:
                raise ProviderError("PARSE_BATCH_ALREADY_RECORDED")
            run.provider_task_id = batch_id
            db.commit()

        batch_id = run.provider_task_id
        if not batch_id:
            _, body = checked_source()
            batch_id = provider.submit(body, run.id, checkpoint=checkpoint)
        try:
            retained = _retained_cloud_archive(settings, job, run, attempt)
            if retained is not None:
                checked_source()
                archive.body = retained
                blocks = provider.parse_archive(retained)
            else:
                blocks = provider.poll(batch_id, run.id)
        except ProviderError:
            invalid_archive = archive.body
            raise
        version, _ = checked_source()
        if blocks is None:
            run.status = "waiting_external"
        else:
            if archive.body is None:
                raise ProviderError("PARSE_ARCHIVE_UNAVAILABLE")
            _attach_archive(db, job, settings, archive.body, status="available")
            for ordinal, (text, locator, kind) in enumerate(blocks, 1):
                db.add(ContentBlock(parse_run_id=run.id, ordinal=ordinal, text_content=text,
                    locator=locator, kind=kind, review_status="unreviewed"))
            db.add(ChunkSet(parse_run_id=run.id, status="ready"))
            version.revision += 1
            run.status = "needs_review"
            audit(db, job.actor_id, "parse.completed", "parse_run", run.id, provider="mineru", scope="synthetic_only")
        run.error_code, job.lease_until = None, None
        db.commit()
    except (ProviderError, ArtifactError, SafeError, SQLAlchemyError) as error:
        code = "PARSE_PERSISTENCE_UNAVAILABLE" if isinstance(error, SQLAlchemyError) else getattr(error, "code", "PARSE_PERSISTENCE_UNAVAILABLE")
        _record_failure(db, run, job, attempt, code, settings, invalid_archive)
    return run


def run_derived_parse_job(db, run_id, settings):
    from .parsing.artifacts import ArtifactError
    from .parsing.derivation import derive_raw_run
    from .parsing.local_pipeline import ParseContractError
    from .services import SafeError, audit
    run, job = db.get(ParseRun, run_id), db.get(ParseServiceJob, run_id)
    if not run or not job or not run.parent_run_id or not run.stage_checkpoint.get("derivation"):
        raise SafeError("NOT_FOUND", "未找到持久纠正任务", 404)
    if run.stage_checkpoint.get("outputs", {}).get("raw_manifest"):
        return run
    attempt = _claim_job(db, job, run)
    if attempt is None:
        return run
    try:
        _local_guard(db, run, job, attempt, settings, require_runtime=False)
        parent, actor = db.get(ParseRun, run.parent_run_id), db.get(User, job.actor_id)
        db.commit()
        derive_raw_run(db, actor, parent, settings, target_run=run, attempt=attempt)
        db.commit()
    except (ArtifactError, ParseContractError, ProviderError, SafeError, SQLAlchemyError, OSError) as error:
        db.rollback()
        run = db.get(ParseRun, run_id, populate_existing=True)
        job = db.get(ParseServiceJob, run_id, populate_existing=True)
        if run.stage_checkpoint.get("outputs", {}).get("raw_manifest"):
            return run
        try:
            _fence_attempt(db, run_id, attempt)
        except (ProviderError, SQLAlchemyError):
            db.rollback()
            return run
        code = "IR_PERSISTENCE_UNAVAILABLE" if isinstance(error, (SQLAlchemyError, OSError)) else (
            error.detail["code"] if isinstance(error, SafeError) else error.code)
        run.status = "cancelled" if job.cancel_requested else (
            "waiting_external" if code in {"IR_PERSISTENCE_UNAVAILABLE", "PARSE_ARTIFACT_STORAGE_UNAVAILABLE"} else "failed")
        run.error_code, job.lease_until = code, None
        run.stage_checkpoint = {**run.stage_checkpoint, "stage_state": run.status, "derivation_error": code}
        audit(db, job.actor_id, "parse.derivation_stopped", "parse_run", run.id, error_code=code)
        db.commit()
    return run


def _retained_cloud_archive(settings: Settings, job: ParseServiceJob, run: ParseRun, attempt: int) -> bytes | None:
    from .parsing.artifacts import work_directory
    for epoch in range(attempt - 1, max(0, attempt - 33), -1):
        workspace = work_directory(settings, run.id, epoch)
        marker, archive = workspace / "cloud_receipt.json", workspace / "result.zip"
        if not marker.is_file() or not archive.is_file():
            continue
        try:
            value = json.loads(marker.read_text(encoding="utf-8"))
            if (not isinstance(value, dict) or value.get("run_id") != run.id or value.get("pin_id") != job.pin_id
                    or value.get("batch_id") != run.provider_task_id or value.get("source_sha256") != job.source_sha256
                    or archive.stat().st_size != value.get("byte_size") or archive.stat().st_size > _MAX_ARCHIVE_BYTES):
                raise ProviderError("PARSE_ARCHIVE_IDENTITY_MISMATCH")
            raw = archive.read_bytes()
            if hashlib.sha256(raw).hexdigest() != value.get("sha256"):
                raise ProviderError("PARSE_ARCHIVE_IDENTITY_MISMATCH")
            return raw
        except (OSError, ValueError):
            raise ProviderError("PARSE_ARCHIVE_IDENTITY_MISMATCH") from None
    return None


def _write_receipt(path: Path, value: dict) -> None:
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary, path)


def _local_guard(db: Session, run: ParseRun, job: ParseServiceJob, attempt: int, settings: Settings, *, require_runtime=True) -> DocumentVersion:
    from .parsing.local_pipeline import fingerprint
    from .services import file_path

    version = _fence_source(db, run, job, attempt)
    _fence_authority(db, run, job)
    file = db.get(FileObject, job.source_file_id, populate_existing=True)
    if not file or file.status != "available" or file.sha256 != job.source_sha256 or file.mime_type != "application/pdf":
        raise ProviderError("PROCESSING_SOURCE_CHANGED")
    if require_runtime and run.request_config.get("runtime") != _local_runtime_identity(settings):
        raise ProviderError("PARSE_PROFILE_CHANGED")
    identity = {"version_id": version.id, "source_file_id": file.id, "source_sha256": file.sha256,
                "request_config": run.request_config}
    if fingerprint(identity) != run.input_fingerprint or not file_path(file, settings).is_file():
        raise ProviderError("PROCESSING_SOURCE_CHANGED")
    return version


def _local_heartbeat(db: Session, run: ParseRun, job: ParseServiceJob, attempt: int, settings: Settings, *, require_runtime=True) -> None:
    _local_guard(db, run, job, attempt, settings, require_runtime=require_runtime)
    db.execute(update(ParseServiceJob).where(ParseServiceJob.parse_run_id == run.id, ParseServiceJob.attempt == attempt,
        ParseServiceJob.lease_until > utc_now(), ParseServiceJob.cancel_requested.is_(False))
        .values(lease_until=utc_now() + _LEASE).execution_options(synchronize_session=False))
    db.commit()


def _recover_workspace(settings: Settings, run: ParseRun, attempt: int):
    """Adopt sealed prior bytes, never redo parsing after an archive/DB window."""
    from .parsing.artifacts import work_directory
    from .parsing.local_pipeline import ParseContractError, read_bundle
    for epoch in range(attempt - 1, max(0, attempt - 33), -1):
        workspace = work_directory(settings, run.id, epoch)
        marker = workspace / "receipt.json"
        if not marker.is_file() or marker.stat().st_size > 1024 * 1024:
            continue
        try:
            receipt = json.loads(marker.read_text(encoding="utf-8"))
            if (not isinstance(receipt, dict) or receipt.get("epoch") != epoch
                    or receipt.get("input_fingerprint") != run.input_fingerprint):
                raise ParseContractError("PARSE_RECOVERY_MANIFEST_INVALID")
            if (receipt.get("state") in {"complete", "running"} and (workspace / "result.zip").is_file()
                    and (workspace / "preflight.json").is_file()):
                preflight = json.loads((workspace / "preflight.json").read_text(encoding="utf-8"))
                if (not isinstance(preflight, dict) or preflight.get("schema") != "dc.pdf-preflight"
                        or preflight.get("source_sha256") != run.stage_checkpoint.get("source_sha256")
                        or type(preflight.get("page_count")) is not int or not isinstance(preflight.get("pages"), list)
                        or len(preflight["pages"]) != preflight["page_count"]):
                    raise ParseContractError("PARSE_RECOVERY_MANIFEST_INVALID")
                # A valid closed ZIP is the SDK's last write of parse content.
                # It is safe to adopt even if the host died before sealing the
                # execution receipt; an incomplete ZIP never causes a rerun.
                read_bundle(workspace / "result.zip", preflight, run.request_config["profile"])
                return epoch, workspace, receipt
            if receipt.get("state") == "running":
                # A dead host does not prove its SDK child exited. Until a
                # durable exit receipt exists, do not dispatch another parse.
                exit_path = workspace / "runner-exit.json"
                if not exit_path.is_file():
                    raise ParseContractError("PARSE_EXECUTION_UNCERTAIN")
                exit_receipt = json.loads(exit_path.read_text(encoding="utf-8"))
                if not isinstance(exit_receipt, dict) or type(exit_receipt.get("returncode")) is not int:
                    raise ParseContractError("PARSE_EXECUTION_UNCERTAIN")
        except (OSError, ValueError, ParseContractError, KeyError, TypeError) as cause:
            error = ParseContractError(getattr(cause, "code", "PARSE_RECOVERY_MANIFEST_INVALID"))
            error.workspace, error.epoch = workspace, epoch
            raise error from None
    return None


def run_local_parse_job(db: Session, run_id: str, settings: Settings, *, runner=None) -> ParseRun:
    """Advance the same persisted job/lease used by selected-service parsing.

    runner is a trusted test boundary only. It is never accepted by an HTTP
    payload, environment setting, document, or model tool call.
    """
    from .parsing.artifacts import ArtifactError, prepare_workspace, bind_workspace, work_directory
    from .parsing.local_pipeline import ParseContractError, read_bundle, source_preflight
    from .parsing.local_mineru import run_standard_bundle
    from .models import ParseArtifact
    from .services import SafeError, audit, file_path

    job, run = db.get(ParseServiceJob, run_id), db.get(ParseRun, run_id)
    if not job or not run or job.mode != "local":
        raise SafeError("NOT_FOUND", "未找到本地解析任务", 404)
    if (run.stage_checkpoint or {}).get("outputs", {}).get("raw_manifest"):
        return run  # Downstream recovery must never overwrite frozen raw outputs.
    prior_epoch, prior_state = job.attempt, run.status
    attempt = _claim_job(db, job, run)
    if attempt is None:
        return run
    if prior_epoch and prior_state == "running":
        history = list((run.stage_checkpoint or {}).get("attempts", []))
        if not any(item.get("epoch") == prior_epoch for item in history):
            history.append({"epoch": prior_epoch, "state": "interrupted", "error_code": "PARSE_ATTEMPT_INTERRUPTED"})
            run.stage_checkpoint = {**(run.stage_checkpoint or {}), "attempts": history}
            db.commit()
    workspace, output_epoch, receipt, recovered = None, attempt, None, None
    try:
        _local_guard(db, run, job, attempt, settings)
        source = db.get(FileObject, job.source_file_id)
        path, expected_size = file_path(source, settings), source.byte_size
        db.commit()
        recovered = _recover_workspace(settings, run, attempt)
        if recovered:
            output_epoch, workspace, receipt = recovered
            preflight = json.loads((workspace / "preflight.json").read_text(encoding="utf-8"))
        else:
            workspace = work_directory(settings, run.id, attempt)
            run.processing_stage = "preflight"
            db.commit()
            preflight = source_preflight(path, job.source_sha256, expected_size,
                lambda: _local_heartbeat(db, run, job, attempt, settings))
            _write_receipt(workspace / "preflight.json", preflight)
            receipt = {"input_fingerprint": run.input_fingerprint, "epoch": attempt, "state": "running", "safe_error": None}
            _write_receipt(workspace / "receipt.json", receipt)
            _local_guard(db, run, job, attempt, settings)
            run.processing_stage = "parsing"
            db.commit()
            invoke = runner or run_standard_bundle
            invoke(path, workspace, settings.local_mineru_executable, settings.local_mineru_home,
                   lambda: _local_heartbeat(db, run, job, attempt, settings))
        # Validate copied bytes/profile/page coverage before any downstream stage.
        middle, members = read_bundle(workspace / "result.zip", preflight, run.request_config["profile"])
        output_directory = workspace / "output"
        if output_directory.is_dir():
            saved = {item.relative_to(output_directory).as_posix(): item for item in output_directory.rglob("*") if item.is_file()}
            if set(saved) != set(members) or any(saved[name].read_bytes() != data for name, data in members.items()):
                raise ParseContractError("PARSE_ARCHIVE_MEMBER_MISMATCH")
        if hashlib.sha256(path.read_bytes()).hexdigest() != job.source_sha256:
            raise ParseContractError("PROCESSING_SOURCE_CHANGED")
        receipt = {"input_fingerprint": run.input_fingerprint, "epoch": output_epoch, "state": "complete", "safe_error": None}
        if not recovered:
            _write_receipt(workspace / "receipt.json", receipt)
        db.commit()
        prepared = prepare_workspace(run.id, job.actor_id, settings, workspace, epoch=output_epoch, complete=True)
        _local_guard(db, run, job, attempt, settings)
        manifest = bind_workspace(db, prepared)
        version = db.get(DocumentVersion, run.document_version_id)
        actual = dict(middle["extensions"]["mineru"])
        actual["producer"] = middle["metadata"]["producer"]
        actual["model_revision"] = middle.get("metadata", {}).get("model_revision")
        run.actual_profile, run.engine_version, run.model_revision = actual, actual["producer"]["version"], actual["model_revision"]
        attempts = list((run.stage_checkpoint or {}).get("attempts", []))
        attempts.append({"epoch": attempt, "output_epoch": output_epoch, "state": "succeeded", "manifest_artifact_id": manifest["manifest_artifact_id"]})
        run.stage_checkpoint = {**(run.stage_checkpoint or {}), "attempts": attempts, "stage_state": "parsed",
            "processed_page_count": len(middle["pages"]), "total_page_count": preflight["page_count"],
            "outputs": {"raw_manifest": manifest["manifest_artifact_id"], "raw_epoch": output_epoch,
                        "raw_manifest_sha256": db.get(FileObject, manifest["manifest_file_id"]).sha256},
            "page_selection": {str(page["page_idx"] + 1): {"attempt_epoch": output_epoch,
                "json_pointer": f"/pages/{index}", "profile_hash": run.input_fingerprint}
                for index, page in enumerate(middle["pages"])}}
        job.checkpoint = {"manifest_artifact_id": manifest["manifest_artifact_id"], "output_epoch": output_epoch,
                          "output_state": "complete", "last_attempt": attempt}
        archive_row = next((row for row in manifest["artifacts"] if row["logical_name"].endswith("/result.zip")), None)
        for selection in run.stage_checkpoint["page_selection"].values():
            selection.update(raw_artifact_id=archive_row["artifact_id"], raw_member_name="middle_json.json")
        run.stage_checkpoint = dict(run.stage_checkpoint)
        job.raw_result_file_id = archive_row["file_id"] if archive_row else None
        run.status, run.error_code, job.lease_until = "needs_review", None, None
        version.revision += 1
        audit(db, job.actor_id, "parse.raw_archived", "parse_run", run.id, profile=run.request_config["profile"]["revision"],
              epoch=attempt, page_count=preflight["page_count"])
        db.commit()
    except (ProviderError, ParseContractError, ArtifactError, SafeError, OSError, SQLAlchemyError) as error:
        code = "PARSE_PERSISTENCE_UNAVAILABLE" if isinstance(error, SQLAlchemyError) else (
            getattr(error, "code", None) or (error.detail["code"] if isinstance(error, SafeError) else "PARSE_PERSISTENCE_UNAVAILABLE"))
        db.rollback()
        try:
            _fence_attempt(db, run.id, attempt)
        except (ProviderError, SQLAlchemyError):
            db.rollback()
            return db.get(ParseRun, run_id, populate_existing=True)
        db.refresh(run)
        db.refresh(job)
        preserve_receipt = hasattr(error, "workspace")
        if preserve_receipt:
            workspace, output_epoch = error.workspace, error.epoch
        # A lost commit acknowledgement can already have installed this exact
        # result. Read it first; never turn a completed commit into a failure.
        if run.status == "needs_review" and job.checkpoint.get("output_state") == "complete":
            db.rollback()
            return run
        manifest = None
        if workspace is not None:
            complete = bool(receipt and receipt.get("state") == "complete")
            archive_error = None if complete else code
            if not complete and not preserve_receipt:
                receipt = {"input_fingerprint": run.input_fingerprint, "epoch": output_epoch, "state": "failed", "safe_error": code}
                try:
                    _write_receipt(workspace / "receipt.json", receipt)
                except OSError:
                    pass
            try:
                db.commit()
                prepared = prepare_workspace(run.id, job.actor_id, settings, workspace,
                    epoch=output_epoch, complete=complete, safe_error=archive_error)
                _fence_attempt(db, run.id, attempt)
                manifest = bind_workspace(db, prepared)
                db.refresh(run)
                db.refresh(job)
            except ArtifactError:
                db.rollback()
                _fence_attempt(db, run.id, attempt)
                db.refresh(run)
                db.refresh(job)
                code = "PARSE_ARCHIVE_STORAGE_UNAVAILABLE"
        state = "cancelled" if job.cancel_requested or code == "PARSE_CANCELLED" else (
            "reconciliation_required" if code == "PARSE_EXECUTION_UNCERTAIN" else
            "waiting_external" if code in {"LOCAL_STANDARD_NOT_READY", "PARSE_PERSISTENCE_UNAVAILABLE", "PARSE_ARCHIVE_STORAGE_UNAVAILABLE"} else "failed")
        previous = list((run.stage_checkpoint or {}).get("attempts", []))
        previous.append({"epoch": attempt, "state": state, "error_code": code,
                         "manifest_artifact_id": manifest["manifest_artifact_id"] if manifest else None})
        run.stage_checkpoint = {**(run.stage_checkpoint or {}), "attempts": previous, "stage_state": "blocked" if state == "waiting_external" else state}
        run.status, run.error_code, job.lease_until = state, code, None
        job.checkpoint = {**(job.checkpoint or {}), "output_state": manifest["output_state"] if manifest else "unarchived",
                          "last_attempt": attempt, "safe_error": code}
        audit(db, job.actor_id, "parse.execution_stopped", "parse_run", run.id, state=state, error_code=code)
        db.commit()
    return run
