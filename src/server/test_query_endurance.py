"""Opt-in local endurance run over an isolated, entirely synthetic PDF library."""

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import cProfile
from dataclasses import replace
from datetime import datetime, timedelta
from hashlib import sha256
from io import BytesIO
import json
import os
from pathlib import Path
import pstats
import re
import shutil
from threading import Event, Lock, Thread
import time
from types import SimpleNamespace
from uuid import uuid4
from weakref import WeakSet
from zipfile import ZIP_DEFLATED, ZipFile

import httpx
import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from . import governance, indexing, local_bge
from .embedding_jobs import ChildEmbeddingBatch, accept_child_embedding_batch, child_batch_payload, run_child_embedding_batch
from .execution_models import QueryAnswerSnapshot
from .models import (AuditEvent, Chunk, ContentBlock, Document, DocumentPage, DocumentVersion, FileObject,
                     IndexGeneration, IndexSource, KnowledgeBase, ParseRun, QueryTask, User, utc_now)
from .parse_jobs import run_local_parse_job
from .parsing.ir_projection import normalize_parse_run
from .parsing.local_pipeline import ParseContractError, source_preflight
from .parsing.processing import run_source_qc, source_confirmation_scope, source_qc_is_current, source_verification, structure_parse_run
from .runtime_models import ModelCallAdmission, TaskBudget
from .service_models import ServiceRevision
from .service_use_models import ServiceUseCall, ServiceUsePin
from .services import file_path
from .test_child_answer_pipeline import LINES, MODEL, PRESSURE, QUERY, STEP_ONE, STEP_TWO, WARNING, _publish_source
from .test_child_answer_pipeline import child_answer_pipeline, recovery_pg  # pytest fixtures
from .test_child_index import _build, _switch
from .test_chunk_processing import build, publish, review_candidate
from .test_execution import _headers
from .test_local_parse_pipeline import accept
from .test_query_capacity import _enable_real_bge, test_query_proof_profile as _profile_query_proof
from .test_query_capacity_scale import QUEUE_LIMIT, _source_inventory
from .testing_governance import seed_confirmation
from .testing_service_use import attest_synthetic_input
from .vector_store import ChunkEmbedding
from .worker import run_one


SCALE_BOUNDARY = os.environ.get("DATACENTER_ENDURANCE_SCALE_BOUNDARY") == "1"
pytestmark = pytest.mark.skipif(
    not os.environ.get("DATACENTER_TEST_POSTGRES_URL") or not (SCALE_BOUNDARY or os.environ.get("DATACENTER_RUN_ENDURANCE") == "1"),
    reason="Explicit local PostgreSQL and synthetic endurance opt-in required",
)

CROSS_MODEL = "SYNTHETIC-CROSSCHECK"
STEPS, LIMITS = LINES[:4], LINES[4:]
SUBJECTS = ("Ceramic", "Linen", "Maple", "Amber", "Marble", "Canvas", "Cedar", "Slate")
PREFLIGHT_PAGE_LIMIT = 600
DOMAIN_REVIEW = "production_per_leaf_review_with_synthetic_confirmation"
BULK_REVIEW = "synthetic_bulk_leaf_review_with_production_confirmation_and_publication"


class EnduranceLimit(RuntimeError):
    pass


class Journal:
    def __init__(self, directory):
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=False)
        self.started = time.monotonic()
        self.lock = Lock()

    def append(self, event, **record):
        line = json.dumps({"event": event, "elapsed_seconds": round(time.monotonic() - self.started, 3), **record},
                          ensure_ascii=False)
        with self.lock, (self.directory / "events.ndjson").open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def checkpoint(self, report):
        temporary = self.directory / "checkpoint.tmp"
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(self.directory / "checkpoint.json")


def _manual_plan(stages, page_count, inventory):
    adjustment = None
    if inventory:
        # Transfer only the anonymous page-count distribution into any fixture or report.
        counts = sorted(row["pages"] for row in json.loads(inventory.read_text(encoding="utf-8-sig"))["files"])
        assert len(counts) == 89 and all(type(count) is int and count > 0 for count in counts)
        for special in (1, 2, 3):
            counts.remove(special)
        cross_pages = (1, 3)
        excessive = max(counts)
        assert excessive > PREFLIGHT_PAGE_LIMIT and max(stages) == 89
        move = excessive - PREFLIGHT_PAGE_LIMIT
        assert counts[-2] <= PREFLIGHT_PAGE_LIMIT and counts[0] + move <= PREFLIGHT_PAGE_LIMIT
        adjustment = {"outside_preflight_limit": excessive, "largest_planned_pdf": PREFLIGHT_PAGE_LIMIT,
                      "redistributed_pages": move, "recipient_original_pages": counts[0],
                      "recipient_synthetic_pages": counts[0] + move}
        counts[-1] = PREFLIGHT_PAGE_LIMIT
        counts[0] += move
    else:
        counts = [page_count] * max(stages)
        cross_pages = (2, 2)
    # Keep the original proof intact, with a distinct two-source proof from the second scale step.
    cross_slot = next((stage for stage in stages if stage >= 4), None)
    plan = [(2, MODEL, LINES)]
    fill_pages = iter(counts)
    for ordinal in range(2, max(stages) + 1):
        if cross_slot is not None and ordinal in (cross_slot - 1, cross_slot):
            index = ordinal - (cross_slot - 1)
            plan.append((cross_pages[index], CROSS_MODEL, (STEPS, LIMITS)[index]))
        else:
            plan.append((next(fill_pages), f"SYNTHETIC-APPENDIX-{ordinal:03}", None))
    return plan, adjustment


def _synthetic_pdf(ordinal, page_count, blocks_per_page, proof):
    import pymupdf

    pages, markdown = [], []
    with pymupdf.open() as pdf:
        for page_index in range(page_count):
            page = pdf.new_page(width=595, height=842)
            subject = SUBJECTS[(ordinal + page_index) % len(SUBJECTS)]
            if proof is None:
                label = f"SYNTHETIC {ordinal:03}-{page_index + 1:03} {subject}"
                entries = [("paragraph_title", label)]
                entries.extend(("paragraph", f"The {subject} catalog item {ordinal:03}-{page_index + 1:03} uses bin {n}.")
                               for n in range(1, blocks_per_page))
            else:
                entries = proof if page_index == 0 else ()
            blocks = []
            for index, (kind, literal) in enumerate(entries):
                y = 66 + 33 * index
                page.insert_text((67, y), literal)
                heading_level = {"level": 1} if kind == "paragraph_title" else {}
                blocks.append({"type": kind, **heading_level, "index": index, "bbox": [.10, (y - 16) / 842, .91, (y + 8) / 842],
                               "content": [{"type": "text", "content": literal}]})
                markdown.append(literal)
            pages.append({"page_idx": page_index, "blocks": blocks})
        original = pdf.tobytes(garbage=4, deflate=True)
    middle = {"schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": True,
              "metadata": {"file_suffix": "pdf", "producer": {"name": "MinerU", "version": "4.2.3"}},
              "extensions": {"mineru": {"tier": "standard", "parse_mode": "txt"}, "docvortex_layout": {
                  "version": 1, "pages": [{"page_idx": index, "width_pt": 595, "height_pt": 842} for index in range(page_count)]}},
              "pages": pages}
    archive = BytesIO()
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as bundle:
        bundle.writestr("middle_json.json", json.dumps(middle))
        bundle.writestr("structured_content.json", json.dumps({"pages": pages}))
        bundle.writestr("markdown.md", "\n".join(markdown))
    return original, archive.getvalue()


def _synthetic_page_facts(original, archive):
    """Cross-check every fixture fact against independently extracted original text."""
    import pymupdf

    with ZipFile(BytesIO(archive)) as bundle:
        pages = json.loads(bundle.read("middle_json.json"))["pages"]
        assert json.loads(bundle.read("structured_content.json")) == {"pages": pages}
        markdown = bundle.read("markdown.md").decode().splitlines()
    facts = []
    with pymupdf.open(stream=original, filetype="pdf") as pdf:
        assert len(pdf) == len(pages)
        for page_index, (page, metadata) in enumerate(zip(pdf, pages, strict=True)):
            assert metadata["page_idx"] == page_index
            blocks = metadata["blocks"]
            assert blocks and blocks[0]["type"] == "paragraph_title"
            expected = []
            for block_index, block in enumerate(blocks):
                kind = "title" if block_index == 0 else "paragraph"
                assert block["type"] == ("paragraph_title" if kind == "title" else kind)
                assert block["index"] == block_index
                literal, = block["content"]
                assert literal["type"] == "text" and literal["content"].strip()
                expected.append(literal["content"])
                facts.append((page_index + 1, kind, literal["content"]))
            assert expected[0].startswith("SYNTHETIC ")
            assert page.get_text("text").strip().splitlines() == expected
    assert markdown == [literal for _, _, literal in facts]
    return facts


