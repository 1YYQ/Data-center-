"""Independent child-index builds with a frozen source and embedding manifest."""
from __future__ import annotations

from collections import Counter
import hashlib
import re

from sqlalchemy import select, update

from . import governance, local_bge
from .chunk_jobs import chunk_set_qc_is_current
from .embedding_jobs import ChildEmbeddingBatch, canonical_embedding_input as embedding_input
from .governance_models import GovernanceOutbox, PublicationEvent
from .models import BlockRelation, Chunk, Document, DocumentVersion, IndexGeneration, IndexSource, KnowledgeBase, utc_now
from .parsing.chunking import TOKENIZER_REVISION, TOKENIZER_SHA256
from .parsing.local_pipeline import fingerprint
from .parsing.processing import source_verification
from .provider_transport import ProviderError
from .service_models import ServiceRevision
from .service_use import selected_revision
from .service_use_models import ServiceSelection
from .services import (SafeError, audit, can_access_document, document_creation_scopes, fence_management_authority,
                       selected_parse)
from .vector_store import ChunkEmbedding, stored_vector

KIND = "child-v1"
PURPOSE = "document.embed"
PREPROCESS = "chunk-content-verbatim/1"
MAX_BATCH_ITEMS = 32
MAX_BATCH_BYTES = 32 * 1024


def _deny(code, message="索引代际与当前资料或成员不匹配", status=409):
    raise SafeError(code, message, status)


def child_generation(generation):
    return bool(generation and generation.build_report.get("kind") == KIND)


def _profile(revision, binding_hash):
    if not revision or (revision.adapter, revision.model) not in {
        ("bge-m3-v1", "BAAI/bge-m3"), ("bge-m3-v1", "Pro/BAAI/bge-m3"), (local_bge.ADAPTER, local_bge.MODEL),
    }:
        raise ProviderError("INDEX_EMBEDDING_PROFILE_UNSUPPORTED")
    profile = {"service_revision_id": revision.id, "service_configuration_hash": revision.configuration_hash,
               "service_binding_hash": binding_hash, "adapter": revision.adapter, "model": revision.model,
               "dimensions": 1024, "distance": "cosine", "preprocess": PREPROCESS,
               "tokenizer_revision": TOKENIZER_REVISION, "tokenizer_sha256": TOKENIZER_SHA256}
    if revision.adapter == local_bge.ADAPTER:
        profile["local_model"] = local_bge.profile()
    return profile


def _selected_profile(db, profile_id, endpoints):
    revision, _, test, binding = selected_revision(db, PURPOSE, profile_id, endpoints)
    selection = db.scalar(select(ServiceSelection).where(ServiceSelection.purpose == PURPOSE)
                          .with_for_update(read=True).execution_options(populate_existing=True))
    if (not selection or not selection.enabled or selection.revision_id != revision.id
            or selection.test_run_id != test.id or selection.binding_hash != binding):
        raise ProviderError("INDEX_EMBEDDING_SELECTION_REQUIRED")
    return _profile(revision, binding)


def lock_child_library(db, kb_id, actor, *, request=None):
    # Source writers acquire Document before KB. Lock in their order, then
    # compare the catalog again under the KB revision fence.
    statement = select(Document).where(Document.knowledge_base_id == kb_id, Document.deleted_at.is_(None)).order_by(Document.id)
    initial = db.scalars(statement.with_for_update(read=True).execution_options(populate_existing=True)).all()
    kb = db.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_id)
                   .with_for_update().execution_options(populate_existing=True))
    if not kb or kb.status != "active":
        _deny("INDEX_LIBRARY_UNAVAILABLE")
    documents = db.scalars(statement.execution_options(populate_existing=True)).all()
    if [doc.id for doc in documents] != [doc.id for doc in initial]:
        _deny("INDEX_LIBRARY_CHANGED")
    actor = fence_management_authority(db, actor, kb_id, request=request)
    if not document_creation_scopes(db, actor, kb_id):
        _deny("INDEX_MANAGEMENT_SCOPE_REQUIRED", "需要知识库和全库资料管理权限", 403)
    for document in documents:
        fence_management_authority(db, actor, kb_id, document_id=document.id)
        if not can_access_document(db, actor, document, "knowledge.manage"):
            _deny("INDEX_MANAGEMENT_SCOPE_REQUIRED", "需要知识库和全库资料管理权限", 403)
    return kb, documents


