"""Application services used by both the HTTP routes and focused tests."""

from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

from fastapi import HTTPException, Request, Response, status
from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .config import Settings, settings
from .models import (
    ApiIdempotencyRecord,
    AuditEvent,
    AuthSession,
    AuthorizationGrant,
    BusinessApproval,
    ChunkSet,
    ContentBlock,
    Document,
    DocumentVersion,
    FileObject,
    IndexGeneration,
    IndexSource,
    KnowledgeBase,
    ParseRun,
    Permission,
    ProviderCall,
    QueryTask,
    Role,
    TaskArtifact,
    TaskEvidence,
    TaskShare,
    TaskAttempt,
    TaskOutbox,
    TaskStep,
    User,
    new_id,
    utc_now,
)
from .security import digest_token, hash_password, random_token, request_hash, verify_password


ROLE_PERMISSIONS = {
    "employee": {"knowledge.query", "document.read", "task.read", "task.share", "feedback.create", "feedback.read_own"},
    "knowledge_manager": {
        "knowledge.query", "document.read", "document.manage", "knowledge.manage", "task.read", "task.share",
        "feedback.create", "feedback.read_own", "feedback.handle"
    },
    "system_admin": {"user.manage", "access.manage", "system.configure", "operations.read", "task.read"},
}
SAFE_REASON_CODES = {"operator_provided", "withdrawal_requested", "document_deleted", "access_changed", "service_failure", "manual_review"}


class SafeError(HTTPException):
    """HTTP error with a stable, non-sensitive error code."""

    def __init__(self, code: str, message: str, status_code: int = 400, *, headers: dict[str, str] | None = None):
        self.code, self.message = code, message
        super().__init__(status_code=status_code, detail={"code": code, "message": message}, headers=headers)


