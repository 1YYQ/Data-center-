"""Isolated synthetic DB + real loopback HTTP tests; never enterprise services."""
from __future__ import annotations

import json
import http.client
import socket
import subprocess
import sys
import threading
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from . import execution_models, governance_models, runtime_gate as gate
from .governance import publication_is_current
from .models import (AuthSession, AuthorizationGrant, Base, BusinessApproval, ChunkSet, ContentBlock,
                     Document, DocumentVersion, FileObject, IndexGeneration, IndexSource,
                     KnowledgeBase, ParseRun, QueryTask, TaskEvidence, TaskStep, User, utc_now)
from .runtime_api import AuthorizeInput, ProjectInput, _credential, register_runtime_routes
from .runtime_models import AgentSessionBinding, ModelCallAdmission, ModelInputProjection, RuntimeProfile, TaskBudget
from .services import SafeError
from .testing_governance import seed_verified_approval, seed_verified_version


@pytest.fixture
def local_upstream():
    state = SimpleNamespace(mode="ok", requests=[], after_request=None)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            state.requests.append(self.rfile.read(int(self.headers["Content-Length"])))
            if state.after_request:
                state.after_request()
            if state.mode == "drop":
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                return
            code = 503 if state.mode == "503" else (302 if state.mode == "redirect" else 200)
            body = json.dumps({"id": "synthetic", "choices": [], "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4}}).encode()
            self.send_response(code)
            if code == 302:
                self.send_header("Location", "http://127.0.0.1:9/forbidden-redirect")
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state.port = server.server_port
    try:
        yield state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture
def runtime_db(tmp_path, local_upstream):
    # Use exactly the production registry, including in fresh child processes;
    # fixtures change only synthetic records and the fixed loopback profile.
    from .execution import ENABLED_TOOLS, model_tool_results, scope_resolution_is_valid
    gate.register_context_tools(ENABLED_TOOLS, model_tool_results, scope_resolution_validator=scope_resolution_is_valid)
    engine = create_engine("sqlite:///" + str(tmp_path / "runtime.db"), connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as db:
        actor = User(username="synthetic-employee", display_name="SYNTHETIC", status="active", roles=["employee", "knowledge_manager"])
        db.add(actor)
        db.flush()
        kb = KnowledgeBase(name="SYNTHETIC local manuals")
        db.add(kb)
        db.flush()
        approval = BusinessApproval(applicant=actor.id, scope={"knowledge_base_id": kb.id, "document_id": None,
            "level": "synthetic-level", "category": "synthetic-category", "need_class": "synthetic-need"},
            operations=["knowledge.manage", "knowledge.query", "document.read"])
        db.add(approval)
        db.flush()
        seed_verified_approval(db, approval)
        for operation in approval.operations:
            db.add(AuthorizationGrant(subject_user_id=actor.id, knowledge_base_id=kb.id, operation=operation,
                 level="synthetic-level", category="synthetic-category", need_class="synthetic-need",
                 approval_id=approval.id, valid_until=approval.valid_until))
        file = FileObject(object_key="synthetic/manual.pdf", sha256="1" * 64, byte_size=1,
                          mime_type="application/pdf", status="available", created_by=actor.id)
        db.add(file)
        db.flush()
        document = Document(knowledge_base_id=kb.id, title="SYNTHETIC manual", access_level="synthetic-level",
                            category="synthetic-category", need_class="synthetic-need")
        db.add(document)
        db.flush()
        version = DocumentVersion(document_id=document.id, version_no=1, source_file_id=file.id,
                                  model_scope=["SYNTHETIC-1"], created_by=actor.id)
        db.add(version)
        db.flush()
        parse = ParseRun(document_version_id=version.id, status="succeeded", origin="synthetic")
        db.add(parse)
        db.flush()
        block = ContentBlock(parse_run_id=parse.id, ordinal=1, kind="warning",
            text_content="SYNTHETIC-1 开机前检查。警告：不得跳过隔离检查。", locator={"file_page": 1, "section": "合成警告"})
        chunk = ChunkSet(parse_run_id=parse.id, status="ready")
        db.add_all([block, chunk])
        db.flush()
        seed_verified_version(db, actor, document, version, parse, chunk.id)
        db.flush()
        assert publication_is_current(db, version)
        generation = IndexGeneration(knowledge_base_id=kb.id, source_revision=kb.content_revision, status="ready")
        db.add(generation)
        db.flush()
        kb.active_generation_id = generation.id
        db.add(IndexSource(index_generation_id=generation.id, document_version_id=version.id, parse_run_id=parse.id, chunk_set_id=chunk.id))
        auth = AuthSession(user_id=actor.id, token_hash="browser-token-hash", csrf_hash="csrf-hash",
                           authz_revision=actor.authz_revision, expires_at=utc_now() + timedelta(hours=1))
        db.add(auth)
        db.flush()
        payload = {"query": "开机注意事项", "model": "SYNTHETIC-1", "knowledge_base_ids": [kb.id],
                   "level": "synthetic-level", "category": "synthetic-category", "need_class": "synthetic-need"}
        scope = {**{key: payload[key] for key in ("model", "knowledge_base_ids", "level", "category", "need_class")},
                 "actor_user_id": actor.id, "operation": "knowledge.query", "origin_session_id": auth.id,
                 "accepted_at": utc_now().isoformat(), "input_hash": gate.digest(gate.canonical_json(payload))}
        task = QueryTask(owner_user_id=actor.id, input_snapshot=dict(payload), effective_input=dict(payload),
                         accepted_scope=scope, auth_session_id=auth.id, execution_revision=1,
                         status="running", kind="query", idempotency_key="synthetic-task")
        db.add(task)
        db.flush()
        db.add(TaskStep(task_id=task.id, step_key="query", position=1, kind="knowledge_query", status="pending"))
        evidence = TaskEvidence(task_id=task.id, document_version_id=version.id, block_id=block.id,
                                quote_snapshot=block.text_content, locator=block.locator, review_at_run="approved")
        db.add(evidence)
        db.commit()
        profile = gate.create_local_profile(db, upstream_port=local_upstream.port,
                                            artifact_manifest_hash="2" * 64, adapter_fingerprint="3" * 64)
        run, token = gate.open_run(db, task.id)
        yield SimpleNamespace(db=db, factory=factory, engine=engine, actor=actor, kb=kb, task=task,
             auth=auth, version=version, block=block, file=file, chunk=chunk, run=run, token=token,
             profile=profile, upstream=local_upstream)
    engine.dispose()


def _admit(fixture, db=None, *, operation="answer", request_key="physical-1"):
    db = db or fixture.db
    projection = gate.project_context(db, fixture.run.id, fixture.token, operation=operation)
    payload = gate.canonical_json({"model": "pi-local-synthetic", "max_tokens": 128,
        "messages": [{"role": "user", "content": "synthetic serialized adapter payload"}]}).encode()
    admission = gate.authorize_model(db, fixture.run.id, fixture.token, projection_id=projection.id,
        context_digest=projection.context_digest, adapter=gate.ADAPTER, payload_digest=gate.digest(payload), request_key=request_key)
    return projection, admission, payload


@pytest.mark.parametrize("marker", ["source_binding_hash", "fragment"])
def test_incomplete_child_identity_cannot_fall_back_to_legacy_projection_or_delivery(runtime_db, marker):
    from .execution import _current_evidence

    f = runtime_db
    evidence = f.db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == f.task.id))
    if marker == "fragment":
        evidence.source_context = {"fragment": {}}
    else:
        evidence.source_binding_hash = "f" * 64
    f.db.commit()
    with pytest.raises(SafeError) as projected:
        gate.project_context(f.db, f.run.id, f.token)
    assert projected.value.code == "RUNTIME_SOURCE_CHANGED"
    with pytest.raises(SafeError) as delivered:
        _current_evidence(f.db, f.task, f.actor)
    assert delivered.value.code == "EVIDENCE_VALIDATION_FAILED"


def test_full_projection_has_server_tools_sources_and_warning(runtime_db):
    f = runtime_db
    projection = gate.project_context(f.db, f.run.id, f.token)
    context = json.loads(projection.context_json)
    assert "dc_evidence_search" in {tool["name"] for tool in context["tools"]}
    assert "警告：不得跳过隔离检查" in projection.context_json
    assert "file_page" in projection.context_json
    assert "publication" in projection.dependencies["sources"][0]
    assert gate.digest(projection.context_json) == projection.context_digest
    assert f.token not in gate.canonical_json(projection.dependencies)


def test_full_prompt_is_ephemeral_and_durable_dependencies_only_store_identities_and_hashes(runtime_db):
    from dataclasses import FrozenInstanceError
    f = runtime_db
    projection = gate.project_context(f.db, f.run.id, f.token)
    assert isinstance(projection, gate.ProjectionResult)
    assert f.block.text_content in projection.context_json
    assert f.task.effective_input["query"] in projection.context_json
    assert "systemPrompt" in projection.context_json and "dc_evidence_search" in projection.context_json
    record = f.db.get(ModelInputProjection, projection.id, populate_existing=True)
    assert record.context_json == ""
    assert record.context_digest == gate.digest(projection.context_json)
    stored = gate.canonical_json(record.dependencies)
    assert f.block.text_content not in stored
    assert f.task.effective_input["query"] not in stored
    assert "SYNTHETIC EXTERNAL WITNESS" not in stored
    assert "SYNTHETIC local manuals" not in stored
    assert set(record.dependencies["sources"][0]["block"]) == {"id", "row_digest"}
    assert set(record.dependencies["grants"][0]["approval"]) == {"id", "row_digest"}
    assert record.dependencies["task"]["effective_input_hash"] == gate.digest(gate.canonical_json(f.task.effective_input))
    assert record.dependencies["task"]["accepted_scope_hash"] == gate.digest(gate.canonical_json(f.task.accepted_scope))
    assert record.dependencies["sources"][0]["version"]["id"] == f.version.id
    assert f.block.text_content not in repr(projection)
    with pytest.raises(FrozenInstanceError):
        projection.context_json = "changed"
    detached = projection.dependencies
    detached["sources"].clear()
    assert projection.dependencies["sources"] and record.dependencies["sources"]
    # The persisted context column is a compatibility placeholder, not an
    # authorization input: current content must be rebuilt from real records.
    gate._current_projection(f.db, f.run.id, f.token, projection.id)


def test_hash_only_dependencies_still_invalidate_changes_not_visible_in_prompt(runtime_db):
    f = runtime_db
    projection = gate.project_context(f.db, f.run.id, f.token)
    item = f.db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == f.task.id))
    item.quote_snapshot += " SYNTHETIC changed historical excerpt"
    f.db.commit()
    run, task, actor, profile = gate.validate_run(f.db, f.run.id, f.token)
    context, dependencies = gate._assemble(f.db, run, task, actor, profile, "answer", "answer")
    # Current source text is unchanged and still fully authorized. The changed
    # persisted evidence dependency must invalidate the prior permission anyway.
    assert gate.digest(context) == projection.context_digest
    assert gate.digest(gate.canonical_json(dependencies)) != projection.dependency_digest
    with pytest.raises(SafeError) as stale:
        gate._current_projection(f.db, f.run.id, f.token, projection.id)
    assert stale.value.detail["code"] == "RUNTIME_PROJECTION_STALE"


