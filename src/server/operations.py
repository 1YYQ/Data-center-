"""Lifecycle and operational helpers with intentionally sanitized outputs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from .models import Document, DocumentVersion, KnowledgeBase, ProviderCall, QueryTask, TaskEvidence, utc_now
from .services import audit


def logical_delete_document(db: Session, document: Document, actor_id: str, reason: str) -> None:
    document.deleted_at = utc_now()
    document.revision += 1
    kb = db.get(KnowledgeBase, document.knowledge_base_id)
    if kb:
        db.execute(update(KnowledgeBase).where(KnowledgeBase.id == kb.id).values(
            content_revision=KnowledgeBase.content_revision + 1, acl_revision=KnowledgeBase.acl_revision + 1)
            .execution_options(synchronize_session=False))
        db.refresh(kb)
    from .governance_models import GovernanceOutbox
    db.add(GovernanceOutbox(event_kind="document.deleted", resource_id=document.id, revision=document.revision))
    audit(db, actor_id, "document.deleted", "document", document.id, reason=reason)
    db.flush()


def invalidate_dependents(db: Session, document_id: str, actor_id: str, reason: str) -> dict[str, int]:
    """Mark dependent task results inaccessible on the next read.

    Evidence remains as a historical snapshot for audit, while every result
    endpoint re-checks the current document and grant state.
    """
    version_ids = [row.id for row in db.execute(select(DocumentVersion).where(DocumentVersion.document_id == document_id)).scalars()]
    task_ids = [row.task_id for row in db.execute(select(TaskEvidence).where(TaskEvidence.document_version_id.in_(version_ids))).scalars()] if version_ids else []
    audit(db, actor_id, "lifecycle.invalidated", "document", document_id, reason=reason, task_count=len(set(task_ids)))
    return {"document_versions": len(version_ids), "tasks": len(set(task_ids))}


def record_provider_call(db: Session, actor_id: str, provider: str, operation: str, status: str, data_scope: str, cost_units: float, error_code: str | None = None) -> ProviderCall:
    call = ProviderCall(provider=provider, operation=operation, status=status, data_scope=data_scope, cost_units=cost_units, error_code=error_code)
    db.add(call)
    audit(db, actor_id, "provider.call_recorded", "provider", None, provider=provider, operation=operation, status=status, data_scope=data_scope, cost_units=cost_units, error_code=error_code)
    return call


def sanitized_provider_view(call: ProviderCall) -> dict[str, Any]:
    return {"id": call.id, "provider": call.provider, "operation": call.operation, "status": call.status, "data_scope": call.data_scope, "cost_units": call.cost_units, "error_code": call.error_code, "created_at": call.created_at.isoformat() if call.created_at else None}


def sanitized_task_view(task: QueryTask) -> dict[str, Any]:
    return {"id": task.id, "kind": task.kind, "status": task.status, "error_code": task.error_code, "created_at": task.created_at.isoformat() if task.created_at else None, "finished_at": task.finished_at.isoformat() if task.finished_at else None}
