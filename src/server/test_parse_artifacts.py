from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import Settings
from .db import build_engine, init_db
from .models import Document, DocumentVersion, FileObject, KnowledgeBase, ParseArtifact, ParseRun, User
from .parsing import artifacts
from .parsing.artifacts import ArtifactError, archive_bytes, archive_workspace, bind_workspace, prepare_workspace, read_artifact, work_directory
from .services import file_path, save_upload


@pytest.fixture()
def artifact_db():
    fixtures = Path("outputs/test-fixtures")
    fixtures.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix="parse-artifacts-", dir=fixtures) as name:
        root = Path(name)
        settings = Settings(database_url=f"sqlite:///{(root / 'artifacts.sqlite').as_posix()}", storage_dir=root / "storage")
        engine = build_engine(settings.database_url)
        try:
            init_db(engine, settings)
            with Session(engine, expire_on_commit=False) as db:
                actor = User(display_name="Synthetic Manager", username="synthetic-manager")
                kb = KnowledgeBase(name="Synthetic Artifact Test")
                db.add_all([actor, kb])
                db.flush()
                source = save_upload([b"%PDF-1.4 synthetic"], "fixture.pdf", "application/pdf", actor, settings)
                document = Document(knowledge_base_id=kb.id, title="Synthetic")
                db.add_all([source, document])
                db.flush()
                version = DocumentVersion(document_id=document.id, version_no=1, source_file_id=source.id, created_by=actor.id)
                db.add(version)
                db.flush()
                run = ParseRun(document_version_id=version.id)
                db.add(run)
                db.commit()
                yield db, settings, run.id, actor.id
        finally:
            engine.dispose()


def test_workspace_archive_reconciles_after_database_rollback(artifact_db):
    db, settings, run_id, actor_id = artifact_db
    work = work_directory(settings, run_id, 1)
    (work / "images").mkdir()
    (work / "images" / "synthetic.png").write_bytes(b"synthetic image bytes")
    (work / "synthetic_middle.json").write_bytes(b'{"pdf_info":[]}')

    first = archive_workspace(db, run_id, actor_id, settings, work, epoch=1, complete=True)
    manifest_file = db.get(FileObject, first["manifest_file_id"])
    manifest_path = file_path(manifest_file, settings)
    original_object = manifest_path.stat()
    db.rollback()
    assert db.get(ParseArtifact, first["manifest_artifact_id"]) is None
    assert db.get(FileObject, first["manifest_file_id"]) is None
    assert manifest_path.is_file()
    assert work_directory(settings, run_id, 1) == work
    assert (work / "images" / "synthetic.png").is_file()

    second = archive_workspace(db, run_id, actor_id, settings, work, epoch=1, complete=True)
    db.commit()
    assert second == first
    assert manifest_path.stat().st_mtime_ns == original_object.st_mtime_ns
    assert second["complete"] and second["output_state"] == "complete"
    assert {entry["kind"] for entry in second["artifacts"]} == {"image", "middle_json"}
    manifest = db.get(ParseArtifact, second["manifest_artifact_id"])
    assert json.loads(read_artifact(db, manifest, settings)) == {
        key: value for key, value in second.items() if key not in {"manifest_artifact_id", "manifest_file_id"}
    }


def test_prepare_uses_only_storage_and_bind_uses_only_existing_metadata(artifact_db, monkeypatch):
    db, settings, run_id, actor_id = artifact_db
    work = work_directory(settings, run_id, 1)
    (work / "result.zip").write_bytes(b"synthetic parser output")
    db.close()

    offline = replace(settings, database_url="unavailable-database://")
    prepared = prepare_workspace(run_id, actor_id, offline, work, epoch=1, complete=True)
    objects = settings.storage_dir / "internal" / "parse-results" / run_id
    bodies = [path.read_bytes() for path in objects.iterdir() if not path.suffix]
    assert b"synthetic parser output" in bodies
    assert db.scalars(select(ParseArtifact)).all() == []

    def unexpected_storage_io(*args, **kwargs):
        pytest.fail("The database-only binding reopened artifact storage")

    with monkeypatch.context() as patches:
        patches.setattr(artifacts, "_storage_path", unexpected_storage_io)
        patches.setattr(artifacts, "_install_once", unexpected_storage_io)
        patches.setattr(artifacts, "_verify", unexpected_storage_io)
        manifest = bind_workspace(db, prepared)
        db.commit()
        assert bind_workspace(db, prepared) == manifest
    stored = db.get(ParseArtifact, manifest["artifacts"][0]["artifact_id"])
    assert read_artifact(db, stored, settings) == b"synthetic parser output"
    body_path = file_path(db.get(FileObject, stored.file_id), settings)
    body_path.unlink()
    with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_INTEGRITY_ERROR$"):
        prepare_workspace(run_id, actor_id, offline, work, epoch=1, complete=True)
    assert not body_path.exists()