def test_persisted_once_only_success_and_budget_on_reopened_connections(runtime_db):
    f = runtime_db
    projection, call, raw = _admit(f)
    f.db.close()
    with f.factory() as reopened:
        assert reopened.get(ModelCallAdmission, call.id).state == "reserved"
        receipt = gate.dispatch_model(reopened, f.run.id, f.token, call.id, raw)
        assert receipt["state"] == "received" and receipt["usage"]["total_tokens"] == 4
    with f.factory() as reopened:
        assert gate.dispatch_model(reopened, f.run.id, f.token, call.id, raw)["state"] == "received"
        assert reopened.get(TaskBudget, f.task.id).reserved_calls == 1
        assert reopened.get(TaskBudget, f.task.id).reserved_tokens == 8192
        assert reopened.get(type(f.run), f.run.id).token_hash != f.token
        assert gate.check_candidate(reopened, f.run.id, f.token, call.id).id == call.id
    assert len(f.upstream.requests) == 1


def test_authorization_retry_reuses_call_and_does_not_double_reserve(runtime_db):
    f = runtime_db
    projection, call, raw = _admit(f)
    again = gate.authorize_model(f.db, f.run.id, f.token, projection_id=projection.id,
        context_digest=projection.context_digest, adapter=gate.ADAPTER, payload_digest=gate.digest(raw), request_key="physical-1")
    assert again.id == call.id
    assert f.db.get(TaskBudget, f.task.id, populate_existing=True).reserved_calls == 1
    with pytest.raises(SafeError):
        gate.authorize_model(f.db, f.run.id, f.token, projection_id=projection.id,
            context_digest=projection.context_digest, adapter=gate.ADAPTER, payload_digest="9" * 64, request_key="physical-1")


