"""Bounded CLI dispatch through the production processing workers."""
from dataclasses import replace
from datetime import timedelta
import json
import os
import subprocess
import sys

import pytest
from sqlalchemy import select

from . import embedding_worker, parse_worker
from .chunk_jobs import accept_chunk_set, run_chunk_job
from .embedding_jobs import EmbeddingServiceJob, accept_child_embedding_batch, child_batch_payload
from .models import ChunkSet, IndexGeneration, ParseArtifact, ParseRun, User, utc_now
from .parse_jobs import ParseServiceJob, run_local_parse_job
from .parsing import local_mineru
from .parsing.ir_projection import normalize_parse_run
from .service_use_models import ServiceUseCall
from .test_child_index import _build, semantic
from .test_chunk_processing import TOKENIZER
from .test_creation_recovery import recovery_pg
from .test_embedding_service_use import _accept, _attest, embedding
from .test_local_parse_pipeline import accept, local_parse, write_saved_bundle
from .testing_service_use import attest_synthetic_input


def test_pilot_consumer_automatically_advances_accepted_parse_and_chunk(local_parse, monkeypatch):
    from .pilot_dispatch import advance_once
    if not TOKENIZER.is_file():
        pytest.skip("fixed local tokenizer is not installed")
    f = local_parse
    config = replace(f.config, local_bge_tokenizer=TOKENIZER)
    f.app.state.settings = config
    monkeypatch.setattr(local_mineru, "run_standard_bundle", write_saved_bundle)
    run_id = accept(f)
    result = advance_once(f.app, gateway_url="http://127.0.0.1:8879", profile_revision=1)
    assert result["kind"] == "processing" and result["result"]["jobs"][0]["state"] == "needs_review"
    with f.app.state.session_factory() as db:
        run = db.get(ParseRun, run_id)
        assert run.stage_checkpoint["outputs"]["source_qc"]
        chunk = accept_chunk_set(db, db.get(User, f.actor_id), run, "dc.semantic-bge-m3/1")
        chunk_id = chunk.id
        db.commit()
    result = advance_once(f.app, gateway_url="http://127.0.0.1:8879", profile_revision=1)
    assert result["kind"] == "processing" and result["result"]["jobs"] == [{"kind":"chunk", "id":chunk_id, "state":"ready"}]
    with f.app.state.session_factory() as db:
        assert db.get(ChunkSet, chunk_id).quality_artifact_id


def test_document_preparation_mode_rejects_new_query_before_acceptance(local_parse):
    from sqlalchemy import func
    from .models import QueryTask
    f = local_parse
    f.app.state.query_acceptance_enabled = False
    response = f.client.post("/knowledge/queries", json={"query":"SYNTHETIC preparation window", "model":"SYNTHETIC-1"},
        headers={"Idempotency-Key":"synthetic-preparation-no-admission"})
    assert response.status_code == 409 and response.json()["code"] == "QUERY_PREPARATION_MODE"
    with f.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(QueryTask)) == 0


def test_parse_and_chunk_scans_resume_in_stage_order_and_leave_ready_chunks_unchanged(local_parse, monkeypatch):
    if not TOKENIZER.is_file():
        pytest.skip("fixed local tokenizer is not installed")
    f = local_parse
    config = replace(f.config, local_bge_tokenizer=TOKENIZER)
    launched = []

    def runner(*args):
        launched.append(1)
        return write_saved_bundle(*args)

    monkeypatch.setattr(local_mineru, "run_standard_bundle", runner)
    interrupted_id = accept(f)
    with f.app.state.session_factory() as db:
        archived = run_local_parse_job(db, interrupted_id, config)
        assert archived.stage_checkpoint["outputs"]["raw_manifest"]
        normalized = normalize_parse_run(db, interrupted_id, config)
        assert normalized.stage_checkpoint["outputs"]["normalized_ir"]
    pending_id = accept(f)
    assert pending_id != interrupted_id

    with f.app.state.session_factory() as stale:
        interrupted = stale.get(ParseRun, interrupted_id)
        interrupted.status = "running"
        stale.get(ParseServiceJob, interrupted_id).lease_until = utc_now() - timedelta(seconds=1)
        stale.commit()
        first = parse_worker.scan_once(stale, config, {}, limit=1)
        assert first == {"count": 1, "jobs": [{"kind": "parse", "id": interrupted_id, "state": "needs_review"}]}
        assert interrupted.stage_checkpoint["outputs"]["source_qc"]
        assert stale.get(ParseRun, pending_id).status == "pending"
        assert launched == [1]

        cached = accept_chunk_set(stale, stale.get(User, f.actor_id), interrupted, "dc.semantic-bge-m3/1")
        chunk_id = cached.id
        stale.commit()
        assert cached.status == "building"
        with f.app.state.session_factory() as db:
            second = parse_worker.scan_once(db, config, {}, limit=2)
            assert second == {"count": 2, "jobs": [{"kind": "parse", "id": pending_id, "state": "needs_review"},
                                                   {"kind": "chunk", "id": chunk_id, "state": "ready"}]}
            assert db.get(ParseRun, pending_id).stage_checkpoint["outputs"]["source_qc"]
            ready = db.get(ChunkSet, chunk_id)
            assert ready.stage_checkpoint["attempt"] == 1 and ready.quality_artifact_id
        assert cached.status == "building"  # Simulate stale identity-map data before the worker takes its locks.
        assert run_chunk_job(stale, chunk_id, config).status == "ready"
        assert cached.stage_checkpoint["attempt"] == 1

    waiting_id = accept(f)
    with f.app.state.session_factory() as db:
        waiting = db.get(ParseRun, waiting_id)
        waiting.status, waiting.error_code = "waiting_external", "LOCAL_STANDARD_NOT_READY"
        db.commit()
        artifacts = db.scalars(select(ParseArtifact.id)).all()
        assert parse_worker.scan_once(db, config, {}) == {"count": 0, "jobs": []}
        assert db.get(ParseServiceJob, waiting_id).attempt == 0
        assert db.scalars(select(ParseArtifact.id)).all() == artifacts
    assert launched == [1, 1]
    env = dict(os.environ, DATACENTER_DATABASE_URL=config.database_url, DATACENTER_STORAGE_DIR=str(config.storage_dir),
               DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS="{}", DATACENTER_GENERATION_SERVICE_ENDPOINTS="{}")
    idle = subprocess.run([sys.executable, "-m", "src.server.parse_worker", "--scan-once", "--limit", "2"],
                          env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert idle.returncode == 0, idle.stderr
    assert json.loads(idle.stdout) == {"count": 0, "jobs": []}


_requires_postgres = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="isolated PostgreSQL required")


