"""Managed index route contracts on isolated SQLite and opt-in PostgreSQL."""
import json
import os
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select

from .app import create_app
from .embedding_jobs import child_batch_payload, run_child_embedding_batch
from .models import AuthorizationGrant, BusinessApproval, IndexGeneration, User
from .security import hash_password
from .test_child_answer_pipeline import child_answer_pipeline
from .test_child_index import semantic
from .test_creation_recovery import recovery_pg
from .test_embedding_service_use import embedding
from .test_services import _ready_fixture, fixture_db
from .testing_governance import seed_verified_approval
from .testing_service_use import attest_synthetic_input


def _login(client, username, password):
    answer = client.post("/auth/login", json={"username": username, "password": password})
    assert answer.status_code == 200, answer.text
    client.headers["X-CSRF-Token"] = answer.json()["csrf_token"]


def _write(etag=None, *, key=None):
    headers = {"Idempotency-Key": key or uuid4().hex}
    if etag is not None:
        headers["If-Match"] = etag
    return headers


def _switch_body(target, status, action="activate"):
    return {"generation_id": target, "expected_active_id": status["active_generation_id"],
            "expected_source_revision": status["content_revision"], "action": action}


@pytest.fixture
def local_indexes(fixture_db):
    db, config = fixture_db
    actor, kb, document, _, _ = _ready_fixture(db, config)
    partial = User(username="synthetic-partial-" + uuid4().hex, display_name="Synthetic document manager",
        roles=["knowledge_manager"], status="active", credential_state="active", credential_hash=hash_password("partial manager password"))
    db.add(partial)
    db.flush()
    dimensions = {"level": document.access_level, "category": document.category, "need_class": document.need_class}
    approval = BusinessApproval(applicant=partial.id, recorded_by=actor.id, operations=["knowledge.manage"],
                                scope={"knowledge_base_id": kb.id, "document_id": document.id, **dimensions})
    db.add(approval)
    db.flush()
    seed_verified_approval(db, approval)
    db.add(AuthorizationGrant(subject_user_id=partial.id, knowledge_base_id=kb.id, document_id=document.id,
        operation="knowledge.manage", approval_id=approval.id, valid_until=approval.valid_until, **dimensions))
    db.commit()
    app = create_app(config)
    try:
        with TestClient(app) as manager_client, TestClient(app) as partial_client:
            _login(manager_client, actor.username, "manager password")
            _login(partial_client, partial.username, "partial manager password")
            yield db, manager_client, partial_client, "/knowledge-bases/" + kb.id
    finally:
        app.state.engine.dispose()