def task_write_guard(db: Session, task: QueryTask, *, allowed_statuses=("pending", "running")) -> None:
    """Serialize a task write against cancellation and execution replacement.

    Call before changing ORM state. The conditional write acquires the task row
    for the caller's transaction; a previously committed cancel cannot be
    overwritten by a stale task loaded before that cancel.
    """
    if task.status not in allowed_statuses:
        raise SafeError("TASK_STATE_CHANGED", "任务状态已改变，拒绝旧执行者提交", 409)
    with db.no_autoflush:
        changed = db.execute(update(QueryTask).where(QueryTask.id == task.id,
            QueryTask.owner_user_id == task.owner_user_id, QueryTask.status == task.status,
            QueryTask.revision == task.revision, QueryTask.execution_revision == task.execution_revision)
            .values(revision=QueryTask.revision).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise SafeError("TASK_STATE_CHANGED", "任务已取消或执行修订已改变，拒绝旧执行者提交", 409)


def audit(db: Session, actor: str | None, action: str, resource_type: str, resource_id: str | None = None,
          reason: str | None = None, **metadata: Any) -> AuditEvent:
    reason = reason if reason in SAFE_REASON_CODES else ("operator_provided" if reason else None)
    event = AuditEvent(actor_user_id=actor, action=action, resource_type=resource_type, resource_id=resource_id,
                       reason=reason, metadata_json={k: v for k, v in metadata.items() if k not in {"body", "content", "token", "url"}})
    db.add(event)
    return event


def _utc(value: datetime) -> datetime:
    """Normalize SQLite's naive timestamp round-trip to an aware UTC value."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def seed_reference_data(db: Session) -> None:
    """Seed fixed role/permission directories without creating a user."""
    existing_roles = {row.code for row in db.scalars(select(Role)).all()}
    role_names = {"employee": "普通使用者", "knowledge_manager": "知识管理员", "system_admin": "系统管理员"}
    for code, name in role_names.items():
        if code not in existing_roles:
            db.add(Role(code=code, name=name))
    existing_permissions = {row.code for row in db.scalars(select(Permission)).all()}
    all_permissions = sorted({permission for values in ROLE_PERMISSIONS.values() for permission in values} | {"artifact.download"})
    for code in all_permissions:
        if code not in existing_permissions:
            db.add(Permission(code=code, description=code))
    db.commit()


def user_view(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "display_name": user.display_name,
        "username": user.username,
        "status": user.status,
        "roles": list(user.roles or []),
        "authz_revision": user.authz_revision,
    }


def create_session(db: Session, user: User, response: Response, config: Settings = settings) -> dict[str, str]:
    now = utc_now()
    token = random_token()
    csrf = random_token(24)
    session = AuthSession(
        user_id=user.id,
        token_hash=digest_token(token),
        csrf_hash=digest_token(csrf),
        issued_at=now,
        last_seen_at=now,
        expires_at=now + timedelta(seconds=config.session_absolute_seconds),
        authz_revision=user.authz_revision,
    )
    db.add(session)
    db.commit()
    response.set_cookie(config.cookie_name, token, httponly=True, secure=config.secure_cookie, samesite="lax",
                        max_age=config.session_absolute_seconds, path="/")
    return {"csrf_token": csrf}


def current_session(request: Request, db: Session, config: Settings = settings, *, persist_heartbeat: bool = True) -> tuple[User, AuthSession]:
    token = request.cookies.get(config.cookie_name)
    if not token:
        raise SafeError("AUTH_REQUIRED", "请先登录", 401)
    session = db.scalar(select(AuthSession).where(AuthSession.token_hash == digest_token(token)).execution_options(populate_existing=True))
    now = utc_now()
    if not session or session.revoked_at or _utc(session.expires_at) <= now:
        raise SafeError("AUTH_REQUIRED", "登录状态已失效，请重新登录", 401)
    user = db.get(User, session.user_id, populate_existing=True)
    if not user or user.status != "active" or user.authz_revision != session.authz_revision:
        if persist_heartbeat and session.revoked_at is None:
            session.revoked_at = now
            db.commit()
        raise SafeError("AUTH_REQUIRED", "登录状态已失效，请重新登录", 401)
    if _utc(session.last_seen_at) + timedelta(seconds=config.session_idle_seconds) <= now:
        if persist_heartbeat:
            session.revoked_at = now
            db.commit()
        raise SafeError("AUTH_REQUIRED", "登录状态已失效，请重新登录", 401)
    if persist_heartbeat:
        session.last_seen_at = now
        db.commit()
    return user, session


def require_document_creation_scope(db: Session, user: User, kb_id: str, category: str | None) -> None:
    """Recheck the actual requested category at every creation boundary."""
    scopes = document_creation_scopes(db, user, kb_id)
    if not scopes or (category is not None and not any(scope["category"] == category for scope in scopes)):
        raise SafeError("FORBIDDEN", "资料类别或知识库管理范围已改变，未提交新资料", 403)


def fence_management_authority(db: Session, user: User, kb_id: str, *,
                               document_id: str | None = None, request: Request | None = None) -> User:
    """Hold management authority while the caller rechecks scope and commits."""
    # Flush Doc/child and any KB update before taking shared authority locks.
    # Grant creation writes KB before User, and account disable writes sessions
    # before User. Approval changes also write the beneficiary User.
    db.flush()
    db.scalar(select(KnowledgeBase.id).where(KnowledgeBase.id == kb_id).with_for_update(read=True))
    if request is not None:
        config = request.app.state.settings
        token = request.cookies.get(config.cookie_name)
        if token:
            db.scalar(select(AuthSession.id).where(AuthSession.user_id == user.id,
                AuthSession.token_hash == digest_token(token)).with_for_update(read=True))
    db.scalar(select(User.id).where(User.id == user.id).with_for_update(read=True))
    scope = and_(AuthorizationGrant.knowledge_base_id == kb_id, AuthorizationGrant.document_id.is_(None))
    if document_id is not None:
        scope = or_(scope, AuthorizationGrant.document_id == document_id)
    db.scalars(select(AuthorizationGrant.id).where(AuthorizationGrant.subject_user_id == user.id,
        AuthorizationGrant.operation == "knowledge.manage", AuthorizationGrant.revoked_at.is_(None), scope)
        .order_by(AuthorizationGrant.id).with_for_update(read=True)).all()
    if request is not None:
        current_actor, _ = current_session(request, db, config, persist_heartbeat=False)
        if current_actor.id != user.id:
            raise SafeError("AUTH_REQUIRED", "登录状态已失效，请重新登录", 401)
        return current_actor
    return user


def require_csrf(request: Request, session: AuthSession) -> None:
    value = request.headers.get("X-CSRF-Token")
    if not value or not __import__("hmac").compare_digest(digest_token(value), session.csrf_hash):
        raise SafeError("CSRF_REQUIRED", "缺少或无效的 CSRF 校验", 403)


def login(db: Session, username: str, password: str, response: Response, config: Settings = settings) -> tuple[User, dict[str, str]]:
    user = db.scalar(select(User).where(User.username == username))
    if not user or user.status != "active" or not verify_password(password, user.credential_hash):
        audit(db, user.id if user else None, "auth.login_failed", "user", user.id if user else None)
        db.commit()
        raise SafeError("LOGIN_FAILED", "账号或口令不正确", 401)
    csrf = create_session(db, user, response, config)
    audit(db, user.id, "auth.login", "user", user.id)
    db.commit()
    return user, csrf


def logout(db: Session, user: User, session: AuthSession, response: Response, config: Settings = settings) -> None:
    session.revoked_at = utc_now()
    response.delete_cookie(config.cookie_name, path="/")
    audit(db, user.id, "auth.logout", "session", session.id)
    db.commit()


def has_permission(user: User, permission: str) -> bool:
    return any(permission in ROLE_PERMISSIONS.get(role, set()) for role in (user.roles or []))


def _grant_valid(grant: AuthorizationGrant, operation: str, now: datetime) -> bool:
    return grant.operation == operation and grant.revoked_at is None and _utc(grant.valid_from) <= now and (grant.valid_until is None or _utc(grant.valid_until) > now)


def approval_allows_grant(approval: BusinessApproval | None, grant: AuthorizationGrant, now: datetime) -> bool:
    """One verified approval must cover the whole grant, never just its operation."""
    if not approval or approval.status != "approved" or approval.verification_state != "verified" or not approval.approver or approval.approved_at is None:
        return False
    from sqlalchemy.orm import object_session
    from .governance import approval_is_current
    approval_db = object_session(approval)
    if approval_db is None or not approval_is_current(approval_db, approval):
        return False
    if approval.applicant != grant.subject_user_id or grant.operation not in (approval.operations or []):
        return False
    if _utc(approval.valid_from) > now or (approval.valid_until and _utc(approval.valid_until) <= now):
        return False
    if grant.valid_from is not None and _utc(grant.valid_from) < _utc(approval.valid_from):
        return False
    if approval.valid_until and (grant.valid_until is None or _utc(grant.valid_until) > _utc(approval.valid_until)):
        return False
    expected = {key: getattr(grant, key) for key in ("knowledge_base_id", "document_id", "level", "category", "need_class")}
    return all(key in (approval.scope or {}) and approval.scope[key] == value for key, value in expected.items())


def can_access_document(db: Session, user: User, document: Document, operation: str = "document.read", *, context: dict[str, str | None] | None = None, hide: bool = True, include_deleted: bool = False) -> bool:
    # A request may outlive an authorization change committed elsewhere.
    # The ORM identity map is not an authority for current ACL decisions.
    user = db.get(User, user.id, populate_existing=True)
    document = db.get(Document, document.id, populate_existing=True)
    if not user or not document or user.status != "active" or not has_permission(user, operation):
        return False
    classification = {"level": document.access_level, "category": document.category, "need_class": document.need_class}
    incomplete_classification = None in classification.values()
    if operation in {"document.read", "knowledge.query"} and incomplete_classification:
        return False
    kb = db.get(KnowledgeBase, document.knowledge_base_id, populate_existing=True)
    if not kb or kb.status != "active" or (document.deleted_at is not None and not (include_deleted and operation == "knowledge.manage")):
        return False
    allow_kb_scope = document.access_mode == "inherit"
    if allow_kb_scope and operation == "knowledge.manage" and incomplete_classification:
        # The first version records the uploader. A later upload cannot transfer this access.
        uploader_id = db.scalar(select(DocumentVersion.created_by).where(
            DocumentVersion.document_id == document.id, DocumentVersion.version_no == 1))
        allow_kb_scope = uploader_id == user.id
    now = utc_now()
    grants = db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == user.id).execution_options(populate_existing=True)).all()
    for grant in grants:
        if not _grant_valid(grant, operation, now):
            continue
        if grant.approval_id:
            approval = db.get(BusinessApproval, grant.approval_id, populate_existing=True)
            if not approval_allows_grant(approval, grant, now):
                continue
        else:
            # Every effective grant must point to a separately recorded approval.
            continue
        for key, target_value in classification.items():
            if target_value is not None and getattr(grant, key) != target_value:
                break
            requested_value = (context or {}).get(key)
            if requested_value is not None and getattr(grant, key) != requested_value:
                break
        else:
            if grant.document_id == document.id or (allow_kb_scope and grant.document_id is None and grant.knowledge_base_id == kb.id):
                return True
    return False


def document_creation_scopes(db: Session, user: User, kb_id: str) -> list[dict[str, str | None]]:
    """Creation requires a current KB-level grant, never a single-document grant.

    This exposes the actor's own management scopes; it does not classify a new
    document, create an ACL or substitute for a business confirmation.
    """
    user = db.get(User, user.id, populate_existing=True)
    kb = db.get(KnowledgeBase, kb_id, populate_existing=True)
    if not user or user.status != "active" or not has_permission(user, "knowledge.manage") or not kb or kb.status != "active":
        return []
    now, scopes = utc_now(), []
    grants = db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == user.id,
        AuthorizationGrant.knowledge_base_id == kb_id, AuthorizationGrant.document_id.is_(None))
        .execution_options(populate_existing=True)).all()
    for grant in grants:
        approval = db.get(BusinessApproval, grant.approval_id, populate_existing=True) if grant.approval_id else None
        if not _grant_valid(grant, "knowledge.manage", now) or not approval_allows_grant(approval, grant, now):
            continue
        scope = {"level": grant.level, "category": grant.category, "need_class": grant.need_class}
        if scope not in scopes:
            scopes.append(scope)
    return scopes


def can_manage_kb(db: Session, user: User, kb_id: str) -> bool:
    if not has_permission(user, "knowledge.manage"):
        return False
    now = utc_now()
    grants = db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == user.id)).all()
    for grant in grants:
        if not _grant_valid(grant, "knowledge.manage", now) or grant.knowledge_base_id != kb_id:
            continue
        if grant.approval_id:
            approval = db.get(BusinessApproval, grant.approval_id, populate_existing=True)
            if not approval_allows_grant(approval, grant, now):
                continue
        else:
            continue
        return True
    return False


def can_query_kb(db: Session, user: User, kb_id: str) -> bool:
    if not has_permission(user, "knowledge.query"):
        return False
    now = utc_now()
    grants = db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == user.id, AuthorizationGrant.knowledge_base_id == kb_id)).all()
    for grant in grants:
        if not _grant_valid(grant, "knowledge.query", now) or not grant.approval_id:
            continue
        approval = db.get(BusinessApproval, grant.approval_id, populate_existing=True)
        if approval_allows_grant(approval, grant, now):
            return True
    return False


def version_is_formal(version: DocumentVersion) -> bool:
    return version.publish_status == "effective" and version.applicability_status == "confirmed" and version.content_review_status == "approved"


def document_has_formal_version(db: Session, document: Document) -> bool:
    from .governance import publication_is_current
    return any(publication_is_current(db, version) for version in db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id)).all())


def evidence_is_readable(db: Session, user: User, item: TaskEvidence, *, saved_read: bool = False) -> bool:
    if item.chunk_id is not None or item.chunk_set_id is not None or item.source_binding_hash is not None:
        from .query_evidence import child_evidence_is_current
        return child_evidence_is_current(db, user, item, saved_read=saved_read)
    from .governance import can_read_version
    version = db.get(DocumentVersion, item.document_version_id, populate_existing=True)
    document = db.get(Document, version.document_id) if version else None
    # Superseded sources remain readable in saved tasks under current ACLs.
    # New searches still require effective versions; withdrawal is distinct
    # from replacement and must continue to block saved results.
    return bool(version and document
                and version.publish_status in {"effective", "superseded"}
                and version.applicability_status == "confirmed"
                and version.content_review_status == "approved"
                and can_read_version(db, user, version, historical=True))


def can_read_task(db: Session, user: User, task: QueryTask) -> bool:
    from .query_evidence import task_readable_evidence

    return task_readable_evidence(db, user, task) is not None


def check_if_match(value: str | None, expected_revision: int, *, required: bool = True) -> None:
    if required and not value:
        raise SafeError("PRECONDITION_REQUIRED", "需要 If-Match 并发条件", 428)
    if value and value.strip('"') != str(expected_revision):
        raise SafeError("PRECONDITION_FAILED", "资源已发生变化，请刷新后重试", 412)


def etag(revision: int) -> str:
    return f'"{revision}"'


def idempotency_record(db: Session, actor: User, request: Request, key: str, body: bytes) -> tuple[ApiIdempotencyRecord, bool]:
    if not key:
        raise SafeError("IDEMPOTENCY_REQUIRED", "写操作需要 Idempotency-Key", 428)
    # The same operation must retain its original concurrency condition.
    fingerprint = body + b"\x00if-match:" + request.headers.get("If-Match", "").encode("utf-8")
    digest = request_hash(request.method, request.url.path, fingerprint)
    existing = db.scalar(select(ApiIdempotencyRecord).where(ApiIdempotencyRecord.actor_user_id == actor.id, ApiIdempotencyRecord.idempotency_key == key))
    if existing:
        if existing.request_hash != digest or existing.method != request.method or existing.request_path != request.url.path:
            raise SafeError("IDEMPOTENCY_CONFLICT", "Idempotency-Key 已用于其他请求", 409)
        if existing.status == "completed":
            return existing, True
        raise SafeError("REQUEST_IN_PROGRESS", "相同请求仍在处理，请稍后查询任务", 409)
    record = ApiIdempotencyRecord(actor_user_id=actor.id, idempotency_key=key, method=request.method, request_path=request.url.path, request_hash=digest)
    db.add(record)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        existing = db.scalar(select(ApiIdempotencyRecord).where(ApiIdempotencyRecord.actor_user_id == actor.id, ApiIdempotencyRecord.idempotency_key == key))
        if existing and existing.request_hash == digest and existing.method == request.method and existing.request_path == request.url.path:
            if existing.status == "completed":
                return existing, True
            raise SafeError("REQUEST_IN_PROGRESS", "相同请求仍在处理，请稍后查询任务", 409)
        raise SafeError("IDEMPOTENCY_CONFLICT", "Idempotency-Key 已用于其他请求", 409)
    return record, False


def complete_idempotency(db: Session, record: ApiIdempotencyRecord, *, status_code: int, refs: dict[str, Any]) -> None:
    record.status = "completed"
    record.response_status = status_code
    record.resource_refs = refs
    # The route commits the business operation and its receipt together.
    db.flush()


def save_upload(stream: Iterable[bytes], filename: str, mime_type: str, actor: User, config: Settings = settings, *, storage_identity: str | None = None) -> FileObject:
    if not filename.lower().endswith(".pdf") or mime_type not in {"application/pdf", "application/octet-stream"}:
        raise SafeError("UNSUPPORTED_FILE", "首期只接受 PDF 文件", 415)
    temp_name = f".upload-{new_id()}.tmp"
    config.ensure_dirs()
    temp_path = config.storage_dir / temp_name
    digest = hashlib.sha256()
    size = 0
    try:
        with temp_path.open("wb") as target:
            for chunk in stream:
                size += len(chunk)
                if size > config.max_upload_bytes:
                    raise SafeError("FILE_TOO_LARGE", "文件超过配置的大小上限", 413)
                digest.update(chunk)
                target.write(chunk)
            target.flush()
            os.fsync(target.fileno())
        if size == 0:
            raise SafeError("EMPTY_FILE", "不能上传空文件", 400)
        with temp_path.open("rb") as handle:
            if handle.read(5) != b"%PDF-":
                raise SafeError("UNSUPPORTED_FILE", "上传文件没有PDF文件标识", 415)
        if storage_identity is not None and not re.fullmatch(r"[0-9a-f]{64}", storage_identity):
            raise ValueError("Upload storage identity must be a server-derived digest")
        key = f"documents/{digest.hexdigest()}-{storage_identity or new_id()}.pdf"
        target_path = config.storage_dir / key
        if config.storage_dir.resolve() not in target_path.resolve().parents:
            raise SafeError("STORAGE_ERROR", "文件存储路径无效", 500)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if storage_identity is None:
            temp_path.replace(target_path)
        else:
            # Stable per-command identity lets a retry recover a complete file
            # promoted before a process died, without duplicate private blobs.
            # Hard-link installation is atomic and never overwrites a target.
            try:
                os.link(temp_path, target_path)
            except FileExistsError:
                with target_path.open("rb") as handle:
                    existing_hash = hashlib.file_digest(handle, "sha256").hexdigest()
                if target_path.stat().st_size != size or existing_hash != digest.hexdigest():
                    raise SafeError("UPLOAD_RECONCILIATION_REQUIRED", "上传目标需核对；未覆盖已有文件", 409)
        return FileObject(storage_name="local", object_key=key, sha256=digest.hexdigest(), byte_size=size, mime_type=mime_type, status="available", created_by=actor.id)
    finally:
        temp_path.unlink(missing_ok=True)


def file_path(file: FileObject, config: Settings = settings) -> Path:
    path = (config.storage_dir / file.object_key).resolve()
    if config.storage_dir.resolve() not in path.parents:
        raise SafeError("STORAGE_ERROR", "文件存储路径无效", 500)
    return path


def document_view(db: Session, document: Document, *, include_drafts: bool = True, user: User | None = None) -> dict[str, Any]:
    from .governance import can_read_original_version
    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id).order_by(DocumentVersion.version_no.desc())).all()
    visible_versions = [v for v in versions if include_drafts or (user is not None and can_read_original_version(db, user, v))]
    return {"id": document.id, "knowledge_base_id": document.knowledge_base_id, "title": document.title, "category": document.category,
            "access_mode": document.access_mode, "deleted": document.deleted_at is not None, "revision": document.revision,
            "current_version_id": next((v.id for v in visible_versions if v.publish_status == "effective"), None),
            "versions": [version_view(v) for v in visible_versions]}


def version_view(version: DocumentVersion) -> dict[str, Any]:
    return {"id": version.id, "document_id": version.document_id, "version_no": version.version_no, "revision_label": version.revision_label,
            "model_scope": list(version.model_scope or []), "applicability_status": version.applicability_status,
            "content_review_status": version.content_review_status, "publish_status": version.publish_status,
            "effective_at": version.effective_at.isoformat() if version.effective_at else None,
            "selected_chunk_set_id": version.selected_chunk_set_id, "selected_review_set_id": version.selected_review_set_id,
            "reference_access_state": version.reference_access_state,
            "read_mode": "reference_only" if version.publish_status == "draft" and version.reference_access_state == "open" else "historical" if version.publish_status in {"superseded", "withdrawn"} else "published_original" if version.publish_status == "effective" else "blocked",
            "formal_use_requires_current_validation": True,
            "revision": version.revision}


def selected_parse(db: Session, version: DocumentVersion) -> tuple[ParseRun, ChunkSet] | None:
    """Resolve the explicit source binding, never infer it from creation time."""
    chunk = db.get(ChunkSet, version.selected_chunk_set_id, populate_existing=True) if version.selected_chunk_set_id else None
    run = db.get(ParseRun, chunk.parse_run_id, populate_existing=True) if chunk else None
    if not chunk or chunk.status != "ready" or not run or run.status != "succeeded" or run.document_version_id != version.id:
        return None
    return run, chunk


def task_view(task: QueryTask, *, viewer_user_id: str | None = None, answer_result: dict | None = None) -> dict[str, Any]:
    result = task.result_summary if answer_result is None else answer_result
    if viewer_user_id is not None and viewer_user_id != task.owner_user_id:
        # Coverage labels are frozen from the owner's original question. A
        # single-answer share does not grant the question or conversation.
        result = {key: value for key, value in result.items() if key != "coverage"}
    return {"id": task.id, "kind": task.kind, "status": task.status, "next_action": task.next_action,
            "input": task.input_snapshot if viewer_user_id is None or viewer_user_id == task.owner_user_id else None,
            "effective_input": task.effective_input if viewer_user_id is None or viewer_user_id == task.owner_user_id else None,
            "result": result, "warnings": list(task.warnings or []),
            "error_code": task.error_code, "revision": task.revision, "created_at": task.created_at.isoformat(),
            "finished_at": task.finished_at.isoformat() if task.finished_at else None,
            "conversation_id": task.conversation_id if viewer_user_id is None or viewer_user_id == task.owner_user_id else None, "execution_revision": task.execution_revision}


def evidence_view(item: TaskEvidence, version: DocumentVersion, document: Document) -> dict[str, Any]:
    return {"id": item.id, "document_version_id": item.document_version_id, "document_title": document.title,
            "version": version_view(version), "quote": item.quote_snapshot, "locator": item.locator, "review_status": item.review_at_run,
            "chunk_id": item.chunk_id, "chunk_set_id": item.chunk_set_id, "evidence_kind": item.evidence_kind,
            "source_spans": item.source_spans, "asset_refs": item.asset_refs, "source_binding_hash": item.source_binding_hash,
            "role": (item.source_context or {}).get("fragment", {}).get("role")}


def split_terms(value: str) -> set[str]:
    terms = set(re.findall(r"[\w\u4e00-\u9fff]{2,}", value.lower()))
    # Index contiguous Chinese text in both questions and long manual passages.
    for span in re.findall(r"[\u4e00-\u9fff]+", value):
        terms.update(span[i : i + 2] for i in range(len(span) - 1))
    return terms


def search_blocks(db: Session, user: User, query: str, model: str | None, kb_ids: list[str], context: dict[str, str | None] | None = None) -> tuple[list[tuple[ContentBlock, DocumentVersion, Document]], list[str]]:
    from .governance import can_read_version, publication_is_current, query_has_version_conflict
    terms = split_terms(query)
    warnings: list[str] = []
    if not terms:
        return [], ["query_empty"]
    if model and query_has_version_conflict(db, user, model, kb_ids, context):
        return [], ["version_conflict"]
    rows = db.execute(select(ContentBlock, ParseRun, DocumentVersion, Document).join(ParseRun, ParseRun.id == ContentBlock.parse_run_id).join(DocumentVersion, DocumentVersion.id == ParseRun.document_version_id).join(Document, Document.id == DocumentVersion.document_id)).all()
    matches: list[tuple[int, ContentBlock, DocumentVersion, Document]] = []
    for block, parse_run, version, document in rows:
        if kb_ids and document.knowledge_base_id not in kb_ids:
            continue
        if not can_access_document(db, user, document, context=context) or not can_access_document(db, user, document, "knowledge.query", context=context):
            continue
        # Pending versions are outside the formal corpus. They must not make a
        # still-valid published version look unselected or not ready.
        if version.publish_status != "effective" or version.applicability_status != "confirmed":
            continue
        kb = db.get(KnowledgeBase, document.knowledge_base_id)
        active_generation = db.get(IndexGeneration, kb.active_generation_id) if kb and kb.active_generation_id else None
        if not active_generation or active_generation.status != "ready":
            if "index_not_ready" not in warnings:
                warnings.append("index_not_ready")
            continue
        if active_generation.build_report.get("kind") == "child-v1":
            warnings.append("exact_child_evidence_required")
            continue  # New generations may never fall back to full blocks.
        if active_generation.source_revision != kb.content_revision:
            if "index_stale" not in warnings:
                warnings.append("index_stale")
            continue
        selected = selected_parse(db, version)
        if not selected:
            warnings.append("source_selection_pending")
            continue
        if selected[0].id != parse_run.id:
            continue
        source = db.scalar(select(IndexSource).where(IndexSource.index_generation_id == active_generation.id, IndexSource.document_version_id == version.id, IndexSource.parse_run_id == parse_run.id, IndexSource.chunk_set_id == selected[1].id))
        if not source:
            continue
        if version.content_review_status != "approved":
            warnings.append("content_pending_review")
            continue
        if block.review_status != "approved":
            warnings.append("evidence_pending_review")
            continue
        if not publication_is_current(db, version) or not can_read_version(db, user, version) or not can_read_version(db, user, version, "knowledge.query"):
            warnings.append("publication_not_ready")
            continue
        if model and version.model_scope and model not in version.model_scope:
            continue
        score = len(terms & split_terms(block.text_content))
        if score:
            matches.append((score, block, version, document))
    if not matches:
        if model:
            warnings.append("model_or_evidence_mismatch")
        if not any(can_access_document(db, user, document) for document in db.scalars(select(Document)).all()):
            warnings.append("access_limited")
        warnings.append("insufficient_evidence")
    matches.sort(key=lambda item: (-item[0], item[1].ordinal))
    chosen = [(block, version, document) for _, block, version, document in matches[:8]]
    # Preserve required warnings/limitations alongside matching operational text.
    chosen_runs = {block.parse_run_id for block, _, _ in chosen}
    ids = {block.id for block, _, _ in chosen}
    for block, run, version, document in rows:
        if run.id in chosen_runs and block.id not in ids and block.kind in {"warning", "caution", "limitation", "footnote", "safety"}:
            if block.review_status != "approved" or not publication_is_current(db, version):
                return [], sorted(set(warnings) | {"evidence_pending_review", "insufficient_evidence"})
            chosen.append((block, version, document))
            ids.add(block.id)
    return chosen, sorted(set(warnings))


def _query_match_is_current(db: Session, user: User, block: ContentBlock, version: DocumentVersion,
                            document: Document, payload: dict[str, Any]) -> bool:
    """Revalidate selected evidence before writing a new answer, not a history read."""
    db.refresh(block)
    db.refresh(version)
    context = {key: payload.get(key) for key in ("level", "category", "need_class")}
    if not can_access_document(db, user, document, context=context) or not can_access_document(db, user, document, "knowledge.query", context=context):
        return False
    if not version_is_formal(version) or block.review_status != "approved":
        return False
    from .governance import can_read_version
    if not can_read_version(db, user, version) or not can_read_version(db, user, version, "knowledge.query"):
        return False
    selected = selected_parse(db, version)
    if not selected or selected[0].id != block.parse_run_id:
        return False
    if payload.get("model") and version.model_scope and payload["model"] not in version.model_scope:
        return False
    kb = db.get(KnowledgeBase, document.knowledge_base_id, populate_existing=True)
    generation = db.get(IndexGeneration, kb.active_generation_id, populate_existing=True) if kb.active_generation_id else None
    if not generation or generation.status != "ready" or generation.source_revision != kb.content_revision:
        return False
    return db.scalar(select(IndexSource.id).where(IndexSource.index_generation_id == generation.id,
                                                 IndexSource.document_version_id == version.id,
                                                 IndexSource.parse_run_id == block.parse_run_id,
                                                 IndexSource.chunk_set_id == selected[1].id)) is not None


def build_query_result(db: Session, task: QueryTask, user: User, *, stage_only: bool = False) -> None:
    from .query_evidence import stage_child_query
    if stage_child_query(db, task, user, stage_only=stage_only):
        return
    payload = task.effective_input or task.input_snapshot
    matches, warnings = search_blocks(db, user, payload.get("query", ""), payload.get("model"), payload.get("knowledge_base_ids", []), {"level": payload.get("level"), "category": payload.get("category"), "need_class": payload.get("need_class")})
    current_matches = [match for match in matches if _query_match_is_current(db, user, *match, payload)]
    if len(current_matches) != len(matches):
        warnings = sorted(set(warnings) | {"evidence_conditions_changed"} | ({"insufficient_evidence"} if not current_matches else set()))
    matches = current_matches
    for block, version, document in matches:
        db.add(TaskEvidence(task_id=task.id, document_version_id=version.id, block_id=block.id, quote_snapshot=block.text_content, locator=block.locator, review_at_run=block.review_status, execution_revision=task.execution_revision))
    task.status = "running" if stage_only else "succeeded"
    task.finished_at = None if stage_only else utc_now()
    task.warnings = warnings
    task.result_summary = {"answer": "\n\n".join(f"• {block.text_content.strip()}" for block, _, _ in matches) if matches else None,
                          "evidence_count": len(matches), "result_status": "candidate" if stage_only else "formal" if matches and not warnings else "insufficient_evidence",
                          "requires_review": stage_only or bool(warnings)}
    task.next_action = "核对每条说明的版本和原文定位" if matches else "补充设备型号或确认资料版本"
    task.revision += 1
    if not stage_only:
        db.add(TaskArtifact(task_id=task.id, kind="query.result.json", validation_status="not_verified"))
        audit(db, user.id, "query.completed", "task", task.id, result_status=task.result_summary["result_status"], evidence_count=len(matches))


def process_query_task(db: Session, task: QueryTask, user: User) -> None:
    """Run one persisted local attempt when a client polls a pending task.

    This keeps the task durable and refresh-recoverable without pretending that
    the local reference service has a Celery worker or a remote cancellation API.
    """
    if task.status not in {"pending", "running"}:
        return
    step = db.scalar(select(TaskStep).where(TaskStep.task_id == task.id, TaskStep.step_key == "query"))
    if not step:
        return
    step.status = "running"
    step.attempt_count += 1
    attempt = db.scalar(select(TaskAttempt).where(TaskAttempt.step_id == step.id, TaskAttempt.attempt_no == step.attempt_count))
    if not attempt:
        attempt = TaskAttempt(step_id=step.id, attempt_no=step.attempt_count, status="running")
        db.add(attempt)
    outbox = db.scalar(select(TaskOutbox).where(TaskOutbox.step_id == step.id).order_by(TaskOutbox.dispatch_no.desc()))
    if outbox:
        outbox.status = "published"
        outbox.published_at = utc_now()
        outbox.publish_attempts += 1
    task.status = "running"
    try:
        build_query_result(db, task, user)
        step.status = "succeeded"
        step.completed_at = utc_now()
        attempt.status = "succeeded"
        attempt.finished_at = utc_now()
    except Exception:
        task.status = "failed"
        task.error_code = "QUERY_EXECUTION_FAILED"
        task.next_action = "检查资料状态后重新提交查询"
        task.finished_at = utc_now()
        step.status = "failed"
        attempt.status = "failed"
        attempt.finished_at = utc_now()
        attempt.error_code = task.error_code
        audit(db, user.id, "query.failed", "task", task.id, reason="service_failure")


def _extract_model_from_title(title: str) -> list[str]:
    candidates = re.findall(r"[A-Za-z]{1,8}[\w-]{1,20}|[\u4e00-\u9fff]{2,8}", title)
    return sorted(set(candidates), key=len, reverse=True)[:8]


def create_default_kb(db: Session, user: User, name: str = "首期设备资料") -> KnowledgeBase:
    # Creating a container grants no business access or approval authority.
    kb = KnowledgeBase(name=name)
    db.add(kb)
    db.flush()
    return kb


def make_parse_run(db: Session, version: DocumentVersion, file: FileObject, config: Settings = settings) -> ParseRun:
    run = ParseRun(document_version_id=version.id, origin="local", status="running", provider_task_id=None)
    db.add(run)
    db.flush()
    from .parsing.providers import LocalPdfProvider

    provider = LocalPdfProvider(config.local_mineru_executable, config.local_mineru_home)
    blocks = provider.parse(file_path(file, config))
    for ordinal, (text, locator, kind, structure) in enumerate(blocks, 1):
        db.add(ContentBlock(parse_run_id=run.id, ordinal=ordinal, kind=kind, text_content=text,
                            locator=locator, structured_content=structure, review_status="unreviewed"))
    db.add(ChunkSet(parse_run_id=run.id, status="ready"))
    run.status = "needs_review"
    version.revision += 1
    db.flush()
    return run


def _legacy_index_manifest(db: Session, kb: KnowledgeBase) -> dict:
    from collections import Counter
    from . import governance
    from .governance_models import PublicationEvent

    versions = db.execute(select(DocumentVersion, Document)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.knowledge_base_id == kb.id, Document.deleted_at.is_(None), DocumentVersion.publish_status == "effective")
        .order_by(Document.id, DocumentVersion.id).execution_options(populate_existing=True)).all()
    published_per_document = Counter(document.id for _, document in versions)
    sources = []
    for version, document in versions:
        item = {"document_id": document.id, "version_id": version.id, "run_id": None,
                "chunk_set_id": version.selected_chunk_set_id, "review_set_id": version.selected_review_set_id,
                "blocks": [], "reason": None}
        sources.append(item)
        if published_per_document[document.id] != 1:
            item["reason"] = "multiple_effective_versions"
            continue
        selected = selected_parse(db, version)
        if not selected:
            item["reason"] = "selected_source_not_ready"
            continue
        run, chunk_set = selected
        item["run_id"] = run.id
        if run.ir_schema_version or run.ir_artifact_id or chunk_set.config_hash:
            item["reason"] = "semantic_source_requires_child_index"
            continue
        if not version.model_scope or not governance.publication_is_current(db, version):
            item["reason"] = "publication_not_current"
            continue
        blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)
            .order_by(ContentBlock.ordinal, ContentBlock.id).execution_options(populate_existing=True)).all()
        if not blocks or any(block.review_status != "approved" or not block.locator or not block.text_content.strip()
                             for block in blocks):
            item["reason"] = "block_coverage_incomplete"
            continue
        event_id = db.scalar(select(PublicationEvent.id).where(PublicationEvent.document_version_id == version.id,
            PublicationEvent.action == "publish").order_by(PublicationEvent.created_at.desc(), PublicationEvent.id.desc()))
        if not event_id:
            item["reason"] = "publication_event_missing"
            continue
        item.update(blocks=[[block.id, governance.block_binding(block)] for block in blocks],
                    publication_event_id=event_id, qualification_hash=governance.digest(governance.qualification_snapshot(db, version)))
    return {"knowledge_base_id": kb.id, "source_revision": kb.content_revision, "provider": "local-keyword",
            "expected_sources": sources}


def create_index(db: Session, kb: KnowledgeBase, actor: User, *, request: Request | None = None) -> IndexGeneration:
    """Build a complete legacy lexical candidate without changing the active pointer."""
    from .governance import digest
    from .indexing import lock_child_library

    kb, _ = lock_child_library(db, kb.id, actor, request=request)
    manifest = _legacy_index_manifest(db, kb)
    sources = manifest["expected_sources"]
    eligible = [item for item in sources if item["reason"] is None]
    blocked = [{"version_id": item["version_id"], "reason": item["reason"]} for item in sources if item["reason"]]
    if not sources:
        blocked.append({"reason": "no_current_published_sources"})
    report = {"kind": "legacy", "provider": "local-keyword", "manifest": manifest, "manifest_hash": digest(manifest),
              "included_versions": [item["version_id"] for item in eligible],
              "integrity": {"status": "failed" if blocked else "passed", "blockers": blocked}}
    generation = IndexGeneration(knowledge_base_id=kb.id, embedding_profile_id="local-keyword",
        status="building", source_revision=kb.content_revision,
        expected_chunk_count=sum(len(item["blocks"]) for item in eligible), build_report=report)
    db.add(generation)
    db.flush()
    db.add_all(IndexSource(index_generation_id=generation.id, document_version_id=item["version_id"],
                          parse_run_id=item["run_id"], chunk_set_id=item["chunk_set_id"]) for item in eligible)
    generation.status = "failed" if blocked else "ready"
    generation.ready_at = None if blocked else utc_now()
    audit(db, actor.id, "index.build_" + generation.status, "index_generation", generation.id,
          source_revision=kb.content_revision, manifest_hash=report["manifest_hash"])
    db.flush()
    return generation


def legacy_index_is_current(db: Session, kb: KnowledgeBase, generation: IndexGeneration | None) -> bool:
    from collections import Counter
    from .governance import digest

    if (not generation or generation.knowledge_base_id != kb.id or generation.status != "ready"
            or not generation.ready_at or generation.source_revision != kb.content_revision or kb.status != "active"
            or generation.embedding_profile_id != "local-keyword" or generation.build_report.get("kind") != "legacy"
            or generation.build_report.get("integrity", {}).get("status") != "passed"):
        return False
    manifest = generation.build_report.get("manifest")
    live = _legacy_index_manifest(db, kb)
    sources = live["expected_sources"]
    if (manifest != live or generation.build_report.get("manifest_hash") != digest(live)
            or not sources or any(item["reason"] for item in sources)
            or generation.expected_chunk_count != sum(len(item["blocks"]) for item in sources)):
        return False
    found = db.execute(select(IndexSource.document_version_id, IndexSource.parse_run_id, IndexSource.chunk_set_id)
                      .where(IndexSource.index_generation_id == generation.id)).all()
    return Counter(map(tuple, found)) == Counter((item["version_id"], item["run_id"], item["chunk_set_id"]) for item in sources)


def switch_legacy_index(db: Session, kb: KnowledgeBase, generation_id: str, actor: User, *,
                        expected_active_id: str | None, expected_source_revision: int, action: str = "activate",
                        request: Request | None = None) -> IndexGeneration:
    """Explicit activation and rollback share the same current-source gate."""
    from .governance_models import GovernanceOutbox
    from .indexing import lock_child_library

    if action not in {"activate", "rollback"}:
        raise SafeError("INDEX_SWITCH_ACTION_INVALID", "不支持索引切换操作", 422)
    kb, _ = lock_child_library(db, kb.id, actor, request=request)
    generation = db.scalar(select(IndexGeneration).where(IndexGeneration.id == generation_id,
        IndexGeneration.knowledge_base_id == kb.id).with_for_update().execution_options(populate_existing=True))
    if not generation or generation.build_report.get("kind") != "legacy":
        raise SafeError("INDEX_GENERATION_NOT_FOUND", "未找到旧式索引代际", 404)
    if kb.active_generation_id != expected_active_id or kb.content_revision != expected_source_revision:
        raise SafeError("INDEX_PRECONDITION_CHANGED", "知识库版本或活动代际已变化", 412)
    if not legacy_index_is_current(db, kb, generation):
        raise SafeError("INDEX_GENERATION_NOT_READY", "索引代际的来源、成员或审核资格不再完整", 409)
    report = generation.build_report
    if action == "rollback" and not report.get("activation_count"):
        raise SafeError("INDEX_ROLLBACK_TARGET_NOT_PREVIOUSLY_ACTIVE", "仅可回滚到曾启用的当前代际", 409)
    if kb.active_generation_id == generation.id:
        return generation
    changed = db.execute(update(KnowledgeBase).where(KnowledgeBase.id == kb.id,
        KnowledgeBase.content_revision == expected_source_revision, KnowledgeBase.active_generation_id == expected_active_id)
        .values(active_generation_id=generation.id).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise SafeError("INDEX_PRECONDITION_CHANGED", "知识库版本或活动代际已变化", 412)
    db.expire(kb, ["active_generation_id"])
    generation.build_report = {**report, "activation_count": report.get("activation_count", 0) + 1}
    action_name = "index.rolled_back" if action == "rollback" else "index.activated"
    db.add(GovernanceOutbox(event_kind=action_name, resource_id=generation.id, revision=generation.source_revision))
    audit(db, actor.id, action_name, "index_generation", generation.id, previous_generation_id=expected_active_id,
          profile_id=generation.embedding_profile_id, manifest_hash=report["manifest_hash"], source_revision=generation.source_revision)
    db.flush()
    return generation
