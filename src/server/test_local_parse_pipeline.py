"""Durable local parsing with real HTTP/SQLite and synthetic SDK save bundles."""
from datetime import timedelta
import io
import json
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile, ZipInfo

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from .app import create_app
from .config import Settings
from .db import init_db
from .models import ChunkSet, ContentBlock, DocumentVersion, ParseArtifact, ParseRun, User, utc_now
from .parse_jobs import ParseServiceJob, run_local_parse_job
from .parsing.artifacts import read_artifact, work_directory
from .security import hash_password
from .services import save_upload, seed_reference_data
from .test_governance import synthetic_draft


PASSWORD = "SYNTHETIC local parse testing"
SYNTHETIC_TEXT = "SYNTHETIC pressure: 5 Pa; disconnect power before maintenance."


def saved_bundle(*, version="4.2.3", missing_page=False, missing_image=False, malformed_metadata=False,
                 malformed_structured=False, empty_nonblank=False, image=False):
    payload = {"schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": True,
        "metadata": {"file_suffix": "pdf", "producer": {"name": "MinerU", "version": version}},
        "extensions": {"mineru": {"tier": "standard", "parse_mode": "txt"},
            "docvortex_layout": {"version": 1, "pages": [
                {"page_idx": 0, "width_pt": 595, "height_pt": 842},
                {"page_idx": 1, "width_pt": 595, "height_pt": 842}]}},
        "pages": [{"page_idx": 0, "blocks": [{"type": "paragraph", "index": 0, "bbox": [.1, .1, .8, .3],
            "content": [{"type": "text", "content": SYNTHETIC_TEXT}]}]}]}
    if not missing_page:
        payload["pages"].append({"page_idx": 1, "blocks": []})
    if missing_image:
        payload["pages"][0]["blocks"].append({"type": "image", "bbox": [.1, .4, .8, .7],
            "content": [{"type": "image_body", "src": "images/missing.png"}]})
    if malformed_metadata:
        payload["metadata"] = []
    if empty_nonblank:
        payload["pages"][0]["blocks"] = []
    if image:
        payload["pages"][0]["blocks"].append({"type": "image", "bbox": [.1, .4, .8, .7],
            "content": [{"type": "image_body", "src": "images/synthetic.png"}]})
    buffer = io.BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, data in (("middle_json.json", json.dumps(payload)),
                           ("structured_content.json", "{" if malformed_structured else json.dumps({"pages": payload["pages"]})),
                           ("markdown.md", "SYNTHETIC fixture")):
            archive.writestr(ZipInfo(name, date_time=(2026, 11, 19, 0, 0, 0)), data)
        if image:
            import base64
            archive.writestr(ZipInfo("images/synthetic.png", date_time=(2026, 11, 19, 0, 0, 0)),
                base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aK1cAAAAASUVORK5CYII="))
    return buffer.getvalue()


@pytest.fixture
def local_parse(tmp_path):
    import pymupdf
    config = Settings(database_url="sqlite:///" + (tmp_path / "pipeline.sqlite").as_posix(), storage_dir=tmp_path / "storage")
    app = create_app(config)
    init_db(app.state.engine, config)
    with pymupdf.open() as pdf:
        page = pdf.new_page()
        page.insert_text((72, 72), SYNTHETIC_TEXT)
        pdf.new_page()
        original = pdf.tobytes()
    with app.state.session_factory() as db:
        seed_reference_data(db)
        actor, _, document, version, _, _ = synthetic_draft(db)
        actor.credential_state, actor.credential_hash = "active", hash_password(PASSWORD)
        file = save_upload([original], "synthetic.pdf", "application/pdf", actor, config)
        db.add(file)
        db.flush()
        version.source_file_id = file.id
        fixture = SimpleNamespace(app=app, config=config, actor_id=actor.id, version_id=version.id,
            document_id=document.id, source=file.object_key, original=original,
            path=f"/documents/{document.id}/versions/{version.id}/parse-runs", username=actor.username)
        db.commit()
    try:
        with TestClient(app) as client:
            login = client.post("/auth/login", json={"username": fixture.username, "password": PASSWORD})
            assert login.status_code == 200
            client.headers["X-CSRF-Token"] = login.json()["csrf_token"]
            fixture.client = client
            yield fixture
    finally:
        app.state.engine.dispose()


def accept(fixture):
    key = str(uuid4())
    result = fixture.client.post(fixture.path, json={}, headers={"Idempotency-Key": key})
    assert result.status_code == 202, result.text
    repeat = fixture.client.post(fixture.path, json={}, headers={"Idempotency-Key": key})
    assert repeat.json() == result.json()
    return result.json()["parse_run_id"]


def write_saved_bundle(source, workspace, executable, home, heartbeat):
    (workspace / "result.zip").write_bytes(saved_bundle())
    return workspace / "result.zip"


def test_http_acceptance_is_durable_read_only_get_and_worker_archives_full_source(local_parse):
    fixture = local_parse
    run_id = accept(fixture)
    before = fixture.client.get("/parse-runs/" + run_id)
    assert before.status_code == 200 and before.json()["status"] == "pending"
    with fixture.app.state.session_factory() as db:
        assert db.get(ParseServiceJob, run_id).attempt == 0
        assert not db.scalars(select(ParseArtifact).where(ParseArtifact.parse_run_id == run_id)).all()
        result = run_local_parse_job(db, run_id, fixture.config, runner=write_saved_bundle)
        assert result.status == "needs_review" and result.stage_checkpoint["stage_state"] == "parsed"
        assert result.stage_checkpoint["processed_page_count"] == 2
        job = db.get(ParseServiceJob, run_id)
        assert job.checkpoint["output_state"] == "complete" and job.raw_result_file_id
        artifact = db.scalar(select(ParseArtifact).where(ParseArtifact.parse_run_id == run_id, ParseArtifact.kind == "raw_archive"))
        assert read_artifact(db, artifact, fixture.config) == saved_bundle()
        assert not db.scalars(select(ChunkSet).where(ChunkSet.parse_run_id == run_id)).all()
        assert not db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id)).all()
    assert (fixture.config.storage_dir / fixture.source).read_bytes() == fixture.original
    assert fixture.client.get("/parse-runs/" + run_id).json()["quality_state"] == "unverified"