def _publish_bulk_synthetic_source(f, original, archive):
    facts = _synthetic_page_facts(original, archive)
    digest = sha256(original).hexdigest()
    run_id = accept(f)

    def recorded_local_output(source, workspace, executable, home, heartbeat):
        result = workspace / "result.zip"
        result.write_bytes(archive)
        return result

    with f.app.state.session_factory() as db:
        parsed = run_local_parse_job(db, run_id, f.config, runner=recorded_local_output)
        assert parsed.stage_checkpoint["stage_state"] == "parsed", parsed.error_code
        normalized = normalize_parse_run(db, run_id, f.config)
        assert normalized.stage_checkpoint["stage_state"] == "normalized", normalized.error_code
        structured = structure_parse_run(db, run_id, f.config)
        assert structured.stage_checkpoint["stage_state"] == "structured", structured.error_code
        checked = run_source_qc(db, run_id, f.config)
        assert checked.stage_checkpoint["source_qc"]["status"] == "passed", checked.stage_checkpoint["source_qc"]["issues"]
    set_id = build(f, run_id)

    with f.app.state.session_factory() as db:
        run, version, actor = db.get(ParseRun, run_id), db.get(DocumentVersion, f.version_id), db.get(User, f.actor_id)
        document = db.get(Document, version.document_id)
        governance.manage(db, actor, document)
        assert document.title.startswith("SYNTHETIC endurance manual ") and version.source_metadata["channel"] == "synthetic"
        assert version.publish_status == "draft" and version.selected_chunk_set_id is None
        assert run.document_version_id == version.id and not governance.parse_run_was_published(db, run)
        source_file = db.get(FileObject, version.source_file_id)
        assert source_file.sha256 == digest and file_path(source_file, f.config).read_bytes() == original
        assert run.ir_schema_version and run.ir_artifact_id and source_qc_is_current(db, run)
        leaves = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id, ContentBlock.structure_role == "leaf")
                            .order_by(ContentBlock.reading_order)).all()
        assert [(row.file_page, row.kind, row.text_content) for row in leaves] == facts
        qc = run.stage_checkpoint["source_qc"]
        assert set(qc["required_leaf_refs"]) == {row.id for row in leaves} and not qc["observation_refs"]
        for row in leaves:
            locator = row.locator
            assert locator["block_id"] == row.id and locator["parse_run_id"] == run.id
            assert locator["document_version_id"] == version.id and locator["source_file_id"] == source_file.id
            assert locator["source_sha256"] == digest
            assert locator["regions"] and all(region["file_page"] == row.file_page and region["geometry_status"] == "exact"
                                               for region in locator["regions"])
            assert row.review_status == "unreviewed" and not row.structured_content.get("pending_graphic_keys")
        # Simulate only the human review inputs for these generated text leaves.
        # Proof registration, review candidate, publication and index still use their normal gates.
        for row in leaves:
            row.review_status = "approved"
            row.review_reason = "SYNTHETIC bulk independent comparison against generated original PDF and archive"
            row.revision += 1
        version.revision += len(leaves)
        db.flush()
        scope = {**governance.source_binding(db, version), **source_confirmation_scope(db, run)}
        professional = seed_confirmation(db, actor, version, "key_content", "accepted", scope)
        assert source_verification(db, run)["state"] == "verified"
        professional_id = professional.id
        db.commit()
    review = review_candidate(f, run_id, set_id, professional_id)
    publish(f, set_id, review)


def _publish_manual(f, ordinal, plan, *, full_domain_review):
    pages, model, proof = plan
    original, archive = _synthetic_pdf(ordinal, pages, f.blocks_per_page, proof)
    metadata = {"title": f"SYNTHETIC endurance manual {ordinal:03}", "category": "manual", "model_scope": [model],
                "revision_label": "SYNTHETIC Endurance R1", "source_metadata": {"channel": "synthetic",
                "source_description": "Generated PDF and matching local Standard contract fixture"}}
    response = f.client.post(f"/knowledge-bases/{f.kb_id}/documents", headers=_headers(),
                             files={"file": (f"synthetic-manual-{ordinal:03}.pdf", original, "application/pdf")},
                             data={"metadata": json.dumps(metadata)})
    assert response.status_code == 201, response.text
    document_id, version_id = response.json()["document"]["id"], response.json()["version"]["id"]
    with f.app.state.session_factory() as db:
        actor, version = db.get(User, f.actor_id), db.get(DocumentVersion, version_id)
        seed_confirmation(db, actor, version, "classification", "classified", {**governance.source_binding(db, version),
            "access_level": "internal", "category": "manual", "need_class": "maintenance", "rule_version": "SYNTHETIC endurance"})
        db.commit()
    source = SimpleNamespace(app=f.app, config=f.config, actor_id=f.actor_id, version_id=version_id, client=f.client,
                             path=f"/documents/{document_id}/versions/{version_id}/parse-runs")
    bulk_review = pages > 8 and not full_domain_review
    if bulk_review:
        assert proof is None
        _publish_bulk_synthetic_source(source, original, archive)
    else:
        _publish_source(source, archive)
    return {"document_id": document_id, "version_id": version_id, "pages": pages, "model": model,
            "expected_leaves": len(proof) if proof else pages * f.blocks_per_page,
            "original_sha256": sha256(original).hexdigest(), "original_bytes": len(original),
            "archive_bytes": len(archive), "quotes": [literal for _, literal in proof or ()],
            "review_setup_method": BULK_REVIEW if bulk_review else DOMAIN_REVIEW}


def _oversized_pdf_preflight(f, journal, page_count):
    def document_count():
        with f.app.state.session_factory() as db:
            return db.scalar(select(func.count()).select_from(Document).where(Document.knowledge_base_id == f.kb_id))

    before = document_count()
    original, _ = _synthetic_pdf(0, page_count, 2, None)
    path = journal.directory / "synthetic-oversized-preflight.pdf"
    path.write_bytes(original)
    digest = sha256(original).hexdigest()
    with pytest.raises(ParseContractError) as error:
        source_preflight(path, digest, len(original))
    after = document_count()
    observed = {"pages": page_count, "error_code": error.value.code, "documents_before": before, "documents_after": after,
                "original_sha256": digest}
    journal.append("oversized_pdf_preflight", **observed)
    assert error.value.code == "PDF_PREFLIGHT_FAILED" and before == after
    return observed


def _observe_duration(monkeypatch, owner, name):
    original, elapsed = getattr(owner, name), []

    def timed(*args, **kwargs):
        begun = time.monotonic()
        try:
            return original(*args, **kwargs)
        finally:
            elapsed.append(time.monotonic() - begun)

    monkeypatch.setattr(owner, name, timed)
    return elapsed


def _source_text_sizes(f, generation_id=None):
    generation_id = generation_id or f.generation_id
    with f.app.state.session_factory() as db:
        leaf_bytes = db.scalar(select(func.coalesce(func.sum(func.octet_length(ContentBlock.text_content)), 0))
            .join(IndexSource, IndexSource.parse_run_id == ContentBlock.parse_run_id)
            .where(IndexSource.index_generation_id == generation_id, ContentBlock.structure_role == "leaf"))
        chunk_bytes = db.scalar(select(func.coalesce(func.sum(func.octet_length(Chunk.content)), 0))
            .join(IndexSource, IndexSource.chunk_set_id == Chunk.chunk_set_id)
            .where(IndexSource.index_generation_id == generation_id))
    return {"source_leaf_text_utf8_bytes": leaf_bytes, "chunk_text_utf8_bytes": chunk_bytes}