@pytest.mark.parametrize("change", ["grant", "cancel", "source", "batch", "content", "index", "classification", "owner", "execution"])
def test_current_changes_reject_previously_issued_permission_before_http(runtime_db, change):
    f = runtime_db
    _, call, raw = _admit(f)
    with f.factory() as other:
        if change == "grant":
            other.scalar(select(AuthorizationGrant).where(AuthorizationGrant.operation == "document.read")).revoked_at = utc_now()
        elif change == "cancel":
            other.get(QueryTask, f.task.id).status = "cancelled"
        elif change == "source":
            other.get(FileObject, f.file.id).status = "deleted"
        elif change == "batch":
            other.get(DocumentVersion, f.version.id).selected_chunk_set_id = None
        elif change == "content":
            other.get(ContentBlock, f.block.id).text_content += " 被修改"
        elif change == "index":
            other.get(KnowledgeBase, f.kb.id).content_revision += 1
        elif change == "classification":
            other.get(User, f.actor.id).roles = ["system_admin"]
        elif change == "owner":
            other.get(User, f.actor.id).status = "disabled"
        elif change == "execution":
            other.get(QueryTask, f.task.id).execution_revision += 1
        other.commit()
    with pytest.raises(SafeError):
        gate.dispatch_model(f.db, f.run.id, f.token, call.id, raw)
    assert f.upstream.requests == []


