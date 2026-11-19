"""Retrieve complete, current child support groups and replay exact evidence spans."""
from __future__ import annotations

from collections import defaultdict
import math
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from . import governance, local_bge
from .indexing import _embedding_identity, _source as _index_source, child_generation, current_child_manifest, embedding_input
from .models import (BlockRelation, Chunk, ChunkBlock, ChunkSet, ContentBlock, Document, DocumentPage, DocumentVersion,
                     FileObject, IndexGeneration, IndexSource, KnowledgeBase, ParseArtifact, User)
from .parsing.local_pipeline import ParseContractError, fingerprint
from .parsing.processing import relation_binding
from .parsing.structure import _part_value
from .provider_transport import ProviderError
from .service_admission import binding, digest
from .service_models import ServiceProfile, ServiceRevision
from .services import SafeError, can_access_document, can_query_kb, split_terms
from .vector_store import ChunkEmbedding, stored_vector


class _InvalidSource(ValueError):
    pass


def _result(mode, *, warnings=()):
    return {"mode": mode, "hits": [], "support_groups": [], "fragments": [], "warnings": list(warnings)}


def _eligible(db, actor, version, document, model, context):
    return (version is not None and document is not None and document.deleted_at is None and model in version.model_scope
            and governance.published_query_is_readable(db, actor, version, document, context=context))


def _group_order(groups, root, consumed):
    """Dependencies precede the selected group, with a bounded cycle check."""
    pending = [(root, False)]
    visiting, done, result = set(), set(consumed), []
    while pending:
        identifier, postorder = pending.pop()
        if identifier in done:
            continue
        group = groups.get(identifier)
        if not group or group.get("complete") is not True or group.get("predecessor_refs") is None:
            raise _InvalidSource("Unverified support group")
        if postorder:
            visiting.remove(identifier)
            done.add(identifier)
            result.append(group)
            continue
        if identifier in visiting or len(visiting) + len(result) >= 128:
            raise _InvalidSource("Invalid support group order")
        visiting.add(identifier)
        pending.append((identifier, True))
        pending.extend((before, False) for before in reversed(group["predecessor_refs"]))
    return result


def _resolve_region(db, block, member):
    region_ref = member["region_ref"]
    if region_ref.startswith("/locator/"):
        region = _part_value({"structured_content": {"locator": block.locator}}, region_ref)
    else:
        region = _part_value({"structured_content": block.structured_content}, region_ref)
    artifact = db.get(ParseArtifact, member["asset_ref"], populate_existing=True)
    file = db.get(FileObject, artifact.file_id, populate_existing=True) if artifact else None
    if (not isinstance(region, dict) or region.get("file_page") != block.file_page
            or region.get("geometry_status") not in {"exact", "table_region_only", "page_only"}
            or not artifact or artifact.parse_run_id != block.parse_run_id
            or artifact.kind not in {"image", "page_preview", "region_crop"} or not file or file.status != "available"):
        raise _InvalidSource("Region or asset outside admitted source")
    return region, file.sha256


def _candidate_mappings(db, group, source, preferred_child):
    child_ids = group["child_chunk_ids"]
    if not child_ids or len(child_ids) != len(set(child_ids)):
        raise _InvalidSource("Missing or repeated support child")
    chunks = {row.id: row for row in db.scalars(select(Chunk).where(Chunk.id.in_(child_ids))
             .execution_options(populate_existing=True))}
    if (set(chunks) != set(child_ids) or any(chunk.chunk_set_id != source["selected_chunk_set_id"]
            or chunk.parse_run_id != source["run_id"] or chunk.level != "child"
            or chunk.support_group_refs != [group["id"]]
            or chunk.eligibility not in {"candidate", "asset_only", "navigation_only"} for chunk in chunks.values())):
        raise _InvalidSource("Support child membership changed")
    lookup = defaultdict(list)
    rows = db.scalars(select(ChunkBlock).where(ChunkBlock.chunk_id.in_(child_ids))
                      .order_by(ChunkBlock.ordinal, ChunkBlock.block_id).execution_options(populate_existing=True)).all()
    for row in sorted(rows, key=lambda row: (row.chunk_id != preferred_child, chunks[row.chunk_id].ordinal)):
        if row.chunk_set_id != source["selected_chunk_set_id"] or row.parse_run_id != source["run_id"]:
            raise _InvalidSource("Chunk mapping outside indexed source")
        lookup[row.block_id].extend((chunks[row.chunk_id], segment) for segment in row.segments)
    return lookup