def _build_generation(f, journal, stage, check_setup, manifest_times, encode_times):
    began = time.monotonic()
    manifest_offset, encode_offset = len(manifest_times), len(encode_times)
    timings = {"batch_seconds": 0.0}
    with f.app.state.session_factory() as db:
        generation = _build(db, f)
        assert generation.build_report["integrity"]["status"] == "pending", generation.build_report["integrity"]
        generation_id, manifest = generation.id, generation.build_report["manifest"]
        db.commit()
        timings["manifest_freeze_seconds"] = time.monotonic() - began
        journal.append("index_building", stage=stage, generation_id=generation_id,
                       batches=len(manifest["batches"]), members=len(manifest["members"]), **timings)
        for batch in manifest["batches"]:
            check_setup()
            started = time.monotonic()
            batch_manifest_offset, batch_encode_offset = len(manifest_times), len(encode_times)
            number = batch["batch_no"]
            _, _, payload = child_batch_payload(db, manifest, number)
            attest_synthetic_input(db, f.actor_id, "document.embed", f"index:{generation_id}:{number}", payload)
            db.flush()
            job_id = accept_child_embedding_batch(db, generation_id, number, db.get(User, f.actor_id),
                                                  f.app.state.service_endpoints).id
            db.commit()
            admitted = time.monotonic()
            job = run_child_embedding_batch(db, job_id, f.app.state.service_endpoints)
            elapsed = time.monotonic() - started
            timings["batch_seconds"] += elapsed
            journal.append("index_batch", stage=stage, generation_id=generation_id, batch=number, state=job.state,
                           error_code=job.error_code, seconds=elapsed, admission_seconds=admitted - started,
                           library_proof_seconds=sum(manifest_times[batch_manifest_offset:]),
                           bge_encode_seconds=sum(encode_times[batch_encode_offset:]))
            assert job.state == "succeeded", job.error_code
        check_setup()
        switching = time.monotonic()
        _switch(db, f, generation_id, f.generation_id)
        db.commit()
        assert db.get(KnowledgeBase, f.kb_id).active_generation_id == generation_id
        timings["switch_seconds"] = time.monotonic() - switching
    f.generation_id = generation_id
    return {**timings, "wall_seconds": time.monotonic() - began,
            "library_proof_seconds": sum(manifest_times[manifest_offset:]), "bge_encode_seconds": sum(encode_times[encode_offset:])}


@contextmanager
def _monitor(f, journal, phase, interval):
    import psutil

    stop = Event()
    process = psutil.Process()
    engine = f.app.state.engine

    def observe():
        while not stop.is_set():
            children = process.children(recursive=True)
            rss, handles = 0, 0
            for member in [process, *children]:
                try:
                    rss += member.memory_info().rss
                    handles += member.num_handles() if hasattr(member, "num_handles") else member.num_fds()
                except psutil.Error:
                    pass
            sample = {**phase, "process_tree_rss_bytes": rss, "process_tree_handles_or_fds": handles,
                      "child_processes": len(children), "test_threads": process.num_threads(),
                      "available_bytes": psutil.virtual_memory().available,
                      "free_output_bytes": shutil.disk_usage(journal.directory).free,
                      "api_pool_checked_out_before_sampling": engine.pool.checkedout()}
            # The profiler counts statements on the app engine. Keep its
            # measurement free of the sampler's own pg_stat_activity queries.
            if sample["phase"] != "proof":
                try:
                    with engine.connect() as db:
                        pg = db.execute(text("SELECT count(*), count(*) FILTER (WHERE state = 'active'), "
                                             "count(*) FILTER (WHERE state = 'idle in transaction') FROM pg_stat_activity "
                                             "WHERE datname = current_database() AND pid <> pg_backend_pid()")).one()
                    sample.update(zip(("pg_connections", "pg_active", "pg_idle_in_transaction"), pg))
                except SQLAlchemyError as error:
                    sample["pg_sampling_error"] = type(error).__name__
            journal.append("resource", **sample)
            stop.wait(interval)

    watcher = Thread(target=observe, name="synthetic-endurance-sampler", daemon=True)
    watcher.start()
    try:
        yield
    finally:
        stop.set()
        watcher.join(10)
        assert not watcher.is_alive(), "The resource sampler did not release its database connection"


def _http_args(f, client):
    return {"base_url": f.gateway, "cookies": dict(client.cookies), "trust_env": False, "timeout": 55,
            "headers": {"X-CSRF-Token": client.headers["X-CSRF-Token"]}}


def _proof_only_stage(f, journal, stage, monkeypatch):
    relative = f"proof-{stage:02d}-documents"
    directory = journal.directory / relative
    journal.append("proof_started", stage=stage, directory=relative)
    begun = time.monotonic()
    with monkeypatch.context() as environment:
        environment.setenv("DATACENTER_CAPACITY_OUTPUT", str(directory))
        _profile_query_proof(f)
    proof = json.loads((directory / "proof.json").read_text(encoding="utf-8"))
    profile = pstats.Stats(str(directory / "proof.prof"))
    proof.update(profile_directory=relative, total_wall_seconds=time.monotonic() - begun,
                 distinct_functions=len(profile.stats), function_calls=profile.total_calls,
                 primitive_function_calls=profile.prim_calls)
    assert proof["source_members"] == 6 and proof["sql_statements"] > 0 and proof["function_calls"] > 0
    (directory / "proof.json").write_text(json.dumps(proof, indent=2), encoding="utf-8")
    journal.append("proof_verified", stage=stage, **proof)
    return proof


def _read_exact_answer(client, task_id, sources):
    begun = time.monotonic()
    answer = client.get(f"/tasks/{task_id}/result")
    standalone = client.get(f"/tasks/{task_id}/evidence")
    assert (answer.status_code, standalone.status_code) == (200, 200), (answer.text, standalone.text)
    result, evidence = answer.json(), standalone.json()["items"]
    assert result["result_status"] == "formal" and result["answer_snapshot_id"]
    assert len(evidence) == 6 and {row["id"] for row in evidence} == {row["id"] for row in result["evidence"]}
    assert len({row["id"] for row in evidence}) == 6
    by_version = defaultdict(list)
    for row in evidence:
        assert row["chunk_id"] and row["chunk_set_id"] and len(row["source_binding_hash"]) == 64
        assert row["locator"]["file_page"] == 1 and len(row["source_spans"]) == 1
        assert row["source_spans"][0]["block_start"] < row["source_spans"][0]["block_end"]
        by_version[row["document_version_id"]].append(row)
    assert set(by_version) == {source["version_id"] for source in sources}
    originals = {}
    for source in sources:
        version, document_id = source["version_id"], source["document_id"]
        original = client.get(f"/documents/{document_id}/versions/{version}/file")
        assert original.status_code == 200 and original.headers["X-Document-Read-Mode"] == "published_original"
        originals[version] = sha256(original.content).hexdigest()
        assert originals[version] == source["original_sha256"]
        rows = by_version[version]
        assert Counter(row["quote"] for row in rows) == Counter(source["quotes"])
        assert all(row["locator"]["source_sha256"] == originals[version] for row in rows)
    wording = result["answer"]
    assert all(literal in wording for literal in (STEP_ONE, STEP_TWO, WARNING, PRESSURE))
    assert wording.index(STEP_ONE) < wording.index(WARNING) < wording.index(STEP_TWO)
    return {"answer_snapshot_id": result["answer_snapshot_id"], "evidence_ids": [row["id"] for row in evidence],
            "original_sha256_by_version": originals, "read_seconds": time.monotonic() - begun}


def _observe_task(client_args, task_id, sources, started_running, stop, row):
    with httpx.Client(**client_args) as client:
        while not stop.is_set():
            begun = time.monotonic()
            response = client.get(f"/tasks/{task_id}/status")
            row["poll_seconds"].append(time.monotonic() - begun)
            response.raise_for_status()
            data = response.json()
            status = data["status"]
            if status not in row["seen_states"]:
                row["seen_states"].append(status)
            if status in {"pending", "running"}:
                assert set(data) == {"id", "status", "revision"}
                if status == "running":
                    started_running.set()
                stop.wait(1)
                continue
            if status == "succeeded":
                row.update(_read_exact_answer(client, task_id, sources))
            return


def _receipts(f, task_id, journal, stage, wave):
    with f.app.state.session_factory() as db:
        task = db.get(QueryTask, task_id)
        budget = db.get(TaskBudget, task_id)
        calls = db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.task_id == task_id)
                           .order_by(ModelCallAdmission.created_at)).all()
        snapshots = db.scalars(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task_id)).all()
        retrieval = list(db.scalars(select(AuditEvent.metadata_json).where(AuditEvent.resource_id == task_id,
            AuditEvent.action == "query.vector_retrieved", AuditEvent.actor_user_id == f.actor_id)))
        row = {"task_id": task_id, "status": task.status, "error_code": task.error_code,
               "calls": [{"state": call.state, "http_status": call.response_status, "error_code": call.error_code} for call in calls],
               "reserved_calls": budget.reserved_calls if budget else 0, "vector_receipts": retrieval,
               "snapshots": [{"id": snapshot.id, "evidence_ids": snapshot.evidence_ids} for snapshot in snapshots]}
    journal.append("query_receipts", stage=stage, wave=wave, **row)
    return row