@pytest.mark.parametrize("operation", ["summary", "compaction"])
def test_auxiliary_operations_inherit_formal_answer_gate(runtime_db, operation):
    f = runtime_db
    # A legacy evidence fixture cannot become a real auxiliary business input
    # merely by replacing the operation string. The SDK auxiliary path has its
    # own source/group/pending-state integration checks.
    with pytest.raises(SafeError) as unsupported:
        gate.project_context(f.db, f.run.id, f.token, operation=operation)
    assert unsupported.value.code == "AUXILIARY_SUPPORT_UNAVAILABLE"
    f.db.rollback()
    f.version.publish_status = "superseded"
    f.db.commit()
    with pytest.raises(SafeError):
        gate.project_context(f.db, f.run.id, f.token, operation=operation)
    with pytest.raises(SafeError):
        gate.project_context(f.db, f.run.id, f.token, operation=operation, business_purpose="history_display")


def test_payload_tampering_is_denied_without_consumption(runtime_db):
    f = runtime_db
    _, call, raw = _admit(f)
    with pytest.raises(SafeError):
        gate.dispatch_model(f.db, f.run.id, f.token, call.id, raw + b" ")
    assert f.db.get(ModelCallAdmission, call.id, populate_existing=True).state == "reserved"
    assert f.upstream.requests == []


@pytest.mark.parametrize("mode", ["drop", "503", "redirect"])
def test_unknown_or_known_failure_is_never_automatically_retried(runtime_db, mode):
    f = runtime_db
    f.upstream.mode = mode
    _, call, raw = _admit(f)
    first = gate.dispatch_model(f.db, f.run.id, f.token, call.id, raw)
    assert first["state"] == ("unknown" if mode == "drop" else "failed")
    assert first["usage_state"] == "unknown" and first["usage"] is None
    with f.factory() as reopened:
        second = gate.dispatch_model(reopened, f.run.id, f.token, call.id, raw)
        assert second["state"] == first["state"]
        assert reopened.get(TaskBudget, f.task.id).reserved_tokens == 8192
    assert len(f.upstream.requests) == 1
    if mode == "drop":
        gate.close_run(f.db, f.run.id, f.token, state="interrupted")
        new_run, new_token = gate.open_run(f.db, f.task.id)
        projection = gate.project_context(f.db, new_run.id, new_token)
        with pytest.raises(SafeError) as caught:
            gate.authorize_model(f.db, new_run.id, new_token, projection_id=projection.id,
                context_digest=projection.context_digest, adapter=gate.ADAPTER, payload_digest=gate.digest(raw), request_key="random-new-key")
        assert caught.value.detail["code"] == "RUNTIME_CALL_UNSETTLED"
        assert f.db.get(TaskBudget, f.task.id, populate_existing=True).reserved_calls == 1


