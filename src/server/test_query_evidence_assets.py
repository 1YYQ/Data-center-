"""Employee evidence image boundaries on isolated, synthetic task data."""

from copy import deepcopy
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from .app import create_app
from .execution_models import QueryAnswerSnapshot, ToolInvocation
from .models import (AuthorizationGrant, BusinessApproval, Chunk, ChunkSet, ContentBlock, Document, DocumentVersion,
                     ParseRun, QueryTask, TaskEvidence, TaskShare, User, utc_now)
from .parsing.artifacts import archive_bytes
from .security import hash_password
from .services import file_path, save_upload
from .test_services import _ready_fixture, fixture_db
from .testing_governance import seed_verified_approval, seed_verified_version


_PASSWORD = "SYNTHETIC isolated evidence password"


def _employee(db, manager, kb, document, nickname, *, grant=True):
    actor = User(username=f"evidence-{nickname}-{uuid4().hex}", display_name="Synthetic employee", roles=["employee"],
                 status="active", credential_state="active", credential_hash=hash_password(_PASSWORD))
    db.add(actor)
    db.flush()
    if grant:
        dims = {"level": document.access_level, "category": document.category, "need_class": document.need_class}
        approval = BusinessApproval(applicant=actor.id, recorded_by=manager.id, operations=["knowledge.query", "document.read"],
            scope={**dims, "knowledge_base_id": kb.id, "document_id": None})
        db.add(approval)
        db.flush()
        seed_verified_approval(db, approval)
        db.add_all(AuthorizationGrant(subject_user_id=actor.id, knowledge_base_id=kb.id, operation=op,
            granted_by=manager.id, approval_id=approval.id, valid_until=approval.valid_until, **dims) for op in approval.operations)
    return actor


@pytest.fixture
def evidence_routes(fixture_db):
    import pymupdf

    db, settings = fixture_db
    manager, kb, previous_document, _, _ = _ready_fixture(db, settings)
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=200, height=200)
        page.draw_rect(pymupdf.Rect(10, 10, 100, 100), color=(.5, 0, 0), fill=(.5, 0, 0))
        page.insert_text((20, 40), "SYNTHETIC")
        original_bytes = pdf.tobytes()
        image_bytes = page.get_pixmap().tobytes("png")
    file = save_upload([original_bytes], "SYNTHETIC.pdf", "application/octet-stream", manager, settings)
    document = Document(knowledge_base_id=kb.id, title="SYNTHETIC original", access_level=previous_document.access_level,
                        category=previous_document.category, need_class=previous_document.need_class)
    db.add_all([file, document])
    db.flush()
    version = DocumentVersion(document_id=document.id, version_no=1, source_file_id=file.id, created_by=manager.id,
                              model_scope=["SYNTHETIC-REGION"])
    db.add(version)
    db.flush()
    run = ParseRun(document_version_id=version.id, status="succeeded", origin="synthetic")
    db.add(run)
    db.flush()
    bbox = [.05, .05, .5, .5]
    region = {"file_page": 1, "bbox_normalized": bbox, "geometry_status": "exact"}
    locator = {"regions": [region]}
    block = ContentBlock(parse_run_id=run.id, ordinal=0, kind="image", text_content="", locator=locator)
    chunk_set = ChunkSet(parse_run_id=run.id)
    db.add_all([block, chunk_set])
    db.flush()
    seed_verified_version(db, manager, document, version, run, chunk_set.id)
    image = archive_bytes(db, run.id, manager.id, settings, image_bytes, kind="image", logical_name="images/actual.png")
    other = archive_bytes(db, run.id, manager.id, settings, image_bytes, kind="image", logical_name="images/other.png")
    raster_claim = archive_bytes(db, run.id, manager.id, settings, b"<script>synthetic</script>",
                                 kind="image", logical_name="images/disguised.png")
    owner = _employee(db, manager, kb, document, "owner")
    recipient = _employee(db, manager, kb, document, "recipient")
    outsider = _employee(db, manager, kb, document, "outsider")
    task = QueryTask(owner_user_id=owner.id, status="succeeded", accepted_scope={"operation": "knowledge.query"},
                     idempotency_key="synthetic-evidence-task")
    db.add(task)
    db.flush()
    evidence = TaskEvidence(task_id=task.id, document_version_id=version.id, block_id=block.id, quote_snapshot="",
                            evidence_kind="region", locator=deepcopy(locator), asset_refs=[image.id], review_at_run="approved")
    receipt = ToolInvocation(actor_user_id=owner.id, task_id=task.id, tool_name="dc_artifacts_validate", logical_action_key="synthetic",
                             args_hash="a" * 64, execution_revision=1, state="completed", output={"ready": True})
    db.add_all([evidence, receipt])
    db.flush()
    snapshot = QueryAnswerSnapshot(task_id=task.id, execution_revision=1, validation_ref=receipt.id,
                                   result={"answer": None}, evidence_ids=[evidence.id])
    db.add(snapshot)
    db.flush()
    share = TaskShare(task_id=task.id, recipient_user_id=recipient.id, granted_by=owner.id, answer_snapshot_id=snapshot.id)
    db.add(share)
    db.commit()
    app = create_app(settings)
    path = f"/tasks/{task.id}/evidence/{evidence.id}"
    with TestClient(app) as owners, TestClient(app) as recipients, TestClient(app) as outsiders:
        for client, actor in ((owners, owner), (recipients, recipient), (outsiders, outsider)):
            answer = client.post("/auth/login", json={"username": actor.username, "password": _PASSWORD})
            assert answer.status_code == 200, answer.text
        yield SimpleNamespace(db=db, settings=settings, path=path, app=app, owner=owners, recipient=recipients, outsider=outsiders,
            evidence=evidence, share=share, snapshot=snapshot, version=version, run=run, block=block, task=task, file=file,
            image=image, other=other, raster_claim=raster_claim, png=image_bytes, region=region, chunk_set=chunk_set,
            manager_id=manager.id)
    app.state.engine.dispose()