def test_archive_commit_failure_recovers_original_bytes_without_running_mineru_again(local_parse, monkeypatch):
    fixture = local_parse
    run_id = accept(fixture)
    calls = []
    def runner(*args):
        calls.append(1)
        return write_saved_bundle(*args)
    with fixture.app.state.session_factory() as db:
        original_commit = db.commit
        failed = []
        def fail_final_commit():
            run = db.get(ParseRun, run_id)
            if run.status == "needs_review" and not failed:
                failed.append(True)
                raise OperationalError("SYNTHETIC commit interruption", {}, Exception("synthetic"))
            original_commit()
        monkeypatch.setattr(db, "commit", fail_final_commit)
        first = run_local_parse_job(db, run_id, fixture.config, runner=runner)
        assert first.status == "waiting_external" and failed
        second = run_local_parse_job(db, run_id, fixture.config, runner=runner)
        assert second.status == "needs_review" and calls == [1]
        assert db.get(ParseServiceJob, run_id).checkpoint["output_epoch"] == 1
        assert second.stage_checkpoint["attempts"][0]["state"] == "waiting_external"
        assert second.stage_checkpoint["attempts"][-1]["epoch"] == 2


def test_worker_death_after_closed_zip_before_receipt_seal_adopts_without_reparsing(local_parse):
    fixture = local_parse
    run_id = accept(fixture)
    with fixture.app.state.session_factory() as db:
        from .models import FileObject
        from .parsing.local_pipeline import source_preflight
        run, job = db.get(ParseRun, run_id), db.get(ParseServiceJob, run_id)
        file = db.get(FileObject, job.source_file_id)
        workspace = work_directory(fixture.config, run_id, 1)
        preflight = source_preflight(fixture.config.storage_dir / file.object_key, file.sha256, file.byte_size)
        (workspace / "preflight.json").write_text(json.dumps(preflight), encoding="utf-8")
        (workspace / "receipt.json").write_text(json.dumps({"input_fingerprint": run.input_fingerprint,
            "epoch": 1, "state": "running", "safe_error": None}), encoding="utf-8")
        (workspace / "result.zip").write_bytes(saved_bundle())
        run.status, job.attempt, job.lease_until = "running", 1, utc_now() - timedelta(seconds=1)
        db.commit()
        def must_not_parse(*args):
            raise AssertionError("closed raw output must be adopted before any new inference")
        result = run_local_parse_job(db, run_id, fixture.config, runner=must_not_parse)
        assert result.status == "needs_review"
        assert db.get(ParseServiceJob, run_id).checkpoint["output_epoch"] == 1
        assert result.stage_checkpoint["attempts"][0]["state"] == "interrupted"
        assert result.stage_checkpoint["page_selection"]["1"]["raw_artifact_id"]


