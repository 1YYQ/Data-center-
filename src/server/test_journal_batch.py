"""A journal checks all consumed child evidence once per validation boundary."""

from copy import deepcopy
from uuid import uuid4

from . import chunk_retrieval, governance, worker
from .execution import canonical, fingerprint
from .execution_models import ToolCallBinding, ToolInvocation
from .models import Chunk, ChunkBlock, Document, TaskEvidence
from .test_private_journal import exact_journal
from .test_runtime_gate import local_upstream, runtime_db


def _add_second_source(fixture):
    db = fixture.db
    original_id = next(iter(fixture.journal.source_bindings["sources"]))
    original = db.get(TaskEvidence, original_id)
    chunk_set_id, parse_run_id = original.chunk_set_id, fixture.block.parse_run_id
    group = deepcopy(fixture.chunk.stage_checkpoint["support_groups"][0])
    group_id, chunk_id = str(uuid4()), str(uuid4())
    member = {**group["members"][0], "block_end": 3}
    group.update(id=group_id, child_chunk_ids=[chunk_id], members=[member])
    fixture.chunk.stage_checkpoint = {"support_groups": [*fixture.chunk.stage_checkpoint["support_groups"], group]}
    chunk = Chunk(id=chunk_id, chunk_set_id=chunk_set_id, parse_run_id=parse_run_id, level="child", ordinal=1,
                  content=fixture.block.text_content[:3], content_hash="3" * 64, kind="paragraph",
                  eligibility="candidate", support_group_refs=[group_id])
    db.add(chunk)
    db.flush()
    segment = {key: value for key, value in member.items() if key != "block_id"}
    db.add(ChunkBlock(chunk_id=chunk_id, block_id=fixture.block.id, chunk_set_id=chunk_set_id,
                      parse_run_id=parse_run_id, ordinal=0, segments=[{**segment, "chunk_start": 0, "chunk_end": 3}]))
    db.flush()
    source = chunk_retrieval.current_child_manifest(db, fixture.kb, fixture.generation)["sources"][0]
    _, fragments = chunk_retrieval._group_output(db, group, chunk_id, source, fixture.generation,
                                                fixture.version, db.get(Document, fixture.version.document_id))
    fragment, = fragments
    evidence = TaskEvidence(task_id=fixture.task.id, execution_revision=fixture.task.execution_revision,
                            document_version_id=fixture.version.id, block_id=fixture.block.id,
                            quote_snapshot=fragment["quote"], locator=fragment["locator"], review_at_run="approved",
                            chunk_id=chunk_id, chunk_set_id=chunk_set_id, evidence_kind=fragment["evidence_kind"],
                            source_spans=fragment["source_spans"], asset_refs=fragment["asset_refs"],
                            source_binding_hash=fragment["source_binding_hash"], source_context={"fragment": fragment})
    db.add(evidence)
    db.flush()
    receipt = ToolInvocation(actor_user_id=fixture.actor.id, task_id=fixture.task.id, tool_name="dc_evidence_search",
                             logical_action_key="synthetic-journal-batch", args_hash="a" * 64,
                             execution_revision=fixture.task.execution_revision, state="completed",
                             evidence_ids=[original.id, evidence.id], output={"evidence": [{"id": original.id}, {"id": evidence.id}]})
    db.add(receipt)
    db.flush()
    db.add(ToolCallBinding(run_id=fixture.run.id, pi_tool_call_id="synthetic-journal-batch", invocation_id=receipt.id))
    db.commit()
    return evidence, fragment, receipt


def test_journal_batches_current_sources_and_preserves_manifest(exact_journal, monkeypatch):
    f = exact_journal
    evidence, fragment, receipt = _add_second_source(f)
    stored = f.journal.source_bindings
    first_source, = stored["sources"].values()
    second_source = {**first_source, "evidence_id": evidence.id, "evidence_row_hash": worker._snapshot(evidence)["row_digest"],
                     "source_binding_hash": fragment["source_binding_hash"], "fragment_hash": fingerprint(fragment),
                     "chunk_id": evidence.chunk_id, **{key: fragment["dependencies"][key] for key in
                                                          ("group_ref", "group_hash", "mapping_hash")}}
    expected = {**stored, "sources": {**stored["sources"], evidence.id: second_source},
                "tools": {receipt.id: worker._snapshot(receipt)["row_digest"]}}

    reads, batch_refs = [], []
    current_manifest, publication = chunk_retrieval.current_child_manifest, governance.publication_dependency_snapshot
    validate_fragments = chunk_retrieval.validate_child_fragments

    def observed_manifest(*args):
        reads.append("generation")
        return current_manifest(*args)

    def observed_publication(*args):
        reads.append("publication")
        return publication(*args)

    def observed_fragments(db, actor, fragments, **scope):
        batch_refs.append([item["id"] for item in fragments])
        return validate_fragments(db, actor, fragments, **scope)

    monkeypatch.setattr(chunk_retrieval, "current_child_manifest", observed_manifest)
    monkeypatch.setattr(governance, "publication_dependency_snapshot", observed_publication)
    monkeypatch.setattr(chunk_retrieval, "validate_child_fragments", observed_fragments)

    manifest, versions = worker._journal_dependencies(f.db, f.actor, f.task, f.run)
    assert canonical(manifest) == canonical(expected)
    assert versions == [f.version.id]
    assert reads == ["generation", "publication"]

    batch_refs.clear()
    inherited, versions = worker._journal_dependencies(f.db, f.actor, f.task, f.run, inherited=stored)
    assert (inherited, versions) == (expected, [f.version.id])
    assert batch_refs == [[fragment["id"]]]

    reads.clear()
    assert worker._journal_dependencies_current(f.db, manifest, f.task, f.actor)
    assert reads == ["generation", "publication"]

    evidence.quote_snapshot += " synthetic mismatch"
    f.db.commit()
    assert not worker._journal_dependencies_current(f.db, manifest, f.task, f.actor)
    incomplete, versions = worker._journal_dependencies(f.db, f.actor, f.task, f.run)
    assert (incomplete, versions) == ({**expected, "state": "incomplete", "sources": stored["sources"]}, [f.version.id])
