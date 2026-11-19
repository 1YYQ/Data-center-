"""Feedback writes and reads stay within the current authority of their sources."""
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Query, Request, Response
from pydantic import BaseModel, ConfigDict, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .execution import accept_query
from .execution_models import Conversation, ConversationMessage
from .feedback_models import DocumentFeedback, FeedbackDependency
from .governance import _tuple_allowed, can_read_original_version, can_read_version, version_access_conditions
from .models import (AuthSession, Document, DocumentVersion, FileObject, IndexGeneration, IndexSource,
                     KnowledgeBase, QueryTask, TaskEvidence, User, utc_now)
from .schemas import QueryInput
from .services import (SafeError, audit, can_access_document, can_read_task, check_if_match, complete_idempotency,
                       current_session, etag, has_permission, idempotency_record, require_csrf, selected_parse)


Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
ResourceId = Annotated[str, StringConstraints(min_length=1, max_length=36)]


class FeedbackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: ResourceId | None = None
    document_version_id: ResourceId | None = None
    text: Note


class HandlingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["processing", "replied"]
    reply: Note | None = None


class RequeryInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: Note | None = None


def missing():
    return SafeError("NOT_FOUND", "资源不存在", 404)


def original_is_readable(db, actor, version):
    original = db.get(FileObject, version.source_file_id, populate_existing=True) if version else None
    return bool(original and original.status == "available" and can_read_original_version(db, actor, version))


def owner_task(db, actor, task_id, *, lock=False):
    task = db.get(QueryTask, task_id, populate_existing=True, with_for_update=lock)
    if not task or task.owner_user_id != actor.id or task.kind != "query" or not has_permission(actor, "task.read"):
        raise missing()
    return task


def version_dependencies(db, row):
    return db.scalars(select(DocumentVersion).join(FeedbackDependency,
        FeedbackDependency.version_id == DocumentVersion.id).where(FeedbackDependency.feedback_id == row.id)
        .order_by(DocumentVersion.id).execution_options(populate_existing=True)).all()


def fence_sources(db, versions, session, *user_ids):
    document_ids = {version.document_id for version in versions}
    kb_ids = db.scalars(select(Document.knowledge_base_id).where(Document.id.in_(document_ids))
                        .order_by(Document.id).with_for_update(read=True)).all()
    db.scalars(select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids))
               .order_by(KnowledgeBase.id).with_for_update(read=True)).all()
    db.scalar(select(AuthSession.id).where(AuthSession.id == session.id).with_for_update(read=True))
    db.scalars(select(User.id).where(User.id.in_(user_ids)).order_by(User.id).with_for_update(read=True)).all()


def reporter_can_read(db, actor, row, versions):
    if not actor or actor.status != "active" or not has_permission(actor, "feedback.read_own"):
        return False
    if row.original_version_id:
        return len(versions) == 1 and original_is_readable(db, actor, versions[0])
    return all(can_read_version(db, actor, version, historical=True) for version in versions)


def can_handle(db, actor, versions):
    if not versions or not has_permission(actor, "feedback.handle"):
        return False
    for version in versions:
        document = db.get(Document, version.document_id, populate_existing=True)
        conditions = version_access_conditions(db, version)
        if (not conditions or not can_access_document(db, actor, document, "knowledge.manage")
                or not can_access_document(db, actor, document, "feedback.handle")
                or not _tuple_allowed(db, actor, document, "feedback.handle", conditions)):
            return False
    return True