@pytest.mark.parametrize("options,code", [
    ({"version": "0.0-SYNTHETIC"}, "PARSE_PROFILE_MISMATCH"),
    ({"missing_page": True}, "PARSE_PAGE_COVERAGE_INCOMPLETE"),
    ({"missing_image": True}, "PARSE_RESOURCE_MISSING"),
    ({"malformed_metadata": True}, "PARSE_CONTRACT_UNSUPPORTED"),
    ({"malformed_structured": True}, "PARSE_CONTRACT_UNSUPPORTED"),
    ({"empty_nonblank": True}, "PARSE_PAGE_COVERAGE_INCOMPLETE"),
])
def test_invalid_bundle_is_retained_but_not_promoted(local_parse, options, code):
    fixture, raw = local_parse, saved_bundle(**options)
    run_id = accept(fixture)
    def runner(source, workspace, executable, home, heartbeat):
        (workspace / "result.zip").write_bytes(raw)
    with fixture.app.state.session_factory() as db:
        result = run_local_parse_job(db, run_id, fixture.config, runner=runner)
        assert result.status == "failed" and result.error_code == code
        assert db.get(ParseServiceJob, run_id).checkpoint["output_state"] == "partial"
        artifact = db.scalar(select(ParseArtifact).where(ParseArtifact.parse_run_id == run_id, ParseArtifact.kind == "raw_archive"))
        from .models import FileObject
        file = db.get(FileObject, artifact.file_id)
        assert file.status == "quarantined"
        assert (fixture.config.storage_dir / file.object_key).read_bytes() == raw
        assert not result.ir_artifact_id


def test_cancel_during_parse_keeps_raw_evidence_but_cannot_commit_a_result(local_parse):
    fixture = local_parse
    run_id = accept(fixture)
    def runner(*args):
        response = fixture.client.post(f"/parse-runs/{run_id}/cancel", json={"reason": "SYNTHETIC cancellation"},
            headers={"If-Match": '"0"', "Idempotency-Key": str(uuid4())})
        assert response.status_code == 200, response.text
        return write_saved_bundle(*args)
    with fixture.app.state.session_factory() as db:
        result = run_local_parse_job(db, run_id, fixture.config, runner=runner)
        assert result.status == "cancelled" and not result.ir_artifact_id
        assert db.scalars(select(ParseArtifact).where(ParseArtifact.parse_run_id == run_id)).all()
        assert not db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id)).all()


def test_missing_standard_environment_is_visible_without_fabricating_raw_output(local_parse):
    fixture = local_parse
    run_id = accept(fixture)
    with fixture.app.state.session_factory() as db:
        result = run_local_parse_job(db, run_id, fixture.config)
        assert result.status == "waiting_external" and result.error_code == "LOCAL_STANDARD_NOT_READY"
        assert db.get(ParseServiceJob, run_id).checkpoint["output_state"] == "none"


def test_expired_host_without_closed_output_or_child_exit_does_not_repeat_inference(local_parse):
    fixture, run_id = local_parse, accept(local_parse)
    with fixture.app.state.session_factory() as db:
        run, job = db.get(ParseRun, run_id), db.get(ParseServiceJob, run_id)
        workspace = work_directory(fixture.config, run_id, 1)
        (workspace / "receipt.json").write_text(json.dumps({"input_fingerprint": run.input_fingerprint,
            "epoch": 1, "state": "running", "safe_error": None}), encoding="utf-8")
        run.status, job.attempt, job.lease_until = "running", 1, utc_now() - timedelta(seconds=1)
        db.commit()
        def must_not_parse(*args):
            raise AssertionError("uncertain SDK lifetime must not dispatch new inference")
        result = run_local_parse_job(db, run_id, fixture.config, runner=must_not_parse)
        assert result.status == "reconciliation_required" and result.error_code == "PARSE_EXECUTION_UNCERTAIN"


def test_profile_change_creates_new_run_instead_of_reusing_waiting_configuration(local_parse, monkeypatch):
    from . import parse_jobs
    fixture, first_id = local_parse, accept(local_parse)
    with fixture.app.state.session_factory() as db:
        result = run_local_parse_job(db, first_id, fixture.config)
        assert result.status == "waiting_external"
    old_identity = parse_jobs._local_runtime_identity(fixture.config)
    monkeypatch.setattr(parse_jobs, "_local_runtime_identity", lambda _: {**old_identity, "model_installations": [{"sha256": "synthetic-new-install"}]})
    second_id = accept(fixture)
    assert second_id != first_id
    with fixture.app.state.session_factory() as db:
        assert db.get(ParseRun, first_id).error_code == "PARSE_PROFILE_CHANGED"
        assert db.get(ParseRun, second_id).request_config != db.get(ParseRun, first_id).request_config