def _assert_private(response, mime):
    assert response.status_code == 200, response.text if response.status_code != 200 else ""
    assert response.headers["content-type"] == mime
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cross-origin-resource-policy"] == "same-origin"


def test_employee_crops_original_and_reads_only_its_raster_asset(evidence_routes):
    import pymupdf

    f = evidence_routes
    cropped = f.owner.get(f.path + "/source-image?region=0")
    _assert_private(cropped, "image/png")
    pixmap = pymupdf.Pixmap(cropped.content)
    assert 240 < pixmap.width < 260 and pixmap.height == pixmap.width
    response = f.owner.get(f.path + "/assets/" + f.image.id)
    _assert_private(response, "image/png")
    assert response.content == f.png
    assert f.owner.get(f"/parse-runs/{f.run.id}/assets/{f.image.id}").status_code in (403, 404)
    assert f.owner.get(f.path + "/assets/" + f.other.id).status_code == 404
    assert f.outsider.get(f.path + "/source-image").status_code == 404
    assert f.owner.get(f.path.replace(f.task.id, str(uuid4())) + "/source-image").status_code == 404


def test_revoked_share_and_answer_membership_cannot_open_binary_from_saved_url(evidence_routes):
    f = evidence_routes
    url = f.path + "/assets/" + f.image.id
    extra = TaskEvidence(task_id=f.task.id, document_version_id=f.version.id, block_id=f.block.id, quote_snapshot="",
                         source_binding_hash="c" * 64)
    f.db.add(extra)
    f.db.commit()
    # A different source in the execution is incomplete, but it is outside
    # this answer's scope and must neither be revealed nor block its images.
    _assert_private(f.recipient.get(url), "image/png")
    task_base = f"/tasks/{f.task.id}"
    collection = f.recipient.get(task_base + "/evidence")
    assert collection.status_code == 200 and [item["id"] for item in collection.json()["items"]] == [f.evidence.id]
    answer = f.recipient.get(task_base + "/result")
    assert answer.status_code == 200 and [item["id"] for item in answer.json()["evidence"]] == [f.evidence.id]
    assert f.recipient.get(f.path.replace(f.evidence.id, extra.id) + "/source-image").status_code == 404
    f.snapshot.evidence_ids = [f.evidence.id, str(uuid4())]
    f.db.commit()
    assert f.recipient.get(url).status_code == 404
    f.db.delete(extra)
    f.snapshot.evidence_ids = []
    f.db.commit()
    assert f.recipient.get(url).status_code == 404
    f.snapshot.evidence_ids = [f.evidence.id]
    f.share.revoked_at = utc_now()
    f.db.commit()
    assert f.recipient.get(url).status_code == 404
    _assert_private(f.owner.get(url), "image/png")