@_requires_postgres
def test_embedding_scan_runs_accepted_child_batch_once(semantic):
    f = semantic
    endpoints = f.app.state.service_endpoints
    with f.app.state.session_factory() as db:
        generation = _build(db, f)
        _, _, payload = child_batch_payload(db, generation.build_report["manifest"], 1)
        attest_synthetic_input(db, f.actor_id, "document.embed", f"index:{generation.id}:1", payload)
        db.flush()
        batch = accept_child_embedding_batch(db, generation.id, 1, db.get(User, f.actor_id), endpoints)
        generation_id, batch_id = generation.id, batch.id
        db.commit()
        assert f.provider.requests == []
        result = embedding_worker.scan_once(db, f.config, endpoints, limit=1)
        assert result == {"count": 1, "jobs": [{"kind": "child_embedding", "id": batch_id, "state": "succeeded"}]}
        assert db.get(IndexGeneration, generation_id).status == "ready"
        assert embedding_worker.scan_once(db, f.config, endpoints) == {"count": 0, "jobs": []}
    assert len(f.provider.requests) == 1


@_requires_postgres
def test_embedding_scan_reconciles_expired_unknown_without_sending_another_request(embedding):
    f = embedding
    _attest(f)
    job_id = _accept(f)["embedding_job_id"]
    endpoints = f.app.state.service_endpoints
    with f.app.state.session_factory() as db:
        job = db.get(EmbeddingServiceJob, job_id)
        job.state, job.attempt, job.lease_until = "running", 1, utc_now() + timedelta(minutes=1)
        receipt = ServiceUseCall(pin_id=job.pin_id, request_key="POST:api", ordinal=1, method="POST", purpose="document.embed",
                                 target_hash="a" * 64, payload_hash="b" * 64, input_hash=job.input_hash, state="dispatching")
        db.add(receipt)
        db.commit()
        assert embedding_worker.scan_once(db, f.config, endpoints) == {"count": 0, "jobs": []}
        assert job.attempt == 1
        job.lease_until = utc_now() - timedelta(seconds=1)
        db.commit()
        result = embedding_worker.scan_once(db, f.config, endpoints, limit=1)
        assert result == {"count": 1, "jobs": [{"kind": "embedding", "id": job_id, "state": "reconciliation_required"}]}
        assert job.attempt == 2 and job.lease_until is None and receipt.state == "unknown"
        assert embedding_worker.scan_once(db, f.config, endpoints) == {"count": 0, "jobs": []}
    assert f.provider.requests == []


@pytest.mark.parametrize(("main", "id_flag"), [(parse_worker.main, "--run-id"), (embedding_worker.main, "--job-id")])
def test_worker_requires_one_explicit_target_and_a_valid_scan_limit(main, id_flag):
    for args in ([], [id_flag, ""], [id_flag, "example", "--scan-once"], ["--scan-once", "--limit", "33"], [id_flag, "example", "--limit", "1"]):
        with pytest.raises(SystemExit) as error:
            main(args)
        assert error.value.code == 2
