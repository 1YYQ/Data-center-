"""Authenticated input responses and scoped conditions; GET never executes."""
from typing import Annotated
from fastapi import Depends, FastAPI, Header, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import get_db
from .models import QueryTask
from .execution_models import Conversation, ContextFact, TaskInputRequest
from .execution import _fact, _message, respond_model
from .services import SafeError, can_query_kb, check_if_match, complete_idempotency, idempotency_record, require_csrf


class InputResponse(BaseModel):
    model_config = {"extra": "forbid"}
    model: str = Field(min_length=1, max_length=200)


class ConditionInput(BaseModel):
    model_config = {"extra": "forbid"}
    model: str | None = Field(default=None, max_length=200)
    remove: bool = False
    knowledge_base_ids: list[str]


def register_execution_routes(app: FastAPI):
    from .app import _json_response, _user_or_401

    @app.get("/tasks/{task_id}/input-requests")
    def requests(task_id: str, request: Request, db: Session = Depends(get_db)):
        actor, _ = _user_or_401(request, db)
        task = db.get(QueryTask, task_id)
        if not task or task.owner_user_id != actor.id:
            raise SafeError("NOT_FOUND", "任务不存在", 404)
        items = db.scalars(select(TaskInputRequest).where(TaskInputRequest.task_id == task.id)).all()
        return {"items": [{"id": row.id, "field": row.requested_field, "reason": row.reason, "status": row.status,
            "revision": row.revision, "execution_revision": row.execution_revision} for row in items]}

    @app.post("/tasks/{task_id}/input-requests/{request_id}/responses")
    def response(task_id: str, request_id: str, payload: InputResponse, request: Request, response: Response,
                 if_match: Annotated[str | None, Header()] = None, idempotency_key: Annotated[str | None, Header()] = None, db: Session = Depends(get_db)):
        actor, auth = _user_or_401(request, db)
        require_csrf(request, auth)
        task, item = db.get(QueryTask, task_id), db.get(TaskInputRequest, request_id)
        if not task or task.owner_user_id != actor.id or not item or item.task_id != task.id:
            raise SafeError("NOT_FOUND", "任务不存在", 404)
        if any(not can_query_kb(db, actor, kb) for kb in task.accepted_scope.get("knowledge_base_ids", [])):
            raise SafeError("ACCESS_LIMITED", "任务访问条件已改变", 403)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if not replayed:
            check_if_match(if_match, item.revision)
            respond_model(db, actor, task, item, payload.model)
            complete_idempotency(db, record, status_code=200, refs={"task_id": task.id, "request_id": item.id})
            db.commit()
        return _json_response({"task_id": task.id, "status": task.status, "request_id": item.id, "execution_revision": task.execution_revision}, response, item.revision)

    def owned_conversation(db, actor, id):
        item = db.get(Conversation, id)
        if not item or item.owner_user_id != actor.id or item.status != "active":
            raise SafeError("NOT_FOUND", "对话不存在", 404)
        return item

    @app.get("/conversations/{conversation_id}/conditions")
    def conditions(conversation_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
        actor, _ = _user_or_401(request, db)
        conversation = owned_conversation(db, actor, conversation_id)
        latest = db.scalar(select(ContextFact).where(ContextFact.conversation_id == conversation.id, ContextFact.fact_key == "device.model").order_by(ContextFact.revision.desc()))
        item = {"key": latest.fact_key, "value": latest.value, "state": latest.state, "source_message_id": latest.source_message_id} if latest else None
        return _json_response({"conversation_id": conversation.id, "revision": conversation.revision, "condition": item}, response, conversation.revision)

    @app.post("/conversations/{conversation_id}/conditions")
    def change_condition(conversation_id: str, payload: ConditionInput, request: Request, response: Response,
                         if_match: Annotated[str | None, Header()] = None, idempotency_key: Annotated[str | None, Header()] = None, db: Session = Depends(get_db)):
        actor, auth = _user_or_401(request, db)
        require_csrf(request, auth)
        conversation = owned_conversation(db, actor, conversation_id)
        if any(not can_query_kb(db, actor, kb) for kb in payload.knowledge_base_ids):
            raise SafeError("ACCESS_LIMITED", "条件范围不在当前授权内", 403)
        if not payload.remove and not (payload.model or "").strip():
            raise SafeError("INVALID_MODEL", "设备型号不能为空", 422)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if not replayed:
            check_if_match(if_match, conversation.revision)
            message = _message(db, conversation, actor, "移除设备型号" if payload.remove else payload.model.strip(), "condition_edit")
            _fact(db, conversation, message, None if payload.remove else payload.model.strip(), payload.knowledge_base_ids, tombstone=payload.remove)
            complete_idempotency(db, record, status_code=200, refs={"conversation_id": conversation.id})
            db.commit()
        return _json_response({"conversation_id": conversation.id, "revision": conversation.revision}, response, conversation.revision)