def test_permission_changed_during_http_withholds_response_but_preserves_receipt(runtime_db):
    f = runtime_db
    _, call, raw = _admit(f)
    def revoke():
        with f.factory() as other:
            other.get(User, f.actor.id).status = "disabled"
            other.commit()
    f.upstream.after_request = revoke
    with pytest.raises(SafeError):
        gate.dispatch_model(f.db, f.run.id, f.token, call.id, raw)
    assert len(f.upstream.requests) == 1  # Sent bytes cannot be retracted.
    with f.factory() as other:
        stored = other.get(ModelCallAdmission, call.id)
        assert stored.state == "received" and stored.usage_json["total_tokens"] == 4


def test_lease_epoch_fences_old_run_and_task_budget_survives_replacement(runtime_db):
    f = runtime_db
    _, call, _ = _admit(f)
    binding = f.db.get(AgentSessionBinding, f.run.session_binding_id)
    binding.lease_until = utc_now() - timedelta(seconds=1)
    f.db.get(TaskStep, f.run.step_id).lease_until = utc_now() - timedelta(seconds=1)
    f.db.commit()
    with pytest.raises(SafeError):
        gate.project_context(f.db, f.run.id, f.token)
    new_run, new_token = gate.open_run(f.db, f.task.id)
    assert new_run.lease_epoch > f.run.lease_epoch
    assert f.db.get(ModelCallAdmission, call.id, populate_existing=True).state == "failed"
    assert f.db.get(TaskBudget, f.task.id, populate_existing=True).reserved_calls == 1
    gate.project_context(f.db, new_run.id, new_token)
    with pytest.raises(SafeError):
        gate.renew_run(f.db, f.run.id, f.token)


def test_accepted_task_survives_browser_logout_and_expiry_but_not_scope_tampering(runtime_db):
    f = runtime_db
    f.auth.expires_at = utc_now() - timedelta(seconds=1)
    f.auth.revoked_at = utc_now()
    f.db.commit()
    gate.project_context(f.db, f.run.id, f.token)
    f.task.effective_input = {**f.task.effective_input, "model": "OTHER"}
    f.db.commit()
    with pytest.raises(SafeError):
        gate.project_context(f.db, f.run.id, f.token)


def test_browser_cookie_unknown_token_and_client_context_cannot_authorize(runtime_db):
    f = runtime_db
    with pytest.raises(SafeError):
        gate.project_context(f.db, f.run.id, "browser-token")
    from starlette.requests import Request
    for client, headers in [("127.0.0.1", [(b"cookie", b"dc_session=browser")]),
                            ("203.0.113.1", [(b"x-runtime-token", f.token.encode())])]:
        request = Request({"type": "http", "headers": headers, "client": (client, 1234)})
        with pytest.raises(SafeError):
            _credential(request)
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        ProjectInput(operation="answer", business_purpose="answer", context={"messages": ["untrusted"]})
    with pytest.raises(ValidationError):
        AuthorizeInput(projection_id="x", context_digest="1" * 64, payload_digest="2" * 64,
                       adapter=gate.ADAPTER, request_key="x", url="http://example.invalid")


def test_profile_is_immutable_and_registry_cannot_be_replaced(runtime_db):
    f = runtime_db
    with pytest.raises(RuntimeError):
        gate.register_context_tools([])
    with pytest.raises(ValueError):
        gate.create_local_profile(f.db, upstream_port=f.upstream.port + 1,
                                  artifact_manifest_hash="2" * 64, adapter_fingerprint="3" * 64)
    f.profile.sdk_version = "floating"
    with pytest.raises(ValueError):
        f.db.commit()
    f.db.rollback()


