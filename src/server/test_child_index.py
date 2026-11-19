"""Generation fencing and exact child evidence using isolated PostgreSQL and synthetic inputs."""
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
import os
from threading import Barrier
from types import SimpleNamespace
from uuid import uuid4

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import event, select

from . import governance, indexing
from .app import create_app
from .chunk_retrieval import search_child_chunks, validate_child_fragment, validate_child_fragments
from .embedding_jobs import accept_child_embedding_batch, child_batch_payload, run_child_embedding_batch
from .models import (AuthorizationGrant, BlockRelation, Chunk, ChunkBlock, ChunkSet, ContentBlock, DocumentPage,
                     DocumentVersion, FileObject, IndexGeneration, KnowledgeBase, ParseArtifact, ParseRun, QueryTask, User)
from .provider_transport import ProviderError
from .query_evidence import persist_child_evidence, validate_task_evidence_batch
from .service_admission import LocalEndpoint
from .service_use_models import ServiceUseCall
from .services import SafeError, file_path, save_upload, seed_reference_data
from .synthetic_provider import run_synthetic_provider
from .test_chunk_processing import build, publish, review_candidate, verify_source
from .test_creation_recovery import PASSWORD, recovery_pg
from .test_embedding_service_use import _ENV, _SECRET, _login, embedding
from .test_local_parse_pipeline import SYNTHETIC_TEXT
from .test_service_use import activate, select_purpose
from .test_source_processing import source_ready
from .security import hash_password
from .testing_service_use import attest_synthetic_input
from .vector_store import ChunkEmbedding, stored_vector