def _support_refs(chunk):
    return list(chunk.support_group_refs)


def _chunk_entry(chunk, version):
    return {"id": chunk.id, "version_id": version.id, "run_id": chunk.parse_run_id, "chunk_set_id": chunk.chunk_set_id,
            "content_hash": chunk.content_hash, "token_count": chunk.token_count,
            "text_hash": hashlib.sha256(chunk.content.encode("utf-8")).hexdigest(),
            "support_group_refs": _support_refs(chunk), "relation_ids": list(chunk.required_relation_ids),
            "assembly_hash": fingerprint(chunk.assembly)}


def _source(db, document, version, profile, *, historical=False):
    source = {"document_id": document.id, "version_id": version.id, "selected_chunk_set_id": version.selected_chunk_set_id,
              "review_set_id": version.selected_review_set_id, "run_id": None, "reason": None, "excluded_chunks": []}
    selected = selected_parse(db, version)
    if not selected:
        return {**source, "reason": "selected_source_not_ready"}, []
    run, chunk_set = selected
    source["run_id"] = run.id
    if not chunk_set.config_hash:
        return {**source, "reason": "legacy_source_requires_child_reprocessing"}, []
    if not governance.publication_is_current(db, version, historical=historical) or source_verification(db, run)["state"] != "verified":
        return {**source, "reason": "source_verification_not_current"}, []
    if (not chunk_set_qc_is_current(db, chunk_set) or chunk_set.tokenizer_revision != profile["tokenizer_revision"]
            or chunk_set.splitter_config.get("tokenizer_sha256") != profile["tokenizer_sha256"]):
        return {**source, "reason": "collection_qc_or_tokenizer_not_current"}, []
    publication = db.scalar(select(PublicationEvent).where(PublicationEvent.document_version_id == version.id,
        PublicationEvent.action.in_(("publish", "source_rebind"))).order_by(PublicationEvent.created_at.desc(), PublicationEvent.id.desc()))
    if publication is None:
        return {**source, "reason": "publication_event_missing"}, []
    checkpoint = chunk_set.stage_checkpoint
    source.update({"publication_event_id": publication.id,
        "qualification_hash": governance.digest(governance.qualification_snapshot(db, version)),
        "ir_hash": chunk_set.ir_content_hash, "ir_artifact_id": run.ir_artifact_id,
        "source_qc_ref": run.stage_checkpoint.get("outputs", {}).get("source_qc"),
        "source_qc_hash": run.stage_checkpoint.get("outputs", {}).get("source_qc_sha256"),
        "collection_qc_ref": chunk_set.quality_artifact_id,
        "collection_qc_hash": checkpoint.get("outputs", {}).get("quality_sha256"),
        "chunk_config_hash": chunk_set.config_hash, "projection_hash": checkpoint.get("projection_fingerprint")})
    rows = db.scalars(select(Chunk).where(Chunk.chunk_set_id == chunk_set.id).order_by(Chunk.ordinal, Chunk.id)
                      .execution_options(populate_existing=True)).all()
    by_ref = {group["id"]: group for group in checkpoint.get("support_groups", [])}
    if not by_ref or len(by_ref) != len(checkpoint.get("support_groups", [])):
        return {**source, "reason": "support_groups_missing_or_duplicate"}, []
    eligible = []
    members_by_group = {group_ref: set() for group_ref in by_ref}
    covered = set()
    regions = set()
    for chunk in rows:
        refs = _support_refs(chunk)
        if (chunk.parse_run_id != run.id or len(refs) != 1 or not set(refs) <= by_ref.keys()
                or chunk.eligibility not in {"candidate", "navigation_only", "asset_only", "oversized"}):
            return {**source, "reason": "child_content_or_support_invalid"}, []
        for ref in refs:
            if chunk.level == "child":
                members_by_group[ref].add(chunk.id)
        if chunk.level == "child" and chunk.eligibility == "candidate":
            if (not chunk.content or not chunk.content.strip() or not 1 <= (chunk.token_count or 0) <= 1536
                    or len(chunk.content.encode("utf-8")) > 8192 or len(embedding_input([chunk.content])) > MAX_BATCH_BYTES
                    or not re.fullmatch(r"[a-f0-9]{64}", chunk.content_hash)):
                return {**source, "reason": "child_content_or_support_invalid"}, []
            eligible.append((chunk, _chunk_entry(chunk, version)))
            covered.update(refs)
        else:
            if chunk.eligibility == "asset_only":
                regions.update(refs)
            source["excluded_chunks"].append({"id": chunk.id, "level": chunk.level, "eligibility": chunk.eligibility,
                                              "support_group_refs": refs})
    if any(group.get("parse_run_id") != run.id or group.get("complete") is not True or group.get("predecessor_refs") is None
           or Counter(group.get("child_chunk_ids", [])) != Counter(members_by_group[ref])
           or not set(group["predecessor_refs"]) <= by_ref.keys() for ref, group in by_ref.items()):
        return {**source, "reason": "support_group_membership_incomplete"}, []
    if any(not any(m.get("kind") == "region" and m.get("role") == "body" for m in by_ref[ref]["members"])
           for ref in regions - covered):
        return {**source, "reason": "asset_only_group_has_no_verified_region"}, []
    useful = [by_ref[ref] for ref in covered | regions]
    matched_blocks = {member["block_id"] for group in useful for member in group["members"]}
    matched_relations = {relation for group in useful for relation in group["required_relation_ids"]}
    source_qc = run.stage_checkpoint.get("source_qc", {})
    missing_blocks = sorted(set(source_qc.get("critical_refs", {})) - matched_blocks)
    required_relations = set(source_qc.get("required_relation_refs", []))
    observation_relations = set(db.scalars(select(BlockRelation.id).where(BlockRelation.id.in_(required_relations),
        BlockRelation.parse_run_id == run.id, BlockRelation.relation_type == "covered_by")))
    missing_relations = sorted(required_relations - matched_relations - observation_relations)
    if missing_blocks or missing_relations or any(row.eligibility == "oversized" for row in rows):
        return {**source, "reason": "required_source_coverage_incomplete", "missing_block_refs": missing_blocks,
                "missing_relation_refs": missing_relations}, []
    source["unindexed_group_refs"] = sorted(by_ref.keys() - covered)
    source["asset_only_group_refs"] = sorted(regions - covered)
    source["observation_relation_refs"] = sorted(observation_relations)
    source["coverage_hash"] = fingerprint({"quality": checkpoint["quality"], "covered_groups": sorted(covered)})
    return source, eligible


