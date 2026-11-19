"""Private journal recovery uses an exact child source and durable local objects."""

from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select

from . import chunk_retrieval, governance, indexing, runtime_gate, worker
from .config import Settings
from .execution import canonical, fingerprint
from .execution_models import RuntimeJournal
from .models import Chunk, ChunkBlock, ChunkSet, Document, DocumentPage, IndexGeneration, TaskArtifact, TaskEvidence, utc_now
from .runtime_models import AgentSessionBinding, ModelCallAdmission, ModelInputProjection
from .test_runtime_gate import local_upstream, runtime_db


@pytest.fixture
def exact_journal(runtime_db, tmp_path, monkeypatch):
    fixture = runtime_db
    db = fixture.db
    # Supply a synthetic admitted manifest. The production verifier below still
    # replays stored members and independently rechecks current document/query grants.
    monkeypatch.setattr(governance, "publication_is_current", lambda db, version, historical=False: version.publish_status == "effective")
    monkeypatch.delenv("DATACENTER_JOURNAL_RECOVERY_SECONDS", raising=False)
    parse_id = fixture.block.parse_run_id
    db.add(DocumentPage(parse_run_id=parse_id, file_page=1, state="processed"))
    db.flush()
    fixture.block.file_page = 1
    generation = db.get(IndexGeneration, fixture.kb.active_generation_id)
    generation.ready_at = utc_now()
    generation.build_report = {"kind": indexing.KIND, "manifest_hash": "a" * 64, "integrity": {"status": "passed"}}
    version, old_set = fixture.version, fixture.chunk
    child_id, group_id = str(uuid4()), str(uuid4())
    start, end = 0, len(fixture.block.text_content)
    member = {"block_id": fixture.block.id, "kind": "text", "role": "body", "block_start": start, "block_end": end}
    group = {"id": group_id, "kind": "fact_group", "parse_run_id": parse_id, "scope_key": str(uuid4()),
             "structure_root_id": None, "members": [member], "required_relation_ids": [], "ordered_step_refs": [],
             "child_chunk_ids": [child_id], "complete": True, "predecessor_refs": []}
    old_set.stage_checkpoint = {"support_groups": [group]}
    child = Chunk(id=child_id, chunk_set_id=old_set.id, parse_run_id=parse_id, level="child", ordinal=0,
                  content=fixture.block.text_content, content_hash="2" * 64, kind="paragraph", eligibility="candidate",
                  support_group_refs=[group_id])
    db.add(child)
    db.flush()
    segment = {key: value for key, value in member.items() if key != "block_id"}
    db.add(ChunkBlock(chunk_id=child_id, block_id=fixture.block.id, chunk_set_id=old_set.id, parse_run_id=parse_id,
                      ordinal=0, segments=[{**segment, "chunk_start": start, "chunk_end": end}]))
    source = {"run_id": parse_id, "selected_chunk_set_id": old_set.id, "version_id": version.id,
              "document_id": version.document_id, "ir_hash": "e" * 64, "source_qc_ref": str(uuid4()),
              "collection_qc_ref": str(uuid4()), "review_set_id": version.selected_review_set_id,
              "publication_event_id": governance._source_event(db, version).id}
    monkeypatch.setattr(chunk_retrieval, "current_child_manifest", lambda *_: {"sources": [source]})
    db.flush()
    _, fragments = chunk_retrieval._group_output(db, group, child.id, source, generation, version, db.get(Document, version.document_id))
    fragment = fragments[0]
    evidence = db.scalar(select(TaskEvidence).where(TaskEvidence.task_id == fixture.task.id))
    evidence.chunk_id, evidence.chunk_set_id = child.id, old_set.id
    evidence.source_context = {"fragment": fragment}
    evidence.source_binding_hash = fragment["source_binding_hash"]
    evidence.source_spans, evidence.asset_refs = fragment["source_spans"], fragment["asset_refs"]
    evidence.locator, evidence.evidence_kind = fragment["locator"], fragment["evidence_kind"]
    db.flush()
    dependencies = {"sources": [{"evidence": runtime_gate._snapshot(evidence), "fragment": fragment,
         "source_binding_hash": evidence.source_binding_hash, "version": {"id": version.id},
         "publication": {"digest": fingerprint(governance.publication_dependency_snapshot(db, version))}}]}
    projection = ModelInputProjection(run_id=fixture.run.id, operation="answer", business_purpose="answer", context_json="",
        context_digest="1" * 64, dependency_digest=fingerprint(dependencies), dependencies=dependencies,
        profile_configuration_hash=fixture.profile.configuration_hash, expires_at=utc_now() + timedelta(minutes=1))
    db.add(projection)
    db.flush()
    call = ModelCallAdmission(run_id=fixture.run.id, task_id=fixture.task.id, projection_id=projection.id, request_key="synthetic",
        context_digest=projection.context_digest, payload_digest="b" * 64, adapter=fixture.profile.adapter,
        lease_epoch=fixture.run.lease_epoch, reserved_tokens=100, state="received", response_body="SYNTHETIC private response",
        response_status=200, expires_at=projection.expires_at, dispatched_at=utc_now())
    db.add(call)
    config = Settings(database_url=str(fixture.engine.url), storage_dir=tmp_path / "private-storage")
    binding = db.get(AgentSessionBinding, fixture.run.session_binding_id)
    working = config.storage_dir / "runtime" / binding.id / "journal" / "synthetic.jsonl"
    working.parent.mkdir(parents=True)
    pi_session = str(uuid4())
    working.write_text(canonical({"type": "session", "version": 3, "id": pi_session}) + "\n", encoding="utf-8")
    artifacts = {"artifact_manifest_hash": fixture.profile.artifact_manifest_hash, "adapter_fingerprint": fixture.profile.adapter_fingerprint,
                 "manifest": fixture.profile.configuration["artifact_manifest"]}
    worker._save_journal(db, config, fixture.run, {"pi_session_id": pi_session, "session_file": str(working)}, 1,
                         profile=fixture.profile, artifacts=artifacts)
    db.commit()
    runtime_gate.close_run(db, fixture.run.id, fixture.token)
    journal = db.scalar(select(RuntimeJournal).where(RuntimeJournal.run_id == fixture.run.id))
    assert journal.state == "active", journal.source_bindings
    assert journal.source_bindings["sources"][evidence.id]["chunk_id"] == child.id
    assert fragment["quote"] not in canonical(journal.source_bindings)
    fixture.config, fixture.binding, fixture.working = config, binding, working
    fixture.artifacts, fixture.journal, fixture.call = artifacts, journal, call
    fixture.generation, fixture.fragment = generation, fragment
    return fixture