def available_updates(db, actor, versions):
    """Only a newer current version in the ready index may offer a requery."""
    updates = []
    for old in versions:
        doc = db.get(Document, old.document_id, populate_existing=True)
        kb = db.get(KnowledgeBase, doc.knowledge_base_id, populate_existing=True)
        current = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == doc.id,
            DocumentVersion.publish_status == "effective").execution_options(populate_existing=True)).all()
        version = current[0] if len(current) == 1 else None
        if (not version or version.version_no <= old.version_no
                or not can_read_version(db, actor, version) or not can_read_version(db, actor, version, "knowledge.query")):
            continue
        generation = db.get(IndexGeneration, kb.active_generation_id, populate_existing=True) if kb.active_generation_id else None
        selected = selected_parse(db, version)
        if (generation and generation.status in {"ready", "ready_partial"} and generation.source_revision == kb.content_revision
                and selected and db.scalar(select(IndexSource.id).where(IndexSource.index_generation_id == generation.id,
                    IndexSource.document_version_id == version.id, IndexSource.parse_run_id == selected[0].id,
                    IndexSource.chunk_set_id == selected[1].id))):
            updates.append(version)
    return updates


def employee_view(db, actor, row, versions):
    if not reporter_can_read(db, actor, row, versions):
        return {"id": row.id, "status": "access_limited"}
    status = "awaiting_routing" if row.handler_id is None else row.status
    return {"id": row.id, "status": status, "text": row.text, "reply": row.reply,
            "responded_at": row.responded_at, "update_available": bool(available_updates(db, actor, versions))}


def manual_view(db, version):
    document = db.get(Document, version.document_id)
    return {"title": document.title, "revision_label": version.revision_label or str(version.version_no),
            "reference_only": version.publish_status == "draft",
            "file_url": f"/documents/{document.id}/versions/{version.id}/file"}


def handler_view(db, row, actor, versions):
    assignment = "unclaimed" if row.handler_id is None else "mine" if row.handler_id == actor.id else "assigned"
    return {"id": row.id, "status": row.status, "text": row.text, "reply": row.reply,
            "assignment": assignment, "revision": row.revision,
            "sources": [manual_view(db, version) for version in versions]}


