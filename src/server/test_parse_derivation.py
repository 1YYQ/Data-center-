"""Exercise local raw aliases through the real normalization worker."""
import json
from datetime import timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

import pytest

from .config import Settings
from .db import build_engine, init_db
from .models import AuthorizationGrant, ContentBlock, DocumentPage, DocumentVersion, FileObject, ParseArtifact, ParseRun, User, utc_now
from .parse_jobs import ParseServiceJob, _claim_job, accept_local_parse, run_local_parse_job, run_parse_job
from .parsing import derivation
from .parsing.artifacts import ArtifactError, archive_bytes, read_artifact
from .parsing.corrections import apply_manual_corrections, inherit_manual_corrections
from .parsing.derivation import derive_raw_run
from .parsing.ir import normalize_ir
from .parsing.ir_projection import _import_projection, _raw_inputs
from .provider_transport import ProviderError
from .services import SafeError, file_path, save_upload, seed_reference_data
from .test_governance import synthetic_draft
from .test_local_parse_pipeline import saved_bundle


@pytest.fixture
def local_parse():
    import pymupdf

    base = Path("outputs/test-fixtures")
    base.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="derive-", dir=base) as relative:
        root = Path(relative)
        config = Settings(database_url="sqlite:///" + (root / "derivation.sqlite").as_posix(), storage_dir=root / "storage",
                          local_mineru_executable=None, local_mineru_home=None)
        engine = build_engine(config.database_url)
        try:
            init_db(engine, config)
            factory = sessionmaker(bind=engine, class_=Session, expire_on_commit=False)
            with pymupdf.open() as pdf:
                pdf.new_page().insert_text((72, 72), "SYNTHETIC: power off before service.")
                pdf.new_page()
                original = pdf.tobytes()
            with factory() as db:
                seed_reference_data(db)
                actor, _, _, version, _, _ = synthetic_draft(db)
                file = save_upload([original], "synthetic.pdf", "application/pdf", actor, config)
                db.add(file)
                db.flush()
                version.source_file_id = file.id
                fixture = SimpleNamespace(factory=factory, config=config, actor_id=actor.id, version_id=version.id)
                db.commit()
            yield fixture
        finally:
            engine.dispose()


def normalized(fixture, *, image=False):
    def runner(source, workspace, executable, home, heartbeat):
        (workspace / "result.zip").write_bytes(saved_bundle(image=image))

    with fixture.factory() as db:
        actor, version = db.get(User, fixture.actor_id), db.get(DocumentVersion, fixture.version_id)
        run = accept_local_parse(db, actor, version, db.get(FileObject, version.source_file_id), fixture.config)
        run_id = run.id
        db.commit()
        raw = run_local_parse_job(db, run_id, fixture.config, runner=runner)
        assert raw.stage_checkpoint["stage_state"] == "parsed", raw.error_code
        result = run_parse_job(db, run_id, fixture.config, {})
        outputs = result.stage_checkpoint["outputs"]
        assert result.stage_checkpoint["stage_state"].startswith("qc_"), result.error_code
        assert outputs["normalized_ir"] != outputs["structured_ir"]
        assert outputs["structured_ir"] == result.ir_artifact_id
        return run_id


def _raw(db, run):
    selection = run.stage_checkpoint["page_selection"]["1"]
    return db.get(ParseArtifact, selection["raw_artifact_id"])