def _wave(f, journal, stage, number, proofs, ranked_by_model):
    client_args = _http_args(f, f.client)
    begun, stop, first_running = time.monotonic(), Event(), Event()
    tasks, outcomes, receipts_by_task = {}, [], {}
    offset = len(f.gateway_trace)
    errors_before = len(f.model.errors)
    journal.append("wave_started", stage=stage, wave=number)
    with httpx.Client(**client_args) as client:
        for index in range(QUEUE_LIMIT):
            model = CROSS_MODEL if index == QUEUE_LIMIT - 1 and CROSS_MODEL in proofs else MODEL
            payload = {"query": QUERY if model == MODEL else f"{model} startup and pressure", "model": model,
                       "knowledge_base_ids": [f.kb_id]}
            posted = time.monotonic()
            accepted = client.post("/knowledge/queries", json=payload, headers=_headers())
            assert accepted.status_code == 202, accepted.text
            task_id = accepted.json()["task_id"]
            tasks[task_id] = {"model": model, "accept_seconds": time.monotonic() - posted, "accepted_at": time.monotonic(),
                              "seen_states": [], "poll_seconds": []}
            journal.append("query_accepted", stage=stage, wave=number, task_id=task_id, model=model,
                           accept_seconds=tasks[task_id]["accept_seconds"])
        assert len(tasks) == QUEUE_LIMIT
        for task_id in tasks:
            response = client.get(f"/tasks/{task_id}/status")
            assert response.status_code == 200 and response.json()["status"] == "pending"
            assert set(response.json()) == {"id", "status", "revision"}
        with ThreadPoolExecutor(max_workers=QUEUE_LIMIT) as readers, ThreadPoolExecutor(max_workers=1) as worker:
            observers = {task_id: readers.submit(_observe_task, client_args, task_id, proofs[row["model"]],
                                                  first_running if index == 0 else Event(), stop, row)
                         for index, (task_id, row) in enumerate(tasks.items())}
            try:
                for index, (task_id, row) in enumerate(tasks.items()):
                    working = time.monotonic()
                    future = worker.submit(run_one, f.config, gateway_url=f.gateway)
                    if index == 0:
                        assert first_running.wait(20), "The first accepted task never became visibly running"
                        overflow = client.post("/knowledge/queries", json=payload, headers=_headers())
                        rejection = {"http": overflow.status_code, "code": overflow.json().get("code")}
                        journal.append("overflow", stage=stage, wave=number, **rejection)
                        assert rejection == {"http": 429, "code": "QUERY_CAPACITY_FULL"}
                    outcome = future.result()
                    finished = time.monotonic()
                    receipts = receipts_by_task[task_id] = _receipts(f, task_id, journal, stage, number)
                    observed = {"task_id": task_id, "model": row["model"], "outcome": outcome,
                                "queue_seconds": working - row["accepted_at"], "worker_seconds": finished - working,
                                "end_to_end_seconds": finished - row["accepted_at"]}
                    journal.append("query_worker_finished", stage=stage, wave=number, **observed)
                    assert outcome == {"task_id": task_id, "state": "succeeded", "error_code": None}, (outcome, receipts)
                    assert receipts["calls"] == [{"state": "received", "http_status": 200, "error_code": None}] * 3
                    assert receipts["reserved_calls"] == 3 and len(receipts["snapshots"]) == 1
                    receipt, = receipts["vector_receipts"]
                    assert receipt["method"] == "pgvector_cosine" and receipt["generation_ids"] == [f.generation_id]
                    assert receipt["service_revision_id"] == f.revision_id and len(receipt["vector_sha256"]) == 64
                    assert receipt["ranked_candidate_count"] == ranked_by_model[row["model"]] and receipt["evidence_count"] == 6
                    outcomes.append(observed)
                for observed in outcomes:
                    task_id = observed["task_id"]
                    row, receipts = tasks[task_id], receipts_by_task[task_id]
                    observers[task_id].result(timeout=85)
                    assert {"running", "succeeded"} <= set(row["seen_states"])
                    assert receipts["snapshots"][0]["id"] == row["answer_snapshot_id"]
                    assert set(receipts["snapshots"][0]["evidence_ids"]) == set(row["evidence_ids"])
                    observed.update({key: value for key, value in row.items() if key != "accepted_at"})
                    journal.append("query_verified", stage=stage, wave=number, **observed)
            finally:
                stop.set()
                journal.append("wave_gateway", stage=stage, wave=number, requests=f.gateway_trace[offset:],
                               tasks={task_id: {"seen_states": row["seen_states"], "poll_seconds": row["poll_seconds"]}
                                      for task_id, row in tasks.items()})
    assert len(f.model.errors) == errors_before, f.model.errors[errors_before:]
    assert run_one(f.config, gateway_url=f.gateway) == {"state": "idle"}
    with httpx.Client(**_http_args(f, f.recipient)) as recipient:
        denied = {name: recipient.get(f"/tasks/{next(iter(tasks))}/{name}").status_code for name in ("result", "evidence")}
    assert denied == {"result": 404, "evidence": 404}
    return {"wave": number, "wall_seconds": time.monotonic() - begun, "query_count": len(outcomes), "tasks": outcomes,
            "successful_worker_seconds": sum(row["worker_seconds"] for row in outcomes),
            "overflow": rejection, "other_user_without_share": denied, "idle_after": True}


@pytest.fixture
def isolated_endurance_pipeline(request, monkeypatch):
    endpoint = make_url(os.environ["DATACENTER_TEST_POSTGRES_URL"])
    assert (endpoint.drivername, endpoint.host, endpoint.port) == ("postgresql+psycopg", "127.0.0.1", 55440)
    assert endpoint.database.startswith("dc_local_capacity_marker_") and not endpoint.query
    for name in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "DATACENTER_BAILIAN_SERVICE_ENDPOINT"):
        monkeypatch.delenv(name, raising=False)
    f = request.getfixturevalue("child_answer_pipeline")
    assert f.config.query_service_mode == "local"
    assert f.app.state.engine.url.database.startswith("dc_local_creation_recovery_")
    assert f.app.state.engine.url.get_backend_name() == "postgresql"
    return f