@pytest.fixture
def semantic(embedding, monkeypatch):
    f = embedding
    # These three qualifications belong to P3/P4's separately verified input
    # contract. This fixture exercises P5 with a synthetic locked projection.
    monkeypatch.setattr(indexing, "chunk_set_qc_is_current", lambda db, row: row.stage_checkpoint["quality"]["status"] == "passed")
    monkeypatch.setattr(indexing, "source_verification", lambda db, run: {"state": "verified"})
    monkeypatch.setattr(governance, "publication_is_current", lambda db, version, historical=False: version.publish_status == "effective")
    with f.app.state.session_factory() as db:
        chunk_set = db.get(ChunkSet, f.chunk_id)
        run = db.get(ParseRun, f.run_id)
        version = db.get(DocumentVersion, f.version_id)
        block = db.get(ContentBlock, f.blocks[0])
        # Ensure the query test uses a real, separately checked purpose grant.
        for grant in db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == f.actor_id)):
            if grant.operation == "knowledge.query":
                break
        else:
            from .models import BusinessApproval
            from .testing_governance import seed_verified_approval
            document = db.get(indexing.Document, version.document_id)
            dimensions = {"level": document.access_level, "category": document.category, "need_class": document.need_class}
            approval = BusinessApproval(applicant=f.actor_id, recorded_by=f.actor_id, operations=["knowledge.query"],
                scope={**dimensions, "knowledge_base_id": f.kb_id, "document_id": None})
            db.add(approval)
            db.flush()
            seed_verified_approval(db, approval)
            db.add(AuthorizationGrant(subject_user_id=f.actor_id, knowledge_base_id=f.kb_id,
                operation="knowledge.query", **dimensions, approval_id=approval.id, granted_by=f.actor_id, valid_until=approval.valid_until))
        db.add(DocumentPage(parse_run_id=f.run_id, file_page=2, printed_page="iv", state="processed"))
        db.flush()
        block.file_page = 2
        block.locator = {"section": "合成来源"}
        block.text_content = "标题：▶️ 合成启动。此句不得出现在引用里。"
        warning = ContentBlock(parse_run_id=f.run_id, ordinal=500, kind="warning", file_page=2, review_status="approved",
            text_content="红灯亮起时不得继续。", locator={"section": "合成来源"})
        db.add(warning)
        db.flush()
        relation = BlockRelation(parse_run_id=f.run_id, from_block_id=warning.id, to_block_id=block.id, relation_type="warns")
        region = {"file_page": 2, "geometry_status": "exact", "bbox_normalized": [0.1, 0.2, 0.3, 0.4]}
        drawing = ContentBlock(parse_run_id=f.run_id, ordinal=501, kind="image", file_page=2, review_status="approved",
                               locator={"regions": [region]})
        image_bytes = bytes.fromhex("89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c020000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082")
        key = "synthetic/region.png"
        path = f.config.storage_dir / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(image_bytes)
        file = FileObject(object_key=key, sha256=sha256(image_bytes).hexdigest(), status="available",
                          byte_size=len(image_bytes), mime_type="image/png", created_by=f.actor_id)
        db.add_all([relation, drawing, file])
        db.flush()
        asset = ParseArtifact(parse_run_id=f.run_id, file_id=file.id, kind="region_crop", logical_name=key)
        reference = BlockRelation(parse_run_id=f.run_id, from_block_id=block.id, to_block_id=drawing.id, relation_type="refers_to")
        db.add_all([asset, reference])
        db.flush()
        f.asset_ref = asset.id
        begin, end = 3, block.text_content.index("此")
        source_text = block.text_content[begin:end]
        f.expected = [source_text, warning.text_content]
        f.child_id, f.group_id, f.parent_id = str(uuid4()), str(uuid4()), str(uuid4())
        f.model = version.model_scope[0]
        groups = [{"id": f.group_id, "kind": "fact_group", "parse_run_id": f.run_id, "scope_key": "confirmed-device",
                   "structure_root_id": None, "members": [
                       {"block_id": block.id, "kind": "text", "role": "body", "block_start": begin, "block_end": end},
                       {"block_id": warning.id, "kind": "text", "role": "warning", "block_start": 0,
                        "block_end": len(warning.text_content), "relation_id": relation.id},
                       {"block_id": drawing.id, "kind": "region", "role": "context", "region_ref": "/locator/regions/0",
                        "asset_ref": asset.id, "relation_id": reference.id}],
                   "required_relation_ids": [relation.id, reference.id], "ordered_step_refs": [], "child_chunk_ids": [f.child_id],
                   "complete": True, "predecessor_refs": []}]
        chunk_set.config_hash, chunk_set.ir_content_hash = "a" * 64, "b" * 64
        chunk_set.tokenizer_revision = indexing.TOKENIZER_REVISION
        chunk_set.splitter_config = {"tokenizer_sha256": indexing.TOKENIZER_SHA256}
        chunk_set.stage_checkpoint = {"quality": {"status": "passed"}, "support_groups": groups,
                                      "projection_fingerprint": "c" * 64}
        run.stage_checkpoint = {"source_qc": {"critical_refs": {warning.id: ["warning"]},
                                              "required_relation_refs": [relation.id, reference.id]}}
        joined = "\n".join(f.expected)
        db.add(Chunk(id=f.child_id, chunk_set_id=f.chunk_id, parse_run_id=f.run_id, ordinal=0, level="child",
            kind="paragraph", eligibility="candidate", content=joined, content_hash="d" * 64,
            token_count=22, support_group_refs=[f.group_id], required_relation_ids=[relation.id, reference.id],
            assembly=[{"kind": "chunk_join", "text": "\n", "chunk_start": len(source_text), "chunk_end": len(source_text) + 1}]))
        db.add(Chunk(id=f.parent_id, chunk_set_id=f.chunk_id, parse_run_id=f.run_id, ordinal=1, level="parent",
            kind="section", eligibility="navigation_only", content_hash="a" * 64, support_group_refs=[f.group_id]))
        db.flush()
        for i, member in enumerate(groups[0]["members"]):
            segment = {key: value for key, value in member.items() if key != "block_id"}
            if member["kind"] == "text":
                start = 0 if i == 0 else len(source_text) + 1
                segment.update(chunk_start=start, chunk_end=start + member["block_end"] - member["block_start"])
            else:
                segment["locator_ref"] = segment.pop("region_ref")
            db.add(ChunkBlock(chunk_id=f.child_id, block_id=member["block_id"], chunk_set_id=f.chunk_id, parse_run_id=f.run_id,
                ordinal=i, segments=[segment]))
        db.commit()
    return f


def _build(db, f):
    kb, actor = db.get(KnowledgeBase, f.kb_id), db.get(User, f.actor_id)
    return indexing.create_child_generation(db, kb, actor, embedding_profile_id=f.revision_id,
                                            endpoints=f.app.state.service_endpoints)


def _switch(db, f, target_id, previous_id, *, action="activate"):
    kb, actor = db.get(KnowledgeBase, f.kb_id), db.get(User, f.actor_id)
    return indexing.switch_child_generation(db, kb, target_id, actor, expected_active_id=previous_id,
                                            expected_source_revision=kb.content_revision, action=action)


