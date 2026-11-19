"""Opt-in single-worker replay against a larger synthetic PDF library."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
from threading import Event
import time
from types import SimpleNamespace
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from sqlalchemy import func, select

from . import governance
from .execution_models import QueryAnswerSnapshot
from .models import AuditEvent, Chunk, ChunkSet, ContentBlock, DocumentPage, DocumentVersion, IndexGeneration, ParseRun, User
from .runtime_models import ModelCallAdmission, TaskBudget
from .test_child_answer_pipeline import (MODEL, PRESSURE, QUERY, STEP_ONE, STEP_TWO, WARNING, _publish_source,
                                        child_answer_pipeline, recovery_pg)
from .test_execution import _headers
from .test_query_capacity import _enable_real_bge
from .testing_governance import seed_confirmation
from .vector_store import ChunkEmbedding
from .worker import run_one


pytestmark = pytest.mark.skipif(
    not os.environ.get("DATACENTER_TEST_POSTGRES_URL") or os.environ.get("DATACENTER_RUN_CAPACITY") != "1",
    reason="Explicit isolated local PostgreSQL and synthetic capacity opt-in required")

APPENDIX_MODEL = "SYNTHETIC-APPENDIX-2"
QUEUE_LIMIT = 4


def _appendix():
    import pymupdf

    pages, markdown = [], []
    with pymupdf.open() as pdf:
        for index, subject in enumerate(("Ceramic", "Linen", "Maple", "Amber", "Marble", "Canvas", "Cedar", "Slate")):
            page = pdf.new_page(width=595, height=842)
            lines = [f"SYNTHETIC archive catalog for {subject}", f"A violet label identifies the {subject} display.",
                     f"The {subject} sample occupies compartment {index + 1}.",
                     f"The inventory describes the {subject} grouping in the archive."]
            blocks = []
            for ordinal, literal in enumerate(lines):
                y = 84 + 39 * ordinal
                page.insert_text((67, y), literal)
                heading = {"type": "paragraph_title", "level": 1} if ordinal == 0 else {"type": "paragraph"}
                blocks.append({**heading, "index": ordinal, "bbox": [.10, (y - 16) / 842, .91, (y + 8) / 842],
                               "content": [{"type": "text", "content": literal}]})
            pages.append({"page_idx": index, "blocks": blocks})
            markdown.extend(lines)
        original = pdf.tobytes()
    middle = {"schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": True,
              "metadata": {"file_suffix": "pdf", "producer": {"name": "MinerU", "version": "4.2.3"}},
              "extensions": {"mineru": {"tier": "standard", "parse_mode": "txt"}, "docvortex_layout": {
                  "version": 1, "pages": [{"page_idx": n, "width_pt": 595, "height_pt": 842} for n in range(len(pages))]}},
              "pages": pages}
    archive = BytesIO()
    with ZipFile(archive, "w") as bundle:
        bundle.writestr("middle_json.json", json.dumps(middle))
        bundle.writestr("structured_content.json", json.dumps({"pages": pages}))
        bundle.writestr("markdown.md", "\n".join(markdown))
    return original, archive.getvalue()


def _publish_appendix(f):
    original, bundle = _appendix()
    metadata = {"title": "SYNTHETIC eight-page appendix", "category": "manual", "model_scope": [APPENDIX_MODEL],
                "revision_label": "SYNTHETIC Scale R1", "source_metadata": {"channel": "synthetic",
                    "source_description": "Generated capacity-only PDF and matching Standard contract fixture"}}
    uploaded = f.client.post(f"/knowledge-bases/{f.kb_id}/documents", headers=_headers(),
                             files={"file": ("synthetic-scale-appendix.pdf", original, "application/pdf")},
                             data={"metadata": json.dumps(metadata)})
    assert uploaded.status_code == 201, uploaded.text
    document_id, version_id = uploaded.json()["document"]["id"], uploaded.json()["version"]["id"]
    with f.app.state.session_factory() as db:
        actor, version = db.get(User, f.actor_id), db.get(DocumentVersion, version_id)
        seed_confirmation(db, actor, version, "classification", "classified", {**governance.source_binding(db, version),
            "access_level": "internal", "category": "manual", "need_class": "maintenance", "rule_version": "SYNTHETIC scale"})
        db.commit()
    source = SimpleNamespace(app=f.app, config=f.config, actor_id=f.actor_id, version_id=version_id, client=f.client,
                             path=f"/documents/{document_id}/versions/{version_id}/parse-runs")
    _publish_source(source, bundle)
    return version_id, sha256(original).hexdigest()


def _source_inventory(f):
    with f.app.state.session_factory() as db:
        generation = db.get(IndexGeneration, f.generation_id)
        manifest = generation.build_report["manifest"]
        assert generation.status == "ready" and generation.build_report["integrity"]["status"] == "passed"
        sources = []
        for ref in manifest["sources"]:
            run = db.get(ParseRun, ref["run_id"])
            chunk_set = db.get(ChunkSet, ref["selected_chunk_set_id"])
            version = db.get(DocumentVersion, ref["version_id"])
            source = {"version": version.id, "models": version.model_scope, "published": version.publish_status,
                      "source_qc": run.stage_checkpoint["source_qc"]["status"], "chunk_qc": chunk_set.stage_checkpoint["quality"]["status"],
                      "pages": db.scalar(select(func.count()).select_from(DocumentPage).where(DocumentPage.parse_run_id == run.id)),
                      "leaves": db.scalar(select(func.count()).select_from(ContentBlock).where(
                          ContentBlock.parse_run_id == run.id, ContentBlock.structure_role == "leaf")),
                      "chunks": db.scalar(select(func.count()).select_from(Chunk).where(Chunk.chunk_set_id == chunk_set.id)),
                      "indexed_members": sum(row["version_id"] == version.id for row in manifest["members"])}
            assert source["published"] == "effective" and source["source_qc"] == source["chunk_qc"] == "passed"
            sources.append(source)
        vector_count = db.scalar(select(func.count()).select_from(ChunkEmbedding).where(ChunkEmbedding.generation_id == generation.id))
        assert vector_count == len(manifest["members"]) == generation.expected_chunk_count
        return {"generation_id": generation.id, "sources": sources, "members": len(manifest["members"]),
                "batches": len(manifest["batches"]), "pgvector_rows": vector_count}


def _poll(f, client_args, task_id, row, stop):
    with httpx.Client(**client_args) as client:
        while not stop.is_set():
            begun = time.perf_counter()
            progress = client.get(f"/tasks/{task_id}/status")
            row["progress_seconds"].append(time.perf_counter() - begun)
            progress.raise_for_status()
            state = progress.json()["status"]
            row["poll_states"].add(state)
            if state in {"pending", "running"}:
                assert set(progress.json()) <= {"id", "status", "revision"}
                stop.wait(1)
                continue
            if state != "succeeded":
                return
            begun = time.perf_counter()
            result = client.get(f"/tasks/{task_id}/result")
            evidence = client.get(f"/tasks/{task_id}/evidence")
            original = client.get(f"/documents/{f.document_id}/versions/{f.version_id}/file")
            row["read_seconds"] = time.perf_counter() - begun
            for reply in (result, evidence, original):
                reply.raise_for_status()
            data, standalone = result.json(), evidence.json()["items"]
            row["formal"] = data["result_status"] == "formal"
            row["evidence_count"] = len(standalone)
            row["original_sha256"] = sha256(original.content).hexdigest()
            assert row["formal"] and row["evidence_count"] == 6
            assert {item["id"] for item in standalone} == {item["id"] for item in data["evidence"]}
            assert all(item["document_version_id"] == f.version_id and item["locator"]["file_page"] == 1 and
                       item["locator"]["source_sha256"] == row["original_sha256"] for item in standalone)
            assert row["original_sha256"] == sha256(f.original).hexdigest()
            assert all(literal in data["answer"] for literal in (STEP_ONE, STEP_TWO, WARNING, PRESSURE))
            assert data["answer"].index(WARNING) < data["answer"].index(STEP_TWO)
            return


def _attach_receipts(f, tasks, target_members):
    with f.app.state.session_factory() as db:
        for task_id, row in tasks.items():
            calls = list(db.scalars(select(ModelCallAdmission.state).where(ModelCallAdmission.task_id == task_id)))
            budget = db.get(TaskBudget, task_id)
            snapshots = db.scalar(select(func.count()).select_from(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task_id))
            vector = list(db.scalars(select(AuditEvent.metadata_json).where(AuditEvent.resource_id == task_id,
                AuditEvent.action == "query.vector_retrieved", AuditEvent.actor_user_id == f.actor_id)))
            row.update(calls=calls, reserved_calls=budget.reserved_calls if budget else None, snapshots=snapshots, vector_audit=vector)
            assert calls == ["received"] * 3 and budget.reserved_calls == 3 and snapshots == 1
            assert len(vector) == 1 and vector[0]["method"] == "pgvector_cosine"
            assert vector[0]["generation_ids"] == [f.generation_id] and vector[0]["service_revision_id"] == f.revision_id
            assert vector[0]["ranked_candidate_count"] == target_members and vector[0]["evidence_count"] == 6
            assert len(vector[0]["vector_sha256"]) == 64


def test_real_bge_scaled_library_with_four_queued_queries(child_answer_pipeline):
    f = child_answer_pipeline
    root = Path(os.environ["DATACENTER_CAPACITY_OUTPUT"])
    root.mkdir(parents=True, exist_ok=True)
    report, tasks, stop = {"scope": "Synthetic Standard contract, published distinct-model appendix, real offline BGE and PGvector, installed Pi SDK with loopback generation",
                          "workers": 1, "queue_limit": QUEUE_LIMIT}, {}, Event()
    try:
        appendix, original_sha = _publish_appendix(f)
        report["appendix"] = {"version": appendix, "original_sha256": original_sha, "model": APPENDIX_MODEL}
        report["embedding_setup"] = _enable_real_bge(f)
        inventory = report["inventory"] = _source_inventory(f)
        assert len(inventory["sources"]) == 2 and sum(row["pages"] for row in inventory["sources"]) == 10
        appendix_source = next(row for row in inventory["sources"] if row["version"] == appendix)
        assert appendix_source["pages"] == 8 and appendix_source["leaves"] >= 32 and appendix_source["indexed_members"] >= 8
        target = next(row for row in inventory["sources"] if row["version"] == f.version_id)
        f.config = replace(f.config, query_max_inflight=QUEUE_LIMIT)
        f.app.state.settings = f.config
        f.app.state.session_factory.configure(info={"settings": f.config})
        client_args = {"base_url": f.gateway, "cookies": dict(f.client.cookies), "trust_env": False, "timeout": 35,
                       "headers": {"X-CSRF-Token": f.client.headers["X-CSRF-Token"]}}
        payload = {"query": QUERY, "model": MODEL, "knowledge_base_ids": [f.kb_id]}
        wave_start = time.perf_counter()
        with httpx.Client(**client_args) as client:
            for _ in range(QUEUE_LIMIT):
                begun = time.perf_counter()
                accepted = client.post("/knowledge/queries", json=payload, headers=_headers(key=str(uuid4())))
                assert accepted.status_code == 202, accepted.text
                now = time.perf_counter()
                tasks[accepted.json()["task_id"]] = {"accepted_at": now - wave_start, "accept_seconds": now - begun,
                                                   "progress_seconds": [], "poll_states": set()}
            denied = client.post("/knowledge/queries", json=payload, headers=_headers(key=str(uuid4())))
            report["overflow"] = {"http": denied.status_code, "code": denied.json().get("code")}
            assert report["overflow"] == {"http": 429, "code": "QUERY_CAPACITY_FULL"}
            for task_id in tasks:
                progress = client.get(f"/tasks/{task_id}/status")
                assert progress.status_code == 200 and progress.json()["status"] == "pending"
                assert set(progress.json()) == {"id", "status", "revision"}
        offset = len(f.gateway_trace)
        with ThreadPoolExecutor(max_workers=QUEUE_LIMIT) as readers:
            futures = [readers.submit(_poll, f, client_args, task_id, row, stop) for task_id, row in tasks.items()]
            try:
                for task_id, row in tasks.items():
                    begun = time.perf_counter()
                    outcome = run_one(f.config, gateway_url=f.gateway)
                    end = time.perf_counter()
                    row.update(outcome=outcome, queue_seconds=begun - wave_start - row["accepted_at"],
                               worker_seconds=end - begun, accepted_to_worker_finish_seconds=end - wave_start - row["accepted_at"])
                    assert outcome["task_id"] == task_id, outcome
                for future in futures:
                    future.result(timeout=45)
            finally:
                stop.set()
                report["gateway"] = f.gateway_trace[offset:]
                report["wall_seconds"] = time.perf_counter() - wave_start
        _attach_receipts(f, tasks, target["indexed_members"])
        assert all(row.get("formal") and row["outcome"]["state"] == "succeeded" and
                   {"running", "succeeded"} <= row["poll_states"] for row in tasks.values())
        assert run_one(f.config, gateway_url=f.gateway) == {"state": "idle"}
        report["passed"] = True
    finally:
        report["tasks"] = {identifier: {**row, "poll_states": sorted(row["poll_states"])} for identifier, row in tasks.items()}
        (root / "scale.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