def test_child_run_reuses_physical_files_and_rebuilds_owned_page_references(local_parse):
    fixture = local_parse
    parent_id = normalized(fixture, image=True)
    with fixture.factory() as db:
        parent, actor = db.get(ParseRun, parent_id), db.get(User, fixture.actor_id)
        parent_ir = db.get(ParseArtifact, parent.stage_checkpoint["outputs"]["normalized_ir"])
        parent_body = read_artifact(db, parent_ir, fixture.config)
        parent_raw = _raw(db, parent)
        before = db.scalar(select(func.count(FileObject.id)))
        child = derive_raw_run(db, actor, parent, fixture.config)
        assert child.parent_run_id == parent.id and child.document_version_id == parent.document_version_id
        assert child.actual_profile == parent.actual_profile and child.input_fingerprint == parent.input_fingerprint
        aliases = db.scalars(select(ParseArtifact).where(ParseArtifact.parse_run_id == child.id,
                           ParseArtifact.derived_from_artifact_id.is_not(None))).all()
        assert {alias.kind for alias in aliases} == {"raw_archive", "preflight", "document_ir", "image"}
        for alias in aliases:
            source = db.get(ParseArtifact, alias.derived_from_artifact_id)
            assert source.parse_run_id == parent.id and alias.file_id == source.file_id
            assert alias.producer == source.producer
            assert read_artifact(db, alias, fixture.config) == read_artifact(db, source, fixture.config)
        assert db.scalar(select(func.count(FileObject.id))) == before + 1  # Only the new run-bound manifest.
        child_raw = _raw(db, child)
        assert child_raw.id != parent_raw.id and child_raw.file_id == parent_raw.file_id
        child_id = child.id
        db.commit()

        child = run_parse_job(db, child_id, fixture.config, {})
        outputs = child.stage_checkpoint["outputs"]
        assert child.stage_checkpoint["stage_state"].startswith("qc_"), child.error_code
        assert outputs["normalized_ir"] != outputs["structured_ir"]
        assert outputs["structured_ir"] == child.ir_artifact_id
        assert outputs["normalized_ir"] != parent_ir.id
        assert read_artifact(db, parent_ir, fixture.config) == parent_body
        for number in (1, 2):
            source = db.get(DocumentPage, (parent.id, number))
            derived = db.get(DocumentPage, (child.id, number))
            assert (derived.file_page, derived.state, derived.transforms) == (source.file_page, source.state, source.transforms)
            assert derived.raw_ref["artifact_id"] == child_raw.id
        blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == child.id)).all()
        assert blocks and all(block.review_status == "unreviewed" for block in blocks)
        source_blocks = [block for block in blocks if block.source_kind == "parser"]
        assert source_blocks and all(block.raw_ref["artifact_id"] == child_raw.id for block in source_blocks)

        descendant = derive_raw_run(db, actor, child, fixture.config)
        descendant_alias = _raw(db, descendant)
        assert descendant_alias.derived_from_artifact_id == child_raw.id
        assert descendant_alias.file_id == parent_raw.file_id
        assert read_artifact(db, descendant_alias, fixture.config) == read_artifact(db, parent_raw, fixture.config)


@pytest.mark.parametrize("invalid", ["version", "profile", "cycle"])
def test_inconsistent_ancestry_cannot_read_or_derive_a_raw_alias(local_parse, invalid):
    fixture = local_parse
    parent_id = normalized(fixture)
    with fixture.factory() as db:
        actor, parent = db.get(User, fixture.actor_id), db.get(ParseRun, parent_id)
        child = derive_raw_run(db, actor, parent, fixture.config)
        child_id = child.id
        db.commit()
        if invalid == "version":
            version = db.get(DocumentVersion, parent.document_version_id)
            second = DocumentVersion(document_id=version.document_id, source_file_id=version.source_file_id,
                                     created_by=actor.id, version_no=version.version_no + 1)
            db.add(second)
            db.flush()
            parent.document_version_id = second.id
        elif invalid == "profile":
            parent.actual_profile = {**parent.actual_profile, "parse_mode": "ocr" if parent.actual_profile["parse_mode"] == "txt" else "txt"}
        else:
            parent.parent_run_id = child_id
        db.commit()
        with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_LINEAGE_INVALID$"):
            read_artifact(db, _raw(db, child), fixture.config)
        with pytest.raises(SafeError) as caught:
            derive_raw_run(db, actor, child, fixture.config)
        assert caught.value.code == "PARSE_ARTIFACT_LINEAGE_INVALID"


def test_authority_must_still_hold_after_child_manifest_preparation(local_parse, monkeypatch):
    fixture = local_parse
    parent_id = normalized(fixture)
    prepare = derivation.prepare_artifact_set

    def revoke_after_preparation(*args, **kwargs):
        prepared = prepare(*args, **kwargs)
        with fixture.factory() as other:
            other.execute(update(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == fixture.actor_id)
                          .values(revoked_at=utc_now()))
            other.commit()
        return prepared

    monkeypatch.setattr(derivation, "prepare_artifact_set", revoke_after_preparation)
    with fixture.factory() as db:
        actor, parent = db.get(User, fixture.actor_id), db.get(ParseRun, parent_id)
        with pytest.raises(SafeError) as caught:
            derive_raw_run(db, actor, parent, fixture.config)
        assert caught.value.code == "FORBIDDEN"
        db.rollback()
        assert not db.scalar(select(ParseRun.id).where(ParseRun.parent_run_id == parent_id))
        assert db.get(ParseServiceJob, parent_id).checkpoint["output_state"] == "complete"