def test_child_generation_uses_exact_admitted_provider_batch_then_explicit_activation_and_evidence(semantic, record_property):
    f = semantic
    endpoints = f.app.state.service_endpoints
    with f.app.state.session_factory() as db:
        generation = _build(db, f)
        actor, kb = db.get(User, f.actor_id), db.get(KnowledgeBase, f.kb_id)
        assert generation.status == "building" and kb.active_generation_id == f.index_id
        generation_id = generation.id
        db.commit()
        pending = indexing.finalize_child_generation(db, generation, actor)
        assert pending.status == "building" and pending.build_report["integrity"]["missing_child_refs"] == [f.child_id]
        with pytest.raises(ProviderError, match="SERVICE_SYNTHETIC_INPUT_REQUIRED"):
            accept_child_embedding_batch(db, generation_id, 1, actor, endpoints)
        db.rollback()
        generation = db.get(IndexGeneration, generation_id)
        entries, texts, payload = child_batch_payload(db, generation.build_report["manifest"], 1)
        assert texts == ["\n".join(f.expected)] and entries[0]["id"] == f.child_id
        attest_synthetic_input(db, f.actor_id, "document.embed", f"index:{generation_id}:1", payload)
        db.commit()
        job = accept_child_embedding_batch(db, generation_id, 1, actor, endpoints)
        job_id, pin_id = job.id, job.pin_id
        db.commit()
        assert run_child_embedding_batch(db, job_id, endpoints).state == "succeeded"
        assert run_child_embedding_batch(db, job_id, endpoints).state == "succeeded"
        assert len(f.provider.requests) == 1
        assert db.scalar(select(ServiceUseCall).where(ServiceUseCall.pin_id == pin_id)).state == "received"
        generation = db.get(IndexGeneration, generation_id, populate_existing=True)
        assert generation.status == "ready" and db.get(KnowledgeBase, f.kb_id).active_generation_id == f.index_id
        assert indexing.child_generation_view(db, generation)["embedding_batches"][0]["state"] == "succeeded"
        assert db.get(ChunkEmbedding, (generation_id, f.child_id)) is not None
        _switch(db, f, generation_id, f.index_id)
        db.commit()
        scopes = {"model": f.model, "kb_ids": [f.kb_id], "max_bytes": 32768}
        result = search_child_chunks(db, actor, "合成启动", **scopes)
        assert result["mode"] == "child", result
        assert [item["quote"] for item in result["fragments"]] == [*f.expected, ""]
        assert result["support_groups"][0]["members"][1]["role"] == "warning"
        first = result["fragments"][0]
        assert first["printed_page"] == "iv" and first["file_page"] == 2 and first["block_start"] == 3
        assert first["dependencies"]["generation_id"] == generation_id and first["source_binding_hash"]
        region = result["fragments"][2]
        assert region["text"] is None and region["block_start"] is None and region["asset_refs"] == [f.asset_ref]
        assert region["locator"]["region"]["bbox_normalized"] == [0.1, 0.2, 0.3, 0.4]
        authority = {"model": f.model, "kb_ids": [f.kb_id]}
        assert all(validate_child_fragment(db, actor, item, **authority) for item in result["fragments"])
        assert not validate_child_fragment(db, actor, {**first, "block_end": first["block_end"] + 1}, **authority)
        assert not validate_child_fragment(db, actor, {**region, "asset_refs": ["another-asset"]}, **authority)
        assert not validate_child_fragment(db, actor, {**first, "locator": {**first["locator"], "file_page": 1}}, **authority)
        assert search_child_chunks(db, actor, "合成启动", **{**scopes, "max_bytes": 10})["warnings"] == ["evidence_budget"]
        vectors = search_child_chunks(db, actor, "", **scopes, query_vector=[1.0] + [0.0] * 1023, embedding_profile_id=f.revision_id)
        assert vectors["mode"] == "child" and len(vectors["hits"]) == 1
        assert search_child_chunks(db, actor, "合成启动", **scopes, query_vector=[1.0] + [0.0] * 1023,
                                   embedding_profile_id="wrong-space")["mode"] == "unavailable"

        connection = db.connection()
        statements = []

        def count_statement(*_):
            statements.append(1)

        event.listen(connection, "before_cursor_execute", count_statement)
        try:
            assert all(validate_child_fragment(db, actor, item, **authority) for item in result["fragments"])
            singles = len(statements)
            statements.clear()
            assert validate_child_fragments(db, actor, result["fragments"], **authority)
            batched = len(statements)
        finally:
            event.remove(connection, "before_cursor_execute", count_statement)
        record_property("individual_sql_statements", singles)
        record_property("batch_sql_statements", batched)
        assert 0 < batched < singles / 2
        assert not validate_child_fragments(db, actor, [first, {**region, "asset_refs": ["another-asset"]}], **authority)

        version = db.get(DocumentVersion, f.version_id)
        version.source_metadata = {**version.source_metadata, "source_description": "SYNTHETIC source changed after proof"}
        db.commit()
        assert not validate_child_fragments(db, actor, result["fragments"], **authority)