def _library_manifest(db, kb, profile):
    versions = db.execute(select(Document, DocumentVersion).join(DocumentVersion, DocumentVersion.document_id == Document.id)
        .where(Document.knowledge_base_id == kb.id, Document.deleted_at.is_(None))
        .order_by(Document.id, DocumentVersion.id).execution_options(populate_existing=True)).all()
    effective = Counter(doc.id for doc, version in versions if version.publish_status == "effective")
    sources, excluded, members, texts = [], [], [], {}
    for document, version in versions:
        if version.publish_status != "effective":
            excluded.append({"version_id": version.id, "publish_status": version.publish_status})
            continue
        item, children = _source(db, document, version, profile)
        if effective[document.id] != 1:
            item["reason"], children = "multiple_effective_versions", []
        sources.append(item)
        for chunk, entry in children:
            members.append(entry)
            texts[chunk.id] = chunk.content
    batches, ids, inputs = [], [], []

    def seal_batch():
        if ids:
            batches.append({"batch_no": len(batches) + 1, "child_ids": list(ids),
                            "input_hash": hashlib.sha256(embedding_input(inputs)).hexdigest()})

    for member in members:
        text = texts[member["id"]]
        if len(ids) >= MAX_BATCH_ITEMS or len(embedding_input([*inputs, text])) > MAX_BATCH_BYTES:
            seal_batch()
            ids, inputs = [], []
        ids.append(member["id"])
        inputs.append(text)
    seal_batch()
    return {"knowledge_base_id": kb.id, "source_revision": kb.content_revision, "profile": profile,
            "sources": sources, "members": members, "batches": batches}, excluded


