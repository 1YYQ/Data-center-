"""First-release query execution: scoped inputs, controlled tools and final checks.

The SDK orchestrates these tools. Only this business layer can publish a query
answer. Model prose and SDK completion events cannot set a task to succeeded.
"""
from __future__ import annotations

import hashlib
import json
from datetime import timezone
from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from .models import AuthSession, AuthorizationGrant, Document, DocumentVersion, KnowledgeBase, QueryTask, TaskEvidence, TaskOutbox, TaskStep, User, utc_now
from .execution_models import Conversation, ConversationMessage, ContextFact, QueryAnswerSnapshot, TaskInputRequest, TaskInputResolution, ToolInvocation, ToolCallBinding
from .services import SafeError, audit, build_query_result, can_query_kb, evidence_is_readable, has_permission, task_write_guard
from .answer_delivery import delivery_schema
from .answer_context import freeze_request_items


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


EMPTY = {"type": "object", "properties": {}, "additionalProperties": False}
ENABLED_TOOLS = [
    {"name": "dc_context_read", "description": "Read this query's current confirmed device conditions.", "parameters": EMPTY},
    {"name": "dc_sources_search", "description": "Find source identifiers in this query's accepted and current scope.", "parameters": EMPTY},
    {"name": "dc_evidence_search", "description": "Retrieve published, reviewed evidence and required warnings for this query.", "parameters": EMPTY},
    {"name": "dc_sources_read", "description": "Read one evidence item already belonging to this query.", "parameters": {"type": "object", "properties": {"evidence_id": {"type": "string"}}, "required": ["evidence_id"], "additionalProperties": False}},
    {"name": "dc_artifacts_validate", "description": "Validate query evidence and current permissions; does not publish a result.", "parameters": EMPTY},
    {"name": "dc_artifacts_deliver", "description": "Submit answer-plan/1 using complete source groups and all their evidence refs. The server renders and validates the answer; no free prose is allowed.", "parameters": delivery_schema()},
    {"name": "dc_tasks_request_input", "description": "Request the missing device model from the actual task owner.", "parameters": EMPTY},
    {"name": "dc_tasks_control", "description": "Read the current query state. Changes require the authenticated owner workflow.", "parameters": EMPTY},
]
TOOL_NAMES = {tool["name"] for tool in ENABLED_TOOLS}


def _message(db, conversation, actor, content, purpose):
    item = ConversationMessage(conversation_id=conversation.id, author_user_id=actor.id, content=content, purpose=purpose)
    db.add(item)
    db.flush()
    return item