def test_process_restart_keeps_dispatched_intent_and_never_resends(runtime_db):
    f = runtime_db
    _, call, raw = _admit(f)
    # Crash after durable intent, before a receipt: whether upstream saw it is
    # unknowable. A genuinely fresh Python process must not send the HTTP again.
    call.state, call.dispatched_at = "dispatching", utc_now()
    f.db.commit()
    script = """
import json,sys
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from src.server import execution_models,governance_models,runtime_gate as gate
data=json.load(sys.stdin)
assert gate._registry_locked and gate._tools == data['tools']
with Session(create_engine(data['database_url'])) as db:
    try:
        result=gate.dispatch_model(db,data['run_id'],data['token'],data['call_id'],data['payload'].encode())
    except Exception as error:
        from src.server import governance as g
        from src.server.models import DocumentVersion,ContentBlock,ParseRun,ChunkSet,FileObject,KnowledgeBase,IndexGeneration,User,QueryTask
        task=db.get(QueryTask,data['task_id']); owner=db.get(User,task.owner_user_id)
        version=db.get(DocumentVersion,data['version_id']); block=db.get(ContentBlock,data['block_id'])
        chunk=db.get(ChunkSet,version.selected_chunk_set_id); parse=db.get(ParseRun,chunk.parse_run_id)
        kb=db.get(KnowledgeBase,data['kb_id']); generation=db.get(IndexGeneration,kb.active_generation_id)
        print(json.dumps({'error':str(type(error)), 'publication':g.publication_is_current(db,version),
            'blockers':g.publication_blockers(db,version),'read':g.can_read_version(db,owner,version),
            'query':g.can_read_version(db,owner,version,'knowledge.query'),
            'states':[db.get(FileObject,version.source_file_id).status,block.review_status,parse.status,chunk.status,generation.status],
            'revisions':[kb.content_revision,generation.source_revision]}),file=sys.stderr)
        raise
    print(json.dumps({'state':result['state'],'usage_state':result['usage_state']}))
"""
    request = {"tools": gate._tools, "database_url": str(f.engine.url), "run_id": f.run.id,
               "token": f.token, "call_id": call.id, "payload": raw.decode(), "task_id": f.task.id,
               "version_id": f.version.id, "block_id": f.block.id, "kb_id": f.kb.id}
    result = subprocess.run([sys.executable, "-c", script], input=json.dumps(request),
                            text=True, capture_output=True, timeout=15)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"state": "dispatching", "usage_state": "unknown"}
    assert f.upstream.requests == []
    assert f.db.get(TaskBudget, f.task.id, populate_existing=True).reserved_calls == 1


def test_task_budget_exhaustion_and_missing_accepted_scope_fail_closed(runtime_db):
    f = runtime_db
    budget = f.db.get(TaskBudget, f.task.id)
    budget.max_calls = 1
    f.db.commit()
    _, call, raw = _admit(f)
    gate.dispatch_model(f.db, f.run.id, f.token, call.id, raw)
    projection = gate.project_context(f.db, f.run.id, f.token)
    with pytest.raises(SafeError) as caught:
        gate.authorize_model(f.db, f.run.id, f.token, projection_id=projection.id,
            context_digest=projection.context_digest, adapter=gate.ADAPTER,
            payload_digest=gate.digest(raw), request_key="physical-2")
    assert caught.value.detail["code"] == "RUNTIME_BUDGET_EXHAUSTED"
    assert len(f.upstream.requests) == 1
    f.task.accepted_scope = {}
    f.db.commit()
    with pytest.raises(SafeError):
        gate.project_context(f.db, f.run.id, f.token)


def test_expired_grant_and_empty_scope_do_not_load_any_model_context(runtime_db):
    f = runtime_db
    grant = f.db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.operation == "knowledge.query"))
    grant.valid_until = utc_now() - timedelta(seconds=1)
    f.db.commit()
    with pytest.raises(SafeError):
        gate.project_context(f.db, f.run.id, f.token)
    f.task.effective_input = {**f.task.effective_input, "knowledge_base_ids": []}
    f.db.commit()
    with pytest.raises(SafeError):
        gate.project_context(f.db, f.run.id, f.token)