def create_child_generation(db, kb, actor, *, embedding_profile_id, endpoints, request=None):
    """Freeze a building generation. Service admission and activation are separate."""
    if db.get_bind().dialect.name != "postgresql":
        _deny("INDEX_VECTOR_STORE_REQUIRED")
    kb, _ = lock_child_library(db, kb.id, actor, request=request)
    profile = _selected_profile(db, embedding_profile_id, endpoints)
    manifest, excluded = _library_manifest(db, kb, profile)
    blockers = [{"version_id": item["version_id"], "reason": item["reason"]} for item in manifest["sources"] if item["reason"]]
    if not manifest["sources"]:
        blockers.append({"reason": "no_current_published_sources"})
    elif not blockers and not manifest["members"]:
        blockers.append({"reason": "no_verified_vector_child"})
    generation = IndexGeneration(knowledge_base_id=kb.id, embedding_profile_id=embedding_profile_id,
        source_revision=kb.content_revision, status="building", expected_chunk_count=len(manifest["members"]),
        build_report={"kind": KIND, "actor_id": actor.id, "manifest": manifest, "manifest_hash": fingerprint(manifest),
                      "excluded_versions": excluded, "integrity": {"status": "blocked" if blockers else "pending",
                      "blockers": blockers}})
    db.add(generation)
    db.flush()
    db.add_all(IndexSource(index_generation_id=generation.id, document_version_id=item["version_id"],
                          parse_run_id=item["run_id"], chunk_set_id=item["selected_chunk_set_id"])
               for item in manifest["sources"] if item["reason"] is None)
    db.add(GovernanceOutbox(event_kind="index.build_requested", resource_id=generation.id, revision=generation.source_revision))
    audit(db, actor.id, "index.build_accepted", "index_generation", generation.id, source_revision=generation.source_revision)
    db.flush()
    return generation


def lock_child_generation(db, generation_id, actor, *, request=None):
    row = db.get(IndexGeneration, generation_id, populate_existing=True)
    if not child_generation(row):
        _deny("INDEX_GENERATION_NOT_FOUND", "未找到子块索引代际", 404)
    kb, _ = lock_child_library(db, row.knowledge_base_id, actor, request=request)
    generation = db.scalar(select(IndexGeneration).where(IndexGeneration.id == generation_id)
                           .with_for_update().execution_options(populate_existing=True))
    return kb, generation


def current_child_manifest(db, kb, generation):
    if (not child_generation(generation) or generation.knowledge_base_id != kb.id
            or generation.source_revision != kb.content_revision or kb.status != "active"):
        _deny("INDEX_GENERATION_STALE")
    report = generation.build_report
    manifest = report.get("manifest")
    if (not isinstance(manifest, dict) or set(manifest) != {"knowledge_base_id", "source_revision", "profile",
            "sources", "members", "batches"} or not isinstance(manifest["profile"], dict)
            or not all(isinstance(manifest[key], list) for key in ("sources", "members", "batches"))
            or fingerprint(manifest) != report.get("manifest_hash")):
        _deny("INDEX_MANIFEST_CHANGED")
    if not manifest["sources"] or not manifest["members"] or any(item["reason"] for item in manifest["sources"]):
        _deny("INDEX_SOURCE_INCOMPLETE")
    profile = manifest["profile"]
    if (generation.expected_chunk_count != len(manifest["members"])
            or profile.get("service_revision_id") != generation.embedding_profile_id):
        _deny("INDEX_MANIFEST_CHANGED")
    revision = db.get(ServiceRevision, generation.embedding_profile_id, populate_existing=True)
    if _profile(revision, profile["service_binding_hash"]) != profile:
        _deny("INDEX_EMBEDDING_PROFILE_CHANGED")
    current, _ = _library_manifest(db, kb, profile)
    if current != manifest:
        _deny("INDEX_GENERATION_STALE")
    records = db.execute(select(IndexSource.document_version_id, IndexSource.parse_run_id, IndexSource.chunk_set_id)
                         .where(IndexSource.index_generation_id == generation.id)).all()
    expected = [(item["version_id"], item["run_id"], item["selected_chunk_set_id"]) for item in manifest["sources"]]
    if Counter(map(tuple, records)) != Counter(expected):
        _deny("INDEX_SOURCE_MEMBERSHIP_CHANGED")
    return manifest


