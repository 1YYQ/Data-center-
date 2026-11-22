"""Read-only workspace projections. No acceptance, dispatch or provider effects."""
from fastapi import Depends, FastAPI, Query, Request
from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session
from sqlalchemy.orm import aliased

from .db import get_db
from .models import ApiIdempotencyRecord, QueryTask, TaskShare, utc_now
from .services import SafeError, can_read_task, task_view


def register_workspace_routes(app: FastAPI):
    from .app import _user_or_401

    def page_view(db, actor, statement, limit, offset):
        rows = list(db.scalars(statement.order_by(QueryTask.created_at.desc(), QueryTask.id.desc()).offset(offset).limit(limit + 1)))
        items = []
        for task in rows[:limit]:
            if can_read_task(db, actor, task):
                data = task_view(task, viewer_user_id=actor.id)
                items.append({key: data[key] for key in ("id", "kind", "status", "input", "conversation_id", "created_at", "revision")})
            elif task.owner_user_id == actor.id:
                items.append({"id": task.id, "status": "access_limited"})
        return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    @app.get("/workspace-data/works")
    def works(request: Request, limit: int = Query(10, ge=1, le=50), offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
        actor, _ = _user_or_401(request, db)
        newer = aliased(QueryTask)
        later = select(newer.id).where(newer.owner_user_id == actor.id,
            newer.conversation_id == QueryTask.conversation_id,
            or_(newer.created_at > QueryTask.created_at, and_(newer.created_at == QueryTask.created_at, newer.id > QueryTask.id))).exists()
        own_head = and_(QueryTask.owner_user_id == actor.id, or_(QueryTask.conversation_id.is_(None), ~later))
        shared = select(TaskShare.id).where(TaskShare.task_id == QueryTask.id, TaskShare.recipient_user_id == actor.id,
            TaskShare.granted_by == QueryTask.owner_user_id, TaskShare.status == "active", TaskShare.revoked_at.is_(None),
            or_(TaskShare.expires_at.is_(None), TaskShare.expires_at > utc_now())).exists()
        return page_view(db, actor, select(QueryTask).where(or_(own_head, shared)), limit, offset)

    @app.get("/conversations/{conversation_id}/executions")
    def executions(conversation_id: str, request: Request, limit: int = Query(10, ge=1, le=50),
                   offset: int = Query(0, ge=0), db: Session = Depends(get_db)):
        actor, _ = _user_or_401(request, db)
        # Sharing an answer never grants the owner's conversation or other executions.
        from .execution_models import Conversation
        conversation = db.get(Conversation, conversation_id)
        if not conversation or conversation.owner_user_id != actor.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return page_view(db, actor, select(QueryTask).where(QueryTask.conversation_id == conversation_id,
            QueryTask.owner_user_id == actor.id), limit, offset)

    @app.get("/operation-receipts")
    def operation_receipt(request: Request, key: str = Query(min_length=1, max_length=200),
                          db: Session = Depends(get_db)):
        actor, _ = _user_or_401(request, db)
        row = db.scalar(select(ApiIdempotencyRecord).where(
            ApiIdempotencyRecord.actor_user_id == actor.id,
            ApiIdempotencyRecord.idempotency_key == key))
        # An absent row is not proof that an in-flight transaction won't commit.
        if not row or row.status != "completed":
            return {"state": "unresolved", "retry_allowed": False, "resource_refs": {}}
        refs = {}
        if row.resource_refs.get("task_id"):
            task = db.get(QueryTask, row.resource_refs["task_id"])
            if not task or task.owner_user_id != actor.id or not can_read_task(db, actor, task):
                raise SafeError("NOT_FOUND", "资源不存在", 404)
            refs["task_id"] = task.id
        # Never return stored request bodies or arbitrary business object refs.
        return {"state": "completed", "method": row.method, "path": row.request_path,
                "retry_allowed": False, "resource_refs": refs}