def test_asset_and_original_must_contain_verified_bytes(evidence_routes):
    f = evidence_routes
    f.evidence.asset_refs = [f.raster_claim.id]
    f.db.commit()
    assert f.owner.get(f.path + "/assets/" + f.raster_claim.id).status_code == 409
    foreign_run = ParseRun(document_version_id=f.version.id, status="succeeded", origin="synthetic")
    f.db.add(foreign_run)
    f.db.flush()
    foreign_asset = archive_bytes(f.db, foreign_run.id, f.manager_id, f.settings, f.png,
                                  kind="image", logical_name="images/foreign.png")
    f.evidence.asset_refs = [foreign_asset.id]
    f.db.commit()
    assert f.owner.get(f.path + "/assets/" + foreign_asset.id).status_code == 404
    f.evidence.asset_refs = [f.image.id]
    f.db.commit()
    original = file_path(f.file, f.settings)
    body = original.read_bytes()
    original.write_bytes(body[:-1] + (b"b" if body[-1:] == b"a" else b"a"))
    assert f.owner.get(f.path + "/source-image").status_code == 404
    assert f.owner.get(f.path + "/assets/" + f.image.id).status_code == 409


def test_exact_child_region_uses_its_single_locator_and_rechecks_current_qualification(evidence_routes, monkeypatch):
    from . import evidence_api, query_evidence

    f = evidence_routes
    child = Chunk(chunk_set_id=f.chunk_set.id, parse_run_id=f.run.id, ordinal=0, level="child", kind="figure",
                  eligibility="asset_only", content_hash="a" * 64)
    f.db.add(child)
    f.db.flush()
    f.evidence.chunk_id, f.evidence.chunk_set_id, f.evidence.source_binding_hash = child.id, f.chunk_set.id, "b" * 64
    f.evidence.source_context = {"fragment": {"block_id": f.block.id, "document_version_id": f.version.id,
        "chunk_id": child.id, "chunk_set_id": f.chunk_set.id, "evidence_kind": "region"}}
    f.evidence.locator = {"block_id": f.block.id, "document_version_id": f.version.id, "parse_run_id": f.run.id,
        "source_file_id": f.file.id, "source_sha256": f.file.sha256, "region": deepcopy(f.region),
        "regions": [{**f.region, "file_page": 99}]}
    f.db.commit()
    active = True

    def current(db, actor, fragments, payload, saved_read):
        assert len(fragments) == 1 and fragments[0]["chunk_id"] == child.id
        assert saved_read is True
        return active

    monkeypatch.setattr(query_evidence, "_validate_task_fragments", current)
    _assert_private(f.owner.get(f.path + "/source-image?region=0"), "image/png")
    assert f.owner.get(f.path + "/source-image?region=1").status_code == 404
    f.evidence.locator = {**f.evidence.locator, "region": {"file_page": 1, "geometry_status": "page_only"}}
    f.db.commit()
    response = f.owner.get(f.path + "/source-image")
    assert response.status_code == 404 and response.json()["code"] == "EVIDENCE_REGION_UNAVAILABLE"
    assert response.headers["cache-control"] == "private, no-store"
    f.evidence.locator = {**f.evidence.locator, "region": deepcopy(f.region)}
    f.db.commit()
    original_path = file_path(f.file, f.settings)
    original = original_path.read_bytes()
    original_path.write_bytes(original[:-1] + (b"b" if original[-1:] == b"a" else b"a"))
    assert f.owner.get(f.path + "/source-image").status_code == 403
    original_path.write_bytes(original)
    render = evidence_api.render_source_region

    def render_then_revoke(*args, **kwargs):
        nonlocal active
        result = render(*args, **kwargs)
        active = False
        return result

    monkeypatch.setattr(evidence_api, "render_source_region", render_then_revoke)
    assert f.owner.get(f.path + "/source-image").status_code == 403
    assert f.owner.get(f.path + "/assets/" + f.image.id).status_code == 403