@pytest.mark.skipif(SCALE_BOUNDARY, reason="The separate scale-boundary measurement is selected")
def test_real_bge_query_endurance(isolated_endurance_pipeline, monkeypatch):
    import psutil

    f = isolated_endurance_pipeline
    from . import worker as query_worker

    worker_engines = WeakSet()
    original_build_engine = query_worker.build_engine
    worker_engine_counts = {"created": 0}

    def observed_build_engine(*args, **kwargs):
        engine = original_build_engine(*args, **kwargs)
        worker_engines.add(engine)
        worker_engine_counts["created"] += 1
        return engine

    monkeypatch.setattr(query_worker, "build_engine", observed_build_engine)
    proof_only = os.environ.get("DATACENTER_ENDURANCE_PROOF_ONLY") == "1"
    recovery_enabled = os.environ.get("DATACENTER_ENDURANCE_RECOVERY") == "1"
    stages = tuple(int(value) for value in os.environ.get("DATACENTER_ENDURANCE_STAGES", "10" if proof_only else "2,10").split(","))
    configured_sustain_stage = os.environ.get("DATACENTER_ENDURANCE_SUSTAIN_STAGE")
    sustain_stage = int(configured_sustain_stage) if configured_sustain_stage and not proof_only else stages[-1]
    pages = int(os.environ.get("DATACENTER_ENDURANCE_PAGES", "8"))
    f.blocks_per_page = int(os.environ.get("DATACENTER_ENDURANCE_BLOCKS", "4"))
    full_domain_review = os.environ.get("DATACENTER_ENDURANCE_FULL_DOMAIN_REVIEW") == "1"
    min_waves = int(os.environ.get("DATACENTER_ENDURANCE_WAVES", "1"))
    default_sustained_seconds = "1800" if configured_sustain_stage or stages[-1] == 89 else "0"
    sustained_seconds = int(os.environ.get("DATACENTER_ENDURANCE_SECONDS", default_sustained_seconds))
    max_tasks = int(os.environ.get("DATACENTER_ENDURANCE_MAX_TASKS", "80"))
    setup_budget = int(os.environ.get("DATACENTER_ENDURANCE_SETUP_SECONDS", "1500"))
    sample_seconds = float(os.environ.get("DATACENTER_ENDURANCE_SAMPLE_SECONDS", "3"))
    free_memory_mib = int(os.environ.get("DATACENTER_ENDURANCE_FREE_MIB", "256" if proof_only else "512"))
    free_memory = free_memory_mib * 1024 ** 2
    inventory = os.environ.get("DATACENTER_ENDURANCE_PAGE_INVENTORY")
    assert stages == tuple(sorted(set(stages))) and all(2 <= stage <= 89 for stage in stages)
    assert sustain_stage in stages
    assert not (proof_only and recovery_enabled), "Recovery requires the full SDK query mode"
    assert 1 <= pages <= PREFLIGHT_PAGE_LIMIT and 2 <= f.blocks_per_page <= 22
    assert 1 <= min_waves <= max_tasks // QUEUE_LIMIT and min_waves * QUEUE_LIMIT <= max_tasks <= 80 and sustained_seconds >= 0
    assert setup_budget > 0 and sample_seconds >= 1 and free_memory > 0
    plan, redistribution = _manual_plan(stages, pages, Path(inventory) if inventory else None)
    base = Path(os.environ.get("DATACENTER_ENDURANCE_OUTPUT", "outputs/pilot-endurance/2026-11-21"))
    journal = Journal(base / f"run-{uuid4().hex[:12]}")
    print(f"Synthetic endurance evidence: {journal.directory.resolve()}", flush=True)
    scope = ("Synthetic PDFs, fixture Standard archives, production validation, fixture loopback embedding and PGvector, "
             "production six-member SQL/function profile" if proof_only else
             "Synthetic PDFs, fixture Standard archives, production validation, real offline BGE-M3, PGvector, installed Pi SDK and loopback generation")
    report = {"mode": "proof_only" if proof_only else "endurance", "scope": scope,
              "database": f.app.state.engine.url.database, "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
              "planned_document_counts": stages, "planned_total_pages": sum(row[0] for row in plan),
              "largest_planned_pdf_pages": max(row[0] for row in plan), "blocks_per_regular_page": f.blocks_per_page,
              "page_redistribution_from_anonymous_inventory": redistribution, "free_memory_threshold_mib": free_memory_mib,
              "review_setup_method": {"pages_at_most_eight": DOMAIN_REVIEW,
                                      "pages_above_eight": DOMAIN_REVIEW if full_domain_review else BULK_REVIEW},
              "setup_budget_seconds": setup_budget, "stages": [], "passed": False}
    if not proof_only:
        report.update(workers=1, max_inflight=QUEUE_LIMIT, sustain_stage=sustain_stage, max_query_tasks_per_stage=max_tasks,
                      sustain_stage_minimum_waves=min_waves, sustain_stage_successful_worker_seconds_required=sustained_seconds,
                      recovery_after_sustain_stage=recovery_enabled)
    f.config = replace(f.config, query_max_inflight=QUEUE_LIMIT)
    f.app.state.settings = f.config
    f.app.state.session_factory.configure(info={"settings": f.config})
    sources = [{"document_id": f.document_id, "version_id": f.version_id, "model": MODEL, "pages": 2,
                "expected_leaves": len(LINES), "original_sha256": sha256(f.original).hexdigest(),
                "original_bytes": len(f.original), "quotes": [literal for _, literal in LINES], "review_setup_method": DOMAIN_REVIEW}]
    prep_seconds = 0.0
    phase = {"phase": "warmup"}
    manifest_times = _observe_duration(monkeypatch, indexing, "_library_manifest")
    encode_times = _observe_duration(monkeypatch, local_bge.LocalBgeEndpoint, "embed")

    def check_resources():
        if psutil.virtual_memory().available < free_memory:
            raise EnduranceLimit("Available memory fell below the configured threshold")
        if shutil.disk_usage(journal.directory).free < 1024 ** 3:
            raise EnduranceLimit("Evidence volume has less than 1 GiB available")

    def check_setup():
        check_resources()
        if prep_seconds + time.monotonic() - stage_started >= setup_budget:
            raise EnduranceLimit("Setup time budget reached")

    journal.checkpoint(report)
    try:
        with _monitor(f, journal, phase, sample_seconds):
            stage_started = time.monotonic()
            check_setup()
            if redistribution:
                phase.update(phase="oversized_preflight")
                report["oversized_pdf_preflight"] = _oversized_pdf_preflight(f, journal, redistribution["outside_preflight_limit"])
                journal.checkpoint(report)
                phase.update(phase="warmup")
            if proof_only:
                with f.app.state.session_factory() as db:
                    revision = db.get(ServiceRevision, f.revision_id)
                    assert revision.adapter != local_bge.ADAPTER and revision.endpoint_ref == "synthetic"
                    report["embedding_setup"] = {"service_revision_id": revision.id, "adapter": revision.adapter,
                                                 "mode": "loopback_synthetic_vectors", "real_bge": False}
            else:
                report["embedding_setup"] = _enable_real_bge(f)
            prep_seconds += time.monotonic() - stage_started
            journal.checkpoint(report)
            for stage in stages:
                stage_started = time.monotonic()
                phase.update(phase="setup", stage=stage, wave=None)
                progress = {"target_documents": stage, "validated_documents": len(sources), "waves": [], "passed": False}
                report["stages"].append(progress)
                journal.checkpoint(report)
                journal.append("stage_started", stage=stage, validated_documents=len(sources))
                while len(sources) < stage:
                    check_setup()
                    index = len(sources)
                    started = time.monotonic()
                    journal.append("manual_started", stage=stage, ordinal=index + 1, planned_pages=plan[index][0])
                    source = _publish_manual(f, index + 1, plan[index], full_domain_review=full_domain_review)
                    sources.append(source)
                    progress["validated_documents"] = len(sources)
                    journal.append("manual_published", stage=stage, ordinal=index + 1, seconds=time.monotonic() - started,
                                   **{key: value for key, value in source.items() if key != "quotes"})
                    journal.checkpoint(report)
                progress["source_seconds"] = time.monotonic() - stage_started
                phase.update(phase="index")
                check_setup()
                progress["index_timing"] = _build_generation(f, journal, stage, check_setup, manifest_times, encode_times)
                actual = progress["inventory"] = _source_inventory(f)
                by_version = {row["version"]: row for row in actual["sources"]}
                assert set(by_version) == {source["version_id"] for source in sources}
                for source in sources:
                    row = by_version[source["version_id"]]
                    assert row["models"] == [source["model"]] and row["pages"] == source["pages"]
                    assert row["leaves"] == source["expected_leaves"] and row["indexed_members"] > 0
                    row["review_setup_method"] = source["review_setup_method"]
                actual.update(documents=len(by_version), pages=sum(row["pages"] for row in by_version.values()),
                              leaves=sum(row["leaves"] for row in by_version.values()), chunks=sum(row["chunks"] for row in by_version.values()),
                              review_setup_methods=dict(Counter(row["review_setup_method"] for row in by_version.values())))
                actual.update(_source_text_sizes(f))
                proofs = {model: [source for source in sources if source["model"] == model] for model in (MODEL, CROSS_MODEL)
                          if any(source["model"] == model for source in sources)}
                ranked_counts = {model: sum(by_version[source["version_id"]]["indexed_members"] for source in model_sources)
                                 for model, model_sources in proofs.items()}
                assert ranked_counts[MODEL] == 2
                if CROSS_MODEL in proofs:
                    assert len(proofs[CROSS_MODEL]) == ranked_counts[CROSS_MODEL] == 2
                progress["setup_seconds"] = time.monotonic() - stage_started
                prep_seconds += progress["setup_seconds"]
                journal.checkpoint(report)
                journal.append("stage_ready", stage=stage, setup_seconds=progress["setup_seconds"],
                               **{key: value for key, value in actual.items() if key != "sources"})
                if proof_only:
                    phase.update(phase="proof")
                    check_resources()
                    progress["proof"] = _proof_only_stage(f, journal, stage, monkeypatch)
                    assert not encode_times
                    progress["passed"] = True
                    journal.checkpoint(report)
                    continue

                barrier_value = os.environ.get("DATACENTER_ENDURANCE_QUERY_BARRIER")
                if barrier_value:
                    barrier = Path(barrier_value).resolve()
                    assert barrier.is_relative_to(base.resolve()), 'Query barrier must stay in this evidence root'
                    phase.update(phase="waiting_for_query_barrier")
                    journal.append("waiting_for_query_barrier", stage=stage, path=str(barrier))
                    deadline = time.monotonic() + 1800
                    while not barrier.is_file():
                        if time.monotonic() >= deadline:
                            raise EnduranceLimit("Coordinator query barrier was not released")
                        time.sleep(1)
                    journal.append("query_barrier_released", stage=stage)
                required_waves, required_seconds = (min_waves, sustained_seconds) if stage == sustain_stage else (1, 0)
                progress.update(required_waves=required_waves, required_successful_worker_seconds=required_seconds)
                progress["successful_worker_seconds"] = 0.0
                while len(progress["waves"]) < required_waves or progress["successful_worker_seconds"] < required_seconds:
                    wave = len(progress["waves"]) + 1
                    if wave * QUEUE_LIMIT > max_tasks:
                        raise EnduranceLimit("Maximum task count reached before sustained worker time was met")
                    phase.update(phase="query", wave=wave)
                    check_resources()
                    timing = _wave(f, journal, stage, wave, proofs, ranked_counts)
                    timing["worker_engine_lifecycle"] = {**worker_engine_counts, "live": len(worker_engines)}
                    progress["waves"].append(timing)
                    progress["successful_worker_seconds"] += timing["successful_worker_seconds"]
                    journal.append("wave_verified", stage=stage, wave=wave, successful_worker_seconds=progress["successful_worker_seconds"],
                                   wall_seconds=timing["wall_seconds"], verified_queries=wave * QUEUE_LIMIT,
                                   worker_engine_lifecycle=timing["worker_engine_lifecycle"])
                    journal.checkpoint(report)
                if recovery_enabled and stage == sustain_stage:
                    from .test_query_endurance_recovery import run_recovery_scenarios

                    phase.update(phase="recovery", wave=None)
                    output = "recovery-mechanisms.json"
                    progress["recovery"] = {"report": output, "passed": False}
                    journal.checkpoint(report)
                    journal.append("recovery_started", stage=stage, report=output)
                    recovery = run_recovery_scenarios(f, journal.directory / output)
                    fence = recovery["scenarios"]["nonmatching_source_fence"]
                    assert recovery["passed"] and fence["status"] == "verified"
                    progress["recovery"].update(passed=True, nonmatching_source_fence=fence)
                    journal.append("recovery_verified", stage=stage, report=output, nonmatching_source_fence=fence)
                progress["passed"] = True
                journal.checkpoint(report)
            report["passed"] = True
    except BaseException as error:
        report["failure"] = {**phase, "type": type(error).__name__}
        if isinstance(error, EnduranceLimit):
            report["failure"]["reason"] = str(error)
        journal.append("run_interrupted", **report["failure"])
        raise
    finally:
        report["elapsed_seconds"] = time.monotonic() - journal.started
        journal.checkpoint(report)


