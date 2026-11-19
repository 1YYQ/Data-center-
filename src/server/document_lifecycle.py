"""Document tombstone metadata and logical deletion; never physical purge."""
from __future__ import annotations

from typing import Any
from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session
from .models import AuditEvent, Document, DocumentVersion, FileObject, User
from .governance_models import BusinessConfirmation, DocumentUseHold, ReviewSet
from .services import (SafeError, can_access_document, check_if_match,
    complete_idempotency, etag, fence_management_authority, idempotency_record)


def require_metadata_management(db: Session, actor: User, document: Document) -> None:
    """Deleted documents allow only this purpose-specific metadata projection."""
    if not can_access_document(db, actor, document, "knowledge.manage", include_deleted=True):
        raise SafeError("NOT_FOUND", "资源不存在", 404)


def document_metadata(db: Session, document: Document) -> dict[str, Any]:
    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id)
        .order_by(DocumentVersion.version_no.desc()).execution_options(populate_existing=True)).all()
    current = next((v.id for v in versions if v.publish_status == "effective"), None)
    return {"id": document.id, "title": document.title, "knowledge_base_id": document.knowledge_base_id,
        "revision": document.revision, "etag": etag(document.revision),
        "state": "deleted" if document.deleted_at else "active",
        "deleted_at": document.deleted_at.isoformat() if document.deleted_at else None,
        "current_version_id": current,
        "versions": [{"id": v.id, "version_no": v.version_no, "revision": v.revision, "etag": etag(v.revision), "revision_label": v.revision_label,
            "publish_status": v.publish_status, "reference_access_state": v.reference_access_state} for v in versions]}


def eligible_resolution_refs(db: Session, version: DocumentVersion, hold: DocumentUseHold) -> list[str]:
    from .governance import _utc, confirmation_is_current, review_set_is_current
    if hold.released_at:
        return []
    if hold.kind == "quality":
        rows = db.scalars(select(ReviewSet).where(ReviewSet.document_version_id == version.id)
            .execution_options(populate_existing=True)).all()
        return [r.id for r in rows if _utc(r.reviewed_at) >= _utc(hold.created_at)
            and review_set_is_current(db, version, r)]
    if hold.kind == "conflict":
        rows = db.scalars(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == version.id,
            BusinessConfirmation.kind == "applicability", BusinessConfirmation.conclusion == "applicable")
            .execution_options(populate_existing=True)).all()
        return [r.id for r in rows if _utc(r.recorded_at) >= _utc(hold.created_at)
            and confirmation_is_current(db, r)]
    return []


_DOCUMENT_ACTIONS = frozenset({"document.uploaded", "document.deleted", "lifecycle.invalidated", "document.updated"})
_VERSION_ACTIONS = frozenset({"document.version_created", "version.updated", "parse.block_reviewed",
    "confirmation.registered", "confirmation.verified", "review_set.created", "version.published",
    "version.withdrawn", "version.held", "version.hold_released", "version.reference_access_changed",
    "version.source_metadata_changed"})


def lifecycle_history(db: Session, document: Document, version_ids: list[str]) -> list[dict]:
    from .services import SAFE_REASON_CODES
    rows = db.scalars(select(AuditEvent).where(or_(
        and_(AuditEvent.resource_type == "document", AuditEvent.resource_id == document.id,
             AuditEvent.action.in_(_DOCUMENT_ACTIONS)),
        and_(AuditEvent.resource_type == "document_version", AuditEvent.resource_id.in_(version_ids),
             AuditEvent.action.in_(_VERSION_ACTIONS))))
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())).all()
    return [{"event_id": r.id, "action": r.action, "actor_id": r.actor_user_id,
        "created_at": r.created_at.isoformat(),
        "reason": r.reason if r.reason in SAFE_REASON_CODES else ("operator_provided" if r.reason else None),
        "resource_ref": {"type": r.resource_type, "id": r.resource_id}} for r in rows]


def lifecycle_view(db: Session, actor: User, document: Document) -> dict:
    from .governance_api import hold_view
    require_metadata_management(db, actor, document)
    metadata = document_metadata(db, document)
    versions = []
    for item in metadata["versions"]:
        version = db.get(DocumentVersion, item["id"], populate_existing=True)
        source = db.get(FileObject, version.source_file_id, populate_existing=True)
        holds = db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == version.id)
            .order_by(DocumentUseHold.created_at, DocumentUseHold.id).execution_options(populate_existing=True)).all()
        versions.append({**item, "original": {"status": source.status if source else "missing"},
            "holds": [{**hold_view(db, h), "eligible_resolution_refs": [] if document.deleted_at else eligible_resolution_refs(db, version, h)} for h in holds]})
    return {"document": metadata, "versions": versions,
        "purge_allowed": False, "retention_policy": "unconfigured", "retention_state": "retained",
        "physical_integrity_verified": False,
        "blocked_reasons": ["RETENTION_POLICY_UNCONFIGURED", "PERMANENT_PURGE_NOT_AVAILABLE"],
        "history": lifecycle_history(db, document, [v["id"] for v in versions]), "etag": metadata["etag"]}


def deletion_impact(db: Session, actor: User, document: Document) -> dict:
    require_metadata_management(db, actor, document)
    metadata = document_metadata(db, document)
    return {"document": metadata, "document_etag": metadata["etag"], "version_count": len(metadata["versions"]),
        "effect": "current_access_blocked" if document.deleted_at else "logical_access_will_be_blocked",
        "originals_retained": True, "physical_integrity_verified": False,
        "retention_policy": "unconfigured", "purge_allowed": False,
        "blocked_reasons": ["RETENTION_POLICY_UNCONFIGURED", "PERMANENT_PURGE_NOT_AVAILABLE"]}


def delete_document(db: Session, actor: User, request: Any, document: Document,
                    if_match: str | None, key: str, reason: str, normalized_body: bytes, *,
                    enforce_session: bool = False) -> None:
    """Commit the logical tombstone and its original idempotent receipt together."""
    from .operations import invalidate_dependents, logical_delete_document
    require_metadata_management(db, actor, document)
    record, replayed = idempotency_record(db, actor, request, key, normalized_body)
    if not replayed:
        if document.deleted_at:
            raise SafeError("DOCUMENT_ALREADY_DELETED", "资料已逻辑删除，不会重复删除", 409)
        expected = document.revision
        check_if_match(if_match, expected)
        claimed = db.execute(update(Document).where(Document.id == document.id,
            Document.revision == expected, Document.deleted_at.is_(None)).values(revision=Document.revision)
            .execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise SafeError("PRECONDITION_FAILED", "资源已发生变化，请刷新后重试", 412)
        db.refresh(document)
        require_metadata_management(db, actor, document)
        logical_delete_document(db, document, actor.id, reason)
        invalidate_dependents(db, document.id, actor.id, "文档删除")
    actor = fence_management_authority(db, actor, document.knowledge_base_id, document_id=document.id,
        request=request if enforce_session else None)
    require_metadata_management(db, actor, document)
    if not replayed:
        complete_idempotency(db, record, status_code=204, refs={"document_id": document.id})
        db.commit()
