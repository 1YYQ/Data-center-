"""Accepted embedding jobs with real loopback HTTP and disposable pgvector."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
import subprocess
import sys
from threading import Event
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select

from .app import create_app
from .embedding_jobs import EmbeddingServiceJob, PURPOSE, canonical_embedding_input, run_embedding_job
from .models import AuthorizationGrant, ChunkSet, ContentBlock, Document, DocumentVersion, IndexGeneration, KnowledgeBase, ParseRun, User, utc_now
from .security import hash_password
from .service_admission import LocalEndpoint, digest
from .service_use_models import ServiceUseCall, ServiceUsePin
from .services import seed_reference_data
from .synthetic_provider import EMBEDDING_PATH, _Handler, run_synthetic_provider
from .test_creation_recovery import PASSWORD, recovery_pg
from .test_service_admission import _change, _etag, _expect, _headers, _preview, _test_run
from .test_service_use import activate, select_purpose
from .test_services import _activate_fixture_index
from .testing_governance import seed_verified_version
from .testing_service_use import attest_synthetic_input
from .vector_store import DocumentEmbedding, upsert_embedding


pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"),
    reason="Embedding job integration requires a disposable PostgreSQL with pgvector")
_ENV = "DATACENTER_SYNTHETIC_EMBEDDING_BUSINESS_KEY"
_SECRET = "SYNTHETIC embedding fixture secret"


def _login(client, username):
    login = _expect(client.post("/auth/login", json={"username": username, "password": PASSWORD}), 200)
    client.headers["X-CSRF-Token"] = login["csrf_token"]


@pytest.fixture
def embedding(recovery_pg, monkeypatch):
    _, config, fixture, _ = recovery_pg
    monkeypatch.setenv(_ENV, _SECRET)
    monkeypatch.delenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", raising=False)
    app = create_app(config)
    try:
        with app.state.session_factory() as db:
            seed_reference_data(db)
            actor = db.get(User, fixture["actor_id"])
            document = db.get(Document, fixture["doc_id"])
            version = db.get(DocumentVersion, fixture["version_id"])
            run = db.scalar(select(ParseRun).where(ParseRun.document_version_id == version.id))
            chunk = db.scalar(select(ChunkSet).where(ChunkSet.parse_run_id == run.id))
            seed_verified_version(db, actor, document, version, run, chunk.id)
            index = _activate_fixture_index(db, db.get(KnowledgeBase, document.knowledge_base_id), actor)
            admin = User(username="synthetic-config-" + uuid4().hex, display_name="SYNTHETIC service administrator",
                status="active", credential_state="active", credential_hash=hash_password(PASSWORD), roles=["system_admin"])
            db.add(admin)
            db.commit()
            blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)
                .order_by(ContentBlock.ordinal, ContentBlock.id)).all()
            scope = SimpleNamespace(app=app, config=config, actor_id=actor.id, username=actor.username,
                admin_username=admin.username, version_id=version.id, kb_id=document.knowledge_base_id,
                run_id=run.id, chunk_id=chunk.id, index_id=index.id, blocks=[block.id for block in blocks],
                texts=[block.text_content for block in blocks], path=f"/document-versions/{version.id}/embedding-jobs")
        with run_synthetic_provider(credential=_SECRET) as provider, TestClient(app) as client, TestClient(app) as admin_client:
            app.state.service_endpoints = {"synthetic": LocalEndpoint(provider.port, _ENV)}
            _login(client, scope.username)
            _login(admin_client, scope.admin_username)
            profile, _, revision_id = activate(admin_client, "embedding")
            select_purpose(admin_client, PURPOSE, revision_id)
            provider.requests.clear()
            scope.client, scope.admin_client, scope.provider = client, admin_client, provider
            scope.profile, scope.revision_id = profile, revision_id
            yield scope
    finally:
        app.state.engine.dispose()


def _attest(fixture, texts=None):
    with fixture.app.state.session_factory() as db:
        attest_synthetic_input(db, fixture.actor_id, PURPOSE, "version:" + fixture.version_id,
                               canonical_embedding_input(fixture.texts if texts is None else texts))
        db.commit()


def _accept(fixture, headers=None):
    return _expect(fixture.client.post(fixture.path, headers=headers or _headers()), 202)


def _advance(fixture, job_id):
    with fixture.app.state.session_factory() as db:
        job = run_embedding_job(db, job_id, fixture.config, fixture.app.state.service_endpoints)
        return job.state, job.error_code


def _vector_count(fixture):
    with fixture.app.state.session_factory() as db:
        return db.scalar(select(func.count(DocumentEmbedding.id)))


def test_http_acceptance_pinned_worker_pgvector_and_unchanged_keyword_index(embedding):
    f = embedding
    key = _headers()
    _attest(f, f.texts[1:] + f.texts[:1])
    denied = f.client.post(f.path, headers=key)
    assert denied.status_code == 409 and denied.json()["code"] == "SERVICE_SYNTHETIC_INPUT_REQUIRED"
    assert f.admin_client.post(f.path, headers=_headers()).status_code == 403
    _attest(f)
    receipt = _accept(f, key)
    assert receipt["state"] == "pending"
    assert _accept(f, key) == _accept(f) == receipt
    assert f.provider.requests == []
    job_id = receipt["embedding_job_id"]

    latest = _expect(f.admin_client.post(f.profile + "/revisions", json={"endpoint_ref": "synthetic",
        "adapter": "bge-m3-v1", "model": "Pro/BAAI/bge-m3"}, headers=_headers(_etag(f.admin_client, f.profile))), 201)["id"]
    _test_run(f.admin_client, f.profile + "/revisions/" + latest)
    preview = _expect(_preview(f.admin_client, f.profile, "activate", revision_id=latest), 200)
    _expect(_change(f.admin_client, f.profile, "activate", preview), 200)
    select_purpose(f.admin_client, PURPOSE, latest)
    f.provider.requests.clear()
    worker = subprocess.run([sys.executable, "-m", "src.server.embedding_worker", "--job-id", job_id],
        env={**os.environ, "DATACENTER_DATABASE_URL": f.config.database_url,
            "DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS": json.dumps({"synthetic": {
                "port": f.provider.port, "credential_env": _ENV}})},
        capture_output=True, text=True, encoding="utf-8", timeout=20)
    assert worker.returncode == 0 and worker.stdout.strip() == f"{job_id} succeeded"
    assert len(f.provider.requests) == 1
    sent = f.provider.requests[0]
    assert (sent.method, sent.target, sent.authenticated) == ("POST", EMBEDDING_PATH, True)
    assert json.loads(sent.body) == {"model": "BAAI/bge-m3", "input": f.texts, "encoding_format": "float"}
    status = _expect(f.client.get(f"/embedding-jobs/{job_id}"), 200)
    assert status["state"] == "succeeded"
    assert status["result"] == {"count": len(f.blocks), "dimensions": 1024,
        "model": "BAAI/bge-m3", "service_revision_id": f.revision_id,
        "usage": {"prompt_tokens": 6, "total_tokens": 6}}
    assert all(label not in json.dumps(status) for label in [*f.texts, _SECRET, "vectors"])
    assert _accept(f, key)["embedding_job_id"] == job_id
    with f.app.state.session_factory() as db:
        job = db.get(EmbeddingServiceJob, job_id)
        pin = db.get(ServiceUsePin, job.pin_id)
        assert pin.revision_id == f.revision_id and pin.resource_key == "version:" + f.version_id
        rows = db.scalars(select(DocumentEmbedding)).all()
        assert {row.block_id for row in rows} == set(f.blocks)
        for row in rows:
            assert (row.document_version_id, row.parse_run_id, row.chunk_set_id) == (f.version_id, f.run_id, f.chunk_id)
            assert (row.provider, row.model_revision, row.dimensions) == ("bge-m3-v1", f.revision_id, 1024)
            assert list(row.embedding) == [1.0] + [0.0] * 1023
        call = db.scalar(select(ServiceUseCall).where(ServiceUseCall.pin_id == pin.id))
        assert (call.state, call.input_hash, call.payload_hash, call.usage, call.settled_cost) == (
            "received", digest(canonical_embedding_input(f.texts)), digest(sent.body), status["result"]["usage"], None)
        kb = db.get(KnowledgeBase, f.kb_id)
        assert kb.active_generation_id == f.index_id
        assert db.get(IndexGeneration, f.index_id).embedding_profile_id == "local-keyword"


def test_unknown_post_cannot_be_resubmitted_with_new_http_key_or_worker_attempt(embedding):
    f = embedding
    _attest(f)
    accepted = _accept(f)
    job_id = accepted["embedding_job_id"]
    f.provider.mode = "disconnect"
    assert _advance(f, job_id) == ("reconciliation_required", "SERVICE_OUTCOME_UNKNOWN")
    f.provider.mode = "normal"
    assert _accept(f)["embedding_job_id"] == job_id
    assert _advance(f, job_id) == ("reconciliation_required", "SERVICE_OUTCOME_UNKNOWN")
    assert len(f.provider.requests) == 1 and not _vector_count(f)
    with f.app.state.session_factory() as db:
        job = db.get(EmbeddingServiceJob, job_id)
        assert job.attempt == 1 and job.lease_until is None
        calls = db.scalars(select(ServiceUseCall).where(ServiceUseCall.pin_id == job.pin_id)).all()
        assert len(calls) == 1 and calls[0].state == "unknown" and calls[0].settled_cost is None


def test_changed_review_snapshot_blocks_provider_write(embedding):
    f = embedding
    _attest(f)
    job_id = _accept(f)["embedding_job_id"]
    with f.app.state.session_factory() as db:
        block = db.get(ContentBlock, f.blocks[0])
        block.locator = {"file_page": 2, "section": "SYNTHETIC changed locator"}
        db.commit()
    assert _advance(f, job_id) == ("failed", "EMBEDDING_FORMAL_SOURCE_REQUIRED")
    assert f.provider.requests == [] and not _vector_count(f)


def test_consumed_reply_cannot_write_vectors_after_management_revocation(embedding, monkeypatch):
    f = embedding
    _attest(f)
    job_id = _accept(f)["embedding_job_id"]
    received, respond = Event(), Event()
    infer = _Handler._inference

    def held_response(handler, path, payload):
        received.set()
        if respond.wait(5):
            infer(handler, path, payload)

    monkeypatch.setattr(_Handler, "_inference", held_response)
    with ThreadPoolExecutor(max_workers=1) as worker:
        run = worker.submit(_advance, f, job_id)
        try:
            assert received.wait(20)
            with f.app.state.session_factory() as db:
                grant = db.scalar(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.actor_id,
                    AuthorizationGrant.operation == "knowledge.manage"))
                grant.revoked_at = utc_now()
                db.commit()
        finally:
            respond.set()
        assert run.result(timeout=10) == ("failed", "FORBIDDEN")
    assert len(f.provider.requests) == 1 and not _vector_count(f)
    assert f.client.get(f"/embedding-jobs/{job_id}").status_code == 403
    with f.app.state.session_factory() as db:
        pin_id = db.get(EmbeddingServiceJob, job_id).pin_id
        assert db.scalar(select(ServiceUseCall.state).where(ServiceUseCall.pin_id == pin_id)) == "received"


def test_batch_write_rolls_back_if_last_vector_would_replace_legacy_identity(embedding):
    f = embedding
    _attest(f)
    job_id = _accept(f)["embedding_job_id"]
    old_values = [0.5] + [0.0] * 1023
    with f.app.state.session_factory() as db:
        upsert_embedding(db, block_id=f.blocks[-1], version_id=f.version_id, parse_run_id=f.run_id,
            chunk_set_id=f.chunk_id, provider="synthetic-legacy", model_revision="synthetic-v0", values=old_values)
        db.commit()
    assert _advance(f, job_id) == ("failed", "EMBEDDING_VECTOR_PERSISTENCE_REJECTED")
    assert len(f.provider.requests) == 1
    with f.app.state.session_factory() as db:
        rows = db.scalars(select(DocumentEmbedding)).all()
        assert len(rows) == 1 and rows[0].block_id == f.blocks[-1]
        assert rows[0].provider == "synthetic-legacy" and list(rows[0].embedding) == old_values