def test_same_name_conflict_never_replaces_orphaned_bytes(artifact_db):
    db, settings, run_id, actor_id = artifact_db
    source = b"synthetic parser response"
    logical_name = "attempt/1/output/result.zip"
    first = archive_bytes(db, run_id, actor_id, settings, source, kind="raw_archive", logical_name=logical_name)
    first_id = first.id
    path = file_path(db.get(FileObject, first.file_id), settings)
    db.rollback()

    with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_CONFLICT$"):
        archive_bytes(db, run_id, actor_id, settings, b"different bytes", kind="raw_archive", logical_name=logical_name)
    assert path.read_bytes() == source
    assert db.get(ParseArtifact, first_id) is None
    artifact = archive_bytes(db, run_id, actor_id, settings, source, kind="raw_archive", logical_name=logical_name)
    assert artifact.id == first_id
    assert read_artifact(db, artifact, settings) == source

    separate_run = ParseRun(document_version_id=db.get(ParseRun, run_id).document_version_id)
    db.add(separate_run)
    db.flush()
    separate = archive_bytes(db, separate_run.id, actor_id, settings, source, kind="raw_archive", logical_name=logical_name)
    assert separate.file_id != artifact.file_id
    assert file_path(db.get(FileObject, separate.file_id), settings) != path


def test_reference_rejects_tampering_and_missing_objects(artifact_db):
    db, settings, run_id, actor_id = artifact_db
    logical_name = "attempt/1/output/result.zip"
    artifact = archive_bytes(db, run_id, actor_id, settings, b"fixture", kind="raw_archive", logical_name=logical_name)
    db.commit()
    path = file_path(db.get(FileObject, artifact.file_id), settings)
    path.write_bytes(b"changed")
    with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_INTEGRITY_ERROR$"):
        read_artifact(db, artifact, settings)
    path.unlink()
    with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_INTEGRITY_ERROR$"):
        read_artifact(db, artifact, settings)
    with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_INTEGRITY_ERROR$"):
        archive_bytes(db, run_id, actor_id, settings, b"fixture", kind="raw_archive", logical_name=logical_name)
    assert not path.exists()


def test_failed_attempt_records_no_output_and_preserves_real_partial_output(artifact_db):
    db, settings, run_id, actor_id = artifact_db
    empty = work_directory(settings, run_id, 1)
    failed = archive_workspace(db, run_id, actor_id, settings, empty, epoch=1, complete=False, safe_error="PARSE_TIMEOUT")
    assert (failed["output_state"], failed["complete"], failed["safe_error"], failed["artifacts"]) == (
        "none", False, "PARSE_TIMEOUT", [],
    )
    assert db.scalars(select(ParseArtifact.logical_name)).all() == ["attempt/1/manifest.json"]

    work = work_directory(settings, run_id, 2)
    (work / "preflight.json").write_bytes(b"{}")
    (work / "preflight.pending").write_bytes(b"{")
    (work / "receipt.json").write_bytes(b"{}")
    (work / "receipt.pending").write_bytes(b"{")
    (work / "runtime-config.json").write_bytes(b"{}")
    (work / "stdout.log").write_bytes(b"parser started")
    ancillary = archive_workspace(db, run_id, actor_id, settings, work, epoch=2, complete=False, safe_error="PARSE_TIMEOUT")
    assert (ancillary["output_state"], ancillary["complete"]) == ("none", False)
    assert {entry["logical_name"] for entry in ancillary["artifacts"]} == {
        "attempt/2/output/preflight.json", "attempt/2/output/preflight.pending", "attempt/2/output/receipt.json",
        "attempt/2/output/receipt.pending", "attempt/2/output/runtime-config.json", "attempt/2/output/stdout.log",
    }
    assert all(db.get(FileObject, entry["file_id"]).status == "available" for entry in ancillary["artifacts"])

    work = work_directory(settings, run_id, 3)
    (work / "output").mkdir()
    (work / "output" / "middle_json.json").write_bytes(b'{"incomplete"')
    (work / "output" / "structured_content.json").write_bytes(b"[]")
    (work / "result.zip").write_bytes(b"PK")
    partial = archive_workspace(db, run_id, actor_id, settings, work, epoch=3, complete=False, safe_error="PARSE_TIMEOUT")
    assert (partial["output_state"], partial["complete"], partial["safe_error"]) == ("partial", False, "PARSE_TIMEOUT")
    expected = {"middle_json": b'{"incomplete"', "structured": b"[]", "raw_archive": b"PK"}
    assert {entry["kind"] for entry in partial["artifacts"]} == expected.keys()
    for entry in partial["artifacts"]:
        artifact = db.get(ParseArtifact, entry["artifact_id"])
        file = db.get(FileObject, artifact.file_id)
        assert file.status == "quarantined"
        assert file_path(file, settings).read_bytes() == expected[entry["kind"]]
        with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_UNAVAILABLE$"):
            read_artifact(db, artifact, settings)
    assert db.get(FileObject, partial["manifest_file_id"]).status == "available"


def test_workspace_requires_the_validated_attempt_path(artifact_db):
    db, settings, run_id, actor_id = artifact_db
    with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_IDENTITY_INVALID$"):
        work_directory(settings, "../escape", 1)
    other_attempt = work_directory(settings, run_id, 2)
    with pytest.raises(ArtifactError, match="^PARSE_ARTIFACT_PATH_INVALID$"):
        archive_workspace(db, run_id, actor_id, settings, other_attempt, epoch=1, complete=False)