def _boundary_bge(f, prior=None):
    from .test_embedding_service_use import _login
    from .test_service_admission import _change, _expect, _headers as service_headers, _preview, _profile, _test_run
    from .test_service_use import select_purpose

    endpoint = local_bge.read_model_endpoint()
    assert endpoint is not None, "Approved existing offline BGE snapshot required"
    f.app.state.service_endpoints[local_bge.ENDPOINT_REF] = endpoint
    with httpx.Client(base_url=f.gateway, trust_env=False, timeout=135) as admin:
        _login(admin, f.admin_name)
        if prior is None:
            path, etag = _profile(admin, "embedding")
            configuration = {"endpoint_ref": local_bge.ENDPOINT_REF, "adapter": local_bge.ADAPTER, "model": local_bge.MODEL}
            revision = _expect(admin.post(path + "/revisions", headers=service_headers(etag), json=configuration), 201)["id"]
        else:
            path, revision = prior["profile_path"], prior["revision_id"]
        tested, _ = _test_run(admin, path + "/revisions/" + revision)
        assert tested["state"] == "succeeded" and tested["revision_id"] == revision and tested["valid_until"], tested
        assert tested["result"]["local_model"] == local_bge.profile() and len(tested["result"]["result_sha256"]) == 64
        if prior is None:
            preview = _expect(_preview(admin, path, "activate", revision_id=revision), 200)
            _expect(_change(admin, path, "activate", preview), 200)
        selection = select_purpose(admin, "document.embed", revision)
    assert selection["test_run_id"] == tested["id"] and selection["revision_id"] == revision
    if prior:
        assert selection["test_run_id"] != prior["test_run_id"]
        assert (selection["binding_hash"], selection["epoch"]) == (prior["binding_hash"], prior["epoch"])
    f.revision_id = revision
    return {**selection, "profile_path": path, "valid_until": tested["valid_until"], "test_result": tested["result"]}


def _profile_boundary_manifest(f, generation_id, expected, journal):
    engine = f.app.state.engine
    sql_seconds, table_reads = [], Counter()

    def before(_conn, _cursor, statement, _params, context, _many):
        table_reads.update(re.findall(r"(?:FROM|JOIN)\s+([a-z_]+)", statement, re.IGNORECASE) or [statement.split(None, 1)[0]])
        context._scale_boundary_started = time.perf_counter()

    def after(_conn, _cursor, _statement, _params, context, _many):
        sql_seconds.append(time.perf_counter() - context._scale_boundary_started)

    def read(db):
        kb, candidate = db.get(KnowledgeBase, f.kb_id), db.get(IndexGeneration, generation_id)
        return indexing.current_child_manifest(db, kb, candidate)

    profiler = cProfile.Profile()
    with f.app.state.session_factory() as db:
        event.listen(engine, "before_cursor_execute", before)
        event.listen(engine, "after_cursor_execute", after)
        try:
            started = time.perf_counter()
            current = profiler.runcall(read, db)
            profiled_seconds = time.perf_counter() - started
        finally:
            event.remove(engine, "before_cursor_execute", before)
            event.remove(engine, "after_cursor_execute", after)
        assert current == expected
    profiler.dump_stats(str(journal.directory / "manifest.prof"))
    with (journal.directory / "manifest-profile.txt").open("w", encoding="utf-8") as stream:
        stats = pstats.Stats(profiler, stream=stream).strip_dirs().sort_stats("cumulative")
        stats.print_stats(60)
    report = {"profiled_wall_seconds": profiled_seconds, "sql_statements": len(sql_seconds), "sql_seconds": sum(sql_seconds),
              "table_reads": dict(table_reads.most_common()), "function_calls": stats.total_calls,
              "distinct_functions": len(stats.stats), "manifest_hash": indexing.fingerprint(current)}
    journal.append("manifest_profiled", **report)
    # Confirm a threshold crossing without cProfile or SQL instrumentation.
    if profiled_seconds > 20:
        with f.app.state.session_factory() as db:
            started = time.perf_counter()
            current = read(db)
            report["unprofiled_wall_seconds"] = time.perf_counter() - started
            assert current == expected
    report["exceeds_native_gateway_budget"] = report.get("unprofiled_wall_seconds", 0) > 20
    journal.append("manifest_measured", **report)
    return report


def _boundary_batch(f, generation_id, manifest, number, journal, manifest_times, encode_times):
    journal.append("boundary_batch_started", generation_id=generation_id, batch=number)
    started, proof_offset, encode_offset = time.monotonic(), len(manifest_times), len(encode_times)
    with f.app.state.session_factory() as db:
        _, _, payload = child_batch_payload(db, manifest, number)
        attest_synthetic_input(db, f.actor_id, "document.embed", f"index:{generation_id}:{number}", payload)
        job_id = accept_child_embedding_batch(db, generation_id, number, db.get(User, f.actor_id),
                                              f.app.state.service_endpoints).id
        db.commit()
        admitted = time.monotonic()
        journal.append("boundary_batch_accepted", generation_id=generation_id, batch=number, job_id=job_id,
                       admission_seconds=admitted - started)
        run_child_embedding_batch(db, job_id, f.app.state.service_endpoints)
        worker_seconds = time.monotonic() - admitted
        job = db.get(ChildEmbeddingBatch, job_id, populate_existing=True)
        pin = db.get(ServiceUsePin, job.pin_id)
        calls = db.scalars(select(ServiceUseCall).where(ServiceUseCall.pin_id == pin.id).order_by(ServiceUseCall.ordinal)).all()
        generation = db.get(IndexGeneration, generation_id, populate_existing=True)
        if number <= 3 or number == len(manifest["batches"]):
            integrity = indexing.child_vector_integrity(db, generation, manifest)
        else:
            vector_count = db.scalar(select(func.count()).select_from(ChunkEmbedding)
                                     .where(ChunkEmbedding.generation_id == generation_id))
            integrity = {"actual_count": vector_count, "expected_count": len(manifest["members"])}
        lease_lost = job.error_code == "EMBEDDING_ATTEMPT_STALE" or (
            job.state == "running" and job.attempt == 1 and job.lease_until is not None and job.lease_until <= utc_now())
        result = {"batch": number, "job_id": job_id, "batch_items": len(manifest["batches"][number - 1]["child_ids"]),
                  "job_attempt": job.attempt, "job_state": job.state, "error_code": job.error_code,
                  "lease_until": job.lease_until.isoformat() if job.lease_until else None, "lease_lost": lease_lost,
                  "pin_id": pin.id, "pin_test_run_id": pin.test_run_id, "pin_expires_at": pin.expires_at.isoformat(),
                  "input_sha256": job.input_hash, "calls": [{"method": call.method, "state": call.state,
                      "response_sha256": call.response_hash, "error_code": call.safe_error, "usage": call.usage} for call in calls],
                  "admission_seconds": admitted - started, "worker_seconds": worker_seconds,
                  "library_proof_seconds": sum(manifest_times[proof_offset:]), "bge_encode_seconds": sum(encode_times[encode_offset:]),
                  "vector_integrity": {key: value for key, value in integrity.items() if not key.endswith("_refs")}}
    journal.append("boundary_batch_finished", generation_id=generation_id, **result)
    return result


