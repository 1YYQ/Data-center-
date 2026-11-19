"""Real offline tokenizer, persistent chunks and explicit publication/rebind."""
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from . import governance as g
from .chunk_jobs import accept_chunk_set, chunk_set_qc_is_current, run_chunk_job
from .models import Chunk, ChunkBlock, ContentBlock, Document, DocumentVersion, KnowledgeBase, ParseArtifact, ParseRun, User
from .parsing.artifacts import read_artifact
from .parsing.derivation import derive_raw_run
from .parsing.processing import source_confirmation_scope, source_verification
from .parse_jobs import run_parse_job
from .test_local_parse_pipeline import local_parse
from .test_source_processing import source_ready
from .testing_governance import seed_confirmation
from .services import SafeError


TOKENIZER = Path("ai_models/modelscope_cache/models/models/BAAI--bge-m3/snapshots/master/tokenizer.json")


def build(fixture, run_id):
    if not TOKENIZER.is_file():
        pytest.skip("fixed local BGE-M3 tokenizer is not installed; no download in tests")
    with fixture.app.state.session_factory() as db:
        run, actor = db.get(ParseRun, run_id), db.get(User, fixture.actor_id)
        result = accept_chunk_set(db, actor, run, "dc.semantic-bge-m3/1")
        set_id = result.id
        db.commit()
        result = run_chunk_job(db, set_id, replace(fixture.config, local_bge_tokenizer=TOKENIZER))
        assert result.status == "ready", result.stage_checkpoint
        assert chunk_set_qc_is_current(db, result), result.stage_checkpoint.get("quality")
        return set_id


def verify_source(fixture, run_id, *, repair_of=()):
    with fixture.app.state.session_factory() as db:
        run, actor = db.get(ParseRun, run_id), db.get(User, fixture.actor_id)
        version = db.get(DocumentVersion, run.document_version_id)
        for block in db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id, ContentBlock.structure_role == "leaf")):
            g.review_content_block(db, actor, run, block, "approved", "SYNTHETIC original checked")
        scope = {**g.source_binding(db, version), **source_confirmation_scope(db, run)}
        if repair_of:
            scope["repair_of_hold_refs"] = list(repair_of)
        item = seed_confirmation(db, actor, version, "key_content", "accepted", scope)
        assert source_verification(db, run)["state"] == "verified"
        db.commit()
        return item.id


def review_candidate(fixture, run_id, set_id, professional):
    with fixture.app.state.session_factory() as db:
        actor, run = db.get(User, fixture.actor_id), db.get(ParseRun, run_id)
        version = db.get(DocumentVersion, run.document_version_id)
        leaves = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id, ContentBlock.structure_role == "leaf")).all()
        review = g.create_review_set(db, actor, version, {"parse_run_id": run.id, "chunk_set_id": set_id,
            "reviewed_block_refs": [{"id": row.id, "revision": row.revision, "content_hash": g.block_binding(row)} for row in leaves],
            "professional_confirmation_refs": [professional], "reason": "SYNTHETIC candidate review"})
        db.commit()
        return review.id


def publish(fixture, set_id, review_id):
    with fixture.app.state.session_factory() as db:
        actor, version = db.get(User, fixture.actor_id), db.get(DocumentVersion, fixture.version_id)
        doc = db.get(Document, version.document_id)
        binding = g.source_binding(db, version)
        seed_confirmation(db, actor, version, "applicability", "applicable", binding)
        seed_confirmation(db, actor, version, "classification", "classified", {**binding,
            "access_level": doc.access_level, "category": doc.category, "need_class": doc.need_class, "rule_version": "SYNTHETIC classification"})
        g.select_source_candidate(db, actor, version, {"chunk_set_id": set_id, "review_set_id": review_id})
        preview = g.create_publication_preview(db, actor, doc, version, {"action": "publish"})
        assert preview.allowed, preview.blocked_reasons
        g.publish_from_preview(db, actor, doc, version, {"preview_id": preview.id})
        db.commit()
        assert g.publication_is_current(db, version)


def test_offline_chunk_build_before_human_review_has_exact_members_and_keeps_source_frozen(local_parse):
    fixture, run_id = local_parse, source_ready(local_parse)
    set_id = build(fixture, run_id)
    with fixture.app.state.session_factory() as db:
        run = db.get(ParseRun, run_id)
        before = read_artifact(db, db.get(ParseArtifact, run.ir_artifact_id), fixture.config)
        assert run.status == "needs_review"  # Ready collection cannot sign source facts.
        from .models import ChunkSet
        chunk_set = db.get(ChunkSet, set_id)
        chunks = db.scalars(select(Chunk).where(Chunk.chunk_set_id == set_id)).all()
        mappings = db.scalars(select(ChunkBlock).where(ChunkBlock.chunk_set_id == set_id)).all()
        assert chunks and mappings and any(row.level == "child" and row.token_count > 0 for row in chunks)
        assert not db.get(DocumentVersion, fixture.version_id).selected_chunk_set_id
        for mapping in mappings:
            block = db.get(ContentBlock, mapping.block_id)
            chunk = db.get(Chunk, mapping.chunk_id)
            for span in mapping.segments:
                if span["kind"] == "text":
                    assert chunk.content[span["chunk_start"]:span["chunk_end"]] == block.text_content[span["block_start"]:span["block_end"]]
        again = accept_chunk_set(db, db.get(User, fixture.actor_id), run, "dc.semantic-bge-m3/1")
        assert again.id == set_id
        db.commit()
        assert read_artifact(db, db.get(ParseArtifact, run.ir_artifact_id), fixture.config) == before
    professional = verify_source(fixture, run_id)
    review_id = review_candidate(fixture, run_id, set_id, professional)
    with fixture.app.state.session_factory() as db:
        version = db.get(DocumentVersion, fixture.version_id)
        assert version.selected_chunk_set_id is None  # Review is a candidate, never an implicit switch.
    base = f"/documents/{fixture.document_id}/versions/{fixture.version_id}"
    version_rev = next(row for row in fixture.client.get(f"/documents/{fixture.document_id}/versions").json()["items"] if row["id"] == fixture.version_id)["revision"]
    key = str(uuid4())
    body = {"chunk_set_id": set_id, "review_set_id": review_id}
    headers = {"Idempotency-Key": key, "If-Match": f'"{version_rev}"'}
    response = fixture.client.post(base + "/source-selection", json=body, headers=headers)
    assert response.status_code == 200, response.text
    assert fixture.client.post(base + "/source-selection", json=body, headers=headers).json() == response.json()