def test_legacy_http_build_receipts_separate_explicit_switch_rollback_and_status_scope(local_indexes):
    db, manager, partial, base = local_indexes
    status_response = manager.get(base + "/index-status")
    initial = status_response.json()
    active = initial["active_generation_id"]
    assert initial["status"] == "ready" and initial["can_manage_index"] and status_response.headers["ETag"] == initial["etag"]
    assert partial.get(base + "/index-status").json()["can_manage_index"] is False
    assert partial.get(base + "/embedding-profiles").status_code == 403
    assert partial.get(base + "/index-generations").status_code == 403
    assert partial.post(base + "/index-generations", json={}, headers=_write()).status_code == 403
    profiles = manager.get(base + "/embedding-profiles").json()
    assert profiles == {"items": [], "next_cursor": None, "legacy_mode_available": True}
    create_key = _write()
    reply = manager.post(base + "/index-generations", json={}, headers=create_key)
    assert reply.status_code == 202, reply.text
    candidate = reply.json()["index_generation_id"]
    assert candidate != active and reply.json()["status"] == "ready"
    assert manager.post(base + "/index-generations", json={}, headers=create_key).json() == reply.json()
    assert manager.get(base + "/index-status").json() == initial
    page = manager.get(base + "/index-generations", params={"limit": 1}).json()
    assert page["items"][0]["id"] == candidate and page["next_cursor"] == candidate
    tail = manager.get(base + "/index-generations", params={"cursor": page["next_cursor"]}).json()
    assert tail["items"][0]["id"] == active
    row = manager.get(base + f"/index-generations/{candidate}").json()
    assert not row["active"] and row["kind"] == "legacy" and row["actual_chunk_count"] == row["expected_chunk_count"] == 1
    desired = _switch_body(candidate, initial)
    assert manager.put(base + "/active-index", json=desired, headers=_write()).status_code == 428
    wrong = {**desired, "expected_active_id": None}
    assert manager.put(base + "/active-index", json=wrong, headers=_write(initial["etag"])).status_code == 412
    first_key = _write(initial["etag"])
    switched = manager.put(base + "/active-index", json=desired, headers=first_key)
    assert switched.status_code == 200, switched.text
    selected = switched.json()
    assert selected["active_generation_id"] == candidate and selected["etag"] != initial["etag"]
    assert manager.put(base + "/active-index", json=desired, headers=_write(initial["etag"])).status_code == 412
    rolled = manager.put(base + "/active-index", json=_switch_body(active, selected, "rollback"), headers=_write(selected["etag"]))
    assert rolled.status_code == 200 and rolled.json()["active_generation_id"] == active
    assert rolled.json()["etag"] != initial["etag"]
    assert manager.put(base + "/active-index", json=desired, headers=_write(initial["etag"])).status_code == 412
    original_receipt = manager.put(base + "/active-index", json=desired, headers=first_key)
    assert original_receipt.json() == selected and manager.get(base + "/index-status").json()["active_generation_id"] == active
    db.expire_all()
    assert len(db.scalars(select(IndexGeneration)).all()) == 2


@pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="isolated PostgreSQL required")
def test_child_http_uses_only_current_profile_and_accepts_the_private_attested_batch(semantic):
    f = semantic
    base = "/knowledge-bases/" + f.kb_id
    initial = f.client.get(base + "/index-status").json()
    catalogue = f.client.get(base + "/embedding-profiles")
    assert catalogue.status_code == 200, catalogue.text
    assert catalogue.json()["items"] == [{"id": f.revision_id, "model_name": "BAAI/bge-m3", "model_revision": None,
                                          "dimension": 1024, "distance_metric": "cosine"}]
    assert "credential" not in catalogue.text and "endpoint" not in catalogue.text
    assert f.client.post(base + "/index-generations", json={"embedding_profile_id": f.revision_id, "embedding": [0]},
                         headers=_write()).status_code == 422
    accepted = f.client.post(base + "/index-generations", json={"embedding_profile_id": f.revision_id}, headers=_write())
    assert accepted.status_code == 202, accepted.text
    generation_id = accepted.json()["index_generation_id"]
    assert accepted.json()["status"] == "building" and f.client.get(base + "/index-status").json() == initial
    detail = f.client.get(base + "/index-generations/" + generation_id).json()
    assert detail["batch_count"] == 1 and detail["embedding_batches"] == [
        {"id": None, "batch_no": 1, "state": "not_accepted", "error_code": None, "can_accept": True}]
    before_ready = f.client.put(base + "/active-index", json=_switch_body(generation_id, initial), headers=_write(initial["etag"]))
    assert before_ready.status_code == 409
    path = base + f"/index-generations/{generation_id}/embedding-batches/1"
    headers = _write()
    assert f.client.post(path, json={"vectors": [[1]]}, headers=headers).status_code == 422
    no_proof = f.client.post(path, headers=headers)
    assert no_proof.status_code == 409 and no_proof.json()["code"] == "SERVICE_SYNTHETIC_INPUT_REQUIRED"
    assert not f.provider.requests
    with f.app.state.session_factory() as db:
        gen = db.get(IndexGeneration, generation_id)
        _, _, body = child_batch_payload(db, gen.build_report["manifest"], 1)
        attest_synthetic_input(db, f.actor_id, "document.embed", f"index:{generation_id}:1", body)
        db.commit()
    batch = f.client.post(path, headers=headers)
    assert batch.status_code == 202, batch.text
    job_id = batch.json()["embedding_job_id"]
    assert f.client.post(path, headers=headers).json() == batch.json()
    assert not f.provider.requests and "pin_id" not in batch.json()
    assert f.client.get("/embedding-jobs/" + job_id).status_code == 404
    accepted_row = f.client.get(base + "/index-generations/" + generation_id).json()["embedding_batches"][0]
    assert accepted_row == {"id": job_id, "batch_no": 1, "state": "pending", "error_code": None, "can_accept": False}
    with f.app.state.session_factory() as db:
        assert run_child_embedding_batch(db, job_id, f.app.state.service_endpoints).state == "succeeded"
    assert len(f.provider.requests) == 1
    indexed = f.client.get(base + "/index-generations/" + generation_id).json()
    assert indexed["status"] == "ready" and indexed["integrity"]["status"] == "passed" and not indexed["active"]
    assert indexed["embedding_batches"][0]["state"] == "succeeded"
    changed = f.client.put(base + "/active-index", json=_switch_body(generation_id, initial), headers=_write(initial["etag"]))
    assert changed.status_code == 200 and changed.json()["active_generation_id"] == generation_id, changed.text


