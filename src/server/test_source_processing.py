"""Isolated persistent structure/QC and real human-proof governance mechanics."""
import json

import pytest
from sqlalchemy import select

from . import governance as g
from .governance_models import GovernanceEvidence
from .models import BlockRelation, ChunkSet, ContentBlock, DocumentPage, DocumentVersion, ParseArtifact, ParseRun, User
from .parsing.artifacts import read_artifact
from .parsing.processing import (assert_structured_projection, load_structured_ir, projection_fingerprint,
    relation_review_state, run_source_qc, source_confirmation_scope, source_qc_is_current, source_verification, structure_parse_run)
from .test_ir_projection import normalized
from .test_local_parse_pipeline import local_parse
from .testing_governance import seed_confirmation


def source_ready(fixture):
    run_id = normalized(fixture)
    with fixture.app.state.session_factory() as db:
        structured = structure_parse_run(db, run_id, fixture.config)
        assert structured.stage_checkpoint["stage_state"] == "structured", structured.error_code
        result = run_source_qc(db, run_id, fixture.config)
        assert result.stage_checkpoint["source_qc"]["status"] == "passed", result.stage_checkpoint["source_qc"]["issues"]
    return run_id


def test_structured_artifact_qc_and_business_proof_are_separate_and_no_chunk_is_required(local_parse):
    fixture, run_id = local_parse, source_ready(local_parse)
    with fixture.app.state.session_factory() as db:
        run = db.get(ParseRun, run_id)
        version, actor = db.get(DocumentVersion, run.document_version_id), db.get(User, fixture.actor_id)
        normalized_artifact = db.get(ParseArtifact, run.stage_checkpoint["outputs"]["normalized_ir"])
        normalized_bytes = read_artifact(db, normalized_artifact, fixture.config)
        frozen = db.get(ParseArtifact, run.ir_artifact_id)
        structured_bytes = read_artifact(db, frozen, fixture.config)
        ir = json.loads(structured_bytes)
        assert_structured_projection(db, run, ir)
        assert source_qc_is_current(db, run) and source_verification(db, run)["state"] == "pending"
        assert run.status == "needs_review" and not db.scalar(select(ChunkSet.id).where(ChunkSet.parse_run_id == run.id))
        relations = db.scalars(select(BlockRelation).where(BlockRelation.parse_run_id == run.id)).all()
        assert relations and all(relation_review_state(db, run, row) == "candidate" for row in relations)
        leaves = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id, ContentBlock.structure_role == "leaf")).all()
        for block in leaves:
            g.review_content_block(db, actor, run, block, "approved", "SYNTHETIC original checked")
        assert run.status == "needs_review"  # Block clicks cannot create a professional statement.
        professional = seed_confirmation(db, actor, version, "key_content", "accepted",
            {**g.source_binding(db, version), **source_confirmation_scope(db, run)})
        assert run.status == "succeeded" and source_verification(db, run)["state"] == "verified"
        assert all(relation_review_state(db, run, row) == "confirmed" for row in relations)
        db.add(ChunkSet(parse_run_id=run.id, status="failed", splitter_version="SYNTHETIC failed downstream"))
        db.commit()
        assert source_verification(db, run)["state"] == "verified" and run.status == "succeeded"
        assert read_artifact(db, frozen, fixture.config) == structured_bytes
        assert read_artifact(db, normalized_artifact, fixture.config) == normalized_bytes
        proof = db.get(GovernanceEvidence, professional.proof_refs[0])
        from .models import utc_now
        proof.revoked_at = utc_now()
        db.commit()
        assert source_qc_is_current(db, run) and source_verification(db, run)["state"] == "pending"
        assert all(relation_review_state(db, run, row) == "candidate" for row in relations)
        assert read_artifact(db, frozen, fixture.config) == structured_bytes
    visible = fixture.client.get(f"/parse-runs/{run_id}/blocks")
    assert visible.status_code == 200
    assert visible.json()["source_qc"]["status"] == "passed" and visible.json()["source_verification"]["state"] == "pending"
    block_id = visible.json()["items"][0]["id"]
    image = fixture.client.get(f"/parse-runs/{run_id}/blocks/{block_id}/source-image")
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"


def test_frozen_relationship_change_invalidates_source_qc_and_business_proof(local_parse):
    fixture, run_id = local_parse, source_ready(local_parse)
    with fixture.app.state.session_factory() as db:
        run = db.get(ParseRun, run_id)
        before = projection_fingerprint(db, run.id)
        relation = db.scalar(select(BlockRelation).where(BlockRelation.parse_run_id == run.id))
        relation.basis = {**relation.basis, "scope": {"document": "SYNTHETIC drift"}}
        db.commit()
        assert projection_fingerprint(db, run.id) != before
        assert not source_qc_is_current(db, run)
        assert source_verification(db, run)["state"] == "pending"
        # No attempt to repair/rewrite the immutable structured artifact.
        assert read_artifact(db, db.get(ParseArtifact, run.ir_artifact_id), fixture.config)


def test_source_confirmation_keeps_legacy_projection_digest_and_one_row_snapshot(local_parse):
    """Preserve the signed field/order contract while removing duplicate reads."""
    from sqlalchemy import event
    from .parsing.ir_projection import PAGE_FIELDS, BLOCK_FIELDS
    from .parsing.processing import RELATION_FIELDS
    from .parsing.local_pipeline import fingerprint
    run_id = source_ready(local_parse)
    with local_parse.app.state.session_factory() as db:
        run = db.get(ParseRun, run_id)
        pages = db.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == run_id).order_by(DocumentPage.file_page)).all()
        blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id).order_by(ContentBlock.ordinal)).all()
        relations = db.scalars(select(BlockRelation).where(BlockRelation.parse_run_id == run_id).order_by(BlockRelation.id)).all()
        old_digest = fingerprint({'pages': [{key: getattr(row, key) for key in PAGE_FIELDS} for row in pages],
            'blocks': [{key: getattr(row, key) for key in BLOCK_FIELDS} for row in blocks],
            'relations': [{key: getattr(row, key) for key in RELATION_FIELDS} for row in relations]})
        statements = []
        def observe(conn, cursor, statement, *args):
            statements.append(statement)
        event.listen(local_parse.app.state.engine, 'before_cursor_execute', observe)
        try:
            scope = source_confirmation_scope(db, run)
        finally:
            event.remove(local_parse.app.state.engine, 'before_cursor_execute', observe)
        assert scope['source_projection_hash'] == old_digest
        assert len(statements) == 3
        blocks[0].text_content += ' SYNTHETIC mutation'
        db.commit()
        assert source_confirmation_scope(db, run)['source_projection_hash'] != old_digest