def test_effective_candidate_stays_unselected_until_current_refresh_preview_is_consumed(local_parse):
    fixture, first = local_parse, source_ready(local_parse)
    first_set = build(fixture, first)
    first_review = review_candidate(fixture, first, first_set, verify_source(fixture, first))
    publish(fixture, first_set, first_review)
    with fixture.app.state.session_factory() as db:
        parent = db.get(ParseRun, first)
        child = derive_raw_run(db, db.get(User, fixture.actor_id), parent, fixture.config)
        child_id = child.id
        db.commit()
        assert run_parse_job(db, child_id, fixture.config, {}).stage_checkpoint["source_qc"]["status"] == "passed"
    child_set = build(fixture, child_id)
    child_review = review_candidate(fixture, child_id, child_set, verify_source(fixture, child_id))
    with fixture.app.state.session_factory() as db:
        version, actor = db.get(DocumentVersion, fixture.version_id), db.get(User, fixture.actor_id)
        doc = db.get(Document, version.document_id)
        assert version.selected_chunk_set_id == first_set and g.publication_is_current(db, version)
        old_revision = db.get(KnowledgeBase, doc.knowledge_base_id).content_revision
        preview = g.create_publication_preview(db, actor, doc, version, {"action": "source_rebind", "mode": "refresh",
            "chunk_set_id": child_set, "review_set_id": child_review})
        assert preview.allowed, preview.blocked_reasons
        result = g.rebind_source_from_preview(db, actor, doc, version, {"preview_id": preview.id})
        db.commit()
        assert result["index_readiness"] == "requires_rebuild" and version.selected_chunk_set_id == child_set
        assert g.publication_is_current(db, version)
        assert db.get(KnowledgeBase, doc.knowledge_base_id).content_revision == old_revision + 1
        assert db.get(ParseRun, first).ir_artifact_id != db.get(ParseRun, child_id).ir_artifact_id


def test_quality_repair_requires_actual_hold_coverage_and_releases_only_bound_quality_holds(local_parse):
    fixture, first = local_parse, source_ready(local_parse)
    first_set = build(fixture, first)
    first_review = review_candidate(fixture, first, first_set, verify_source(fixture, first))
    publish(fixture, first_set, first_review)
    with fixture.app.state.session_factory() as db:
        parent = db.get(ParseRun, first)
        child = derive_raw_run(db, db.get(User, fixture.actor_id), parent, fixture.config)
        child_id = child.id
        db.commit()
        run_parse_job(db, child_id, fixture.config, {})
    child_set = build(fixture, child_id)
    child_review = review_candidate(fixture, child_id, child_set, verify_source(fixture, child_id))
    with fixture.app.state.session_factory() as db:
        actor, version = db.get(User, fixture.actor_id), db.get(DocumentVersion, fixture.version_id)
        doc = db.get(Document, version.document_id)
        hold = g.create_hold(db, actor, version, {"kind": "quality", "reason": "SYNTHETIC source defect",
                    "affected_refs": [first]})
        hold_id = hold.id
        db.commit()
        assert not g.publication_is_current(db, version)
        blocked = g.create_publication_preview(db, actor, doc, version, {"action": "source_rebind", "mode": "quality_repair",
                    "chunk_set_id": child_set, "review_set_id": child_review})
        assert not blocked.allowed and "QUALITY_HOLD_REPAIR_UNPROVEN" in blocked.blocked_reasons
        assert version.selected_chunk_set_id == first_set and hold.released_at is None
    # Only a new actual-statement fixture that explicitly names this hold can
    # authorize the bound repair. The technical QC button cannot do so.
    repaired_review = review_candidate(fixture, child_id, child_set, verify_source(fixture, child_id, repair_of=[hold_id]))
    with fixture.app.state.session_factory() as db:
        actor, version = db.get(User, fixture.actor_id), db.get(DocumentVersion, fixture.version_id)
        doc = db.get(Document, version.document_id)
        before_version = version.id
        preview = g.create_publication_preview(db, actor, doc, version, {"action": "source_rebind", "mode": "quality_repair",
                    "chunk_set_id": child_set, "review_set_id": repaired_review})
        assert preview.allowed, preview.blocked_reasons
        g.rebind_source_from_preview(db, actor, doc, version, {"preview_id": preview.id})
        db.commit()
        from .governance_models import DocumentUseHold
        assert db.get(DocumentUseHold, hold_id).released_at is not None
        assert version.id == before_version and version.publish_status == "effective"
        assert version.selected_chunk_set_id == child_set and g.publication_is_current(db, version)
        old = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == first, ContentBlock.structure_role == "leaf"))
        with pytest.raises(SafeError) as caught:
            g.review_content_block(db, actor, db.get(ParseRun, first), old, "unreviewed", "SYNTHETIC old source mutation")
        assert caught.value.detail["code"] == "PUBLISHED_BATCH_IMMUTABLE"