def _boundary_index_status(client, f, generation_id, outcome):
    path = f"/knowledge-bases/{f.kb_id}"
    previous = client.get(path + "/index-status")
    assert previous.status_code == 200, previous.text
    before = previous.json()
    assert before["status"] == "stale" and before["active_generation_id"] == f.generation_id
    response = client.put(path + "/active-index", headers={**_headers(), "If-Match": before["etag"]}, json={
        "generation_id": generation_id, "expected_active_id": before["active_generation_id"],
        "expected_source_revision": before["content_revision"], "action": "activate"})
    if outcome == "capacity_not_met":
        assert response.status_code == 409 and response.json()["code"] == "INDEX_GENERATION_NOT_READY", response.text
        assert client.get(path + "/index-status").json() == before
    else:
        assert response.status_code == 200 and response.json()["active_generation_id"] == generation_id, response.text
    return {"previous": before, "activation_http": response.status_code, "activation_code": response.json().get("code")}


def _boundary_reject_query(f, journal, client):
    accepted = client.post("/knowledge/queries", headers=_headers(), json={"query": QUERY, "model": MODEL,
                           "knowledge_base_ids": [f.kb_id]})
    assert accepted.status_code == 202, accepted.text
    identifier = accepted.json()["task_id"]
    offset, request_count = len(f.gateway_trace), len(f.model.requests)
    errors_before, prior_mode = len(f.model.errors), f.model.mode
    f.model.mode = "no_evidence_stop"
    try:
        outcome = run_one(f.config, gateway_url=f.gateway)
    finally:
        f.model.mode = prior_mode
    status = client.get(f"/tasks/{identifier}")
    result = client.get(f"/tasks/{identifier}/result")
    evidence = client.get(f"/tasks/{identifier}/evidence")
    ledger = _receipts(f, identifier, journal, 89, 0)
    observed = {"accepted_http": accepted.status_code, "task_id": identifier, "worker": outcome,
                "status_http": status.status_code, "task_status": status.json().get("status"),
                "warnings": status.json().get("warnings"), "result_http": result.status_code,
                "evidence_http": evidence.status_code, "evidence_count": len(evidence.json().get("items", [])),
                "loopback_requests": len(f.model.requests) - request_count, "calls": ledger["calls"],
                "snapshots": len(ledger["snapshots"]), "gateway_trace": f.gateway_trace[offset:]}
    journal.append("ordinary_query_index_fence", **observed)
    assert outcome == {"task_id": identifier, "state": "blocked", "error_code": "INDEX_NOT_READY"}, observed
    assert status.status_code == 200 and status.json()["status"] == "blocked" and "index_stale" in observed["warnings"]
    assert result.status_code == 409 and evidence.status_code == 200 and evidence.json()["items"] == []
    assert not (status.json().get("result") or {}).get("answer") and not ledger["snapshots"] and not ledger["vector_receipts"]
    assert len(f.model.errors) == errors_before and all(row["state"] == "received" for row in ledger["calls"])
    assert ledger["reserved_calls"] == len(ledger["calls"]) == observed["loopback_requests"]
    return observed


@pytest.mark.skipif(not SCALE_BOUNDARY, reason="Explicit 89-document scale-boundary opt-in required")
def test_scale_unready_http_fences(isolated_endurance_pipeline):
    """Observe refusal at the full published scale without loading another model.

    This separate negative test uses the fixture's synthetic embedding service.
    It does not replace the real-BGE manifest measurement or certify throughput.
    """
    import psutil

    from .test_embedding_service_use import _login

    f = isolated_endurance_pipeline
    inventory = Path(os.environ.get("DATACENTER_ENDURANCE_PAGE_INVENTORY", "outputs/sample-inspection/2026-11-15/inventory.json"))
    plan, redistribution = _manual_plan((89,), 8, inventory)
    assert len(plan) == 89 and sum(row[0] for row in plan) == 6372
    f.blocks_per_page = 4
    output = Path(os.environ["DATACENTER_ENDURANCE_OUTPUT"])
    journal = Journal(output / f"unready-{uuid4().hex[:12]}")
    print(f"Synthetic 89-document HTTP refusal evidence: {journal.directory.resolve()}", flush=True)
    report = {"mode": "published_scale_unready_http_fences", "real_bge": False,
              "closure_verified": False, "passed": False, "database": f.app.state.engine.url.database,
              "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
              "page_redistribution": redistribution, "published_documents": 1, "published_pages": 2}
    phase = {"phase": "publish", "stage": 89}
    f.config = replace(f.config, query_max_inflight=QUEUE_LIMIT)
    f.app.state.settings = f.config
    f.app.state.session_factory.configure(info={"settings": f.config})
    journal.checkpoint(report)
    try:
        with _monitor(f, journal, phase, 5):
            for ordinal, item in enumerate(plan[1:], 2):
                if psutil.virtual_memory().available < 512 * 1024 ** 2:
                    raise EnduranceLimit("Available memory fell below the configured threshold")
                if shutil.disk_usage(journal.directory).free < 1024 ** 3:
                    raise EnduranceLimit("Evidence volume has less than 1 GiB available")
                started = time.monotonic()
                source = _publish_manual(f, ordinal, item, full_domain_review=False)
                report["published_documents"] += 1
                report["published_pages"] += source["pages"]
                journal.append("manual_published", ordinal=ordinal, seconds=time.monotonic() - started,
                               **{key: value for key, value in source.items() if key != "quotes"})
                journal.checkpoint(report)
            phase.update(phase="manifest_freeze")
            with f.app.state.session_factory() as db:
                candidate = _build(db, f)
                generation_id = candidate.id
                manifest = candidate.build_report["manifest"]
                assert candidate.status == "building" and not candidate.ready_at
                assert len(manifest["sources"]) == 89 and len(manifest["members"]) == 6370
                actual = indexing.child_vector_integrity(db, candidate, manifest)
                assert actual["status"] == "incomplete" and actual["actual_count"] == 0
                assert not actual["unexpected_child_refs"] and not actual["invalid_child_refs"]
                report["candidate"] = {"id": generation_id, "status": candidate.status,
                    "expected_vectors": len(manifest["members"]), "actual_vectors": 0}
                manager = db.get(User, f.actor_id).username
                db.commit()
            journal.checkpoint(report)
            phase.update(phase="activation_and_query")
            _login(f.client, manager)
            with httpx.Client(**{**_http_args(f, f.client), "timeout": 120}) as client:
                report["index_http_fence"] = _boundary_index_status(client, f, generation_id, "capacity_not_met")
                journal.checkpoint(report)
                report["ordinary_http_query"] = _boundary_reject_query(f, journal, client)
            report["passed"] = True
    finally:
        report["elapsed_seconds"] = time.monotonic() - journal.started
        journal.checkpoint(report)


