"""Frozen synthetic package -> normalized artifact -> real relational/HTTP views."""
import hashlib
import json
from uuid import uuid4

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError, OperationalError

from .models import AuthorizationGrant, ContentBlock, DocumentPage, ParseArtifact, ParseRun
from .parse_jobs import run_local_parse_job, run_parse_job
from .parsing.artifacts import read_artifact
from .parsing.ir_projection import assert_projection, normalize_parse_run
from .parsing.local_pipeline import ParseContractError
from .test_local_parse_pipeline import local_parse, accept, saved_bundle


def normalized(fixture, *, image=False):
    run_id = accept(fixture)
    def runner(source, workspace, executable, home, heartbeat):
        (workspace / "result.zip").write_bytes(saved_bundle(image=image))
    with fixture.app.state.session_factory() as db:
        raw = run_local_parse_job(db, run_id, fixture.config, runner=runner)
        assert raw.stage_checkpoint["stage_state"] == "parsed"
        result = normalize_parse_run(db, run_id, fixture.config)
        assert result.stage_checkpoint["stage_state"] == "normalized", result.error_code
    return run_id


def test_normalized_artifact_is_frozen_and_projection_preserves_all_pages_sources_and_assets(local_parse):
    fixture = local_parse
    run_id = normalized(fixture, image=True)
    with fixture.app.state.session_factory() as db:
        run = db.get(ParseRun, run_id)
        artifact = db.get(ParseArtifact, run.stage_checkpoint["outputs"]["normalized_ir"])
        body = read_artifact(db, artifact, fixture.config)
        assert hashlib.sha256(body).hexdigest() == run.stage_checkpoint["outputs"]["normalized_ir_sha256"]
        ir = json.loads(body)
        assert ir["phase"] == "normalized" and len(ir["pages"]) == 2
        assert_projection(db, run_id, ir)
        blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id)).all()
        assert all(block.review_status == "unreviewed" and block.file_page == 1 for block in blocks)
        assert all(block.raw_ref["artifact_id"] == artifact.derived_from_artifact_id for block in blocks)
        assert not run.ir_artifact_id  # Only Structured IR can become the selected frozen IR.
        normalize_parse_run(db, run_id, fixture.config)
        assert read_artifact(db, artifact, fixture.config) == body
        assert db.get(DocumentPage, (run_id, 2)).state == "blank"
        image = db.scalar(select(ParseArtifact).where(ParseArtifact.parse_run_id == run_id, ParseArtifact.logical_name.like("assets/%")))
        assert image and image.derived_from_artifact_id == artifact.derived_from_artifact_id
        image_id = image.id
    pages = fixture.client.get(f"/parse-runs/{run_id}/pages", params={"limit": 1}).json()
    assert len(pages["items"]) == 1 and pages["next_after"] == 1 and pages["total_page_count"] == 2
    assert fixture.client.get(f"/parse-runs/{run_id}/pages", params={"after": 1}).json()["items"][0]["state"] == "blank"
    response = fixture.client.get(f"/parse-runs/{run_id}/assets/{image_id}")
    assert response.status_code == 200 and response.headers["cache-control"] == "private, no-store"
    assert response.headers["content-type"] == "image/png"
    assert fixture.client.get(f"/parse-runs/{run_id}/assets/{artifact.id}").status_code == 404
    other_id = normalized(fixture)
    assert fixture.client.get(f"/parse-runs/{other_id}/assets/{image_id}").status_code == 404
    with fixture.app.state.session_factory() as db:
        from .models import utc_now
        db.execute(update(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == fixture.actor_id).values(revoked_at=utc_now()))
        db.commit()
    assert fixture.client.get(f"/parse-runs/{run_id}/pages").status_code == 403
    assert fixture.client.get(f"/parse-runs/{run_id}/assets/{image_id}").status_code == 403


