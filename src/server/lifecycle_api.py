"""Purpose-limited document lifecycle reads; no restore or purge endpoint."""
from __future__ import annotations

from typing import Literal
from fastapi import Depends, FastAPI, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session
from .db import get_db
from .models import Document, KnowledgeBase
from .services import SafeError, can_access_document, current_session, has_permission
from .document_lifecycle import deletion_impact, document_metadata, lifecycle_view


def register_lifecycle_routes(app: FastAPI) -> None:
    def actor(request, db):
        return current_session(request, db, request.app.state.settings)[0]

    def headers(response, etag=None):
        response.headers["Cache-Control"] = "private, no-store"
        if etag:
            response.headers["ETag"] = etag

    def document(db, document_id):
        row = db.get(Document, document_id, populate_existing=True)
        if not row:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return row

    @app.get("/knowledge-bases/{kb_id}/document-lifecycle")
    def list_lifecycle(kb_id: str, request: Request, response: Response,
                       state: Literal["active", "deleted", "all"] = "active", cursor: str | None = None,
                       limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)):
        user = actor(request, db)
        kb = db.get(KnowledgeBase, kb_id, populate_existing=True)
        if not kb or kb.status != "active" or not has_permission(user, "knowledge.manage"):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        statement = select(Document).where(Document.knowledge_base_id == kb_id)
        if state != "all":
            statement = statement.where(Document.deleted_at.is_not(None) if state == "deleted" else Document.deleted_at.is_(None))
        rows = db.scalars(statement.order_by(Document.id).execution_options(populate_existing=True)).all()
        visible = [row for row in rows if can_access_document(db, user, row, "knowledge.manage", include_deleted=True)]
        if cursor:
            visible = [row for row in visible if row.id > cursor]
        selected = visible[:limit]
        headers(response)
        return {"items": [document_metadata(db, row) for row in selected],
            "next_cursor": selected[-1].id if selected and len(visible) > limit else None}

    @app.get("/documents/{document_id}/lifecycle")
    def get_lifecycle(document_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
        user = actor(request, db)
        result = lifecycle_view(db, user, document(db, document_id))
        headers(response, result["etag"])
        return result

    @app.get("/documents/{document_id}/deletion-impact")
    def get_deletion_impact(document_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
        user = actor(request, db)
        result = deletion_impact(db, user, document(db, document_id))
        headers(response, result["document_etag"])
        return result