def _embedding_identity(entry, profile_hash):
    return {"chunk_set_id": entry["chunk_set_id"], "parse_run_id": entry["run_id"],
            "document_version_id": entry["version_id"], "entry_hash": fingerprint(entry), "profile_hash": profile_hash}


def _match_embedding(row, entry, profile_hash):
    if row.chunk_id != entry["id"] or any(getattr(row, key) != value
            for key, value in _embedding_identity(entry, profile_hash).items()):
        raise ValueError("Vector membership does not match the frozen generation")
    return stored_vector(row.embedding)


def persist_child_embeddings(db, generation, entries, values):
    """Application-owned worker writes from one admitted provider response."""
    if generation.status != "building":
        _deny("INDEX_GENERATION_NOT_BUILDING")
    profile_hash = fingerprint(generation.build_report["manifest"]["profile"])
    for entry, vector in zip(entries, values, strict=True):
        vector = stored_vector(vector)
        prior = db.get(ChunkEmbedding, (generation.id, entry["id"]))
        if prior:
            if _match_embedding(prior, entry, profile_hash) != vector:
                raise ValueError("An established generation vector cannot be overwritten")
            continue
        db.add(ChunkEmbedding(generation_id=generation.id, chunk_id=entry["id"], embedding=vector,
                              **_embedding_identity(entry, profile_hash)))
    db.flush()


def child_vector_integrity(db, generation, manifest):
    expected = {entry["id"]: entry for entry in manifest["members"]}
    seen, invalid = set(), set()
    profile_hash = fingerprint(manifest["profile"])
    for row in db.scalars(select(ChunkEmbedding).where(ChunkEmbedding.generation_id == generation.id)
                         .execution_options(yield_per=128, populate_existing=True)):
        if row.chunk_id in seen:
            invalid.add(row.chunk_id)
        seen.add(row.chunk_id)
        if row.chunk_id in expected:
            try:
                _match_embedding(row, expected[row.chunk_id], profile_hash)
            except (ValueError, TypeError):
                invalid.add(row.chunk_id)
    missing = sorted(expected.keys() - seen)
    extra = sorted(seen - expected.keys())
    return {"status": "passed" if expected and not (missing or extra or invalid) else "incomplete",
            "expected_count": len(expected), "actual_count": len(seen), "missing_child_refs": missing,
            "unexpected_child_refs": extra, "invalid_child_refs": sorted(invalid)}


def finalize_child_generation(db, generation, actor, *, request=None):
    kb, generation = lock_child_generation(db, generation.id, actor, request=request)
    if generation.status != "building":
        return generation
    try:
        manifest = current_child_manifest(db, kb, generation)
        integrity = child_vector_integrity(db, generation, manifest)
    except (SafeError, ProviderError) as error:
        integrity = {**generation.build_report.get("integrity", {}), "status": "failed", "error_code": error.code}
        generation.status = "failed"
    else:
        if integrity["status"] == "passed":
            generation.status, generation.ready_at = "ready", utc_now()
        elif integrity["unexpected_child_refs"] or integrity["invalid_child_refs"]:
            generation.status = "failed"
    generation.build_report = {**generation.build_report, "integrity": integrity}
    if generation.status != "building":
        audit(db, actor.id, "index.build_" + generation.status, "index_generation", generation.id,
              source_revision=generation.source_revision, manifest_hash=generation.build_report["manifest_hash"])
    db.flush()
    return generation