def _replay(member, block, mappings):
    """Replay a group member. A frozen span may cross two source child mappings."""
    compatible = [(child, segment) for child, segment in mappings if segment["kind"] == member["kind"]
        and (segment["role"] == member["role"] or child.kind == "formula_definition"
             and segment["role"] == "body" and member["role"] == "definition")
        and all(segment.get(key) == member.get(key) for key in ("relation_id", "step_number", "cell_range"))]
    if member["kind"] == "region":
        for child, segment in compatible:
            if (segment.get("locator_ref"), segment.get("asset_ref")) == (member["region_ref"], member["asset_ref"]):
                return [(child, segment, None, None, None)]
        raise _InvalidSource("Missing exact region membership")
    if member["kind"] != "text":
        raise _InvalidSource("Unknown evidence member type")
    start, end = member["block_start"], member["block_end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(block.text_content):
        raise _InvalidSource("Invalid code point range")
    cursor, fragments = start, []
    while cursor < end:
        options = [(child, segment) for child, segment in compatible
                   if segment["block_start"] <= cursor < segment["block_end"]]
        if not options:
            raise _InvalidSource("Gap in child mappings for admitted span")
        child, segment = max(options, key=lambda pair: min(end, pair[1]["block_end"]))
        first, last = segment["chunk_start"], segment["chunk_end"]
        low, high = segment["block_start"], segment["block_end"]
        if (not child.content or not 0 <= first < last <= len(child.content) or last - first != high - low
                or not 0 <= low < high <= len(block.text_content)
                or child.content[first:last] != block.text_content[low:high]):
            raise _InvalidSource("Chunk text no longer matches original mapping")
        stop = min(end, high)
        fragments.append((child, segment, cursor, stop, block.text_content[cursor:stop]))
        cursor = stop
    return fragments


def _group_output(db, group, preferred_child, source, generation, version, document):
    if group["parse_run_id"] != source["run_id"]:
        raise _InvalidSource("Group outside selected parse run")
    lookup = _candidate_mappings(db, group, source, preferred_child)
    relation_refs = group["required_relation_ids"]
    if len(set(relation_refs)) != len(relation_refs):
        raise _InvalidSource("Duplicated relationship")
    relations = []
    for relation_ref in relation_refs:
        row = db.get(BlockRelation, relation_ref, populate_existing=True)
        if not row or row.parse_run_id != source["run_id"]:
            raise _InvalidSource("Relationship is unavailable")
        relations.append({"id": row.id, "relation_type": row.relation_type, "binding_hash": relation_binding(row)})
    dependency = {
        "knowledge_base_id": generation.knowledge_base_id, "generation_id": generation.id,
        "generation_manifest_hash": generation.build_report["manifest_hash"], "source_revision": generation.source_revision,
        "embedding_profile_id": generation.embedding_profile_id, "source": source, "group_ref": group["id"],
        "group_hash": fingerprint(group), "relation_bindings": relations,
    }
    fragments, members = [], []
    for member in group["members"]:
        block = db.get(ContentBlock, member["block_id"], populate_existing=True)
        page = db.get(DocumentPage, (source["run_id"], block.file_page), populate_existing=True) if block and block.file_page else None
        if (not block or block.parse_run_id != source["run_id"] or block.review_status != "approved"
                or not block.locator or not page or page.file_page != block.file_page):
            raise _InvalidSource("Evidence block or page is unavailable")
        for child, segment, start, end, quote in _replay(member, block, lookup[block.id]):
            locator = {**block.locator, "file_page": page.file_page}
            if page.printed_page is not None:
                locator["printed_page"] = page.printed_page
            asset_hash = None
            if member["kind"] == "region":
                region, asset_hash = _resolve_region(db, block, member)
                locator["region"] = region
            if member.get("cell_range") is not None:
                locator["cell_range"] = member["cell_range"]
            span = {**member, "block_start": start, "block_end": end} if quote is not None else member
            binding = fingerprint({**dependency, "chunk_id": child.id, "content_hash": child.content_hash,
                "mapping_hash": fingerprint(segment), "block_hash": governance.block_binding(block), "mapped_span": span,
                "quote": quote, "locator": locator, "asset_hash": asset_hash})
            fragment_ref = str(uuid5(NAMESPACE_URL, "dc.child-evidence/" + binding))
            fragments.append({"id": fragment_ref, "evidence_kind": member["kind"], "role": member["role"],
                "step_number": member.get("step_number"), "document_id": document.id, "document_title": document.title,
                "document_version_id": version.id, "version_label": version.revision_label, "parse_run_id": source["run_id"],
                "ir_hash": source["ir_hash"], "chunk_set_id": child.chunk_set_id, "chunk_id": child.id, "block_id": block.id,
                "file_page": page.file_page, "printed_page": page.printed_page, "section": locator.get("section"),
                "quote": quote or "", "text": quote, "source_spans": [span], "locator": locator,
                "block_start": start, "block_end": end, "region_ref": member.get("region_ref"), "asset_ref": member.get("asset_ref"),
                "asset_refs": [member["asset_ref"]] if member.get("asset_ref") else [],
                "source_binding_hash": binding, "dependencies": {"generation_id": generation.id,
                    "generation_manifest_hash": dependency["generation_manifest_hash"],
                    "source_revision": generation.source_revision, "embedding_profile_id": generation.embedding_profile_id,
                    "source_qc_ref": source["source_qc_ref"], "collection_qc_ref": source["collection_qc_ref"],
                    "review_set_id": source["review_set_id"], "publication_event_id": source["publication_event_id"],
                    "group_ref": group["id"], "group_hash": dependency["group_hash"], "mapping_hash": fingerprint(segment),
                    "content_hash": child.content_hash}})
            members.append({"evidence_ref": fragment_ref, "role": member["role"], "step_number": member.get("step_number"),
                            "source_binding_hash": binding})
    if not fragments or len({item["id"] for item in fragments}) != len(fragments):
        raise _InvalidSource("Support group returned empty or repeated fragments")
    return ({"id": group["id"], "kind": group["kind"], "scope_key": group["scope_key"],
             "structure_root_id": group["structure_root_id"], "complete": True,
             "members": members, "ordered_step_refs": group["ordered_step_refs"], "predecessor_refs": group["predecessor_refs"],
             "relations": relations, "source": source}, fragments)


def _ranked(db, libraries, actor, query, model, context, query_vector, profile_id):
    terms = split_terms(query) if query_vector is None else set()
    ranked = []
    for kb, generation, manifest in libraries:
        if query_vector is not None and generation.embedding_profile_id != profile_id:
            raise _InvalidSource("Wrong vector space")
        sources = {}
        for source in manifest["sources"]:
            version = db.get(DocumentVersion, source["version_id"], populate_existing=True)
            document = db.get(Document, source["document_id"], populate_existing=True)
            if _eligible(db, actor, version, document, model, context):
                sources[version.id] = (source, version, document)
        by_chunk = {item["id"]: item for item in manifest["members"] if item["version_id"] in sources}
        if not by_chunk:
            continue
        statement = select(Chunk).where(Chunk.id.in_(by_chunk))
        if query_vector is None:
            for chunk in db.scalars(statement.execution_options(populate_existing=True)):
                text = chunk.content.casefold()
                score = sum(term in text for term in terms)
                if score:
                    ranked.append((float(score), chunk, generation, *sources[by_chunk[chunk.id]["version_id"]]))
        else:
            distance = ChunkEmbedding.embedding.cosine_distance(query_vector)
            profile_hash = fingerprint(manifest["profile"])
            vectors = db.execute(statement.add_columns(distance, ChunkEmbedding.chunk_set_id, ChunkEmbedding.parse_run_id,
                ChunkEmbedding.document_version_id, ChunkEmbedding.entry_hash, ChunkEmbedding.profile_hash).join(ChunkEmbedding,
                (ChunkEmbedding.chunk_id == Chunk.id) & (ChunkEmbedding.generation_id == generation.id))
                .execution_options(populate_existing=True)).all()
            if len(vectors) != len(by_chunk):
                raise _InvalidSource("Vector membership changed")
            for chunk, score, *identity in vectors:
                if not math.isfinite(score) or tuple(_embedding_identity(by_chunk[chunk.id], profile_hash).values()) != tuple(identity):
                    raise _InvalidSource("Incorrect active vector identity")
                ranked.append((1.0 - float(score), chunk, generation, *sources[by_chunk[chunk.id]["version_id"]]))
    return sorted(ranked, key=lambda row: (-row[0], row[2].id, row[1].ordinal, row[1].id))


def _local_query_vector(db, query, profile):
    revision = db.get(ServiceRevision, profile["service_revision_id"], populate_existing=True)
    owner = db.get(ServiceProfile, revision.profile_id, populate_existing=True) if revision else None
    endpoint = local_bge.read_model_endpoint()
    if (not owner or owner.kind != "embedding" or not owner.enabled or not endpoint
            or binding(revision, {local_bge.ENDPOINT_REF: endpoint}) != profile["service_binding_hash"]):
        raise ProviderError("LOCAL_BGE_PROFILE_UNAVAILABLE")
    epoch = owner.use_epoch
    vector, = endpoint.embed([query])["vectors"]
    db.refresh(owner)
    if not owner.enabled or owner.use_epoch != epoch:
        raise ProviderError("LOCAL_BGE_PROFILE_UNAVAILABLE")
    return stored_vector(vector), {"method": "pgvector_cosine", "service_revision_id": revision.id,
        "service_binding_hash": profile["service_binding_hash"], "local_model": profile["local_model"],
        "query_sha256": digest(embedding_input([query])), "vector_sha256": digest(vector)}


def search_child_chunks(db, actor, query, *, model, kb_ids, context=None, limit=12, max_bytes=16000,
                        query_vector=None, embedding_profile_id=None):
    """Return whole support groups or a safe unavailable state, with no index writes."""
    if not kb_ids or not model or not 1 <= limit <= 100 or not 1 <= max_bytes <= 262144:
        raise ValueError("Bounded query scope, model and evidence budget are required")
    if (query_vector is None) != (embedding_profile_id is None):
        raise ValueError("A vector requires its exact embedding profile")
    actor = db.get(User, actor.id, populate_existing=True)
    libraries, modes = [], set()
    if not actor or actor.status != "active":
        return _result("unavailable", warnings=["access_limited"])
    for kb_id in sorted(set(kb_ids)):
        kb = db.get(KnowledgeBase, kb_id, populate_existing=True)
        if not kb or kb.status != "active" or not can_query_kb(db, actor, kb_id):
            return _result("unavailable", warnings=["access_limited"])
        generation = db.get(IndexGeneration, kb.active_generation_id, populate_existing=True) if kb.active_generation_id else None
        if not generation or generation.knowledge_base_id != kb_id or generation.status != "ready":
            return _result("unavailable", warnings=["index_unavailable"])
        if generation.source_revision != kb.content_revision:
            return _result("unavailable", warnings=["index_stale"])
        if generation.build_report.get("kind") not in (None, "legacy") and not child_generation(generation):
            return _result("unavailable", warnings=["index_unavailable"])
        modes.add("child" if child_generation(generation) else "legacy")
        libraries.append((kb, generation))
    if len(modes) != 1:
        return _result("unavailable", warnings=["mixed_index_modes"])
    if modes == {"legacy"}:
        return _result("legacy")
    if db.get_bind().dialect.name != "postgresql":
        return _result("unavailable", warnings=["index_unavailable"])
    result = _result("child")
    consumed, size = set(), 0
    try:
        if query_vector is not None:
            query_vector = stored_vector(query_vector)
        manifests = []
        fences = {}
        for kb, generation in libraries:
            if generation.build_report.get("integrity", {}).get("status") != "passed" or not generation.ready_at:
                raise _InvalidSource("A ready generation has no complete integrity report")
            manifests.append((kb, generation, current_child_manifest(db, kb, generation)))
            fences[kb.id] = (kb.content_revision, kb.active_generation_id, kb.acl_revision)
        local_profiles = [manifest["profile"] for _, _, manifest in manifests if manifest["profile"]["adapter"] == local_bge.ADAPTER]
        if local_profiles:
            profile = local_profiles[0]
            if (query_vector is not None or len(local_profiles) != len(manifests)
                    or any(candidate != profile for candidate in local_profiles)):
                return _result("unavailable", warnings=["mixed_index_modes"])
            try:
                query_vector, receipt = _local_query_vector(db, query, profile)
            except ProviderError:
                return _result("unavailable", warnings=["index_not_ready"])
            embedding_profile_id = profile["service_revision_id"]
            result["retrieval_receipt"] = {**receipt, "generation_ids": [row.id for _, row, _ in manifests]}
        ranked = _ranked(db, manifests, actor, query, model, context, query_vector, embedding_profile_id)
        if "retrieval_receipt" in result:
            result["retrieval_receipt"]["ranked_candidate_count"] = len(ranked)
        for score, chunk, generation, source, version, document in ranked:
            group_ref, = chunk.support_group_refs
            if group_ref in consumed:
                continue
            chunk_set = db.get(ChunkSet, source["selected_chunk_set_id"], populate_existing=True)
            groups = {item["id"]: item for item in chunk_set.stage_checkpoint["support_groups"]}
            if not _eligible(db, actor, version, document, model, context):
                return _result("unavailable", warnings=["source_access_changed"])
            group_results, fragments = [], []
            for group in _group_order(groups, group_ref, consumed):
                group_result, group_fragments = _group_output(db, group, chunk.id, source, generation, version, document)
                group_results.append(group_result)
                fragments.extend(group_fragments)
            from .evidence_projection import support_projection_size
            group_size = support_projection_size(group_results, fragments)
            if size + group_size > max_bytes or len(result["support_groups"]) + len(group_results) > limit:
                if "evidence_budget" not in result["warnings"]:
                    result["warnings"].append("evidence_budget")
                continue
            hit = {"chunk_id": chunk.id, "parent_chunk_id": chunk.parent_chunk_id, "support_group_ref": group_ref,
                   "document_version_id": version.id, "generation_id": generation.id, "score": score}
            size += group_size
            consumed.update(group["id"] for group in group_results)
            result["hits"].append(hit)
            result["support_groups"].extend(group_results)
            result["fragments"].extend(fragments)
            if len(result["support_groups"]) >= limit:
                break
        for kb_id, expected in fences.items():
            kb = db.get(KnowledgeBase, kb_id, populate_existing=True)
            if not kb or kb.status != "active" or (kb.content_revision, kb.active_generation_id, kb.acl_revision) != expected:
                return _result("unavailable", warnings=["index_stale"])
    except (ProviderError, SafeError, ParseContractError, KeyError, TypeError, ValueError):
        return _result("unavailable", warnings=["index_stale"])
    return result


def _saved_manifest(kb, generation):
    report = generation.build_report
    manifest = report["manifest"]
    if (not isinstance(manifest, dict) or set(manifest) != {"knowledge_base_id", "source_revision", "profile", "sources", "members", "batches"}
            or not isinstance(manifest["profile"], dict) or not all(isinstance(manifest[key], list) for key in ("sources", "members", "batches"))
            or manifest["knowledge_base_id"] != kb.id or manifest["source_revision"] != generation.source_revision
            or manifest["profile"]["service_revision_id"] != generation.embedding_profile_id
            or not manifest["sources"] or any(source["reason"] for source in manifest["sources"])
            or not manifest["members"] or generation.expected_chunk_count != len(manifest["members"])
            or fingerprint(manifest) != report["manifest_hash"]):
        raise _InvalidSource("Frozen generation does not match its recorded inputs")
    return manifest


def _saved_source_is_current(db, actor, kb, generation, manifest, source, version, document, model, context):
    if (not version or not document or document.knowledge_base_id != kb.id or model not in version.model_scope
            or not governance.can_read_version(db, actor, version, historical=True)
            or not can_access_document(db, actor, document, context=context)):
        return False
    binding = db.execute(select(IndexSource.parse_run_id, IndexSource.chunk_set_id).where(
        IndexSource.index_generation_id == generation.id, IndexSource.document_version_id == version.id)).one_or_none()
    if binding != (source["run_id"], source["selected_chunk_set_id"]):
        return False
    current, candidates = _index_source(db, document, version, manifest["profile"], historical=True)
    frozen = [item for item in manifest["members"] if item["version_id"] == version.id]
    return not current["reason"] and current == source and [entry for _, entry in candidates] == frozen


def validate_child_fragments(db, actor, fragments, *, model, kb_ids, context=None, saved_read=False) -> bool:
    """Replay one boundary's evidence. All shared proofs expire when this call returns."""
    if not fragments:
        return True
    try:
        actor = db.get(User, actor.id, populate_existing=True)
        if not actor or actor.status != "active" or not model:
            return False
        eligible_statuses = {"ready", "retired"} if saved_read else {"ready"}
        generations, sources, groups, replays = {}, {}, {}, {}
        for fragment in fragments:
            dependencies = fragment["dependencies"]
            generation_id = dependencies["generation_id"]
            if generation_id not in generations:
                generation = db.get(IndexGeneration, generation_id, populate_existing=True)
                if not child_generation(generation) or generation.status not in eligible_statuses or not generation.ready_at:
                    return False
                kb = db.get(KnowledgeBase, generation.knowledge_base_id, populate_existing=True)
                if (not kb or kb.status != "active" or kb.id not in kb_ids
                        or generation.build_report.get("integrity", {}).get("status") != "passed"
                        or (not saved_read and (kb.active_generation_id != generation.id or not can_query_kb(db, actor, kb.id)))):
                    return False
                manifest = _saved_manifest(kb, generation) if saved_read else current_child_manifest(db, kb, generation)
                generations[generation_id] = generation, kb, manifest
            generation, kb, manifest = generations[generation_id]

            version_id = fragment["document_version_id"]
            source_key = (generation_id, version_id)
            if source_key not in sources:
                matching = [item for item in manifest["sources"] if item["version_id"] == version_id]
                if len(matching) != 1:
                    return False
                source = matching[0]
                version = db.get(DocumentVersion, version_id, populate_existing=True)
                document = db.get(Document, source["document_id"], populate_existing=True)
                if saved_read:
                    eligible = _saved_source_is_current(db, actor, kb, generation, manifest, source, version, document, model, context)
                else:
                    eligible = _eligible(db, actor, version, document, model, context)
                if not eligible:
                    return False
                chunk_set = db.get(ChunkSet, source["selected_chunk_set_id"], populate_existing=True)
                support = {item["id"]: item for item in chunk_set.stage_checkpoint["support_groups"]}
                sources[source_key] = source, version, document, support
            source, version, document, support = sources[source_key]

            group_key = (*source_key, dependencies["group_ref"])
            if group_key not in groups:
                groups[group_key] = _group_order(support, dependencies["group_ref"], set())[-1]
            group = groups[group_key]
            child_id = fragment["chunk_id"]
            replay_key = (*group_key, child_id)
            if replay_key not in replays:
                if child_id not in group["child_chunk_ids"]:
                    return False
                _, replay = _group_output(db, group, child_id, source, generation, version, document)
                replays[replay_key] = {item["id"]: item for item in replay}
            if replays[replay_key].get(fragment["id"]) != fragment:
                return False
        return True
    except (ProviderError, SafeError, ParseContractError, ArithmeticError, AttributeError, KeyError, IndexError, TypeError, ValueError):
        return False


def validate_child_fragment(db, actor, fragment, *, model, kb_ids, context=None, saved_read=False) -> bool:
    """Apply the same exact proof to a single fragment."""
    return validate_child_fragments(db, actor, [fragment], model=model, kb_ids=kb_ids, context=context, saved_read=saved_read)
