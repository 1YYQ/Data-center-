"""Two durable HTTP correction requests with exact inherited source provenance."""
import json
from uuid import uuid4

from sqlalchemy import select

from .models import ContentBlock, ParseArtifact, ParseRun
from .parse_jobs import run_parse_job
from .parsing.artifacts import read_artifact
from .parsing.ir_projection import normalize_parse_run
from .parsing.processing import source_verification
from .test_local_parse_pipeline import local_parse
from .test_source_processing import source_ready


def test_http_accepts_before_io_and_worker_preserves_both_manual_sources_across_two_generations(local_parse):
    fixture, parent_id = local_parse, source_ready(local_parse)
    with fixture.app.state.session_factory() as db:
        parent = db.get(ParseRun, parent_id)
        original_ir = db.get(ParseArtifact, parent.ir_artifact_id)
        original_bytes = read_artifact(db, original_ir, fixture.config)
    previous = parent_id
    history = []
    for number in (6, 7):
        with fixture.app.state.session_factory() as db:
            block = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == previous, ContentBlock.structure_role == "leaf"))
            payload = {"blocks": [{"block_id": block.id, "text": block.text_content.replace("5 Pa", f"{number} Pa").replace("6 Pa", f"{number} Pa"),
                "reason": "SYNTHETIC proposed correction; not a business approval", "basis": {"file_page": 1,
                    "description": "SYNTHETIC original-page comparison fixture"}}]}
        key = str(uuid4())
        path = f"/parse-runs/{previous}/corrections"
        response = fixture.client.post(path, json=payload, headers={"Idempotency-Key": key})
        assert response.status_code == 202, response.text
        new_id = response.json()["parse_run_id"]
        assert response.json()["status"] == "pending"
        assert fixture.client.post(path, json=payload, headers={"Idempotency-Key": key}).json() == response.json()
        with fixture.app.state.session_factory() as db:
            assert not db.scalar(select(ParseArtifact.id).where(ParseArtifact.parse_run_id == new_id))
            result = run_parse_job(db, new_id, fixture.config, {})
            assert result.stage_checkpoint["source_qc"]["status"] == "passed", result.stage_checkpoint.get("source_qc", result.error_code)
            assert source_verification(db, result)["state"] == "pending" and result.status == "needs_review"
            assert result.parent_run_id == previous
            current = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == new_id, ContentBlock.structure_role == "leaf"))
            assert current.source_kind == "manual" and current.source_block_ref == block.id
            assert f"{number} Pa" in current.text_content
            assert result.stage_checkpoint["source_qc"]["critical_refs"][current.id]
            request_id = result.stage_checkpoint["outputs"]["manual_corrections"]
            request = json.loads(read_artifact(db, db.get(ParseArtifact, request_id), fixture.config))
            assert request["blocks"][0]["text"] == current.text_content
            assert current.structured_content["text_map"][0]["raw_ref"]["artifact_id"] == request_id
            normalize_parse_run(db, new_id, fixture.config)  # Replays raw + every frozen manual overlay.
            assert read_artifact(db, original_ir, fixture.config) == original_bytes
            history.append(request_id)
        previous = new_id
    with fixture.app.state.session_factory() as db:
        final = db.get(ParseRun, previous)
        aliases = final.stage_checkpoint["inherited_artifact_map"]
        assert history[0] in aliases
        assert read_artifact(db, db.get(ParseArtifact, aliases[history[0]]), fixture.config)
