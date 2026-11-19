"""Exact persisted child evidence shared by reads, model projection and delivery."""
from copy import deepcopy
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .execution_models import QueryAnswerSnapshot
from .models import ContentBlock, QueryTask, TaskEvidence, TaskShare, User, utc_now
from .services import SafeError

_SEPARATE = {"quote", "text", "locator", "source_spans", "asset_refs", "source_binding_hash"}


def child_fragment(item):
    """Rebuild the original retrieval identity; TaskEvidence IDs are task-scoped."""
    fragment = deepcopy((item.source_context or {}).get("fragment", {}))
    fragment.update(quote=item.quote_snapshot, text=item.quote_snapshot if item.evidence_kind == "text" else None,
                    locator=item.locator, source_spans=item.source_spans, asset_refs=item.asset_refs,
                    source_binding_hash=item.source_binding_hash)
    return fragment


def _task_fragment(task, item):
    if (not task or item.task_id != task.id or item.execution_revision != task.execution_revision
            or not item.chunk_id or not item.chunk_set_id or not item.source_binding_hash
            or not isinstance((item.source_context or {}).get("fragment"), dict)):
        return None
    fragment = child_fragment(item)
    if (fragment.get("document_version_id") != item.document_version_id
            or fragment.get("block_id") != item.block_id or fragment.get("chunk_id") != item.chunk_id
            or fragment.get("chunk_set_id") != item.chunk_set_id or fragment.get("evidence_kind") != item.evidence_kind):
        return None
    return fragment


def _validate_task_fragments(db, actor, fragments, payload, saved_read):
    from .chunk_retrieval import validate_child_fragments

    return validate_child_fragments(db, actor, fragments, model=payload.get("model"),
        kb_ids=payload.get("knowledge_base_ids", []), context={key: payload.get(key) for key in ("level", "category", "need_class")},
        saved_read=saved_read)


def child_evidence_is_current(db, actor, item, payload=None, *, saved_read=False):
    task = db.get(QueryTask, item.task_id, populate_existing=True)
    fragment = _task_fragment(task, item)
    return fragment is not None and _validate_task_fragments(db, actor, [fragment],
        payload or task.effective_input or task.input_snapshot, saved_read)


def validate_task_evidence_batch(db, actor, task, items, *, saved_read=False) -> bool:
    """Validate the exact child evidence for one current task and use boundary."""
    current = db.get(QueryTask, task.id, populate_existing=True)
    if current is None:
        return False
    items = list(items)
    fragments = []
    for item in items:
        fragment = _task_fragment(current, item)
        if fragment is None:
            return False
        fragments.append(fragment)
    return (_validate_task_fragments(db, actor, fragments, current.effective_input or current.input_snapshot, saved_read)
            and _originals_are_current(db, items))


def shared_task_answer(db, actor, task) -> tuple[QueryAnswerSnapshot | None, list[str]] | None:
    """Identify the evidence scope of an active one-answer share."""
    if task.kind != "query" or task.status != "succeeded" or actor.id == task.owner_user_id:
        return None
    share = db.scalar(select(TaskShare).where(TaskShare.task_id == task.id, TaskShare.recipient_user_id == actor.id,
        TaskShare.granted_by == task.owner_user_id, TaskShare.status == "active", TaskShare.revoked_at.is_(None),
        TaskShare.expires_at.is_(None) | (TaskShare.expires_at > utc_now())).execution_options(populate_existing=True))
    if not share:
        return None
    if share.answer_snapshot_id:
        snapshot = db.get(QueryAnswerSnapshot, share.answer_snapshot_id, populate_existing=True)
        if not snapshot or snapshot.task_id != task.id or snapshot.execution_revision != task.execution_revision:
            return None
        ids = snapshot.evidence_ids
        if not isinstance(ids, list) or any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
            return None
        return snapshot, list(ids)

    # Legacy queries represented a single answer directly on the task.
    if task.accepted_scope or db.scalar(select(QueryAnswerSnapshot.id).where(QueryAnswerSnapshot.task_id == task.id).limit(1)):
        return None
    legacy = db.execute(select(TaskEvidence.id, TaskEvidence.chunk_id, TaskEvidence.chunk_set_id, TaskEvidence.source_binding_hash)
        .where(TaskEvidence.task_id == task.id, TaskEvidence.execution_revision == task.execution_revision)).all()
    if any(chunk_id is not None or chunk_set_id is not None or binding is not None for _, chunk_id, chunk_set_id, binding in legacy):
        return None
    return None, [item.id for item in legacy]


def _originals_are_current(db, evidence):
    from .models import DocumentVersion, FileObject
    from .parsing.derivation import _verify_original
    from .parsing.local_pipeline import ParseContractError

    config = db.info.get("settings")
    originals = set()
    for item in evidence:
        if item.chunk_id is None:
            continue
        locator = item.locator
        if (not isinstance(locator, dict) or not isinstance(locator.get("source_file_id"), str)
                or not isinstance(locator.get("source_sha256"), str)):
            return False
        originals.add((item.document_version_id, locator["source_file_id"], locator["source_sha256"]))
    for version_id, file_id, sha256 in originals:
        version = db.get(DocumentVersion, version_id, populate_existing=True)
        file = db.get(FileObject, file_id, populate_existing=True)
        if (not version or version.source_file_id != file_id or not file or file.status != "available"
                or file.storage_name != "local" or file.sha256 != sha256
                or file.mime_type not in {"application/pdf", "application/octet-stream"}):
            return False
        if config is not None:
            try:
                _verify_original(file, config)
            except (OSError, ParseContractError, SafeError, ValueError):
                return False
    return True