def _restore(fixture, **overrides):
    return worker._restore_journal(fixture.db, fixture.config, fixture.binding, fixture.actor,
                                   **{"profile": fixture.profile, "artifacts": fixture.artifacts, **overrides})


def test_current_exact_child_recovers_but_same_version_source_rebind_quarantines(exact_journal):
    f = exact_journal
    assert _restore(f) == (str(f.working), 1)
    alternate = ChunkSet(parse_run_id=f.block.parse_run_id)
    f.db.add(alternate)
    f.db.flush()
    f.version.selected_chunk_set_id = alternate.id
    f.db.commit()
    assert _restore(f) == (None, 2)
    assert f.journal.state == "quarantined" and f.working.is_file()
    assert worker._journal_snapshot(f.db, f.config, f.journal)


@pytest.mark.parametrize("change", ["tools", "runtime_artifacts", "working", "snapshot", "legacy"])
def test_changed_profile_or_unprovable_body_cannot_restore(exact_journal, monkeypatch, change):
    f = exact_journal
    artifacts = deepcopy(f.artifacts)
    if change == "tools":
        monkeypatch.setattr(worker, "ENABLED_TOOLS", [*worker.ENABLED_TOOLS, {"name": "synthetic-drift"}])
    elif change == "runtime_artifacts":
        artifacts["artifact_manifest_hash"] = "4" * 64
    elif change == "working":
        f.working.write_bytes(b"synthetic-corrupt-work")
    elif change == "snapshot":
        (f.config.storage_dir / f"internal/runtime-journals/{f.journal.run_id}.jsonl").write_bytes(b"synthetic-corrupt-snapshot")
    else:
        f.journal.source_bindings = None
        f.journal.source_binding_hash = None
        assert f.journal.version_ids == [f.version.id]
        f.db.commit()
    assert _restore(f, artifacts=artifacts) == (None, 2)
    assert f.journal.state == "quarantined"


def test_expired_restore_and_cleanup_policy_preserve_all_referenced_bodies(exact_journal):
    f = exact_journal
    f.journal.recovery_expires_at = utc_now() - timedelta(seconds=1)
    f.db.add(TaskArtifact(task_id=f.task.id, kind="answer_candidate", file_object_id=f.journal.snapshot_file_id))
    f.db.commit()
    assert _restore(f) == (None, 2)
    f.db.flush()
    missing = worker.private_journal_cleanup_preflight(f.db, f.config, f.journal.id)
    assert missing["state"] == "policy_required" and not missing["deletion_authorized"]
    policy = {"policy_ref": "isolated-test-retention", "retain_until": utc_now() - timedelta(days=1), "legal_hold": False}
    plan = worker.private_journal_cleanup_preflight(f.db, f.config, f.journal.id, policy=policy)
    assert plan["state"] == "referenced" and plan["snapshot_integrity"] == "verified"
    assert {item["table"] for item in plan["snapshot_references"]} >= {"runtime_journals", "task_artifacts"}
    assert plan["model_response_call_refs"] == [f.call.id] and f.working.is_file()
    assert not plan["deletion_authorized"] and f.call.response_body == "SYNTHETIC private response"