def test_invalid_scope_membership_and_stale_rollback_leave_the_active_pointer_unchanged(semantic):
    f = semantic
    with f.app.state.session_factory() as db:
        actor, kb = db.get(User, f.actor_id), db.get(KnowledgeBase, f.kb_id)
        generations = []
        for _ in range(3):
            row = _build(db, f)
            db.flush()
            manifest = row.build_report["manifest"]
            indexing.persist_child_embeddings(db, row, manifest["members"], [[1.0] + [0.0] * 1023])
            assert indexing.finalize_child_generation(db, row, actor).status == "ready"
            generations.append(row.id)
        # A real FK-valid parent row is still an unexpected vector member.
        db.add(ChunkEmbedding(generation_id=generations[1], chunk_id=f.parent_id, chunk_set_id=f.chunk_id,
            parse_run_id=f.run_id, document_version_id=f.version_id, entry_hash="1" * 64, profile_hash="2" * 64,
            embedding=[1.0] + [0.0] * 1023))
        db.flush()
        with pytest.raises(SafeError) as extra:
            _switch(db, f, generations[1], f.index_id)
        assert extra.value.code == "INDEX_VECTOR_MEMBERSHIP_CHANGED"
        db.delete(db.get(ChunkEmbedding, (generations[1], f.parent_id)))
        db.flush()
        _switch(db, f, generations[0], f.index_id)
        db.commit()
    barrier = Barrier(2)

    def race(target):
        with f.app.state.session_factory() as db:
            barrier.wait(timeout=10)
            try:
                _switch(db, f, target, generations[0])
                db.commit()
                return target, 200
            except SafeError as error:
                return target, error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(race, generations[1:]))
    assert sorted(status for _, status in outcomes) == [200, 412]
    winner = next(target for target, status in outcomes if status == 200)
    with f.app.state.session_factory() as db:
        _switch(db, f, generations[0], winner, action="rollback")
        db.commit()
        kb = db.get(KnowledgeBase, f.kb_id, populate_existing=True)
        assert kb.active_generation_id == generations[0]
        kb.content_revision += 1
        db.commit()
        with pytest.raises(SafeError, match="索引代际"):
            _switch(db, f, winner, generations[0], action="rollback")
        db.rollback()
        assert db.get(KnowledgeBase, f.kb_id).active_generation_id == generations[0]


@pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="isolated PostgreSQL required")
def test_frozen_pdf_source_qc_review_publication_child_index_and_live_evidence(recovery_pg, monkeypatch):
    """Use production P3/P4 qualification predicates over separate synthetic proofs."""
    import pymupdf

    _, config, account, _ = recovery_pg
    monkeypatch.setenv(_ENV, _SECRET)
    monkeypatch.delenv("DATACENTER_SYNTHETIC_SERVICE_ENDPOINTS", raising=False)
    app = create_app(config)
    try:
        with pymupdf.open() as pdf:
            pdf.new_page().insert_text((72, 72), SYNTHETIC_TEXT)
            pdf.new_page()
            original = pdf.tobytes()
        f = SimpleNamespace(app=app, config=config, actor_id=account["actor_id"],
            document_id=account["doc_id"], version_id=account["version_id"], kb_id=account["kb_id"],
            path=f"/documents/{account['doc_id']}/versions/{account['version_id']}/parse-runs")
        admin_name = "synthetic-p5-" + uuid4().hex
        with app.state.session_factory() as db:
            seed_reference_data(db)
            actor = db.get(User, f.actor_id)
            version = db.get(DocumentVersion, f.version_id)
            file = save_upload([original], "synthetic.pdf", "application/pdf", actor, config)
            db.add(file)
            db.flush()
            version.source_file_id = file.id
            db.add(User(username=admin_name, display_name="Synthetic service administrator", status="active",
                        credential_state="active", credential_hash=hash_password(PASSWORD), roles=["system_admin"]))
            db.commit()
        with run_synthetic_provider(credential=_SECRET) as provider, TestClient(app) as manager, TestClient(app) as administrator:
            app.state.service_endpoints = {"synthetic": LocalEndpoint(provider.port, _ENV)}
            _login(manager, account["username"])
            _login(administrator, admin_name)
            f.client = manager
            run_id = source_ready(f)
            set_id = build(f, run_id)
            review_id = review_candidate(f, run_id, set_id, verify_source(f, run_id))
            publish(f, set_id, review_id)
            _, _, f.revision_id = activate(administrator, "embedding")
            select_purpose(administrator, "document.embed", f.revision_id)
            provider.requests.clear()
            endpoints = app.state.service_endpoints
            with app.state.session_factory() as db:
                actor = db.get(User, f.actor_id)
                version = db.get(DocumentVersion, f.version_id)
                generation = _build(db, f)
                assert generation.build_report["integrity"]["status"] == "pending", generation.build_report["integrity"]
                assert generation.expected_chunk_count > 0
                generation_id = generation.id
                manifest = generation.build_report["manifest"]
                assert manifest["sources"][0]["review_set_id"] == review_id
                assert manifest["sources"][0]["source_qc_ref"] and manifest["sources"][0]["collection_qc_ref"]
                assert manifest["sources"][0]["publication_event_id"]
                jobs = []
                for batch in manifest["batches"]:
                    number = batch["batch_no"]
                    _, _, data = child_batch_payload(db, manifest, number)
                    attest_synthetic_input(db, actor.id, "document.embed", f"index:{generation_id}:{number}", data)
                    db.flush()
                    jobs.append(accept_child_embedding_batch(db, generation_id, number, actor, endpoints).id)
                db.commit()
                for job_id in jobs:
                    job = run_child_embedding_batch(db, job_id, endpoints)
                    assert job.state == "succeeded", job.error_code
                assert len(provider.requests) == len(jobs)
                _switch(db, f, generation_id, None)
                db.commit()
                scope = {"model": version.model_scope[0], "kb_ids": [f.kb_id]}
                match = search_child_chunks(db, actor, "pressure", max_bytes=50000, **scope)
                assert match["mode"] == "child" and match["fragments"], match
                assert any("5 Pa" in fragment["quote"] for fragment in match["fragments"])
                assert all(validate_child_fragment(db, actor, item, **scope) for item in match["fragments"])
                task = QueryTask(owner_user_id=actor.id, idempotency_key="synthetic-original-proof", effective_input={
                    "model": version.model_scope[0], "knowledge_base_ids": [f.kb_id]})
                db.add(task)
                db.flush()
                evidence = persist_child_evidence(db, task, match)
                db.commit()
                assert validate_task_evidence_batch(db, actor, task, evidence)
                original_path = file_path(db.get(FileObject, version.source_file_id), config)
                original_path.write_bytes(original[:-1] + (b"b" if original[-1:] == b"a" else b"a"))
                # The original FileObject, evidence and index remain unchanged.
                assert not validate_task_evidence_batch(db, actor, task, evidence)
                original_path.write_bytes(original)
                # Model a replaced version and retired vector set over the same intact source.
                version.publish_status = "superseded"
                generation.status = "retired"
                db.commit()
                assert all(validate_child_fragment(db, actor, item, **scope, saved_read=True) for item in match["fragments"])
                assert validate_child_fragments(db, actor, match["fragments"], **scope, saved_read=True)
                assert not any(validate_child_fragment(db, actor, item, **scope) for item in match["fragments"])
                version.publish_status, generation.status = "effective", "ready"
                db.commit()
                governance.create_hold(db, actor, version, {"kind": "quality", "reason": "SYNTHETIC changed source",
                                                            "affected_refs": [run_id]})
                db.commit()
                assert not any(validate_child_fragment(db, actor, item, **scope, saved_read=True) for item in match["fragments"])
                assert not validate_child_fragments(db, actor, match["fragments"], **scope, saved_read=True)
                assert not any(validate_child_fragment(db, actor, item, **scope) for item in match["fragments"])
                assert search_child_chunks(db, actor, "pressure", **scope)["warnings"] == ["index_stale"]
    finally:
        app.state.engine.dispose()


@pytest.mark.parametrize("value", [0.0, 1e-100, float("inf"), 1e100])
def test_pgvector_rejects_zero_nonfinite_and_float32_underflow_or_overflow(value):
    with pytest.raises(ValueError):
        stored_vector([value] + [0.0] * 1023)