def test_runtime_migration_preserves_legacy_task_and_does_not_grant_scope(tmp_path):
    import importlib
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    engine = create_engine("sqlite:///" + str(tmp_path / "legacy.db"))
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE users (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE auth_sessions (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("CREATE TABLE query_tasks (id VARCHAR(36) PRIMARY KEY, input_snapshot JSON NOT NULL)"))
        connection.execute(text("CREATE TABLE task_steps (id VARCHAR(36) PRIMARY KEY)"))
        connection.execute(text("INSERT INTO query_tasks(id,input_snapshot) VALUES ('legacy','{}')"))
        module = importlib.import_module("src.server.migrations.versions.0005_runtime")
        with Operations.context(MigrationContext.configure(connection)):
            module.upgrade()
            module.upgrade()  # Dynamic fresh 0001 and an existing schema both safe.
        row = connection.execute(text("SELECT id,accepted_scope,effective_input,auth_session_id,execution_revision FROM query_tasks")).one()
        assert tuple(row) == ("legacy", "{}", "{}", None, 1)
        assert "model_call_admissions" in inspect(connection).get_table_names()
    engine.dispose()


def test_private_http_routes_use_run_token_and_server_projection(runtime_db):
    from contextlib import asynccontextmanager
    from fastapi import FastAPI
    import uvicorn
    f, started = runtime_db, threading.Event()

    @asynccontextmanager
    async def lifespan(app):
        started.set()
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.session_factory = f.factory
    register_runtime_routes(app)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    server = uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False))
    thread = threading.Thread(target=lambda: server.run(sockets=[listener]), daemon=True)
    thread.start()
    assert started.wait(timeout=5)
    prefix = f"/private/runtime/runs/{f.run.id}"

    def request(path, body=None, headers=None, method="POST"):
        connection = http.client.HTTPConnection("127.0.0.1", listener.getsockname()[1], timeout=5)
        try:
            connection.request(method, prefix + path, body=body, headers=headers or {})
            response = connection.getresponse()
            if path == "/context-project" and response.status == 200:
                assert response.getheader("Cache-Control") == "no-store"
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    try:
        data = gate.canonical_json({"operation": "answer", "business_purpose": "answer"})
        assert request("/context-project", data, {"Content-Type": "application/json", "Cookie": "dc_session=browser"})[0] == 401
        headers = {"Content-Type": "application/json", "X-Runtime-Token": f.token}
        code, projection = request("/context-project", data, headers)
        assert code == 200 and "警告" in projection["context_json"]
        raw = gate.canonical_json({"model": "pi-local-synthetic", "max_tokens": 128, "messages": []})
        authorization = {"projection_id": projection["id"], "context_digest": projection["context_digest"],
                         "payload_digest": gate.digest(raw), "adapter": gate.ADAPTER, "request_key": "http-physical-1"}
        code, admission = request("/model-authorizations", gate.canonical_json(authorization), headers)
        assert code == 200
        code, answer = request("/models/chat/completions", raw, {**headers, "X-Model-Call": admission["id"]})
        assert code == 200 and answer["id"] == "synthetic"
        code, receipt = request("/model-calls/" + admission["id"], headers=headers, method="GET")
        assert code == 200 and receipt["state"] == "received" and "body" not in receipt
        assert len(f.upstream.requests) == 1
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        listener.close()


@pytest.mark.parametrize("bad", ["missing_output_cap", "oversized_output", "oversized_body", "multiple_choices"])
def test_conservative_request_budget_rejects_unbounded_payload_before_http(runtime_db, bad):
    f = runtime_db
    projection = gate.project_context(f.db, f.run.id, f.token)
    data = {"model": "pi-local-synthetic", "messages": [], "max_tokens": 128}
    if bad == "missing_output_cap":
        del data["max_tokens"]
    elif bad == "oversized_output":
        data["max_tokens"] = 513
    elif bad == "oversized_body":
        data["messages"] = [{"role": "user", "content": "x" * 8000}]
    else:
        data["n"] = 2
    raw = gate.canonical_json(data).encode()
    call = gate.authorize_model(f.db, f.run.id, f.token, projection_id=projection.id,
        context_digest=projection.context_digest, adapter=gate.ADAPTER, payload_digest=gate.digest(raw), request_key="bounded")
    with pytest.raises(SafeError):
        gate.dispatch_model(f.db, f.run.id, f.token, call.id, raw)
    assert f.upstream.requests == []
    assert f.db.get(ModelCallAdmission, call.id, populate_existing=True).state == "reserved"


def test_concurrent_consumers_send_only_one_http_request(runtime_db):
    from concurrent.futures import ThreadPoolExecutor
    f = runtime_db
    _, call, raw = _admit(f)
    barrier = threading.Barrier(2)

    def consume():
        with f.factory() as db:
            barrier.wait(timeout=5)
            try:
                return gate.dispatch_model(db, f.run.id, f.token, call.id, raw)["state"]
            except SafeError as error:
                return error.detail["code"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: consume(), range(2)))
    assert "received" in outcomes
    assert set(outcomes) <= {"received", "dispatching", "RUNTIME_ADMISSION_CONSUMED"}
    assert len(f.upstream.requests) == 1


