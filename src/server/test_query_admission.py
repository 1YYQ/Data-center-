"""Capacity bounds roll back whole commands; pagination never replaces ACL."""
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select

from .execution import request_model
from .execution_models import Conversation, ConversationMessage, TaskInputRequest
from .models import (ApiIdempotencyRecord, AuthorizationGrant, QueryTask, TaskOutbox, TaskShare, TaskStep, utc_now)
from .runtime_models import TaskBudget
from .service_use_models import ServiceUsePin
from .test_answer_share import share_http, _produce_answer, _share
from .test_execution import execution_http, _create, _headers
from .test_runtime_gate import local_upstream, runtime_db


def _limit(f, maximum):
    config = replace(f.app.state.settings, query_max_inflight=maximum)
    f.app.state.settings = config
    f.app.state.session_factory.configure(info={'settings': config})


def _counts(db):
    return [db.scalar(select(func.count()).select_from(model)) for model in
            (QueryTask, TaskStep, TaskOutbox, Conversation, ConversationMessage, ApiIdempotencyRecord, ServiceUsePin, TaskBudget)]


def test_full_capacity_creation_is_atomic_and_original_receipt_still_replays(execution_http):
    f = execution_http
    f.task.status = 'cancelled'
    f.db.commit()
    _limit(f, 1)
    body = {'query': 'SYNTHETIC admission', 'model': 'SYNTHETIC-1', 'knowledge_base_ids': [f.kb.id]}
    key = str(uuid4())
    first = f.client.post('/knowledge/queries', json=body, headers=_headers(key=key))
    assert first.status_code == 202
    before = _counts(f.db)
    rejected = f.client.post('/knowledge/queries', json={**body, 'query': 'SYNTHETIC rejected'}, headers=_headers())
    assert rejected.status_code == 429 and rejected.json()['code'] == 'QUERY_CAPACITY_FULL'
    assert _counts(f.db) == before
    replay = f.client.post('/knowledge/queries', json=body, headers=_headers(key=key))
    assert replay.status_code == 202 and replay.json()['task_id'] == first.json()['task_id']
    assert _counts(f.db) == before


def test_full_capacity_input_response_and_retry_preserve_original_state(execution_http):
    f = execution_http
    f.task.status = 'cancelled'
    f.db.commit()
    task = _create(f, model=None)
    request = request_model(f.db, task)
    f.db.commit()
    _limit(f, 1)
    blocker = _create(f)
    before = _counts(f.db)
    revisions = (task.revision, task.execution_revision, request.revision)
    denied = f.client.post(f'/tasks/{task.id}/input-requests/{request.id}/responses', json={'model': 'SYNTHETIC-1'},
                           headers=_headers(request.revision))
    assert denied.status_code == 429, denied.text
    f.db.refresh(task); f.db.refresh(request)
    assert task.status == 'waiting_input' and request.status == 'open'
    assert (task.revision, task.execution_revision, request.revision) == revisions
    assert _counts(f.db) == before
    task.status = 'blocked'
    f.db.add(TaskBudget(task_id=task.id, max_calls=8, reserved_calls=1, reserved_tokens=8192, max_reserved_tokens=65536))
    f.db.commit()
    step = f.db.scalar(select(TaskStep).where(TaskStep.task_id == task.id))
    prior = (task.revision, task.execution_revision, step.lease_epoch)
    before = _counts(f.db)
    denied = f.client.post(f'/tasks/{task.id}/retry', json={'reason': 'SYNTHETIC capacity'}, headers=_headers(task.revision))
    assert denied.status_code == 429, denied.text
    f.db.refresh(task); f.db.refresh(step)
    assert task.status == 'blocked' and (task.revision, task.execution_revision, step.lease_epoch) == prior
    assert f.db.get(TaskBudget, task.id).reserved_calls == 1
    assert _counts(f.db) == before
    assert blocker.status == 'pending'


def test_task_history_only_checks_requested_page_and_keeps_owner_redacted(share_http, monkeypatch):
    import importlib
    f = share_http
    tasks = [_create(f, query=f'SYNTHETIC page {n}') for n in range(6)]
    module = importlib.import_module(f'{__package__}.app')
    original = module.can_read_task
    calls = []
    def observed(db, actor, task):
        calls.append(task.id)
        return original(db, actor, task)
    monkeypatch.setattr(module, 'can_read_task', observed)
    page = f.client.get('/tasks?limit=2')
    assert page.status_code == 200 and len(page.json()['items']) == len(calls) == 2
    assert page.json()['next_offset'] == 2
    second = f.client.get('/tasks?limit=2&offset=2').json()
    assert not {x['id'] for x in page.json()['items']} & {x['id'] for x in second['items']}
    for grant in f.db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.actor.id,
                                                             AuthorizationGrant.operation.in_(('knowledge.query', 'document.read', 'knowledge.manage')))):
        grant.revoked_at = utc_now()
    f.db.commit()
    denied = f.client.get('/tasks?limit=50').json()
    source_bound = next(row for row in denied['items'] if row['id'] == f.task.id)
    assert set(source_bound) == {'id', 'kind', 'status', 'next_action'} and source_bound['status'] == 'access_limited'
    assert f.client.get('/tasks?limit=51').status_code == 422
    assert f.client.get('/tasks?offset=' + '9' * 100).status_code == 422


def test_unreadable_shared_page_has_no_content_and_can_advance_to_readable(share_http):
    f = share_http
    older, older_snapshot, _ = _produce_answer(f)
    newer, newer_snapshot, secret = _produce_answer(f)
    assert _share(f, older, older_snapshot, [f.reader]).status_code == 200
    assert _share(f, newer, newer_snapshot, [f.reader]).status_code == 200
    newer.execution_revision += 1  # The old share cannot follow a later execution.
    f.db.commit()
    first = f.reader_client.get('/tasks?limit=1').json()
    assert first == {'items': [], 'next_offset': 1} and secret not in str(first)
    second = f.reader_client.get('/tasks?limit=1&offset=1').json()
    assert [row['id'] for row in second['items']] == [older.id] and second['next_offset'] is None
    share = f.db.scalar(select(TaskShare).where(TaskShare.task_id == older.id, TaskShare.recipient_user_id == f.reader.id))
    share.expires_at = utc_now() - timedelta(seconds=1)
    f.db.commit()
    assert f.reader_client.get('/tasks?limit=1&offset=1').json()['items'] == []