def switch_child_generation(db, kb, generation_id, actor, *, expected_active_id, expected_source_revision,
                            action="activate", request=None):
    """An explicit activation or rollback. HTTP owns the operation's idempotency."""
    if action not in {"activate", "rollback"}:
        _deny("INDEX_SWITCH_ACTION_INVALID", status=422)
    expected_kb_id = kb.id
    target = db.get(IndexGeneration, generation_id, populate_existing=True)
    if not child_generation(target) or target.knowledge_base_id != expected_kb_id:
        _deny("INDEX_GENERATION_NOT_FOUND", "未找到子块索引代际", 404)
    kb, generation = lock_child_generation(db, generation_id, actor, request=request)
    if kb.id != expected_kb_id:
        _deny("INDEX_GENERATION_NOT_FOUND", "未找到子块索引代际", 404)
    if kb.active_generation_id != expected_active_id or kb.content_revision != expected_source_revision:
        _deny("INDEX_PRECONDITION_CHANGED", "知识库版本或活动代际已变化", 412)
    integrity = generation.build_report.get("integrity", {})
    if generation.status != "ready" or not generation.ready_at or integrity.get("status") != "passed":
        _deny("INDEX_GENERATION_NOT_READY")
    manifest = current_child_manifest(db, kb, generation)
    if child_vector_integrity(db, generation, manifest) != integrity:
        _deny("INDEX_VECTOR_MEMBERSHIP_CHANGED")
    if action == "rollback" and not generation.build_report.get("activation_count"):
        _deny("INDEX_ROLLBACK_TARGET_NOT_PREVIOUSLY_ACTIVE")
    if expected_active_id == generation.id:
        return generation
    changed = db.execute(update(KnowledgeBase).where(KnowledgeBase.id == kb.id,
        KnowledgeBase.content_revision == expected_source_revision, KnowledgeBase.active_generation_id == expected_active_id)
        .values(active_generation_id=generation.id).execution_options(synchronize_session=False))
    if changed.rowcount != 1:
        _deny("INDEX_PRECONDITION_CHANGED", "知识库版本或活动代际已变化", 412)
    db.expire(kb, ["active_generation_id"])
    report = generation.build_report
    generation.build_report = {**report, "activation_count": report.get("activation_count", 0) + 1}
    action_name = "index.rolled_back" if action == "rollback" else "index.activated"
    db.add(GovernanceOutbox(event_kind=action_name, resource_id=generation.id, revision=generation.source_revision))
    audit(db, actor.id, action_name, "index_generation", generation.id, previous_generation_id=expected_active_id,
          profile_id=generation.embedding_profile_id, manifest_hash=report["manifest_hash"], source_revision=generation.source_revision)
    db.flush()
    return generation


def child_generation_view(db, generation):
    report = generation.build_report
    kb = db.get(KnowledgeBase, generation.knowledge_base_id, populate_existing=True)
    batches = db.scalars(select(ChildEmbeddingBatch).where(ChildEmbeddingBatch.generation_id == generation.id)
                         .order_by(ChildEmbeddingBatch.batch_no)) if (child_generation(generation)
                             and db.get_bind().dialect.name == "postgresql") else ()
    return {"id": generation.id, "knowledge_base_id": generation.knowledge_base_id,
            "active": kb.active_generation_id == generation.id, "source_revision": generation.source_revision,
            "status": generation.status, "stale": kb.content_revision != generation.source_revision,
            "kind": report.get("kind"), "embedding_profile_id": generation.embedding_profile_id,
            "expected_chunk_count": generation.expected_chunk_count, "manifest_hash": report.get("manifest_hash"),
            "batch_count": len(report.get("manifest", {}).get("batches", [])), "integrity": report.get("integrity"),
            "embedding_batches": [{"id": row.id, "batch_no": row.batch_no, "state": row.state,
                                   "error_code": row.error_code} for row in batches]}