def test_both_leases_are_required_and_renewed_together(runtime_db):
    f = runtime_db
    gate.renew_run(f.db, f.run.id, f.token, lease_seconds=120)
    binding = f.db.get(AgentSessionBinding, f.run.session_binding_id, populate_existing=True)
    step = f.db.get(TaskStep, f.run.step_id, populate_existing=True)
    assert binding.lease_owner == step.lease_owner == f.run.id
    assert abs((binding.lease_until - step.lease_until).total_seconds()) < 1
    _, call, raw = _admit(f)
    step.lease_epoch += 1  # Binding remains valid, business step writer changes.
    f.db.commit()
    with pytest.raises(SafeError):
        gate.dispatch_model(f.db, f.run.id, f.token, call.id, raw)
    with pytest.raises(SafeError):
        gate.renew_run(f.db, f.run.id, f.token)
    assert f.upstream.requests == []


def test_sdk_settled_does_not_mark_business_step_success(runtime_db):
    f = runtime_db
    gate.close_run(f.db, f.run.id, f.token, state="settled")
    assert f.db.get(TaskStep, f.run.step_id, populate_existing=True).status == "running"
    from .models import TaskAttempt
    assert f.db.get(TaskAttempt, f.run.attempt_id, populate_existing=True).status == "interrupted"
    assert f.db.get(QueryTask, f.task.id, populate_existing=True).status == "running"


def test_missing_real_step_is_not_synthesized_by_runtime(runtime_db):
    f = runtime_db
    gate.close_run(f.db, f.run.id, f.token, state="interrupted")
    f.db.get(TaskStep, f.run.step_id).step_key = "unrelated"
    f.db.commit()
    with pytest.raises(SafeError) as caught:
        gate.open_run(f.db, f.task.id)
    assert caught.value.detail["code"] == "RUNTIME_STEP_REQUIRED"


def test_projection_only_uses_current_execution_evidence(runtime_db):
    f = runtime_db
    previous = f.db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == f.task.id))
    gate.close_run(f.db, f.run.id, f.token, state="interrupted")
    f.task.execution_revision += 1
    current = TaskEvidence(task_id=f.task.id, execution_revision=f.task.execution_revision,
        document_version_id=f.version.id, block_id=f.block.id, quote_snapshot=f.block.text_content,
        locator=f.block.locator, review_at_run="approved")
    f.db.add(current)
    f.db.commit()
    run, token = gate.open_run(f.db, f.task.id)
    projection = gate.project_context(f.db, run.id, token)
    source_ids = {row["evidence"]["id"] for row in projection.dependencies["sources"]}
    assert source_ids == {current.id} and previous.id not in projection.context_json


def test_cancel_between_accepted_task_check_and_run_claim_prevents_all_admission_writes(runtime_db, monkeypatch):
    f = runtime_db
    gate.close_run(f.db, f.run.id, f.token, state="interrupted")
    baseline_runs = len(f.db.scalars(select(type(f.run)).where(type(f.run).task_id == f.task.id)).all())
    step = f.db.get(TaskStep, f.run.step_id, populate_existing=True)
    baseline_attempts = step.attempt_count
    original = gate._accepted_task
    cancelled = []

    def accepted_then_cancel(db, task):
        result = original(db, task)
        if task.id == f.task.id and not cancelled:
            with f.factory() as independent:
                current = independent.get(QueryTask, task.id)
                current.status = "cancelled"
                current.revision += 1
                independent.commit()
            cancelled.append(True)
        return result

    monkeypatch.setattr(gate, "_accepted_task", accepted_then_cancel)
    with pytest.raises(SafeError) as denied:
        gate.open_run(f.db, f.task.id)
    f.db.rollback()
    assert denied.value.detail["code"] == "TASK_STATE_CHANGED"
    assert cancelled and f.db.get(QueryTask, f.task.id, populate_existing=True).status == "cancelled"
    assert len(f.db.scalars(select(type(f.run)).where(type(f.run).task_id == f.task.id)).all()) == baseline_runs
    assert f.db.get(TaskStep, f.run.step_id, populate_existing=True).attempt_count == baseline_attempts
    assert f.db.get(AgentSessionBinding, f.run.session_binding_id, populate_existing=True).lease_owner is None
