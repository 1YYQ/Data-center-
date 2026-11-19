"""Real PostgreSQL parent-tombstone races for the older maintenance routes."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import threading
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from . import test_creation_recovery as creation
from .models import ApiIdempotencyRecord, ContentBlock, Document, DocumentVersion, ParseRun, User
from .services import save_upload
from .test_worker import synthetic_pdf

OUTPUT = Path(os.environ.get("DATACENTER_TEST_EVIDENCE_ROOT", "outputs/local-browser")) / "legacy-maintenance"
pytestmark = pytest.mark.skipif(not os.environ.get("DATACENTER_TEST_POSTGRES_URL"), reason="Explicit local PostgreSQL required")


@pytest.fixture
def legacy_pg(request, monkeypatch):
    monkeypatch.setattr(creation, "OUTPUT", OUTPUT)
    yield from creation.recovery_pg.__wrapped__(request)


@pytest.mark.parametrize("action", ["patch", "parse", "correction", "review"])
def test_committed_delete_blocks_pre_authorized_maintenance(legacy_pg, monkeypatch, action):
    engine, config, f, report = legacy_pg
    with Session(engine) as db:
        version = db.get(DocumentVersion, f["version_id"])
        file = save_upload([synthetic_pdf()], "SYNTHETIC.pdf", "application/pdf", db.get(User, f["actor_id"]), config)
        db.add(file); db.flush(); version.source_file_id = file.id
        run = db.scalar(select(ParseRun).where(ParseRun.document_version_id == version.id))
        block = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == run.id))
        run_id, block_id = run.id, block.id
        db.commit()
    base = f'/documents/{f["doc_id"]}/versions/{f["version_id"]}'
    endpoint, method, body = {
        "patch": (base, "PATCH", {"revision_label": "SYNTHETIC stale writer"}),
        "parse": (base + "/parse-runs", "POST", None),
        "correction": (f"/parse-runs/{run_id}/corrections", "POST", {"blocks": []}),
        "review": (f"/parse-runs/{run_id}/blocks/{block_id}/review", "POST", {"review_status": "approved", "reason": "SYNTHETIC checked"}),
    }[action]
    entered, release = threading.Event(), threading.Event()
    pids = []
    original_receipt = creation.app_module.idempotency_record
    def pause(db):
        pids.append(db.scalar(text("SELECT pg_backend_pid()")))
        entered.set()
        assert release.wait(20)
    def receipt(db, actor, request, key, payload):
        result = original_receipt(db, actor, request, key, payload)
        # The original review red paused after its last domain manage check.
        # That point now holds the parent lock: let DELETE win before the new
        # shared fence instead of creating an artificial test-only lock cycle.
        if request.url.path == endpoint: pause(db)
        return result
    monkeypatch.setattr(creation.app_module, "idempotency_record", receipt)
    with creation._clients(config, f) as (clients, headers, apps):
        with Session(engine) as db:
            before = {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in (ParseRun, ApiIdempotencyRecord)}
            initial_block = db.get(ContentBlock, block_id).revision
            initial_version = db.get(DocumentVersion, f["version_id"]).revision
        def send():
            return clients[0].request(method, endpoint, json=body, headers={**headers[0], "Idempotency-Key": str(uuid4()), "If-Match": '"0"'})
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(send)
            try:
                assert entered.wait(20)
                deleted = clients[1].delete(f'/documents/{f["doc_id"]}', headers={**headers[1], "Idempotency-Key": str(uuid4()), "If-Match": f'"{f["revision"]}"'})
                assert deleted.status_code == 204, deleted.text
                with Session(engine) as check:
                    assert check.get(Document, f["doc_id"]).deleted_at is not None
            finally:
                release.set()
            response = pending.result(timeout=20)
        with Session(engine) as db:
            after = {m.__tablename__: db.scalar(select(func.count()).select_from(m)) for m in (ParseRun, ApiIdempotencyRecord)}
            rows = {"maintenance_status": response.status_code, "delete_status": deleted.status_code, "before": before, "after": after,
                "block_revision": db.get(ContentBlock, block_id).revision, "version_revision": db.get(DocumentVersion, f["version_id"]).revision,
                "revision_label": db.get(DocumentVersion, f["version_id"]).revision_label, "maintenance_pids": pids}
        (report / "maintenance-delete.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        assert response.status_code in {403, 404}, response.text
        assert after["parse_runs"] == before["parse_runs"]
        assert after["api_idempotency_records"] == before["api_idempotency_records"] + 1  # DELETE only
        assert rows["block_revision"] == initial_block and rows["version_revision"] == initial_version
        assert rows["revision_label"] is None


@pytest.mark.parametrize("action", ["patch", "review"])
def test_legacy_same_etag_has_one_winner(legacy_pg, monkeypatch, action):
    engine, config, f, report = legacy_pg
    with Session(engine) as db:
        run = db.scalar(select(ParseRun).where(ParseRun.document_version_id == f["version_id"]))
        block = db.scalar(select(ContentBlock).where(ContentBlock.parse_run_id == run.id))
        block_id = block.id
        endpoint = (f'/documents/{f["doc_id"]}/versions/{f["version_id"]}' if action == "patch"
            else f'/parse-runs/{run.id}/blocks/{block.id}/review')
    barrier = threading.Barrier(2, timeout=20)
    original = creation.app_module.idempotency_record
    def meet(db, actor, request, key, body):
        result = original(db, actor, request, key, body)
        if request.url.path == endpoint: barrier.wait()
        return result
    monkeypatch.setattr(creation.app_module, "idempotency_record", meet)
    with creation._clients(config, f) as (clients, headers, apps):
        def send(index):
            body = ({"revision_label": "SYNTHETIC writer " + str(index)} if action == "patch" else
                {"review_status": "approved", "reason": "SYNTHETIC writer " + str(index)})
            return clients[index].request("PATCH" if action == "patch" else "POST", endpoint, json=body,
                headers={**headers[index], "Idempotency-Key": str(uuid4()), "If-Match": '"0"'})
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, (0, 1)))
    with Session(engine) as db:
        version = db.get(DocumentVersion, f["version_id"])
        block = db.get(ContentBlock, block_id)
        receipts = db.scalar(select(func.count()).select_from(ApiIdempotencyRecord).where(ApiIdempotencyRecord.request_path == endpoint))
        row = {"statuses": [r.status_code for r in results], "version_revision": version.revision,
            "block_revision": block.revision, "completed_receipts": receipts}
    (report / "same-etag.json").write_text(json.dumps(row, indent=2), encoding="utf-8")
    assert sorted(row["statuses"]) == [200, 412]
    assert row["version_revision"] == row["completed_receipts"] == 1
    if action == "review": assert row["block_revision"] == 1