def _fact(db, conversation, message, value, kb_ids, *, tombstone=False):
    changed = db.execute(update(Conversation).where(Conversation.id == conversation.id, Conversation.revision == conversation.revision)
        .values(revision=Conversation.revision + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise SafeError("PRECONDITION_FAILED", "对话条件已改变，请刷新", 412)
    db.refresh(conversation)
    item = ContextFact(conversation_id=conversation.id, fact_key="device.model", revision=conversation.revision,
        value={"model": value}, scope={"knowledge_base_ids": sorted(kb_ids), "subject": "query-device"},
        source_message_id=message.id, state="tombstone" if tombstone else "confirmed")
    db.add(item)
    return item


def accept_query(db: Session, actor: User, session: AuthSession, payload: dict, key: str) -> QueryTask:
    """Called only after real HTTP authentication/CSRF/idempotency checks."""
    if actor.status != "active" or not has_permission(actor, "knowledge.query"):
        raise SafeError("FORBIDDEN", "当前账号不能受理查询", 403)
    conversation = db.get(Conversation, payload.get("conversation_id")) if payload.get("conversation_id") else None
    if payload.get("conversation_id") and (not conversation or conversation.owner_user_id != actor.id or conversation.status != "active"):
        raise SafeError("NOT_FOUND", "对话不存在", 404)
    if not conversation:
        conversation = Conversation(owner_user_id=actor.id)
        db.add(conversation)
        db.flush()
    if payload.get("context_revision") is not None and payload["context_revision"] != conversation.revision:
        raise SafeError("PRECONDITION_FAILED", "对话条件已改变，请刷新", 412)
    requested = payload.get("knowledge_base_ids") or [kb.id for kb in db.scalars(select(KnowledgeBase).where(KnowledgeBase.status == "active")).all()]
    kb_ids = sorted({kb for kb in requested if can_query_kb(db, actor, kb)})
    effective = {**payload, "knowledge_base_ids": kb_ids}
    # Reuse only a fact the caller explicitly selected by its current revision.
    if not effective.get("model") and payload.get("context_revision") is not None:
        fact = db.scalar(select(ContextFact).where(ContextFact.conversation_id == conversation.id, ContextFact.fact_key == "device.model").order_by(ContextFact.revision.desc()))
        if fact and fact.state == "confirmed" and fact.scope.get("knowledge_base_ids") == kb_ids:
            effective["model"] = fact.value["model"]
    message = _message(db, conversation, actor, payload["query"], "query")
    if payload.get("model"):
        _fact(db, conversation, message, payload["model"], kb_ids)
    task = QueryTask(owner_user_id=actor.id, kind="query", input_snapshot=payload, effective_input=effective,
        idempotency_key=key, status="pending", next_action="等待本地执行器", auth_session_id=session.id,
        execution_revision=1, authorization_basis="accepted_task", conversation_id=conversation.id, source_message_id=message.id,
        accepted_scope={"actor_user_id": actor.id, "operation": "knowledge.query", "knowledge_base_ids": kb_ids,
            **{k: effective.get(k) for k in ("model", "level", "category", "need_class")}, "accepted_at": utc_now().isoformat(),
            "origin_session_id": session.id, "input_hash": fingerprint(payload),
            "request_items": freeze_request_items(payload["query"], effective.get("model"))})
    db.add(task)
    db.flush()
    step = TaskStep(task_id=task.id, step_key="query", position=1, kind="knowledge_query", status="pending", attempt_count=0)
    db.add(step)
    db.flush()
    db.add(TaskOutbox(step_id=step.id, dispatch_no=1, status="pending", available_at=utc_now()))
    return task


def request_model(db: Session, task: QueryTask) -> TaskInputRequest:
    task_write_guard(db, task)
    item = db.scalar(select(TaskInputRequest).where(TaskInputRequest.task_id == task.id, TaskInputRequest.status == "open"))
    if not item:
        conversation = db.get(Conversation, task.conversation_id, populate_existing=True)
        item = TaskInputRequest(task_id=task.id, execution_revision=task.execution_revision, context_revision=conversation.revision if conversation else 0,
            requested_field="model", reason="请补充设备型号，以确认手册适用范围")
        db.add(item)
    task.status, task.next_action = "waiting_input", "请补充设备型号；提交后继续原任务"
    task.revision += 1
    db.flush()
    return item


def scope_resolution_is_valid(db, task, key, value):
    task = db.get(QueryTask, task.id, populate_existing=True)
    if key != "model" or not value or not task.conversation_id:
        return False
    rows = db.execute(select(TaskInputResolution, TaskInputRequest, ConversationMessage)
        .join(TaskInputRequest, TaskInputResolution.request_id == TaskInputRequest.id)
        .join(ConversationMessage, TaskInputResolution.response_message_id == ConversationMessage.id)
        .where(TaskInputResolution.task_id == task.id, TaskInputRequest.task_id == task.id).execution_options(populate_existing=True)).all()
    return any(responded.value == value and responded.responded_by == task.owner_user_id and request.requested_field == key
        and request.status == "resolved" and request.execution_revision < task.execution_revision
        and message.author_user_id == task.owner_user_id and message.conversation_id == task.conversation_id
        and message.purpose == "input_response" and message.content == value for responded, request, message in rows)


def respond_model(db, actor, task, request, value):
    if task.owner_user_id != actor.id or actor.status != "active" or not has_permission(actor, "knowledge.query"):
        raise SafeError("NOT_FOUND", "任务不存在", 404)
    if task.status != "waiting_input" or request.task_id != task.id or request.status != "open" or request.execution_revision != task.execution_revision:
        raise SafeError("INPUT_RESPONSE_STALE", "补充请求已失效", 409)
    if not value.strip() or len(value) > 200:
        raise SafeError("INVALID_MODEL", "请输入明确设备型号", 422)
    old_execution = task.execution_revision
    claimed = db.execute(update(TaskInputRequest).where(TaskInputRequest.id == request.id, TaskInputRequest.task_id == task.id,
        TaskInputRequest.status == "open", TaskInputRequest.revision == request.revision, TaskInputRequest.execution_revision == old_execution)
        .values(status="resolved", revision=TaskInputRequest.revision + 1).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise SafeError("INPUT_RESPONSE_STALE", "补充请求已由另一响应处理", 409)
    changed = db.execute(update(QueryTask).where(QueryTask.id == task.id, QueryTask.owner_user_id == actor.id,
        QueryTask.status == "waiting_input", QueryTask.execution_revision == old_execution)
        .values(effective_input={**task.effective_input, "model": value.strip()}, execution_revision=old_execution + 1,
            status="pending", finished_at=None, error_code=None, next_action="已收到型号，等待继续原任务", revision=QueryTask.revision + 1)
        .execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise SafeError("INPUT_RESPONSE_STALE", "任务状态已改变", 409)
    conversation = db.get(Conversation, task.conversation_id)
    message = _message(db, conversation, actor, value.strip(), "input_response")
    db.add(TaskInputResolution(task_id=task.id, request_id=request.id, response_message_id=message.id, responded_by=actor.id, value=value.strip()))
    # An old task response may fill that task, but cannot overwrite a newer
    # condition the employee has already edited elsewhere in the conversation.
    if conversation.revision == request.context_revision:
        _fact(db, conversation, message, value.strip(), task.accepted_scope["knowledge_base_ids"])
    db.refresh(request)
    db.refresh(task)
    _enqueue(db, task)
    audit(db, actor.id, "task.input_resolved", "task", task.id, request_id=request.id)


def _enqueue(db, task):
    step = db.scalar(select(TaskStep).where(TaskStep.task_id == task.id, TaskStep.step_key == "query"))
    step.status, step.lease_until, step.completed_at = "pending", None, None
    previous = db.scalar(select(TaskOutbox).where(TaskOutbox.step_id == step.id).order_by(TaskOutbox.dispatch_no.desc()))
    for old in db.scalars(select(TaskOutbox).where(TaskOutbox.step_id == step.id, TaskOutbox.status == "pending")).all():
        old.status = "superseded"
    db.add(TaskOutbox(step_id=step.id, dispatch_no=(previous.dispatch_no + 1 if previous else 1), status="pending", available_at=utc_now()))


def resume_query(db, actor, task):
    if task.owner_user_id != actor.id or task.status not in {"failed", "blocked", "interrupted"}:
        raise SafeError("TASK_NOT_RESUMABLE", "当前任务不能以此方式恢复", 409)
    from .runtime_models import ModelCallAdmission
    if db.scalar(select(ModelCallAdmission.id).where(ModelCallAdmission.task_id == task.id, ModelCallAdmission.state.in_(["unknown", "dispatching", "streaming"]))):
        raise SafeError("MODEL_RECONCILIATION_REQUIRED", "原模型请求结果待核对，不能重新派发", 409)
    if any(not can_query_kb(db, actor, kb) for kb in task.accepted_scope.get("knowledge_base_ids", [])):
        raise SafeError("ACCESS_LIMITED", "任务原资料范围已失权", 403)
    changed = db.execute(update(QueryTask).where(QueryTask.id == task.id, QueryTask.revision == task.revision,
        QueryTask.execution_revision == task.execution_revision, QueryTask.status.in_(["failed", "blocked", "interrupted"]))
        .values(status="pending", error_code=None, finished_at=None, next_action="等待在原范围内恢复",
            execution_revision=QueryTask.execution_revision + 1, revision=QueryTask.revision + 1).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        raise SafeError("TASK_NOT_RESUMABLE", "任务已被其他操作改变", 409)
    db.refresh(task)
    _enqueue(db, task)


def _current_evidence(db, task, actor):
    from .services import _query_match_is_current
    from .models import ContentBlock
    evidence = db.scalars(select(TaskEvidence).where(TaskEvidence.task_id == task.id, TaskEvidence.execution_revision == task.execution_revision).order_by(TaskEvidence.id)).all()
    positions = {}
    if any(not item.chunk_id and (item.chunk_set_id is not None or item.source_binding_hash is not None
           or "fragment" in (item.source_context or {})) for item in evidence):
        raise SafeError("EVIDENCE_VALIDATION_FAILED", "精确来源契约不完整", 409)
    child_ids = {bool(item.chunk_id) for item in evidence}
    if len(child_ids) > 1:
        raise SafeError("EVIDENCE_VALIDATION_FAILED", "证据集合混用了不同来源契约", 409)
    if child_ids == {True}:
        from .query_evidence import validate_task_evidence_batch
        if not validate_task_evidence_batch(db, actor, task, evidence):
            raise SafeError("ACCESS_LIMITED", "精确来源、索引或访问条件已经改变", 403)
    for item in evidence:
        if item.chunk_id:
            positions[item.id] = (item.source_context.get("position", 0), item.id)
            continue
        version = db.get(DocumentVersion, item.document_version_id, populate_existing=True)
        block = db.get(ContentBlock, item.block_id, populate_existing=True)
        document = db.get(Document, version.document_id, populate_existing=True) if version else None
        if not block or not version or not document or not evidence_is_readable(db, actor, item) or not _query_match_is_current(db, actor, block, version, document, task.effective_input):
            raise SafeError("ACCESS_LIMITED", "证据或访问条件已经改变", 403)
        if item.quote_snapshot != block.text_content or not item.locator or item.locator.get("needs_review"):
            raise SafeError("EVIDENCE_VALIDATION_FAILED", "原文或定位尚不足以核验", 409)
        positions[item.id] = (version.id, block.ordinal)
    evidence.sort(key=lambda item: positions[item.id])
    return evidence


def model_tool_results(db, task, evidence_ids):
    rows = db.scalars(select(ToolInvocation).where(ToolInvocation.task_id == task.id, ToolInvocation.execution_revision == task.execution_revision,
        ToolInvocation.state == "completed").order_by(ToolInvocation.created_at, ToolInvocation.id)).all()
    allowed = set(evidence_ids)
    return [{"tool_name": row.tool_name, "state": row.state,
        "data": {"call_ref": row.id, "state": row.output.get("state", "completed"), "ready": row.output.get("ready")}, "evidence_ids": row.evidence_ids}
        for row in rows if set(row.evidence_ids) <= allowed and row.tool_name not in {"dc_artifacts_deliver"}]


def _lock_delivery_sources(db: Session, task: QueryTask, *, auth_session_id=None) -> None:
    """Hold the current evidence's authority through the answer transaction."""
    # The task guard is already held. On SQLite its write serializes writers.
    # On PostgreSQL use shared locks so independent deliveries can overlap.
    # Lifecycle mutations take Document before KB. Approval revocation and
    # replacement update the beneficiary User in the same transaction.
    evidence_documents = select(DocumentVersion.document_id).join(
        TaskEvidence, TaskEvidence.document_version_id == DocumentVersion.id
    ).where(TaskEvidence.task_id == task.id, TaskEvidence.execution_revision == task.execution_revision)
    documents = db.execute(select(Document.id, Document.knowledge_base_id).where(
        Document.id.in_(evidence_documents)
    ).order_by(Document.id).with_for_update(read=True)).all()
    if not documents:
        return
    document_ids = [row.id for row in documents]
    kb_ids = {row.knowledge_base_id for row in documents}
    db.scalars(select(KnowledgeBase.id).where(KnowledgeBase.id.in_(kb_ids))
        .order_by(KnowledgeBase.id).with_for_update(read=True)).all()
    if auth_session_id:
        db.scalar(select(AuthSession.id).where(AuthSession.id == auth_session_id).with_for_update(read=True))
    db.scalar(select(User.id).where(User.id == task.owner_user_id).with_for_update(read=True))
    db.scalars(select(AuthorizationGrant.id).where(
        AuthorizationGrant.subject_user_id == task.owner_user_id,
        AuthorizationGrant.operation.in_(("document.read", "knowledge.query")),
        AuthorizationGrant.revoked_at.is_(None),
        or_(AuthorizationGrant.document_id.in_(document_ids),
            AuthorizationGrant.document_id.is_(None) & AuthorizationGrant.knowledge_base_id.in_(kb_ids)),
    ).order_by(AuthorizationGrant.id).with_for_update(read=True)).all()


def _write_fence(db, run):
    from .runtime_models import AgentSessionBinding
    now = utc_now()
    changed = db.execute(update(AgentSessionBinding).where(AgentSessionBinding.id == run.session_binding_id,
        AgentSessionBinding.state == "in_use", AgentSessionBinding.lease_owner == run.id,
        AgentSessionBinding.lease_epoch == run.lease_epoch, AgentSessionBinding.lease_until > now)
        .values(lease_epoch=run.lease_epoch).execution_options(synchronize_session=False))
    step = db.execute(update(TaskStep).where(TaskStep.id == run.step_id, TaskStep.lease_owner == run.id,
        TaskStep.lease_epoch == run.step_lease_epoch, TaskStep.status == "running", TaskStep.lease_until > now)
        .values(lease_epoch=run.step_lease_epoch).execution_options(synchronize_session=False))
    if changed.rowcount != 1 or step.rowcount != 1:
        raise SafeError("RUNTIME_LEASE_EXPIRED", "运行租约已失效，拒绝旧执行者提交", 409)


def invoke_tool(db, run_id, token, pi_call_id, name, args, *, settings=None):
    from .runtime_gate import validate_run
    run, task, actor, _ = validate_run(db, run_id, token)
    if name not in TOOL_NAMES or not isinstance(args, dict):
        raise SafeError("TOOL_NOT_ENABLED", "此工具尚未启用", 422)
    valid_args = (set(args) == {"evidence_id"} and isinstance(args["evidence_id"], str)) if name == "dc_sources_read" else (
        set(args) in (set(), {"answer_plan"}) and (not args or isinstance(args["answer_plan"], dict)) if name == "dc_artifacts_deliver" else not args)
    if not valid_args or len(canonical(args).encode("utf-8")) > 65536:
        raise SafeError("INVALID_TOOL_ARGUMENTS", "工具参数不符合声明", 422)
    task_write_guard(db, task)
    action = f"{task.id}:{task.execution_revision}:{fingerprint(args)}"
    binding = db.scalar(select(ToolCallBinding).where(ToolCallBinding.run_id == run.id, ToolCallBinding.pi_tool_call_id == pi_call_id))
    if binding:
        bound = db.get(ToolInvocation, binding.invocation_id)
        if bound.tool_name != name or bound.args_hash != fingerprint(args):
            raise SafeError("TOOL_CALL_CONFLICT", "工具调用身份已用于不同请求", 409)
    existing = db.scalar(select(ToolInvocation).where(ToolInvocation.actor_user_id == actor.id, ToolInvocation.tool_name == name, ToolInvocation.logical_action_key == action))
    if existing:
        _current_evidence(db, task, actor)
        if name == "dc_sources_search":
            from .governance import can_read_version
            for ref in existing.output.get("sources", []):
                version = db.get(DocumentVersion, ref["version_id"], populate_existing=True)
                if not version or not can_read_version(db, actor, version) or not can_read_version(db, actor, version, "knowledge.query"):
                    raise SafeError("ACCESS_LIMITED", "来源资格或访问条件已改变", 403)
        if not binding:
            db.add(ToolCallBinding(run_id=run.id, pi_tool_call_id=pi_call_id, invocation_id=existing.id))
        return {"call_id": existing.id, "state": existing.state, "data": existing.output, "evidence_ids": existing.evidence_ids}
    call = ToolInvocation(actor_user_id=actor.id, task_id=task.id, tool_name=name, logical_action_key=action,
        args_hash=fingerprint(args), execution_revision=task.execution_revision)
    db.add(call)
    db.flush()
    db.add(ToolCallBinding(run_id=run.id, pi_tool_call_id=pi_call_id, invocation_id=call.id))
    output, evidence = {}, []
    if name == "dc_context_read":
        output = {"model": task.effective_input.get("model"), "execution_revision": task.execution_revision, "source_message_ref": task.source_message_id}
    elif name == "dc_sources_search":
        from .governance import can_read_version
        sources = []
        for version, document in db.execute(select(DocumentVersion, Document).join(Document, Document.id == DocumentVersion.document_id)
            .where(Document.knowledge_base_id.in_(task.effective_input["knowledge_base_ids"]))).all():
            if task.effective_input.get("model") in version.model_scope and can_read_version(db, actor, version) and can_read_version(db, actor, version, "knowledge.query"):
                sources.append({"document_id": document.id, "version_id": version.id, "model_scope": version.model_scope})
        output = {"sources": sources, "model": task.effective_input.get("model")}
    elif name == "dc_evidence_search":
        if not task.effective_input.get("model"):
            request = request_model(db, task)
            output = {"state": "waiting_input", "request_id": request.id}
        else:
            if not db.scalar(select(TaskEvidence.id).where(TaskEvidence.task_id == task.id, TaskEvidence.execution_revision == task.execution_revision)):
                build_query_result(db, task, actor, stage_only=True)
                db.flush()
            evidence = _current_evidence(db, task, actor)
            output = {"evidence": [{"id": e.id, "quote": e.quote_snapshot, "locator": e.locator, "version_id": e.document_version_id,
                                   "kind": e.evidence_kind, "source_spans": e.source_spans, "asset_refs": e.asset_refs} for e in evidence], "warnings": task.warnings}
            if evidence and evidence[0].chunk_id:
                from .answer_delivery import model_answer_context
                from .evidence_projection import model_fragment
                from .query_evidence import child_fragment
                output["evidence"] = [{"id": view["evidence_id"], "quote": view["text"], "locator": view["locator"],
                                       "version_id": view["document_version_id"], "kind": view["evidence_kind"]}
                                      for e in evidence for view in [model_fragment(child_fragment(e), evidence_id=e.id)]]
                _, output["answer_contract"] = model_answer_context(task, evidence)
    elif name == "dc_sources_read":
        evidence = [e for e in _current_evidence(db, task, actor) if e.id == args["evidence_id"]]
        if not evidence:
            raise SafeError("NOT_FOUND", "证据不属于当前任务", 404)
        output = {"quote": evidence[0].quote_snapshot, "locator": evidence[0].locator,
                  "kind": evidence[0].evidence_kind, "source_spans": evidence[0].source_spans, "asset_refs": evidence[0].asset_refs}
    elif name == "dc_tasks_request_input":
        if task.effective_input.get("model"):
            raise SafeError("INPUT_NOT_REQUIRED", "设备型号已明确", 409)
        request = request_model(db, task)
        output = {"state": "waiting_input", "request_id": request.id}
    elif name == "dc_tasks_control":
        output = {"state": task.status, "next_action": task.next_action}
    elif name == "dc_artifacts_validate":
        evidence = _current_evidence(db, task, actor)
        output = {"ready": bool(evidence) and not task.warnings, "evidence_ids": [e.id for e in evidence], "warnings": task.warnings}
        if evidence and evidence[0].chunk_id:
            from .answer_context import build_answer_context
            coverage = build_answer_context(task, evidence)["coverage"]
            output.update(coverage=coverage, ready=output["ready"] and coverage["missing_count"] == 0 and coverage["contract_state"] == "frozen")
            task.result_summary = {**task.result_summary, "coverage": coverage}
    elif name == "dc_artifacts_deliver":
        _lock_delivery_sources(db, task)
        _write_fence(db, run)
        evidence = _current_evidence(db, task, actor)
        validation = db.scalar(select(ToolInvocation).where(ToolInvocation.task_id == task.id, ToolInvocation.execution_revision == task.execution_revision,
            ToolInvocation.tool_name == "dc_artifacts_validate", ToolInvocation.state == "completed"))
        if not validation or not validation.output.get("ready") or not evidence or task.warnings:
            raise SafeError("RESULT_VALIDATION_REQUIRED", "尚无完整、当前有效的核验结果", 409)
        rendered, coverage, record = None, None, None
        if evidence[0].chunk_id:
            if "answer_plan" not in args:
                raise SafeError("ANSWER_PLAN_REQUIRED", "需要模型提交完整、有依据的组织方案", 409)
            from .answer_delivery import validate_candidate
            rendered, coverage, record = validate_candidate(db, settings, run, task, call, pi_call_id, args["answer_plan"], evidence)
            if rendered["status"] != "formal":
                task.status, task.error_code, task.finished_at = "blocked", "ANSWER_REVIEW_REQUIRED", utc_now()
                task.result_summary = {"answer": None, "result_status": "needs_review", "evidence_count": len(evidence),
                                       "model": task.effective_input.get("model"), "coverage": coverage, "requires_review": True}
                task.next_action, task.revision = "现有依据不足以形成完整回答，请核对原页、缺项或提交资料反馈", task.revision + 1
                output = {"state": "blocked", "reason": "ANSWER_REVIEW_REQUIRED", "candidate_record": record,
                          "coverage": coverage, "validation": rendered["report"]}
                call.state, call.output, call.evidence_ids = "completed", output, [item.id for item in evidence]
                _write_fence(db, run)
                db.flush()
                return {"call_id": call.id, "state": "blocked", "data": output, "evidence_ids": call.evidence_ids}
        elif args:
            raise SafeError("ANSWER_PLAN_SOURCE_REQUIRED", "组织答案需要精确来源契约", 409)
        summary = {"answer": rendered["text"] if rendered else "\n\n".join("• " + e.quote_snapshot.strip() for e in evidence), "result_status": "formal", "evidence_count": len(evidence),
                   "model": task.effective_input.get("model"), "generated_at": utc_now().isoformat(),
                   "requires_review": True, "review_notice": "设备操作须由业务人员复核，不能直接作为设备执行指令"}
        if rendered:
            summary.update(sections=rendered["sections"], claims=rendered["claims"], sources=rendered["sources"], coverage=coverage,
                           answer_contract="answer-plan/1", renderer_version=rendered["report"]["renderer_version"])
        _current_evidence(db, task, actor)  # Recheck exact bindings after rendering/storage, before the final CAS.
        # Every source sent to the model remains in the private candidate and
        # projection. A recipient receives only the finalized claims' sources.
        delivered_ids = list(dict.fromkeys(ref for claim in rendered["claims"] for ref in claim["evidence_refs"])) if rendered else [e.id for e in evidence]
        snapshot = QueryAnswerSnapshot(task_id=task.id, execution_revision=task.execution_revision, result=summary,
            evidence_ids=delivered_ids, validation_ref=validation.id)
        db.add(snapshot)
        db.flush()
        changed = db.execute(update(QueryTask).where(QueryTask.id == task.id, QueryTask.status == "running",
            QueryTask.execution_revision == run.execution_revision, QueryTask.revision == task.revision)
            .values(result_summary=summary, status="succeeded", finished_at=utc_now(),
                next_action="逐条核对原文、版本及警告，并由业务人员复核", revision=QueryTask.revision + 1)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise SafeError("TASK_STATE_CHANGED", "任务已取消或执行状态已改变，拒绝旧结果提交", 409)
        db.refresh(task)
        output = {"state": "succeeded", "answer_snapshot_id": snapshot.id}
        if record:
            output.update(candidate_record=record, validation=rendered["report"])
        audit(db, actor.id, "query.presented", "task", task.id, answer_snapshot_id=snapshot.id)
    call.state, call.output, call.evidence_ids = "completed", output, [e.id for e in evidence]
    _write_fence(db, run)
    db.flush()
    return {"call_id": call.id, "state": output.get("state", "completed"), "data": output, "evidence_ids": call.evidence_ids}
