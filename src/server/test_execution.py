"""Synthetic execution-domain checks through real HTTP and real services.

No Node substitute is claimed here: the parent owns the separate pinned SDK /
worker integration check. These tests exercise accepted scope, human responses,
condition revisions and the server's eight tool implementations.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import re
import threading
import uuid
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from .app import create_app
from .config import Settings
from .execution import (ENABLED_TOOLS, fingerprint, invoke_tool, model_tool_results,
                        scope_resolution_is_valid)
from .execution_models import (ContextFact, ConversationMessage, QueryAnswerSnapshot,
                               TaskInputRequest, TaskInputResolution, ToolInvocation)
from .models import (AuthSession, AuthorizationGrant, KnowledgeBase, QueryTask, TaskEvidence,
                     TaskOutbox, TaskStep, User, utc_now)
from .runtime_gate import close_run, open_run, project_context, validate_run
from .security import hash_password
from .services import SafeError
from .test_runtime_gate import local_upstream, runtime_db


def test_query_tool_aliases_match_approved_catalogue_and_node_allowlist():
    root = Path(__file__).resolve().parents[2]
    design = (root / "doc/architecture/Pi-Agent接入与改造方案.md").read_text(encoding="utf-8")
    catalogue = dict(re.findall(r"^\| (T\d{2}) [^|]+ \| (dc_[a-z_]+) \|", design, re.M))
    assert len(catalogue) == len(set(catalogue.values())) == 20
    expected = {catalogue[key] for key in ("T01", "T03", "T04", "T05", "T14", "T16", "T17", "T18")}
    enabled = {tool["name"] for tool in ENABLED_TOOLS}
    assert enabled == expected
    host = (root / "src/agent_runtime/src/runtime-host.ts").read_text(encoding="utf-8")
    declared = re.search(r"const allowed = new Set\((\[[^\n]+\])\)", host)
    assert declared and set(json.loads(declared.group(1))) == enabled


@pytest.fixture
def execution_http(runtime_db, tmp_path):
    f = runtime_db
    f.actor.credential_hash = hash_password("synthetic execution password")
    f.actor.credential_state = "active"
    f.db.commit()
    app = create_app(Settings(database_url=str(f.engine.url), storage_dir=tmp_path / "storage"))
    client = TestClient(app)
    login = client.post("/auth/login", json={"username": f.actor.username, "password": "synthetic execution password"})
    assert login.status_code == 200, login.text
    client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    try:
        yield SimpleNamespace(**vars(f), client=client, app=app)
    finally:
        client.close()
        app.state.engine.dispose()


def _headers(revision=None, key=None):
    result = {"Idempotency-Key": key or str(uuid.uuid4())}
    if revision is not None:
        result["If-Match"] = f'"{revision}"'
    return result


def _create(f, *, model="SYNTHETIC-1", **extra):
    payload = {"query": "开机注意事项", "knowledge_base_ids": [f.kb.id], **extra}
    if model is not None:
        payload["model"] = model
    response = f.client.post("/knowledge/queries", json=payload, headers=_headers())
    assert response.status_code == 202, response.text
    task = f.db.get(QueryTask, response.json()["task_id"], populate_existing=True)
    return task


def _tool(f, run, token, name, args=None, pi_id=None):
    try:
        result = invoke_tool(f.db, run.id, token, pi_id or str(uuid.uuid4()), name, args or {})
        f.db.commit()
        return result
    except Exception:
        f.db.rollback()
        raise


def _missing_request(f):
    task = _create(f, model=None)
    original = copy.deepcopy(task.input_snapshot)
    run, token = open_run(f.db, task.id)
    result = _tool(f, run, token, "dc_tasks_request_input")
    close_run(f.db, run.id, token)
    item = f.db.get(TaskInputRequest, result["data"]["request_id"], populate_existing=True)
    return task, item, original


def test_http_acceptance_records_scope_gets_are_read_only_and_logout_keeps_accepted_task(execution_http):
    f = execution_http
    task = _create(f)
    original = copy.deepcopy(task.input_snapshot)
    assert task.authorization_basis == "accepted_task"
    assert task.accepted_scope["input_hash"] == fingerprint(original)
    assert task.accepted_scope["actor_user_id"] == f.actor.id
    assert task.accepted_scope["knowledge_base_ids"] == [f.kb.id]
    assert task.accepted_scope["origin_session_id"] == task.auth_session_id
    assert f.db.get(AuthSession, task.auth_session_id).user_id == task.owner_user_id
    step = f.db.scalar(select(TaskStep).where(TaskStep.task_id == task.id))
    assert step.status == "pending" and step.attempt_count == 0
    for _ in range(2):
        assert f.client.get(f"/tasks/{task.id}").status_code == 200
        assert f.client.get(f"/tasks/{task.id}/status").json()["status"] == "pending"
        assert f.client.get(f"/tasks/{task.id}/result").status_code == 409
    f.db.refresh(task)
    f.db.refresh(step)
    assert task.status == "pending" and step.attempt_count == 0
    assert f.db.scalar(select(func.count()).select_from(TaskEvidence).where(TaskEvidence.task_id == task.id)) == 0
    forged = f.client.post("/knowledge/queries", json={"query": "x", "accepted_scope": {"actor_user_id": "other"}}, headers=_headers())
    assert forged.status_code == 422
    assert f.client.post("/auth/logout").status_code == 204
    assert f.client.get(f"/tasks/{task.id}").status_code == 401
    run, token = open_run(f.db, task.id)
    project_context(f.db, run.id, token)
    assert f.db.get(QueryTask, task.id, populate_existing=True).input_snapshot == original


def test_t17_owner_response_is_authenticated_revisioned_idempotent_and_resumes_original_task(execution_http):
    f = execution_http
    task, item, original = _missing_request(f)
    url = f"/tasks/{task.id}/input-requests/{item.id}/responses"
    headers = _headers(item.revision)
    csrf = f.client.headers.pop("X-CSRF-Token")
    assert f.client.post(url, json={"model": "SYNTHETIC-1"}, headers=headers).status_code == 403
    f.client.headers["X-CSRF-Token"] = csrf
    assert f.client.post(url, json={"model": "SYNTHETIC-1"}, headers={"If-Match": '"0"'}).status_code == 428
    assert f.client.post(url, json={"model": "SYNTHETIC-1"}, headers=_headers()).status_code == 428
    assert f.client.post(url, json={"model": "SYNTHETIC-1"}, headers=_headers(99)).status_code == 412
    before = f.db.scalar(select(func.count()).select_from(QueryTask))
    first = f.client.post(url, json={"model": "SYNTHETIC-1"}, headers=headers)
    replay = f.client.post(url, json={"model": "SYNTHETIC-1"}, headers=headers)
    assert first.status_code == replay.status_code == 200, first.text
    assert first.json()["task_id"] == replay.json()["task_id"] == task.id
    assert first.json()["execution_revision"] == 2
    f.db.refresh(task)
    assert task.input_snapshot == original and task.effective_input["model"] == "SYNTHETIC-1"
    assert task.accepted_scope["model"] is None and task.status == "pending"
    assert f.db.scalar(select(func.count()).select_from(QueryTask)) == before
    assert f.db.scalar(select(func.count()).select_from(TaskInputResolution).where(TaskInputResolution.task_id == task.id)) == 1
    assert f.db.scalar(select(func.count()).select_from(ConversationMessage).where(ConversationMessage.conversation_id == task.conversation_id, ConversationMessage.purpose == "input_response")) == 1
    step = f.db.scalar(select(TaskStep).where(TaskStep.task_id == task.id))
    assert len(f.db.scalars(select(TaskOutbox).where(TaskOutbox.step_id == step.id)).all()) == 2
    with f.factory() as fresh:
        assert scope_resolution_is_valid(fresh, fresh.get(QueryTask, task.id), "model", "SYNTHETIC-1")
    assert scope_resolution_is_valid(f.db, task, "model", "SYNTHETIC-1")
    assert not scope_resolution_is_valid(f.db, task, "model", "OTHER")
    assert f.client.post(url, json={"model": "SYNTHETIC-1"}, headers=_headers(1)).status_code == 409
    assert f.client.post(url, json={"model": "OTHER"}, headers=headers).status_code == 409
    run, token = open_run(f.db, task.id)
    project_context(f.db, run.id, token)
    resolution = f.db.scalar(select(TaskInputResolution).where(TaskInputResolution.task_id == task.id))
    message = f.db.get(ConversationMessage, resolution.response_message_id)
    message.purpose = "not-a-human-response"
    f.db.commit()
    assert not scope_resolution_is_valid(f.db, task, "model", "SYNTHETIC-1")
    with pytest.raises(SafeError):
        project_context(f.db, run.id, token)


def test_t17_rejects_cross_owner_cross_task_and_forged_fields(execution_http):
    f = execution_http
    task, item, _ = _missing_request(f)
    second, second_item, _ = _missing_request(f)
    url = f"/tasks/{task.id}/input-requests/{item.id}/responses"
    assert f.client.post(f"/tasks/{second.id}/input-requests/{item.id}/responses", json={"model": "SYNTHETIC-1"}, headers=_headers(0)).status_code == 404
    assert f.client.post(url, json={"model": "SYNTHETIC-1", "actor_user_id": f.actor.id, "execution_revision": 1}, headers=_headers(0)).status_code == 422
    intruder = User(username="synthetic-intruder", display_name="SYNTHETIC intruder", status="active", roles=["employee"], credential_state="active", credential_hash=hash_password("synthetic intruder password"))
    f.db.add(intruder)
    f.db.commit()
    other = TestClient(f.app)
    try:
        login = other.post("/auth/login", json={"username": intruder.username, "password": "synthetic intruder password"})
        other.headers["X-CSRF-Token"] = login.json()["csrf_token"]
        assert other.get(f"/tasks/{task.id}/input-requests").status_code == 404
        assert other.post(url, json={"model": "SYNTHETIC-1"}, headers=_headers(0)).status_code == 404
    finally:
        other.close()
    assert f.db.scalar(select(func.count()).select_from(TaskInputResolution).where(TaskInputResolution.task_id.in_([task.id, second.id]))) == 0


def test_conditions_require_revision_explicit_reuse_and_tombstone_without_changing_frozen_input(execution_http):
    f = execution_http
    original = _create(f)
    frozen = copy.deepcopy(original.effective_input)
    url = f"/conversations/{original.conversation_id}/conditions"
    current = f.client.get(url)
    assert current.json()["revision"] == 1
    payload = {"model": "SYNTHETIC-2", "knowledge_base_ids": [f.kb.id]}
    assert f.client.post(url, json=payload, headers=_headers()).status_code == 428
    assert f.client.post(url, json=payload, headers=_headers(0)).status_code == 412
    headers = _headers(1)
    changed = f.client.post(url, json=payload, headers=headers)
    replay = f.client.post(url, json=payload, headers=headers)
    assert changed.status_code == replay.status_code == 200
    assert changed.json()["revision"] == 2
    implicit = _create(f, model=None, conversation_id=original.conversation_id)
    explicit = _create(f, model=None, conversation_id=original.conversation_id, context_revision=2)
    assert implicit.effective_input.get("model") is None
    assert explicit.effective_input["model"] == "SYNTHETIC-2" and explicit.input_snapshot["model"] is None
    f.db.refresh(original)
    assert original.effective_input == frozen
    removed = f.client.post(url, json={"remove": True, "knowledge_base_ids": [f.kb.id]}, headers=_headers(2))
    assert removed.status_code == 200
    now = f.client.get(url).json()
    assert now["condition"]["state"] == "tombstone" and now["condition"]["value"]["model"] is None
    after = _create(f, model=None, conversation_id=original.conversation_id, context_revision=3)
    assert after.effective_input.get("model") is None
    assert f.client.post("/knowledge/queries", json={"query": "x", "conversation_id": original.conversation_id, "context_revision": 2}, headers=_headers()).status_code == 412
    f.db.refresh(original)
    assert original.effective_input == frozen


def test_tools_are_scoped_deduplicated_current_and_cannot_present_without_validation(execution_http):
    f = execution_http
    task = _create(f)
    run, token = open_run(f.db, task.id)
    assert len(ENABLED_TOOLS) == 8
    assert _tool(f, run, token, "dc_context_read")["data"]["model"] == "SYNTHETIC-1"
    assert _tool(f, run, token, "dc_sources_search")["data"]["sources"] == [
        {"document_id": f.version.document_id, "version_id": f.version.id, "model_scope": ["SYNTHETIC-1"]}]
    assert _tool(f, run, token, "dc_tasks_control")["data"]["state"] == "pending"
    for name, args in [("dc_files_delete", {}), ("dc_context_read", {"actor_user_id": "other"}), ("dc_sources_read", {"evidence_id": "x", "task_id": f.task.id})]:
        with pytest.raises(SafeError):
            _tool(f, run, token, name, args)
    with pytest.raises(SafeError):
        _tool(f, run, token, "dc_sources_read", {"evidence_id": f.db.scalar(select(TaskEvidence.id).where(TaskEvidence.task_id == f.task.id))})
    with pytest.raises(SafeError) as present:
        _tool(f, run, token, "dc_artifacts_deliver")
    assert present.value.detail["code"] == "RESULT_VALIDATION_REQUIRED"
    first = _tool(f, run, token, "dc_evidence_search", pi_id="pi-evidence-1")
    second = _tool(f, run, token, "dc_evidence_search", pi_id="pi-evidence-2")
    assert first["call_id"] == second["call_id"]
    assert "警告：不得跳过隔离检查" in first["data"]["evidence"][0]["quote"]
    assert f.db.scalar(select(func.count()).select_from(TaskEvidence).where(TaskEvidence.task_id == task.id)) == 1
    evidence_id = first["evidence_ids"][0]
    read = _tool(f, run, token, "dc_sources_read", {"evidence_id": evidence_id}, pi_id="pi-read")
    assert "警告" in read["data"]["quote"]
    with pytest.raises(SafeError) as conflict:
        _tool(f, run, token, "dc_sources_read", {"evidence_id": "different"}, pi_id="pi-read")
    assert conflict.value.detail["code"] == "TOOL_CALL_CONFLICT"
    assert f.db.get(QueryTask, task.id, populate_existing=True).status == "running"
    with pytest.raises(SafeError) as unvalidated:
        _tool(f, run, token, "dc_artifacts_deliver")
    assert unvalidated.value.detail["code"] == "RESULT_VALIDATION_REQUIRED"
    valid = _tool(f, run, token, "dc_artifacts_validate")
    assert valid["data"]["ready"]
    result = _tool(f, run, token, "dc_artifacts_deliver")
    assert result["data"]["state"] == "succeeded"
    close_run(f.db, run.id, token)
    final = f.client.get(f"/tasks/{task.id}/result")
    assert final.status_code == 200 and "警告：不得跳过隔离检查" in final.json()["answer"]
    assert f.db.scalar(select(func.count()).select_from(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task.id)) == 1


def test_existing_tool_receipt_does_not_replay_source_body_after_revocation(execution_http):
    f = execution_http
    task = _create(f)
    run, token = open_run(f.db, task.id)
    first = _tool(f, run, token, "dc_evidence_search", pi_id="read-before-revoke")
    assert first["data"]["evidence"]
    grant = f.db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.actor.id, AuthorizationGrant.operation == "document.read"))
    grant.revoked_at = utc_now()
    f.db.commit()
    with pytest.raises(SafeError):
        _tool(f, run, token, "dc_evidence_search", pi_id="read-after-revoke")
    assert f.db.scalar(select(func.count()).select_from(ToolInvocation).where(ToolInvocation.task_id == task.id, ToolInvocation.tool_name == "dc_evidence_search")) == 1


def test_prior_validation_does_not_authorize_presentation_after_current_source_revocation(execution_http):
    f = execution_http
    task = _create(f)
    run, token = open_run(f.db, task.id)
    _tool(f, run, token, "dc_evidence_search")
    assert _tool(f, run, token, "dc_artifacts_validate")["data"]["ready"]
    grant = f.db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.actor.id, AuthorizationGrant.operation == "document.read"))
    grant.revoked_at = utc_now()
    f.db.commit()
    with pytest.raises(SafeError):
        _tool(f, run, token, "dc_artifacts_deliver")
    assert f.db.get(QueryTask, task.id, populate_existing=True).status == "running"
    assert f.db.scalar(select(func.count()).select_from(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task.id)) == 0


def test_old_execution_evidence_and_results_are_preserved_but_not_projected_or_presented(execution_http):
    f = execution_http
    task = _create(f)
    run, token = open_run(f.db, task.id)
    old = _tool(f, run, token, "dc_evidence_search")
    close_run(f.db, run.id, token, state="interrupted")
    task = f.db.get(QueryTask, task.id, populate_existing=True)
    task.execution_revision += 1
    task.status = "pending"
    f.db.commit()
    new_run, new_token = open_run(f.db, task.id)
    current = _tool(f, new_run, new_token, "dc_evidence_search")
    assert set(old["evidence_ids"]).isdisjoint(current["evidence_ids"])
    assert f.db.scalar(select(func.count()).select_from(TaskEvidence).where(TaskEvidence.task_id == task.id)) == 2
    projections = model_tool_results(f.db, task, current["evidence_ids"])
    assert {row["data"]["call_ref"] for row in projections} == {current["call_id"]}
    _tool(f, new_run, new_token, "dc_artifacts_validate")
    _tool(f, new_run, new_token, "dc_artifacts_deliver")
    snapshot = f.db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task.id))
    assert snapshot.execution_revision == 2 and snapshot.evidence_ids == current["evidence_ids"]


def test_explicit_context_revision_does_not_borrow_device_fact_from_another_authorized_scope(execution_http):
    from .models import BusinessApproval
    from .testing_governance import seed_verified_approval
    f = execution_http
    original = _create(f)
    other_kb = KnowledgeBase(name="SYNTHETIC other scope")
    f.db.add(other_kb)
    f.db.flush()
    approval = BusinessApproval(applicant=f.actor.id, scope={"knowledge_base_id": other_kb.id,
        "document_id": None, "level": "synthetic-level", "category": "synthetic-category", "need_class": "synthetic-need"},
        operations=["knowledge.query"])
    f.db.add(approval)
    f.db.flush()
    seed_verified_approval(f.db, approval)
    f.db.add(AuthorizationGrant(subject_user_id=f.actor.id, knowledge_base_id=other_kb.id,
        operation="knowledge.query", level="synthetic-level", category="synthetic-category", need_class="synthetic-need",
        approval_id=approval.id, valid_until=approval.valid_until))
    f.db.commit()
    # A newly verified grant increments authz_revision, so authenticate again.
    login = f.client.post("/auth/login", json={"username": f.actor.username, "password": "synthetic execution password"})
    assert login.status_code == 200
    f.client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
    other_scope = _create(f, model=None, conversation_id=original.conversation_id,
                          context_revision=1, knowledge_base_ids=[other_kb.id])
    assert other_scope.accepted_scope["knowledge_base_ids"] == [other_kb.id]
    assert other_scope.effective_input.get("model") is None


def _concurrent_posts(f, monkeypatch, path, payloads, revision):
    """Exercise actual domain writes with a deterministic competing-read schedule.

    The barrier controls timing only; real authentication, idempotency records,
    database transactions and response/condition services still execute.
    """
    from concurrent.futures import ThreadPoolExecutor
    from . import execution_api
    original = execution_api.idempotency_record
    barrier = threading.Barrier(2)

    def gate(*args, **kwargs):
        barrier.wait(timeout=10)
        return original(*args, **kwargs)

    monkeypatch.setattr(execution_api, "idempotency_record", gate)
    cookies = dict(f.client.cookies)
    csrf = f.client.headers["X-CSRF-Token"]

    def post(payload):
        client = TestClient(f.app, raise_server_exceptions=False)
        client.cookies.update(cookies)
        client.headers["X-CSRF-Token"] = csrf
        try:
            response = client.post(path, json=payload, headers=_headers(revision))
            return response.status_code
        finally:
            client.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(post, payloads))


def test_condition_revision_cas_returns_conflict_for_concurrent_edits(execution_http, monkeypatch):
    f = execution_http
    task = _create(f)
    path = f"/conversations/{task.conversation_id}/conditions"
    statuses = _concurrent_posts(f, monkeypatch, path,
        [{"model": model, "knowledge_base_ids": [f.kb.id]} for model in ("SYNTHETIC-2", "SYNTHETIC-3")], 1)
    facts = f.db.scalars(select(ContextFact).where(ContextFact.conversation_id == task.conversation_id)).all()
    assert sorted(fact.revision for fact in facts) == [1, 2]
    assert sorted(statuses) == [200, 412], f"Concurrent condition statuses: {statuses}"


def test_t17_concurrent_owner_responses_record_one_resolution_and_conflict(execution_http, monkeypatch):
    f = execution_http
    task, request, _ = _missing_request(f)
    path = f"/tasks/{task.id}/input-requests/{request.id}/responses"
    statuses = _concurrent_posts(f, monkeypatch, path,
        [{"model": "SYNTHETIC-1"}, {"model": "SYNTHETIC-2"}], 0)
    assert f.db.scalar(select(func.count()).select_from(TaskInputResolution).where(TaskInputResolution.task_id == task.id)) == 1
    assert 200 in statuses and len(statuses) == 2
    assert set(statuses) <= {200, 409, 412}, f"Concurrent T17 statuses: {statuses}"


@pytest.mark.parametrize("tool_name,model", [("dc_evidence_search", "SYNTHETIC-1"), ("dc_tasks_request_input", None)])
def test_http_cancel_after_run_validation_cannot_be_overwritten_by_tool_writes(execution_http, monkeypatch, tool_name, model):
    from . import runtime_gate
    f = execution_http
    task = _create(f, model=model)
    run, token = open_run(f.db, task.id)
    original = runtime_gate.validate_run
    cancelled = []

    def validate_then_cancel(db, run_id, credential):
        result = original(db, run_id, credential)
        if run_id == run.id and not cancelled:
            current = f.client.get(f"/tasks/{task.id}")
            response = f.client.post(f"/tasks/{task.id}/cancel", json={"reason": "SYNTHETIC cancellation during tool dispatch"},
                                     headers=_headers(current.json()["revision"]))
            assert response.status_code == 200 and response.json()["status"] == "cancelled", response.text
            cancelled.append(True)
        return result

    monkeypatch.setattr(runtime_gate, "validate_run", validate_then_cancel)
    failure = None
    try:
        invoke_tool(f.db, run.id, token, "cancel-window", tool_name, {})
        f.db.commit()
    except SafeError as error:
        failure = error.detail["code"]
        f.db.rollback()
    current = f.db.get(QueryTask, task.id, populate_existing=True)
    counts = {"tool_receipts": f.db.scalar(select(func.count()).select_from(ToolInvocation).where(ToolInvocation.task_id == task.id)),
              "evidence": f.db.scalar(select(func.count()).select_from(TaskEvidence).where(TaskEvidence.task_id == task.id)),
              "input_requests": f.db.scalar(select(func.count()).select_from(TaskInputRequest).where(TaskInputRequest.task_id == task.id))}
    assert cancelled and failure and current.status == "cancelled" and not any(counts.values()), {
        "tool": tool_name, "denial": failure, "task_status_after_tool": current.status, **counts}