@pytest.mark.skipif(not (os.environ.get("DATACENTER_TEST_POSTGRES_URL") and os.environ.get("DATACENTER_LOCAL_BGE_MODEL_DIR")),
                    reason="requires isolated PostgreSQL and the approved offline BGE-M3 snapshot")
def test_offline_bge_batch_ledger_activation_and_actual_task_vector_query(child_answer_pipeline, record_testsuite_property):
    from sqlalchemy import event

    from . import local_bge
    from .embedding_jobs import ChildEmbeddingBatch, canonical_embedding_input
    from .embedding_worker import scan_once
    from .models import AuditEvent, QueryTask
    from .service_admission import digest
    from .service_use import LOCAL_EMBEDDING_REQUEST
    from .service_use_models import ServiceUseCall
    from .services import process_query_task, split_terms
    from .test_child_answer_pipeline import LINES, MODEL, WARNING
    from .test_creation_recovery import PASSWORD
    from .test_service_admission import _change, _expect, _headers, _preview, _profile, _test_run
    from .test_service_use import select_purpose
    from .vector_store import ChunkEmbedding, stored_vector

    fixture = child_answer_pipeline
    endpoints = fixture.app.state.service_endpoints
    endpoints[local_bge.ENDPOINT_REF] = local_bge.read_model_endpoint()
    with TestClient(fixture.app) as admin:
        _login(admin, fixture.admin_name, PASSWORD)
        profile_path, etag = _profile(admin, "embedding")
        revision = _expect(admin.post(profile_path + "/revisions", headers=_headers(etag), json={
            "endpoint_ref": local_bge.ENDPOINT_REF, "adapter": local_bge.ADAPTER, "model": local_bge.MODEL}), 201)["id"]
        tested, _ = _test_run(admin, profile_path + "/revisions/" + revision)
        assert tested["state"] == "succeeded", tested["safe_error"]
        assert tested["result"]["local_model"] == local_bge.profile()
        proof = _expect(_preview(admin, profile_path, "activate", revision_id=revision), 200)
        _expect(_change(admin, profile_path, "activate", proof), 200)
        select_purpose(admin, "document.embed", revision)

    base = "/knowledge-bases/" + fixture.kb_id
    initial = fixture.client.get(base + "/index-status").json()
    generation_id = _expect(fixture.client.post(base + "/index-generations", headers=_write(),
                            json={"embedding_profile_id": revision}), 202)["index_generation_id"]
    batch_path = base + f"/index-generations/{generation_id}/embedding-batches/1"
    denied = _expect(fixture.client.post(batch_path, headers=_write()), 409)
    assert denied["code"] == "SERVICE_SYNTHETIC_INPUT_REQUIRED"
    with fixture.app.state.session_factory() as db:
        manifest = db.get(IndexGeneration, generation_id).build_report["manifest"]
        assert manifest["profile"]["local_model"] == local_bge.profile()
        assert len(manifest["batches"]) == 1
        entries, _, payload = child_batch_payload(db, manifest, 1)
        assert len(entries) >= 2
        attest_synthetic_input(db, fixture.actor_id, "document.embed", f"index:{generation_id}:1", payload)
        db.commit()
    job_id = _expect(fixture.client.post(batch_path, headers=_write()), 202)["embedding_job_id"]
    with fixture.app.state.session_factory() as db:
        observed = scan_once(db, fixture.config, endpoints)
        assert observed["jobs"] == [{"kind": "child_embedding", "id": job_id, "state": "succeeded"}], db.get(ChildEmbeddingBatch, job_id).error_code
        assert scan_once(db, fixture.config, endpoints)["count"] == 0
        job = db.get(ChildEmbeddingBatch, job_id)
        call = db.scalar(select(ServiceUseCall).where(ServiceUseCall.pin_id == job.pin_id))
        vectors = [stored_vector(db.get(ChunkEmbedding, (generation_id, entry["id"])).embedding) for entry in entries]
        assert (call.method, call.request_key, call.state, call.input_hash, call.response_hash) == (
            "LOCAL", LOCAL_EMBEDDING_REQUEST, "received", digest(payload), digest(vectors))
        assert all(len(vector) == 1024 and abs(sum(value ** 2 for value in vector) - 1) < 1e-5 for vector in vectors)
        assert len({digest(vector) for vector in vectors}) == len(entries)
    assert fixture.client.get(base + "/index-status").json() == initial
    ready = fixture.client.get(base + "/index-generations/" + generation_id).json()
    assert ready["status"] == "ready" and ready["integrity"]["status"] == "passed" and not ready["active"]
    _expect(fixture.client.put(base + "/active-index", json=_switch_body(generation_id, initial),
                             headers=_write(initial["etag"])), 200)

    query = "如何开机，压力限值是多少？"
    assert not any(term in "\n".join(text for _, text in LINES).casefold() for term in split_terms(query))
    body = {"query": query, "model": MODEL, "knowledge_base_ids": [fixture.kb_id]}
    task_id = _expect(fixture.client.post("/knowledge/queries", headers=_write(), json=body), 202)["task_id"]
    # Count the PostgreSQL cosine operator without collecting SQL parameters or vectors.
    def count_cosine(_conn, _cursor, statement, *_rest):
        if "<=>" in statement:
            cosine_queries.append(True)

    cosine_queries = []
    engine = fixture.app.state.engine
    event.listen(engine, "before_cursor_execute", count_cosine)
    try:
        with fixture.app.state.session_factory() as db:
            task = db.get(QueryTask, task_id)
            process_query_task(db, task, db.get(User, fixture.actor_id))
            db.commit()
            persisted = db.scalar(select(AuditEvent).where(AuditEvent.action == "query.vector_retrieved", AuditEvent.resource_id == task_id))
            receipt = persisted.metadata_json
            assert receipt["query_sha256"] == digest(canonical_embedding_input([task.effective_input["query"]]))
            assert receipt["ranked_candidate_count"] == len(entries) and receipt["vector_sha256"] not in {digest(vector) for vector in vectors}
            assert receipt["generation_ids"] == [generation_id] and receipt["service_revision_id"] == revision
            assert receipt["method"] == "pgvector_cosine" and receipt["local_model"] == local_bge.profile()
            record_testsuite_property("offline_bge_query_receipt", json.dumps(receipt, ensure_ascii=False))
    finally:
        event.remove(engine, "before_cursor_execute", count_cosine)
    response = fixture.client.get(f"/tasks/{task_id}/result")
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["result_status"] == "extractive" and WARNING in result["answer"]
    original = fixture.client.get(f"/documents/{fixture.document_id}/versions/{fixture.version_id}/file")
    assert original.status_code == 200 and original.content == fixture.original
    assert len(cosine_queries) == 1 and not fixture.model.requests