def register_feedback_routes(app: FastAPI) -> None:
    def authenticate(request, db, permission, *, write=False, heartbeat=True):
        actor, session = current_session(request, db, request.app.state.settings, persist_heartbeat=heartbeat)
        if not has_permission(actor, permission):
            raise missing()
        if write:
            require_csrf(request, session)
        return actor, session

    def receipt_for(request, db, actor, body):
        return idempotency_record(db, actor, request, request.headers.get("Idempotency-Key", ""), body.model_dump_json().encode())

    def complete(db, actor, receipt, row, action, status):
        audit(db, actor.id, action, "document_feedback", row.id)
        complete_idempotency(db, receipt, status_code=status, refs={"feedback_id": row.id})
        db.commit()

    def feedback_row(db, feedback_id, *, lock=False):
        row = db.get(DocumentFeedback, feedback_id, populate_existing=True, with_for_update=lock)
        if row is None:
            raise missing()
        return row

    def verify_manual(db, actor, version_id):
        version = db.get(DocumentVersion, version_id, populate_existing=True)
        if not original_is_readable(db, actor, version):
            raise missing()
        return version

    def origin(task_id, version_id):
        if (task_id is None) == (version_id is None):
            raise SafeError("FEEDBACK_ORIGIN_REQUIRED", "请选择一个查询或手册版本", 400)

    @app.post("/document-feedback", status_code=201)
    def submit(body: FeedbackInput, request: Request, response: Response, db: Session = Depends(get_db)):
        actor, session = authenticate(request, db, "feedback.create", write=True)
        response.headers["Cache-Control"] = "private, no-store"
        origin(body.task_id, body.document_version_id)
        receipt, replay = receipt_for(request, db, actor, body)
        if replay:
            row = feedback_row(db, receipt.resource_refs["feedback_id"])
            if row.reporter_id != actor.id:
                raise missing()
            return employee_view(db, actor, row, version_dependencies(db, row))
        if body.task_id:
            task = owner_task(db, actor, body.task_id, lock=True)
            conversation = db.get(Conversation, task.conversation_id) if task.conversation_id else None
            message = db.get(ConversationMessage, task.source_message_id) if task.source_message_id else None
            if (not conversation or conversation.owner_user_id != actor.id or not message
                    or message.conversation_id != conversation.id or message.author_user_id != actor.id
                    or message.role != "user" or message.purpose != "query"):
                raise missing()
            versions = db.scalars(select(DocumentVersion).where(DocumentVersion.id.in_(
                select(TaskEvidence.document_version_id).where(TaskEvidence.task_id == task.id,
                    TaskEvidence.execution_revision == task.execution_revision)))).all()
        else:
            versions = [verify_manual(db, actor, body.document_version_id)]
        fence_sources(db, versions, session, actor.id)
        actor, _ = authenticate(request, db, "feedback.create", heartbeat=False)
        row = DocumentFeedback(reporter_id=actor.id, task_id=body.task_id, original_version_id=body.document_version_id,
                               text=body.text)
        if not reporter_can_read(db, actor, row, versions):
            raise missing()
        db.add(row)
        db.flush()
        db.add_all([FeedbackDependency(feedback_id=row.id, version_id=version.id) for version in versions])
        complete(db, actor, receipt, row, "feedback.submitted", 201)
        return employee_view(db, actor, row, versions)

    @app.get("/document-feedback")
    def employee_list(request: Request, response: Response, task_id: ResourceId | None = None,
                      document_version_id: ResourceId | None = None, cursor: ResourceId | None = None,
                      limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)):
        actor, _ = authenticate(request, db, "feedback.read_own")
        response.headers["Cache-Control"] = "private, no-store"
        origin(task_id, document_version_id)
        manual = None
        if task_id:
            owner_task(db, actor, task_id)
            source = DocumentFeedback.task_id == task_id
        else:
            manual = manual_view(db, verify_manual(db, actor, document_version_id))
            source = DocumentFeedback.original_version_id == document_version_id
        statement = select(DocumentFeedback).where(DocumentFeedback.reporter_id == actor.id, source)
        if cursor:
            statement = statement.where(DocumentFeedback.id > cursor)
        rows = db.scalars(statement.order_by(DocumentFeedback.id).limit(limit + 1)).all()
        return {"items": [employee_view(db, actor, row, version_dependencies(db, row)) for row in rows[:limit]], "manual": manual,
                "next_cursor": rows[limit - 1].id if len(rows) > limit else None}

    @app.get("/document-feedback/{feedback_id}")
    def detail(feedback_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
        actor, _ = authenticate(request, db, "feedback.read_own")
        response.headers["Cache-Control"] = "private, no-store"
        row = feedback_row(db, feedback_id)
        if row.reporter_id != actor.id:
            raise missing()
        return employee_view(db, actor, row, version_dependencies(db, row))

    @app.get("/knowledge/feedback")
    def inbox(request: Request, response: Response, cursor: ResourceId | None = None,
              limit: int = Query(50, ge=1, le=100), db: Session = Depends(get_db)):
        actor, _ = authenticate(request, db, "feedback.handle")
        response.headers["Cache-Control"] = "private, no-store"
        statement = select(DocumentFeedback).where(DocumentFeedback.reporter_id != actor.id,
            select(FeedbackDependency.feedback_id).where(FeedbackDependency.feedback_id == DocumentFeedback.id).exists())
        if cursor:
            statement = statement.where(DocumentFeedback.id > cursor)
        visible = []
        for row in db.scalars(statement.order_by(DocumentFeedback.id)):
            versions = version_dependencies(db, row)
            if can_handle(db, actor, versions):
                visible.append(handler_view(db, row, actor, versions))
                if len(visible) > limit:
                    break
        return {"items": visible[:limit], "next_cursor": visible[limit - 1]["id"] if len(visible) > limit else None}

    @app.patch("/knowledge/feedback/{feedback_id}")
    def handle(feedback_id: str, body: HandlingInput, request: Request, response: Response, db: Session = Depends(get_db)):
        actor, session = authenticate(request, db, "feedback.handle", write=True)
        response.headers["Cache-Control"] = "private, no-store"
        receipt, replay = receipt_for(request, db, actor, body)
        row = feedback_row(db, feedback_id, lock=True)
        versions = version_dependencies(db, row)
        fence_sources(db, versions, session, actor.id, row.reporter_id)
        actor, _ = authenticate(request, db, "feedback.handle", heartbeat=False)
        if row.reporter_id == actor.id or not can_handle(db, actor, versions):
            raise missing()
        if not replay:
            check_if_match(request.headers.get("If-Match"), row.revision)
            if body.status == "processing" and not body.reply and row.status == "submitted":
                row.handler_id = actor.id
            elif body.status == "replied" and body.reply and row.status == "processing" and row.handler_id == actor.id:
                reporter = db.get(User, row.reporter_id, populate_existing=True)
                if not reporter_can_read(db, reporter, row, versions):
                    raise SafeError("FEEDBACK_RECIPIENT_RESTRICTED", "当前资料权限不允许交付答复", 409)
                row.reply, row.responded_at = body.reply, utc_now()
            else:
                raise SafeError("FEEDBACK_STATE_CHANGED", "反馈状态或受理人已改变，请刷新", 409)
            row.status = body.status
            row.revision += 1
            complete(db, actor, receipt, row, "feedback." + row.status, 200)
        response.headers["ETag"] = etag(row.revision)
        return handler_view(db, row, actor, versions)

    @app.post("/document-feedback/{feedback_id}/requery", status_code=202)
    def requery(feedback_id: str, body: RequeryInput, request: Request, response: Response, db: Session = Depends(get_db)):
        actor, session = authenticate(request, db, "knowledge.query", write=True)
        response.headers["Cache-Control"] = "private, no-store"
        receipt, replay = receipt_for(request, db, actor, body)
        row = feedback_row(db, feedback_id, lock=True)
        if row.reporter_id != actor.id:
            raise missing()
        task = owner_task(db, actor, row.task_id, lock=True) if row.task_id else None
        versions = version_dependencies(db, row)
        fence_sources(db, versions, session, actor.id)
        actor, session = authenticate(request, db, "knowledge.query", heartbeat=False)
        if not reporter_can_read(db, actor, row, versions):
            raise missing()
        if replay:
            return {"task_id": receipt.resource_refs["task_id"]}
        updates = available_updates(db, actor, versions)
        if not updates:
            raise SafeError("FEEDBACK_UPDATE_NOT_READY", "尚无可用于重新查询的资料更新", 409)
        kb_ids = {db.get(Document, version.document_id).knowledge_base_id for version in updates}
        if task:
            conversation = db.get(Conversation, task.conversation_id, populate_existing=True, with_for_update=True)
            if not conversation or conversation.owner_user_id != actor.id:
                raise missing()
            saved = task.input_snapshot
            requested = saved.get("knowledge_base_ids") or []
            kb_ids = kb_ids.intersection(requested) if requested else kb_ids
            if not kb_ids:
                raise SafeError("FEEDBACK_UPDATE_NOT_READY", "当前查询范围内没有可用资料更新", 409)
            payload = QueryInput(query=saved["query"], knowledge_base_ids=requested, conversation_id=conversation.id,
                context_revision=conversation.revision, **{key: saved.get(key) for key in ("level", "category", "need_class")})
        else:
            if not body.query:
                raise SafeError("FEEDBACK_QUERY_REQUIRED", "请填写要重新查询的问题", 400)
            payload = QueryInput(query=body.query, knowledge_base_ids=sorted(kb_ids))
        new_task = accept_query(db, actor, session, payload.model_dump(exclude={"service_mode"}), receipt.idempotency_key)
        audit(db, actor.id, "feedback.requeried", "document_feedback", row.id)
        complete_idempotency(db, receipt, status_code=202, refs={"task_id": new_task.id})
        db.commit()
        return {"task_id": new_task.id}