def _freeze_edited_candidate(db, actor, fixture, parent, text):
    child = derive_raw_run(db, actor, parent, fixture.config)
    db.commit()
    checkpoint = child.stage_checkpoint
    outputs = dict(checkpoint["outputs"])
    parent_alias = db.get(ParseArtifact, outputs["parent_normalized_ir"])
    parent_ir = json.loads(read_artifact(db, parent_alias, fixture.config))
    parent_block = next(block for block in parent_ir["blocks"] if block["kind"] == "paragraph")
    payload = {"blocks": [{"block_id": parent_block["id"], "text": text, "reason": "合成校正前后叙述",
        "basis": {"file_page": parent_block["file_page"], "description": "核对合成原页正文"}}]}
    raw_request = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    request = archive_bytes(db, child.id, actor.id, fixture.config, raw_request, kind="structured",
                            logical_name="ir/correction-request.json", producer={"role": "manual_correction"})
    job = db.get(ParseServiceJob, child.id)
    raw, _, preflight, middle, _, preflight_id = _raw_inputs(db, child, job, fixture.config)
    base = normalize_ir(run_id=child.id, version_id=child.document_version_id, source_file_id=job.source_file_id,
                        source_sha256=job.source_sha256, profile=child.request_config["profile"], preflight=preflight,
                        middle=middle, raw_artifact_id=raw.id, preflight_artifact_id=preflight_id,
                        asset_refs={ref["raw_member_name"]: ref for ref in checkpoint["inherited_asset_refs"]})
    inherited = inherit_manual_corrections(base, parent_ir=parent_ir, artifact_map=checkpoint["inherited_artifact_map"],
                                            parent_normalized_alias_id=parent_alias.id)
    corrected = apply_manual_corrections(inherited, parent_ir=parent_ir, correction_artifact_id=request.id, payload=payload)
    body = json.dumps(corrected, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    frozen = archive_bytes(db, child.id, actor.id, fixture.config, body, kind="document_ir", logical_name="ir/normalized.json",
                           producer={"phase": "normalized"}, derived_from_artifact_id=raw.id)
    outputs.update(normalized_ir=frozen.id, normalized_ir_sha256=db.get(FileObject, frozen.file_id).sha256)
    child.stage_checkpoint = {**checkpoint, "outputs": outputs, "stage_state": "normalized"}
    _import_projection(db, child, corrected)
    db.commit()
    return child, request, frozen, corrected


def test_repeated_corrections_keep_both_real_requests_and_parent_ir_on_owned_aliases(local_parse):
    fixture = local_parse
    parent_id = normalized(fixture, image=True)
    with fixture.factory() as db:
        actor, parent = db.get(User, fixture.actor_id), db.get(ParseRun, parent_id)
        first, request_1, frozen_1, first_ir = _freeze_edited_candidate(db, actor, fixture, parent, "第一次合成修正 🔧")
        first_body = read_artifact(db, frozen_1, fixture.config)
        second, request_2, _, second_ir = _freeze_edited_candidate(db, actor, fixture, first, "第二次合成修正 🧰")
        checkpoint = second.stage_checkpoint
        first_alias = db.get(ParseArtifact, checkpoint["inherited_artifact_map"][request_1.id])
        first_ir_alias = db.get(ParseArtifact, checkpoint["outputs"]["parent_normalized_ir"])
        assert first_alias.parse_run_id == second.id and first_alias.file_id == request_1.file_id
        assert first_alias.derived_from_artifact_id == request_1.id
        assert first_ir_alias.derived_from_artifact_id == frozen_1.id and first_ir_alias.file_id == frozen_1.file_id
        assert read_artifact(db, first_ir_alias, fixture.config) == read_artifact(db, frozen_1, fixture.config) == first_body
        assert json.loads(read_artifact(db, first_alias, fixture.config))["blocks"][0]["text"] == "第一次合成修正 🔧"
        assert json.loads(read_artifact(db, request_2, fixture.config))["blocks"][0]["text"] == "第二次合成修正 🧰"

        old, new = [next(block for block in ir["blocks"] if block["source_kind"] == "manual") for ir in (first_ir, second_ir)]
        assert new["source_block_ref"] == old["id"] and db.get(ContentBlock, old["id"]).parse_run_id == first.id
        assert new["raw_ref"]["artifact_id"] == _raw(db, second).id
        assert new["structured_content"]["manual_correction"]["text_source_ref"]["artifact_id"] == request_2.id
        assert new["structured_content"]["manual_correction"]["inherited_from"]["artifact_id"] == first_ir_alias.id
        assert {row["file_id"] for row in first_ir["artifact_refs"] if row["kind"] == "image"} == {
            row["file_id"] for row in second_ir["artifact_refs"] if row["kind"] == "image"
        }


def _claim_accepted_correction(db, parent, actor):
    body = {"blocks": [{"ordinal": 0, "text": "合成纠正证据 🧰", "reason": "合成原页核对",
                        "basis": {"file_page": 1, "description": "合成正文复核"}}], "graphic_tables": []}
    previous = {"epoch": 1, "state": "interrupted", "phase": "derivation"}
    source = db.get(ParseServiceJob, parent.id)
    run = ParseRun(document_version_id=parent.document_version_id, parent_run_id=parent.id, origin="manual", status="pending",
        request_config=dict(parent.request_config), actual_profile=dict(parent.actual_profile), input_fingerprint=parent.input_fingerprint,
        stage_checkpoint={"stage_state": "accepted", "derivation": True, "correction_payload": body,
                          "source_sha256": source.source_sha256, "attempts": [previous], "outputs": {}})
    db.add(run)
    db.flush()
    job = ParseServiceJob(parse_run_id=run.id, mode="local", actor_id=actor.id, source_file_id=source.source_file_id,
                          source_sha256=source.source_sha256, attempt=1, checkpoint={"purpose": "existing-test-job"})
    db.add(job)
    db.commit()
    attempt = _claim_job(db, job, run)
    assert attempt == 2
    return run, job, attempt, body, previous


def test_accepted_derivation_reclaims_same_objects_on_new_attempt_after_db_rollback(local_parse):
    fixture = local_parse
    parent_id = normalized(fixture)
    with fixture.factory() as db:
        actor, parent = db.get(User, fixture.actor_id), db.get(ParseRun, parent_id)
        target, job, attempt, request, previous = _claim_accepted_correction(db, parent, actor)
        target_id = target.id
        run = derive_raw_run(db, actor, parent, fixture.config, target_run=target, attempt=attempt)
        outputs = dict(run.stage_checkpoint["outputs"])
        first = db.get(ParseArtifact, outputs["manual_corrections"])
        assert first.logical_name == "corrections/input.json" and first.producer["role"] == "manual_correction"
        assert json.loads(read_artifact(db, first, fixture.config)) == request
        stored = db.get(FileObject, first.file_id)
        path = file_path(stored, fixture.config)
        mtime = path.stat().st_mtime_ns
        assert outputs["manual_corrections_sha256"] == stored.sha256
        with fixture.factory() as other:
            assert other.get(ParseRun, target_id).stage_checkpoint["outputs"] == {}
            assert not other.scalars(select(ParseArtifact).where(ParseArtifact.parse_run_id == target_id)).all()
        db.rollback()

        job.lease_until = utc_now() - timedelta(seconds=1)
        db.commit()
        retry = _claim_job(db, job, target)
        assert retry == attempt + 1
        committed = derive_raw_run(db, actor, parent, fixture.config, target_run=target, attempt=retry)
        db.commit()
        assert committed.id == target_id and committed.stage_checkpoint["outputs"] == outputs
        assert committed.status == "needs_review" and committed.stage_checkpoint["stage_state"] == "parsed"
        assert committed.stage_checkpoint["attempts"][0] == previous and committed.stage_checkpoint["attempts"][-1]["epoch"] == retry
        assert path.stat().st_mtime_ns == mtime
        same_job = db.get(ParseServiceJob, target_id)
        assert same_job.actor_id == actor.id and same_job.attempt == retry and same_job.lease_until is None
        assert same_job.checkpoint["purpose"] == "existing-test-job"
        assert db.scalars(select(ParseRun.id).where(ParseRun.parent_run_id == parent_id)).all() == [target_id]


@pytest.mark.parametrize(("decision", "expected"), [
    ("cancel", "PARSE_CANCELLED"), ("revoke", "PARSE_AUTHORITY_REVOKED"), ("lease", "PARSE_ATTEMPT_STALE"),
])
def test_lost_fences_after_preparing_accepted_derivation_bind_nothing(local_parse, monkeypatch, decision, expected):
    fixture = local_parse
    parent_id = normalized(fixture)
    with fixture.factory() as db:
        actor, parent = db.get(User, fixture.actor_id), db.get(ParseRun, parent_id)
        target, _, attempt, _, _ = _claim_accepted_correction(db, parent, actor)
        target_id = target.id
        original = derivation.prepare_artifact_set

        def invalidate(*args, **kwargs):
            prepared = original(*args, **kwargs)
            with fixture.factory() as other:
                child_job = other.get(ParseServiceJob, target_id)
                if decision == "cancel":
                    child_job.cancel_requested = True
                    other.get(ParseRun, target_id).status = "cancelled"
                elif decision == "revoke":
                    other.execute(update(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == fixture.actor_id)
                                  .values(revoked_at=utc_now()))
                else:
                    child_job.attempt += 1
                other.commit()
            return prepared

        monkeypatch.setattr(derivation, "prepare_artifact_set", invalidate)
        with pytest.raises(ProviderError, match=f"^{expected}$"):
            derive_raw_run(db, actor, parent, fixture.config, target_run=target, attempt=attempt)
        db.rollback()
        assert db.get(ParseRun, target_id).stage_checkpoint["outputs"] == {}
        assert not db.scalars(select(ParseArtifact).where(ParseArtifact.parse_run_id == target_id)).all()