def task_readable_evidence(db, actor, task, *, evidence_id=None) -> list[TaskEvidence] | None:
    """Return authorized rows in answer order, or None when the task cannot be read."""
    from .services import evidence_is_readable, has_permission

    actor = db.get(User, actor.id, populate_existing=True)
    task = db.get(QueryTask, task.id, populate_existing=True)
    if not actor or actor.status != "active" or not task or not has_permission(actor, "task.read"):
        return None
    statement = select(TaskEvidence).where(TaskEvidence.task_id == task.id,
        TaskEvidence.execution_revision == task.execution_revision).execution_options(populate_existing=True)
    if task.owner_user_id == actor.id:
        evidence = list(db.scalars(statement))
        if evidence_id is not None and not any(item.id == evidence_id for item in evidence):
            return None
    else:
        shared = shared_task_answer(db, actor, task)
        if shared is None:
            return None
        _, ids = shared
        if evidence_id is not None and evidence_id not in ids:
            return None
        by_id = {item.id: item for item in db.scalars(statement.where(TaskEvidence.id.in_(ids)))} if ids else {}
        if set(by_id) != set(ids):
            return None
        evidence = [by_id[item_id] for item_id in ids]

    saved_read = task.status == "succeeded"
    child, legacy = [], []
    for item in evidence:
        if item.chunk_id is not None or item.chunk_set_id is not None or item.source_binding_hash is not None:
            child.append(item)
        else:
            legacy.append(item)
    if ((child and not validate_task_evidence_batch(db, actor, task, child, saved_read=saved_read))
            or not all(evidence_is_readable(db, actor, item, saved_read=saved_read) for item in legacy)):
        return None
    return evidence


def persist_child_evidence(db, task, result):
    groups = {group["id"]: group for group in result["support_groups"]}
    group_seen, seen, rows = set(), set(), []
    for position, fragment in enumerate(result["fragments"]):
        if fragment["id"] in seen:
            continue
        seen.add(fragment["id"])
        group_ref = fragment["dependencies"]["group_ref"]
        if group_ref not in groups:
            raise SafeError("EVIDENCE_VALIDATION_FAILED", "来源支持组不完整", 409)
        identity = str(uuid5(NAMESPACE_URL, f"dc.task-evidence/{task.id}/{task.execution_revision}/{fragment['id']}"))
        context = {"fragment": {key: deepcopy(value) for key, value in fragment.items() if key not in _SEPARATE}, "position": position}
        if group_ref not in group_seen:
            context["group"] = deepcopy(groups[group_ref])
            group_seen.add(group_ref)
        row = db.get(TaskEvidence, identity)
        if row:
            if child_fragment(row) != fragment:
                raise SafeError("EVIDENCE_VALIDATION_FAILED", "已冻结证据不能换绑", 409)
        else:
            block = db.get(ContentBlock, fragment["block_id"])
            row = TaskEvidence(id=identity, task_id=task.id, execution_revision=task.execution_revision,
                document_version_id=fragment["document_version_id"], block_id=fragment["block_id"],
                chunk_id=fragment["chunk_id"], chunk_set_id=fragment["chunk_set_id"], evidence_kind=fragment["evidence_kind"],
                quote_snapshot=fragment["quote"], locator=deepcopy(fragment["locator"]), source_spans=deepcopy(fragment["source_spans"]),
                asset_refs=list(fragment["asset_refs"]), source_binding_hash=fragment["source_binding_hash"],
                source_context=context, review_at_run=block.review_status)
            db.add(row)
        rows.append(row)
    if group_seen != groups.keys():
        raise SafeError("EVIDENCE_VALIDATION_FAILED", "支持组没有完整持久来源", 409)
    db.flush()
    return rows


def stage_child_query(db, task, actor, *, stage_only=False):
    from .chunk_retrieval import search_child_chunks
    from .governance import query_has_version_conflict
    from .models import TaskArtifact, utc_now
    from .services import audit
    payload = task.effective_input or task.input_snapshot
    if not payload.get("model") or not payload.get("knowledge_base_ids"):
        return False
    dimensions = {key: payload.get(key) for key in ("level", "category", "need_class")}
    result = search_child_chunks(db, actor, payload["query"], model=payload["model"], kb_ids=payload["knowledge_base_ids"], context=dimensions)
    if result["mode"] == "legacy":
        return False
    receipt = result.get("retrieval_receipt")
    if query_has_version_conflict(db, actor, payload["model"], payload["knowledge_base_ids"], dimensions):
        result = {"support_groups": [], "fragments": [], "warnings": ["version_conflict"]}
    rows = persist_child_evidence(db, task, result)
    task.status, task.finished_at = ("running", None) if stage_only else ("succeeded", utc_now())
    task.warnings = sorted(set(result["warnings"]) | ({"insufficient_evidence"} if not rows else set()))
    task.result_summary = {"answer": None if stage_only or not rows else "\n\n".join(row.quote_snapshot for row in rows if row.quote_snapshot),
        "evidence_count": len(rows), "result_status": "candidate" if stage_only else "extractive" if rows else "insufficient_evidence",
        "requires_review": True, "retrieval_mode": "child", "model": payload["model"]}
    if receipt:
        task.result_summary["retrieval_method"] = receipt["method"]
        audit(db, actor.id, "query.vector_retrieved", "task", task.id,
              execution_revision=task.execution_revision, evidence_count=len(rows), **receipt)
    task.next_action = "核对精确原句与原页；设备操作须由业务人员复核" if rows else "资料范围或索引暂不可用，请查看缺项或联系资料维护人员"
    task.revision += 1
    if not stage_only:
        db.add(TaskArtifact(task_id=task.id, kind="query.result.json", validation_status="extractive"))
        audit(db, actor.id, "query.retrieved", "task", task.id, evidence_count=len(rows))
    return True
