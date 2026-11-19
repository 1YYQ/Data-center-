"""Opt-in, bounded local capacity diagnostics with synthetic sources only."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import cProfile
import json
import os
from pathlib import Path
import pstats
import re
import time
import threading
from uuid import uuid4

import pytest
from sqlalchemy import event

from .models import QueryTask, User
from .runtime_gate import _assemble, close_run
from .test_child_answer_pipeline import (MODEL, QUERY, child_answer_pipeline,
                                        recovery_pg, _manual_validation)
from .test_execution import _headers
from .worker import run_one


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATACENTER_TEST_POSTGRES_URL") or os.environ.get("DATACENTER_RUN_CAPACITY") != "1",
    reason="Explicit isolated local PostgreSQL and synthetic capacity opt-in required")


def test_query_proof_profile(child_answer_pipeline):
    """Measure the actual six-member proof without vendor calls or SDK timing noise."""
    f = child_answer_pipeline
    out = Path(os.environ["DATACENTER_CAPACITY_OUTPUT"])
    out.mkdir(parents=True, exist_ok=True)
    response = f.client.post("/knowledge/queries", json={"query": QUERY, "model": MODEL,
        "knowledge_base_ids": [f.kb_id]}, headers=_headers())
    assert response.status_code == 202, response.text
    task_id = response.json()["task_id"]
    queries = Counter()
    sql_seconds = []
    def before(conn, cursor, statement, parameters, context, many):
        context._capacity_started = time.perf_counter()
        tables = re.findall(r'(?:FROM|JOIN)\s+([a-z_]+)', statement, re.IGNORECASE)
        queries.update(tables or [statement.split(None, 1)[0]])
    def after(conn, cursor, statement, parameters, context, many):
        sql_seconds.append(time.perf_counter() - context._capacity_started)
    with f.app.state.session_factory() as db:
        run, token, _ = _manual_validation(f, db, task_id)
        task = db.get(QueryTask, task_id)
        profile = __import__(f"{__package__}.runtime_models", fromlist=["RuntimeProfile"]).RuntimeProfile
        profile = db.get(profile, ("local-pi", 1))
        event.listen(f.app.state.engine, "before_cursor_execute", before)
        event.listen(f.app.state.engine, "after_cursor_execute", after)
        profiler = cProfile.Profile()
        started = time.perf_counter()
        try:
            profiler.enable()
            context, dependencies = _assemble(db, run, task, db.get(User, task.owner_user_id), profile, "answer", "answer")
            profiler.disable()
        finally:
            event.remove(f.app.state.engine, "before_cursor_execute", before)
            event.remove(f.app.state.engine, "after_cursor_execute", after)
        elapsed = time.perf_counter() - started
        assert len(dependencies["sources"]) == 6
        profiler.dump_stats(str(out / "proof.prof"))
        with (out / "proof-profile.txt").open("w", encoding="utf-8") as stream:
            stats = pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative")
            stats.print_stats(45)
        record = {"scope": "one production _assemble proof, six exact members, isolated PG, no vendor call",
            "task_id": task_id, "seconds": elapsed, "sql_statements": len(sql_seconds),
            "sql_seconds": sum(sql_seconds), "table_reads": dict(queries.most_common()),
            "context_bytes": len(context.encode()), "source_members": 6}
        (out / "proof.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
        task.status = "cancelled"
        task.revision += 1
        db.commit()
        close_run(db, run.id, token)


def _wave(f, parallelism):
    """Real SDK/HTTP, independent task budgets; browser-like live status polls."""
    import httpx
    import psutil
    from sqlalchemy import select
    from .runtime_models import ModelCallAdmission, TaskBudget
    from .execution_models import QueryAnswerSnapshot

    tasks, errors, samples = [], [], []
    cookie = dict(f.client.cookies)
    headers = {"X-CSRF-Token": f.client.headers["X-CSRF-Token"]}
    with httpx.Client(base_url=f.gateway, cookies=cookie, trust_env=False, timeout=30) as client:
        for _ in range(parallelism):
            accepted = client.post('/knowledge/queries', json={'query': QUERY, 'model': MODEL,
                'knowledge_base_ids': [f.kb_id]}, headers={**headers, 'Idempotency-Key': str(uuid4())})
            assert accepted.status_code == 202, accepted.text
            tasks.append(accepted.json()['task_id'])
    stop = threading.Event()
    observations = {task: {'status_requests': 0, 'status_seconds': [], 'terminal': None} for task in tasks}
    def poll(task_id):
        row = observations[task_id]
        with httpx.Client(base_url=f.gateway, cookies=cookie, trust_env=False, timeout=30) as client:
            while not stop.is_set():
                begun = time.perf_counter()
                try:
                    reply = client.get(f'/tasks/{task_id}/status')
                    row['status_requests'] += 1
                    row['status_seconds'].append(time.perf_counter() - begun)
                    if reply.status_code != 200:
                        errors.append({'operation': 'progress', 'task': task_id, 'http': reply.status_code})
                        return
                    if reply.json()['status'] not in {'pending', 'running'}:
                        row['terminal'] = reply.json()['status']
                        if row['terminal'] == 'succeeded':
                            readable = client.get(f'/tasks/{task_id}/result')
                            data = readable.json() if readable.status_code == 200 else {}
                            row['formal'] = data.get('result_status') == 'formal' and len(data.get('evidence', [])) == 6
                            if not row['formal']:
                                errors.append({'operation': 'read_result', 'task': task_id, 'http': readable.status_code})
                        return
                except httpx.HTTPError as error:
                    errors.append({'operation': 'progress', 'task': task_id, 'error': type(error).__name__})
                    return
                stop.wait(1)
    def monitor():
        process = psutil.Process()
        while not stop.wait(.5):
            children = process.children(recursive=True)
            rss = process.memory_info().rss
            for child in children:
                try: rss += child.memory_info().rss
                except psutil.Error: pass
            samples.append({'at': time.monotonic(), 'available_bytes': psutil.virtual_memory().available,
                            'test_and_children_rss': rss, 'child_processes': len(children)})
    watcher = threading.Thread(target=monitor, daemon=True)
    watcher.start()
    offset = len(f.gateway_trace)
    started = time.perf_counter()
    try:
        with ThreadPoolExecutor(max_workers=parallelism) as readers, ThreadPoolExecutor(max_workers=parallelism) as workers:
            polling = [readers.submit(poll, task_id) for task_id in tasks]
            futures = [workers.submit(run_one, f.config, task_id=task_id, gateway_url=f.gateway) for task_id in tasks]
            outcomes = [future.result() for future in futures]
            for future in polling: future.result(timeout=40)
    finally:
        stop.set(); watcher.join(2)
    elapsed = time.perf_counter() - started
    with f.app.state.session_factory() as db:
        ledger = []
        for task_id in tasks:
            task = db.get(QueryTask, task_id)
            calls = list(db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.task_id == task_id)))
            budget = db.get(TaskBudget, task_id)
            snapshots = list(db.scalars(select(QueryAnswerSnapshot.id).where(QueryAnswerSnapshot.task_id == task_id)))
            ledger.append({'id': task_id, 'status': task.status, 'error': task.error_code,
                'calls': [c.state for c in calls], 'reserved_calls': budget.reserved_calls if budget else None,
                'snapshots': len(snapshots)})
    return {'parallelism': parallelism, 'seconds': elapsed, 'outcomes': outcomes, 'ledger': ledger,
        'observations': observations, 'errors': errors, 'gateway': f.gateway_trace[offset:],
        'peak_test_children_rss': max((s['test_and_children_rss'] for s in samples), default=0),
        'minimum_available_bytes': min((s['available_bytes'] for s in samples), default=0),
        'samples': samples,
        'passed': not errors and all(x['state'] == 'succeeded' for x in outcomes)
                  and all(x.get('formal') for x in observations.values())
                  and all(x['calls'] == ['received'] * 3 and x['reserved_calls'] == 3 and x['snapshots'] == 1 for x in ledger)}


def test_local_query_capacity(child_answer_pipeline):
    import psutil
    f = child_answer_pipeline
    out = Path(os.environ['DATACENTER_CAPACITY_OUTPUT'])
    out.mkdir(parents=True, exist_ok=True)
    levels = [int(value) for value in os.environ.get('DATACENTER_CAPACITY_LEVELS', '1,2,4').split(',')]
    assert levels and all(1 <= value <= 8 for value in levels)
    report = {'scope': 'synthetic single-PDF six-member production child proof, actual SDK/HTTP/PGvector; loopback generation and synthetic embedding profile',
              'sessions': 'same authorized identity, independent task IDs and browser-like status polls',
              'native_timeout_ms': 20000, 'upstream_timeout_seconds': 5, 'host_seconds': 90, 'waves': []}
    if os.environ.get('DATACENTER_CAPACITY_REAL_BGE') == '1':
        report['embedding_setup'] = _enable_real_bge(f)
        report['scope'] = 'synthetic single-PDF, actual SDK/HTTP, approved offline BGE-M3 CPU and PGvector; loopback generation only'
    try:
        for level in levels:
            if psutil.virtual_memory().available < 512 * 1024 * 1024:
                report['stopped_for_available_memory'] = True
                break
            wave = _wave(f, level)
            report['waves'].append(wave)
            (out/'capacity.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            if not wave['passed']:
                break
    finally:
        (out/'capacity.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert report['waves'] and all(wave['passed'] for wave in report['waves']), [(w['parallelism'], w['outcomes'], w['errors']) for w in report['waves']]


def _enable_real_bge(f):
    from fastapi.testclient import TestClient
    from . import local_bge
    from .test_child_answer_pipeline import _build_and_activate, PASSWORD
    from .test_service_admission import _profile, _test_run, _preview, _change, _expect, _headers as service_headers
    from .test_embedding_service_use import _login
    from .test_service_use import select_purpose
    started = time.perf_counter()
    endpoint = local_bge.read_model_endpoint()
    assert endpoint is not None, 'Approved existing offline BGE snapshot required; no download'
    f.app.state.service_endpoints[local_bge.ENDPOINT_REF] = endpoint
    with TestClient(f.app) as admin:
        _login(admin, f.admin_name)
        path, etag = _profile(admin, 'embedding')
        revision = _expect(admin.post(path+'/revisions', headers=service_headers(etag), json={
            'endpoint_ref': local_bge.ENDPOINT_REF, 'adapter': local_bge.ADAPTER, 'model': local_bge.MODEL}), 201)['id']
        tested, _ = _test_run(admin, path+'/revisions/'+revision)
        assert tested['state'] == 'succeeded', tested
        preview = _expect(_preview(admin, path, 'activate', revision_id=revision), 200)
        _expect(_change(admin, path, 'activate', preview), 200)
        select_purpose(admin, 'document.embed', revision)
    old = f.generation_id
    f.revision_id = revision
    f.generation_id = _build_and_activate(f, old)
    return {'seconds': time.perf_counter()-started, 'service_revision_id': revision,
            'generation_id': f.generation_id, 'local_model': local_bge.profile(),
            'warm_before_queries': True, 'index_has_actual_vectors': True}


def test_postgres_capacity_last_slot_is_atomic(recovery_pg):
    """Two distinct HTTP servers race for one remaining database-wide slot."""
    from dataclasses import replace
    from sqlalchemy import func, select
    from .test_creation_recovery import _clients
    from .models import TaskOutbox, TaskStep, ApiIdempotencyRecord
    from .execution_models import Conversation, ConversationMessage
    from .service_use_models import ServiceUsePin
    from .runtime_models import TaskBudget
    engine, config, account, report = recovery_pg
    config = replace(config, query_max_inflight=4)
    body = {'query': 'SYNTHETIC last slot', 'model': MODEL, 'knowledge_base_ids': [account['kb_id']]}
    with _clients(config, account, count=2) as (clients, headers, apps):
        for _ in range(3):
            assert clients[0].post('/knowledge/queries', json=body,
                headers={**headers[0], 'Idempotency-Key': str(uuid4())}).status_code == 202
        barrier = threading.Barrier(2)
        keys = [str(uuid4()), str(uuid4())]
        def submit(i):
            barrier.wait(5)
            return clients[i].post('/knowledge/queries', json=body,
                headers={**headers[i], 'Idempotency-Key': keys[i]})
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(submit, range(2)))
        assert sorted(reply.status_code for reply in replies) == [202, 429]
        winner = next(i for i, reply in enumerate(replies) if reply.status_code == 202)
        replay = clients[winner].post('/knowledge/queries', json=body,
            headers={**headers[winner], 'Idempotency-Key': keys[winner]})
        assert replay.status_code == 202 and replay.json() == replies[winner].json()
        with apps[0].state.session_factory() as db:
            counts = {model.__tablename__: db.scalar(select(func.count()).select_from(model))
                for model in (QueryTask, TaskStep, TaskOutbox, ApiIdempotencyRecord, Conversation,
                              ConversationMessage, ServiceUsePin, TaskBudget)}
        assert all(counts[key] == 4 for key in ('query_tasks', 'task_steps', 'task_outbox',
            'api_idempotency_records', 'conversations', 'conversation_messages'))
        assert counts['service_use_pins'] == counts['task_budgets'] == 0
        (report/'capacity-race.json').write_text(json.dumps({'http': [r.status_code for r in replies],
            'replay': replay.status_code, 'counts': counts}, indent=2), encoding='utf-8')