def test_projection_drift_is_detected_and_never_rewrites_frozen_ir(local_parse):
    fixture = local_parse
    run_id = normalized(fixture)
    with fixture.app.state.session_factory() as db:
        run = db.get(ParseRun, run_id)
        artifact = db.get(ParseArtifact, run.stage_checkpoint["outputs"]["normalized_ir"])
        before = read_artifact(db, artifact, fixture.config)
        block = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == run_id))
        block.text_content = "SYNTHETIC drift"
        db.commit()
        with pytest.raises(ParseContractError, match="IR_PROJECTION_MISMATCH"):
            normalize_parse_run(db, run_id, fixture.config)
        db.rollback()
        assert read_artifact(db, artifact, fixture.config) == before


def test_page_and_parent_boundaries_reject_cross_run_references(local_parse):
    fixture = local_parse
    first, second = normalized(fixture), normalized(fixture)
    with fixture.app.state.session_factory() as db:
        one = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == first))
        two = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == second))
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                two.parent_block_id = one.id
                db.flush()
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                one.file_page = 3
                db.flush()
        with pytest.raises(IntegrityError):
            with db.begin_nested():
                page = db.get(DocumentPage, (first, 1))
                db.delete(page)
                db.flush()
        assert db.get(DocumentPage, (first, 1)) is not None


@pytest.mark.parametrize("committed", [False, True])
def test_ir_commit_interruption_resumes_exact_frozen_bytes_without_reparsing(local_parse, monkeypatch, committed):
    fixture = local_parse
    run_id = accept(fixture)
    with fixture.app.state.session_factory() as db:
        from .test_local_parse_pipeline import write_saved_bundle
        run_local_parse_job(db, run_id, fixture.config, runner=write_saved_bundle)
        raw_id = db.get(ParseRun, run_id).stage_checkpoint["outputs"]["raw_manifest"]
        original_commit, failures = db.commit, []
        def interrupted():
            if db.get(ParseRun, run_id).stage_checkpoint.get("stage_state") == "normalized" and not failures:
                failures.append(True)
                if committed:
                    original_commit()
                raise OperationalError("SYNTHETIC interrupted commit", {}, Exception("synthetic"))
            original_commit()
        monkeypatch.setattr(db, "commit", interrupted)
        first = normalize_parse_run(db, run_id, fixture.config)
        assert first.status == ("needs_review" if committed else "waiting_external")
        # The production worker entrypoint must go straight to IR recovery.
        # No executable/model environment is available in this fixture.
        second = run_parse_job(db, run_id, fixture.config, {})
        assert second.stage_checkpoint["outputs"].get("normalized_ir"), second.error_code
        assert second.stage_checkpoint["outputs"].get("structured_ir"), second.error_code
        assert second.stage_checkpoint["outputs"]["raw_manifest"] == raw_id
        irs = db.scalars(select(ParseArtifact).where(ParseArtifact.parse_run_id == run_id,
                        ParseArtifact.logical_name == "ir/normalized.json")).all()
        assert len(irs) == 1 and failures == [True]


def test_ir_prepared_before_authority_revocation_cannot_install_projection(local_parse, monkeypatch):
    fixture, run_id = local_parse, accept(local_parse)
    from .parsing import ir_projection as projection
    from .test_local_parse_pipeline import write_saved_bundle
    prepare = projection.prepare_artifact_set
    def revoked_after_write(*args, **kwargs):
        result = prepare(*args, **kwargs)
        with fixture.app.state.session_factory() as other:
            from .models import utc_now
            other.execute(update(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == fixture.actor_id).values(revoked_at=utc_now()))
            other.commit()
        return result
    with fixture.app.state.session_factory() as db:
        run_local_parse_job(db, run_id, fixture.config, runner=write_saved_bundle)
        monkeypatch.setattr(projection, "prepare_artifact_set", revoked_after_write)
        result = normalize_parse_run(db, run_id, fixture.config)
        assert result.status == "failed" and result.error_code == "PARSE_AUTHORITY_REVOKED"
        assert not db.scalar(select(ContentBlock.id).where(ContentBlock.parse_run_id == run_id))
        assert not result.stage_checkpoint["outputs"].get("normalized_ir")