@pytest.mark.skipif(not SCALE_BOUNDARY, reason="Explicit 89-document scale-boundary opt-in required")
def test_real_bge_scale_boundary(isolated_endurance_pipeline, monkeypatch):
    import psutil

    from .test_embedding_service_use import _login

    f = isolated_endurance_pipeline
    inventory_path = Path(os.environ.get("DATACENTER_ENDURANCE_PAGE_INVENTORY", "outputs/sample-inspection/2026-11-15/inventory.json"))
    plan, redistribution = _manual_plan((89,), 8, inventory_path)
    assert len(plan) == 89 and sum(item[0] for item in plan) == 6372
    f.blocks_per_page = int(os.environ.get("DATACENTER_ENDURANCE_BLOCKS", "4"))
    assert 2 <= f.blocks_per_page <= 22
    full_domain = os.environ.get("DATACENTER_ENDURANCE_FULL_DOMAIN_REVIEW") == "1"
    output = Path(os.environ.get("DATACENTER_ENDURANCE_OUTPUT", "outputs/pilot-endurance/2026-11-21"))
    journal = Journal(output / f"boundary-{uuid4().hex[:12]}")
    print(f"Synthetic 89-document scale-boundary evidence: {journal.directory.resolve()}", flush=True)
    phase = {"phase": "oversized_preflight", "stage": 89}
    stage = {"mode": "scale_boundary", "status": "measuring", "closure_verified": False, "passed": False,
             "database": f.app.state.engine.url.database, "harness_sha256": sha256(Path(__file__).read_bytes()).hexdigest(),
             "active_generation_before": f.generation_id, "published_documents": 1, "published_pages": 2,
             "planned_documents": 89, "planned_pages": 6372, "page_redistribution": redistribution,
             "native_gateway_budget_seconds": 20, "batch_lease_seconds": 120, "batches": [], "embedding_checks": []}
    sources = [{"document_id": f.document_id, "version_id": f.version_id, "model": MODEL, "pages": 2,
                "original_sha256": sha256(f.original).hexdigest(), "quotes": [literal for _, literal in LINES],
                "review_setup_method": DOMAIN_REVIEW}]
    f.config = replace(f.config, query_max_inflight=QUEUE_LIMIT)
    f.app.state.settings = f.config
    f.app.state.session_factory.configure(info={"settings": f.config})
    manifest_times = _observe_duration(monkeypatch, indexing, "_library_manifest")
    encode_times = _observe_duration(monkeypatch, local_bge.LocalBgeEndpoint, "embed")
    free_memory_bytes = int(os.environ.get("DATACENTER_ENDURANCE_FREE_MIB", "512")) * 1024 ** 2
    assert free_memory_bytes > 0

    def available():
        if psutil.virtual_memory().available < free_memory_bytes:
            raise EnduranceLimit("Available memory fell below the configured threshold")
        if shutil.disk_usage(journal.directory).free < 1024 ** 3:
            raise EnduranceLimit("Evidence volume has less than 1 GiB available")

    journal.checkpoint(stage)
    try:
        with _monitor(f, journal, phase, 5):
            available()
            stage["oversized_pdf_preflight"] = _oversized_pdf_preflight(f, journal, redistribution["outside_preflight_limit"])
            phase.update(phase="publish")
            source_started = time.monotonic()
            for index, item in enumerate(plan[1:], 2):
                available()
                started = time.monotonic()
                journal.append("manual_started", stage=89, ordinal=index, planned_pages=item[0])
                source = _publish_manual(f, index, item, full_domain_review=full_domain)
                sources.append(source)
                stage["published_documents"] += 1
                stage["published_pages"] += source["pages"]
                journal.append("manual_published", stage=89, ordinal=index, seconds=time.monotonic() - started,
                               **{key: value for key, value in source.items() if key != "quotes"})
                journal.checkpoint(stage)
            stage["source_setup_seconds"] = time.monotonic() - source_started
            barrier_value = os.environ.get("DATACENTER_SCALE_BOUNDARY_ADMISSION_BARRIER")
            if barrier_value:
                barrier = Path(barrier_value).resolve()
                assert barrier.is_relative_to(output.resolve()), "Admission barrier must stay in this evidence root"
                phase.update(phase="waiting_for_embedding_admission")
                journal.append("waiting_for_embedding_admission", documents=stage["published_documents"],
                               pages=stage["published_pages"], path=str(barrier))
                deadline = time.monotonic() + 3600
                while not barrier.is_file():
                    if time.monotonic() >= deadline:
                        raise EnduranceLimit("Coordinator embedding admission barrier was not released")
                    available()
                    time.sleep(1)
                journal.append("embedding_admission_barrier_released")
            phase.update(phase="embedding_admission")
            available()
            admission = _boundary_bge(f)
            stage["embedding_checks"].append(admission)
            journal.append("boundary_local_bge_admitted", **admission)

            phase.update(phase="manifest_freeze")
            started = time.monotonic()
            with f.app.state.session_factory() as db:
                candidate = _build(db, f)
                generation_id = candidate.id
                manifest, digest = (candidate.build_report[key] for key in ("manifest", "manifest_hash"))
                assert candidate.status == "building" and candidate.build_report["integrity"]["status"] == "pending"
                db.commit()
                pages = dict(db.execute(select(IndexSource.document_version_id, func.count(DocumentPage.file_page))
                    .join(DocumentPage, DocumentPage.parse_run_id == IndexSource.parse_run_id)
                    .where(IndexSource.index_generation_id == generation_id).group_by(IndexSource.document_version_id)).all())
                assert pages == {source["version_id"]: source["pages"] for source in sources}
                published = db.scalar(select(func.count()).select_from(DocumentVersion)
                    .join(IndexSource, IndexSource.document_version_id == DocumentVersion.id)
                    .where(IndexSource.index_generation_id == generation_id, DocumentVersion.publish_status == "effective"))
                assert published == 89
                leaves = db.scalar(select(func.count()).select_from(ContentBlock)
                    .join(IndexSource, IndexSource.parse_run_id == ContentBlock.parse_run_id)
                    .where(IndexSource.index_generation_id == generation_id, ContentBlock.structure_role == "leaf"))
                chunks = db.scalar(select(func.count()).select_from(Chunk)
                    .join(IndexSource, IndexSource.chunk_set_id == Chunk.chunk_set_id)
                    .where(IndexSource.index_generation_id == generation_id))
            assert {row["version_id"] for row in manifest["sources"]} == set(pages)
            assert manifest["profile"]["adapter"] == local_bge.ADAPTER
            assert manifest["profile"]["service_binding_hash"] == admission["binding_hash"]
            assert len(manifest["batches"]) > 3 and indexing.fingerprint(manifest) == digest
            (journal.directory / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
            stage["inventory"] = {"generation_id": generation_id, "documents": len(pages), "pages": sum(pages.values()),
                "published_documents": published, "leaves": leaves, "chunks": chunks,
                "manifest_hash": digest, "expected_vectors": len(manifest["members"]), "expected_batches": len(manifest["batches"]),
                "review_setup_methods": dict(Counter(source["review_setup_method"] for source in sources)),
                "manifest_freeze_seconds": time.monotonic() - started, **_source_text_sizes(f, generation_id)}
            journal.append("boundary_manifest_frozen", **stage["inventory"])
            journal.checkpoint(stage)

            phase.update(phase="proof")
            stage["manifest_profile"] = _profile_boundary_manifest(f, generation_id, manifest, journal)
            assert stage["manifest_profile"]["manifest_hash"] == digest
            journal.checkpoint(stage)
            phase.update(phase="embedding_batch")
            limits = ["single_complete_manifest_recheck_exceeded_20s"] if stage["manifest_profile"]["exceeds_native_gateway_budget"] else []
            for batch in manifest["batches"]:
                available()
                number = batch["batch_no"]
                phase.update(batch=number)
                if datetime.fromisoformat(admission["valid_until"]) <= utc_now() + timedelta(minutes=3):
                    admission = _boundary_bge(f, prior=admission)
                    stage["embedding_checks"].append(admission)
                    journal.append("boundary_local_bge_readmitted", **admission)
                    journal.checkpoint(stage)
                result = _boundary_batch(f, generation_id, manifest, number, journal, manifest_times, encode_times)
                stage["batches"].append(result)
                if result["lease_lost"]:
                    limits.append("production_embedding_batch_lost_120s_lease")
                stage["capacity_limits"] = limits
                journal.checkpoint(stage)
                if limits:
                    break
                assert result["job_state"] == "succeeded", result
                assert result["calls"] and all(call["method"] == "LOCAL" and call["state"] == "received"
                                               and call["response_sha256"] and call["usage"] is None for call in result["calls"])

            phase.update(phase="activation_and_query", batch=None)
            with f.app.state.session_factory() as db:
                candidate, kb = db.get(IndexGeneration, generation_id), db.get(KnowledgeBase, f.kb_id)
                assert kb.active_generation_id == f.generation_id
                actual = indexing.child_vector_integrity(db, candidate, manifest)
                stage["candidate"] = {"status": candidate.status, "actual_vectors": actual["actual_count"],
                    "expected_vectors": actual["expected_count"], "integrity": actual["status"], "ready": bool(candidate.ready_at)}
                manager = db.get(User, f.actor_id).username
            if limits:
                stage["status"] = "capacity_not_met"
                assert stage["candidate"]["status"] == "building" and not stage["candidate"]["ready"]
                assert actual["status"] == "incomplete" and not actual["unexpected_child_refs"] and not actual["invalid_child_refs"]
            else:
                assert stage["candidate"]["status"] == "ready" and actual["status"] == "passed"
            journal.checkpoint(stage)

            _login(f.client, manager)
            with httpx.Client(**{**_http_args(f, f.client), "timeout": 120}) as client:
                stage["index_http_fence"] = _boundary_index_status(client, f, generation_id, stage["status"])
                journal.checkpoint(stage)
                if limits:
                    stage["ordinary_http_query"] = _boundary_reject_query(f, journal, client)
            if not limits:
                f.generation_id = generation_id
                proofs = {model: [source for source in sources if source["model"] == model] for model in (MODEL, CROSS_MODEL)}
                members_per_version = Counter(member["version_id"] for member in manifest["members"])
                ranked_counts = {model: sum(members_per_version[source["version_id"]] for source in rows) for model, rows in proofs.items()}
                assert ranked_counts == {MODEL: 2, CROSS_MODEL: 2}
                stage["successful_query_wave"] = _wave(f, journal, 89, 1, proofs, ranked_counts)
                stage.update(status="capacity_verified_for_one_wave", closure_verified=True)
            stage["passed"] = True
    except BaseException as error:
        stage["failure"] = {**phase, "type": type(error).__name__}
        if isinstance(error, EnduranceLimit):
            stage["failure"]["reason"] = str(error)
        journal.append("boundary_interrupted", **stage["failure"])
        raise
    finally:
        stage["elapsed_seconds"] = time.monotonic() - journal.started
        journal.checkpoint(stage)
