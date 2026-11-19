"""Local governance services. All mutations belong to the caller's transaction.

Controlled initialization may provision authority bases and attested evidence.
Public routes can register claims and record checks, but cannot create trust.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from typing import Any
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from .models import (AuthorizationGrant, BusinessApproval, ChunkSet, ContentBlock, Document,
                     DocumentVersion, FileObject, KnowledgeBase, ParseRun, User, utc_now)
from .governance_models import (ApprovalRevocation, ApprovalSupersession, AuthorityBasis, BusinessConfirmation, DocumentUseHold,
    GovernanceEvidence, GovernanceOutbox, PublicationEvent, PublicationPreview, ReviewSet, VerificationEvent)


def _error(code: str, message: str, status: int = 409):
    from .services import SafeError
    return SafeError(code, message, status)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str).encode()).hexdigest()


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _date(value: Any) -> datetime | None:
    if value is None or isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        raise _error("INVALID_TIME", "时间格式无效", 422)


def _time(value: datetime | None) -> str | None:
    return _utc(value).isoformat() if value else None


def _data(payload: Any) -> dict:
    return payload if isinstance(payload, dict) else payload.model_dump()


def _ref(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return value.get("id", value.get("block_id", value.get("evidence_id", "")))
    raise _error("INVALID_REFERENCE", "依据引用格式无效", 422)


def manage(db: Session, actor: User, document: Document) -> None:
    from .services import can_access_document
    if not document or not can_access_document(db, actor, document, "knowledge.manage"):
        raise _error("FORBIDDEN", "当前账号没有管理该资料范围的权限", 403)


def _changed(db: Session, actor: User, version: DocumentVersion, action: str, *, affects_formal: bool | None = None) -> None:
    from .services import audit
    document = db.get(Document, version.document_id)
    kb = db.get(KnowledgeBase, document.knowledge_base_id)
    version.revision += 1
    if affects_formal is None:
        affects_formal = version.publish_status in {"effective", "superseded"}
    if affects_formal:
        kb.content_revision += 1
    db.add(GovernanceOutbox(event_kind=action, resource_id=version.id, revision=version.revision))
    audit(db, actor.id, action, "document_version", version.id)


def source_binding(db: Session, version: DocumentVersion) -> dict:
    file = db.get(FileObject, version.source_file_id, populate_existing=True)
    return {"version_id": version.id, "source_sha256": file.sha256 if file else None,
            "scope_hash": digest(sorted(version.model_scope or [])), "source_metadata_hash": digest(version.source_metadata or {})}


def block_binding(block: ContentBlock) -> str:
    return digest({"id": block.id, "text": block.text_content, "kind": block.kind,
                   "structure": block.structured_content, "locator": block.locator, "revision": block.revision})


def content_binding(db: Session, run_id: str) -> dict:
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run_id).order_by(ContentBlock.ordinal, ContentBlock.id).execution_options(populate_existing=True)).all()
    return {item.id: {"hash": block_binding(item), "review": item.review_status} for item in blocks}


def review_content_block(db: Session, actor: User, run: ParseRun, block: ContentBlock, status: str, reason: str) -> ContentBlock:
    version = db.get(DocumentVersion, run.document_version_id)
    manage(db, actor, db.get(Document, version.document_id))
    selected = db.get(ChunkSet, version.selected_chunk_set_id) if version.selected_chunk_set_id else None
    if block.parse_run_id != run.id or parse_run_was_published(db, run) or (version.publish_status != "draft" and selected and selected.parse_run_id == run.id):
        raise _error("PUBLISHED_BATCH_IMMUTABLE", "已发布选用批次不可原位核验；未选用的新批次可单独核验")
    if status not in {"approved", "rejected", "unreviewed"}:
        raise _error("INVALID_REVIEW", "核验状态无效", 422)
    if run.ir_schema_version:
        if not run.ir_artifact_id:
            raise _error("IR_NOT_READY", "请先完成结构来源与原页映射，再核验源内容")
        if block.structure_role != "leaf":
            raise _error("SOURCE_REVIEW_SCOPE_REQUIRED", "结构容器按子叶覆盖核验；清点区域须登记业务处置及依据")
    if status == "approved" and (block.structured_content or {}).get("pending_graphic_keys"):
        raise _error("GRAPHIC_KEYS_PENDING", "该表有待核验的图形键，请对照原表校正后重新核验")
    block.review_status, block.review_reason = status, reason
    block.revision += 1
    db.flush()
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)).all()
    if run.ir_schema_version:
        from .parsing.processing import synchronize_source_status
        synchronize_source_status(db, run)
    else:
        run.status = "succeeded" if blocks and all(x.review_status == "approved" for x in blocks) else "needs_review"
    _changed(db, actor, version, "parse.block_reviewed", affects_formal=False)
    db.flush()
    return block


def parse_run_was_published(db, run):
    events = db.scalars(select(PublicationEvent).where(PublicationEvent.document_version_id == run.document_version_id,
                                                     PublicationEvent.action.in_(("publish", "source_rebind"))))
    for event in events:
        selected = event.bound_revisions.get("qualification", {}).get("selected", [])
        chunk = db.get(ChunkSet, selected[0]) if selected else None
        if chunk and chunk.parse_run_id == run.id:
            return True
    return False


def _approval_assertion(approval: BusinessApproval) -> dict:
    return {"kind": "access_approval", "applicant": approval.applicant,
            "actual_identity": approval.actual_approver_snapshot, "decision": approval.actual_decision,
            "decided_at": _time(approval.approved_at), "scope": approval.scope,
            "operations": sorted(approval.operations or []), "valid_from": _time(approval.valid_from),
            "valid_until": _time(approval.valid_until)}


def _confirmation_assertion(item: BusinessConfirmation) -> dict:
    return {"kind": item.kind, "actual_identity": item.actual_confirmer, "decision": item.conclusion,
            "decided_at": _time(item.confirmed_at), "scope": item.scope}


def _proof_dependencies(db: Session, owner_kind: str, owner_id: str, refs: list[str], assertion: dict,
                        basis_id: str, now: datetime) -> dict | None:
    basis = db.get(AuthorityBasis, basis_id, populate_existing=True)
    identity = assertion.get("actual_identity") or {}
    if not basis or basis.revoked_at or _utc(basis.valid_from) > now or (basis.valid_until and _utc(basis.valid_until) <= now):
        return None
    if not basis.source_reference or assertion["kind"] not in basis.purposes or identity.get("authority_basis_ref") != basis.id:
        return None
    if any(identity.get(key) != value for key, value in basis.identity.items()) or not basis.identity.get("identity_label") or not basis.identity.get("responsibility"):
        return None
    scope = assertion.get("scope") or {}
    if not basis.scope or any(scope.get(key) != value for key, value in basis.scope.items()):
        return None
    decided_at = _date(assertion.get("decided_at"))
    if not decided_at or _utc(decided_at) > now or _utc(decided_at) < _utc(basis.valid_from) or (basis.valid_until and _utc(decided_at) >= _utc(basis.valid_until)):
        return None
    proof_states = []
    if not refs:
        return None
    for ref in refs:
        proof = db.get(GovernanceEvidence, _ref(ref), populate_existing=True)
        if not proof or proof.owner_kind != owner_kind or proof.owner_id != owner_id or not proof.attested or proof.revoked_at:
            return None
        if proof.assertion != assertion or not proof.source_reference.get("source_description"):
            return None
        file = db.get(FileObject, proof.file_object_id, populate_existing=True) if proof.file_object_id else None
        if proof.file_object_id and (not file or file.status != "available" or file.sha256 != proof.source_reference.get("sha256")):
            return None
        proof_states.append({"id": proof.id, "assertion": proof.assertion, "source": proof.source_reference,
                             "file": [file.id, file.sha256, file.status] if file else None})
    return {"basis": {"id": basis.id, "identity": basis.identity, "scope": basis.scope,
                       "purposes": basis.purposes, "revision": basis.revision,
                       "valid_from": _time(basis.valid_from), "valid_until": _time(basis.valid_until)}, "proofs": proof_states}


def _latest_verification(db: Session, kind: str, id: str) -> VerificationEvent | None:
    return db.scalar(select(VerificationEvent).where(VerificationEvent.owner_kind == kind,
        VerificationEvent.owner_id == id).order_by(VerificationEvent.revision.desc()).execution_options(populate_existing=True))


def approval_is_current(db: Session, approval: BusinessApproval | None) -> bool:
    if not approval:
        return False
    approval = db.get(BusinessApproval, approval.id, populate_existing=True)
    now = utc_now()
    if approval.actual_decision != "approved" or approval.verification_state != "verified" or approval.status != "approved" or not approval.approved_at:
        return False
    if db.scalar(select(ApprovalSupersession.id).where(ApprovalSupersession.old_approval_id == approval.id)) or db.scalar(select(ApprovalRevocation.id).where(ApprovalRevocation.approval_id == approval.id)):
        return False
    if _utc(approval.valid_from) > now or (approval.valid_until and _utc(approval.valid_until) <= now):
        return False
    event = _latest_verification(db, "approval", approval.id)
    if not event or event.decision != "verified":
        return False
    assertion = _approval_assertion(approval)
    deps = _proof_dependencies(db, "approval", approval.id, approval.proof_refs, assertion, event.authority_basis_id, now)
    return bool(deps and event.dependency_fingerprint == digest({"assertion": assertion, "dependencies": deps}))


def register_approval(db: Session, actor: User, payload: Any) -> BusinessApproval:
    data = _data(payload)
    subject = data.get("subject") or {"kind": "user", "id": data.get("applicant")}
    if subject.get("kind") != "user":
        raise _error("SUBJECT_NOT_SUPPORTED", "首期仅支持明确用户的完整授权组合", 422)
    if not db.get(User, subject.get("id")):
        raise _error("NOT_FOUND", "用户不存在", 404)
    scope = dict(data.get("scope") or {})
    operations = data.get("operations") or scope.pop("operations", [])
    row = BusinessApproval(applicant=subject["id"], recorded_by=actor.id,
        actual_approver_snapshot=data.get("actual_approver") or {}, actual_decision=data.get("decision"),
        approved_at=_date(data.get("approved_at")), scope=scope, operations=operations,
        valid_from=_date(data.get("valid_from")) or utc_now(), valid_until=_date(data.get("valid_until")),
        proof_refs=[_ref(x) for x in data.get("proof_refs", [])], evidence_locator=data.get("evidence_locator"),
        status="pending", verification_state="unverified")
    db.add(row)
    db.flush()
    old_id = data.get("supersedes_id")
    if old_id:
        old = db.get(BusinessApproval, old_id)
        if not old or not approval_is_manageable(db, actor, old) or not approval_is_manageable(db, actor, row):
            raise _error("FORBIDDEN", "替代批准必须具有新旧记录完整范围的当前管理依据", 403)
        if not old or old.applicant != row.applicant or db.scalar(select(ApprovalSupersession.id).where(ApprovalSupersession.old_approval_id == old_id)):
            raise _error("INVALID_SUPERSESSION", "只能追加替代同一受益人的当前批准", 422)
        db.add(ApprovalSupersession(old_approval_id=old_id, new_approval_id=row.id, recorded_by=actor.id))
        db.get(User, row.applicant).authz_revision += 1
        db.flush()
    return row


def revoke_approval(db: Session, actor: User, approval: BusinessApproval, payload: Any) -> ApprovalRevocation:
    if not approval_is_manageable(db, actor, approval):
        raise _error("FORBIDDEN", "缺少当前范围的可信平台职责依据", 403)
    data = _data(payload)
    proof_id = _ref(data.get("verified_revocation_evidence_ref") or "")
    proof = db.get(GovernanceEvidence, proof_id)
    assertion = proof.assertion if proof else {}
    identity = assertion.get("actual_identity") or {}
    deps = _proof_dependencies(db, "approval", approval.id, [proof_id], assertion,
        identity.get("authority_basis_ref", ""), utc_now()) if proof and assertion.get("kind") == "access_revocation" else None
    if not deps or assertion.get("decision") != "revoked" or assertion.get("scope") != {"approval_id": approval.id, **approval.scope} or not data.get("reason"):
        raise _error("PROOF_UNVERIFIABLE", "外部撤销必须有精确批准范围的真实职责及受控依据")
    row = ApprovalRevocation(approval_id=approval.id, evidence_id=proof.id,
        authority_basis_id=identity["authority_basis_ref"], actor_id=actor.id,
        reason=data["reason"], dependency_fingerprint=digest({"assertion": assertion, "dependencies": deps}))
    db.add(row)
    approval.revision += 1
    db.get(User, approval.applicant).authz_revision += 1
    db.add(GovernanceOutbox(event_kind="approval.revoked", resource_id=approval.id, revision=approval.revision))
    db.flush()
    return row


def verify_approval(db: Session, actor: User, approval: BusinessApproval, payload: Any) -> BusinessApproval:
    if not approval_is_manageable(db, actor, approval):
        raise _error("FORBIDDEN", "缺少当前范围的可信平台职责依据", 403)
    data = _data(payload)
    decision = data.get("decision")
    if decision not in {"verified", "rejected"} or not data.get("reason"):
        raise _error("INVALID_VERIFICATION", "核查结论及原因必填", 422)
    refs = [_ref(x) for x in data.get("checked_evidence_refs", data.get("checked_proof_refs", []))]
    assertion = _approval_assertion(approval)
    deps = _proof_dependencies(db, "approval", approval.id, refs, assertion, data.get("authority_basis_ref", ""), utc_now())
    required = {"knowledge_base_id", "document_id", "level", "category", "need_class"}
    complete = required <= set(approval.scope) and all(approval.scope.get(k) for k in ("level", "category", "need_class")) and bool(approval.scope.get("knowledge_base_id") or approval.scope.get("document_id"))
    if decision == "verified" and (not deps or set(refs) != set(approval.proof_refs or []) or not complete or not approval.operations or approval.actual_decision != "approved" or not approval.approved_at or not approval.valid_until or _utc(approval.valid_until) <= _utc(approval.valid_from)):
        raise _error("PROOF_UNVERIFIABLE", "真实批准、职责、完整范围、期限及受控依据必须一致")
    approval.verification_revision += 1
    approval.revision += 1
    fingerprint = digest({"assertion": assertion, "dependencies": deps}) if deps else None
    event = VerificationEvent(owner_kind="approval", owner_id=approval.id, decision=decision, reason=data["reason"],
        checked_refs=refs, authority_basis_id=data.get("authority_basis_ref") if deps else None,
        dependency_fingerprint=fingerprint, actor_id=actor.id, revision=approval.verification_revision)
    db.add(event)
    approval.verification_state = decision
    approval.verified_by, approval.verified_at = actor.id, utc_now()
    approval.dependency_fingerprint = fingerprint
    approval.approver = (approval.actual_approver_snapshot or {}).get("identity_label") if decision == "verified" else None
    approval.status = "approved" if decision == "verified" else "pending"
    target = db.get(User, approval.applicant)
    if target:
        target.authz_revision += 1
    db.add(GovernanceOutbox(event_kind="approval.verification", resource_id=approval.id, revision=approval.revision))
    db.flush()
    return approval


def approval_is_manageable(db: Session, actor: User, approval: BusinessApproval) -> bool:
    from .services import has_permission
    actor = db.get(User, actor.id, populate_existing=True)
    if not actor or actor.status != "active" or not has_permission(actor, "access.manage"):
        return False
    now = utc_now()
    for basis in db.scalars(select(AuthorityBasis).execution_options(populate_existing=True)).all():
        if basis.identity.get("user_ref") != actor.id or "access.manage" not in basis.purposes or not basis.source_reference or basis.revoked_at:
            continue
        if _utc(basis.valid_from) > now or (basis.valid_until and _utc(basis.valid_until) <= now):
            continue
        if basis.scope and all(approval.scope.get(key) == value for key, value in basis.scope.items()):
            return True
    return False


def register_confirmation(db: Session, actor: User, version: DocumentVersion, payload: Any) -> BusinessConfirmation:
    data = _data(payload)
    manage(db, actor, db.get(Document, version.document_id))
    kind = data.get("kind")
    if not isinstance(data.get("actual_confirmer", {}), dict) or not isinstance(data.get("scope", {}), dict) or not isinstance(data.get("proof_refs", []), list):
        raise _error("INVALID_CONFIRMATION", "身份、范围或依据结构无效", 422)
    if data.get("conclusion") not in {"applicability": {"applicable", "not_applicable", "conflict"},
            "key_content": {"accepted", "rejected"}, "classification": {"classified", "conflict"}}.get(kind, set()):
        raise _error("INVALID_CONCLUSION", "确认类型或结论无效", 422)
    old_id = data.get("supersedes_confirmation_id")
    if old_id:
        old = db.get(BusinessConfirmation, old_id)
        if not old or old.document_version_id != version.id or old.kind != kind or db.scalar(select(BusinessConfirmation.id).where(BusinessConfirmation.supersedes_id == old.id)):
            raise _error("INVALID_SUPERSESSION", "替代对象必须是同版本同类型的当前记录", 422)
    row = BusinessConfirmation(document_version_id=version.id, kind=kind,
        actual_confirmer=data.get("actual_confirmer") or {}, confirmed_at=_date(data.get("confirmed_at")),
        scope=data.get("scope") or {}, conclusion=data["conclusion"], proof_refs=[_ref(x) for x in data.get("proof_refs", [])],
        supersedes_id=old_id, recorded_by=actor.id)
    db.add(row)
    # A new pending claim is not a change to the formal corpus. An explicit
    # supersession does immediately fence the old confirmation, even pending.
    _changed(db, actor, version, "confirmation.registered", affects_formal=bool(old_id) and version.publish_status in {"effective", "superseded"})
    db.flush()
    return row


def _confirmation_scope_current(db: Session, version: DocumentVersion, item: BusinessConfirmation, *, allow_initial_classification: bool = False) -> bool:
    base = source_binding(db, version)
    if not all(item.scope.get(k) == value for k, value in base.items()):
        return False
    if item.kind == "key_content":
        run = db.get(ParseRun, item.scope.get("parse_run_id"))
        if not run or run.document_version_id != version.id:
            return False
        if run.ir_schema_version:
            from .parsing.processing import confirmation_scope_matches
            return confirmation_scope_matches(db, run, item.scope) and any(item.scope.get(key) for key in (
                "block_bindings", "relation_bindings", "observation_bindings", "container_bindings", "page_bindings"))
        bindings = item.scope.get("block_bindings")
        if not isinstance(bindings, dict) or not bindings:
            return False
        for id, binding in bindings.items():
            block = db.get(ContentBlock, id, populate_existing=True)
            if not block or block.parse_run_id != run.id or block_binding(block) != binding:
                return False
    if item.kind == "classification":
        document = db.get(Document, version.document_id)
        if not item.scope.get("rule_version") or not all(item.scope.get(key) for key in ("access_level", "category", "need_class")):
            return False
        for key in ("category", "need_class"):
            current = getattr(document, key)
            if item.scope.get(key) != current and not (allow_initial_classification and current is None):
                return False
    return True


def _establish_initial_classification(db: Session, actor: User, version: DocumentVersion, item: BusinessConfirmation) -> None:
    """Establish missing first-upload restrictions only from checked true facts.

    Existing document and historical version restrictions remain authoritative.
    A savepoint ensures a failed complete-tuple postcheck leaves no partial
    baseline behind, including for callers that catch the domain error.
    """
    from .services import can_access_document
    document = db.get(Document, version.document_id)
    dimensions = ("access_level", "category", "need_class")
    missing = [key for key in dimensions if getattr(document, key) is None]
    if not missing:
        return
    if item.conclusion != "classified" or any(getattr(document, key) is not None and getattr(document, key) != item.scope.get(key) for key in dimensions):
        raise _error("VERSION_SCOPE_CONFLICT", "首次分类不能覆盖已有文档限制")
    if db.scalar(select(DocumentVersion.id).where(DocumentVersion.document_id == document.id,
            DocumentVersion.id != version.id, DocumentVersion.publish_status != "draft")):
        raise _error("VERSION_SCOPE_CONFLICT", "旧版本限制缺失须独立核对，不能从新草稿推断")
    with db.begin_nested():
        for key in missing:
            setattr(document, key, item.scope[key])
        document.revision += 1
        db.flush()
        if not can_access_document(db, actor, document, "knowledge.manage"):
            raise _error("FORBIDDEN", "核查者没有新完整分类组合的管理权限", 403)


def confirmation_is_current(db: Session, item: BusinessConfirmation) -> bool:
    item = db.get(BusinessConfirmation, item.id, populate_existing=True)
    if item.verification_state != "verified" or db.scalar(select(BusinessConfirmation.id).where(BusinessConfirmation.supersedes_id == item.id)):
        return False
    version = db.get(DocumentVersion, item.document_version_id, populate_existing=True)
    if not _confirmation_scope_current(db, version, item):
        return False
    event = _latest_verification(db, "confirmation", item.id)
    if not event or event.decision != "verified":
        return False
    assertion = _confirmation_assertion(item)
    deps = _proof_dependencies(db, "version", version.id, item.proof_refs, assertion, event.authority_basis_id, utc_now())
    return bool(deps and event.dependency_fingerprint == digest({"assertion": assertion, "dependencies": deps}))


def verify_confirmation(db: Session, actor: User, item: BusinessConfirmation, payload: Any) -> BusinessConfirmation:
    version = db.get(DocumentVersion, item.document_version_id)
    manage(db, actor, db.get(Document, version.document_id))
    data = _data(payload)
    decision = data.get("decision")
    if decision not in {"verified", "rejected"} or not data.get("reason"):
        raise _error("INVALID_VERIFICATION", "核查结论及原因必填", 422)
    assertion = _confirmation_assertion(item)
    refs = [_ref(x) for x in data.get("checked_proof_refs", [])]
    deps = _proof_dependencies(db, "version", version.id, refs, assertion, data.get("authority_basis_ref", ""), utc_now())
    if decision == "verified" and (not deps or set(refs) != set(item.proof_refs) or not _confirmation_scope_current(db, version, item, allow_initial_classification=True)):
        raise _error("PROOF_UNVERIFIABLE", "真实确认、职责、精确内容范围及受控依据必须一致")
    if db.scalar(select(BusinessConfirmation.id).where(BusinessConfirmation.supersedes_id == item.id)):
        raise _error("CONFIRMATION_SUPERSEDED", "已替代记录不可重新核查")
    if decision == "verified" and item.kind == "classification":
        _establish_initial_classification(db, actor, version, item)
    item.revision += 1
    fingerprint = digest({"assertion": assertion, "dependencies": deps}) if deps else None
    db.add(VerificationEvent(owner_kind="confirmation", owner_id=item.id, decision=decision,
        reason=data["reason"], checked_refs=refs, authority_basis_id=data.get("authority_basis_ref") if deps else None,
        dependency_fingerprint=fingerprint, actor_id=actor.id, revision=item.revision))
    item.verification_state, item.dependency_fingerprint = decision, fingerprint
    selected = db.get(ChunkSet, version.selected_chunk_set_id) if version.selected_chunk_set_id else None
    affects_selected = item.kind != "key_content" or bool(selected and selected.parse_run_id == item.scope.get("parse_run_id"))
    _changed(db, actor, version, "confirmation.verified", affects_formal=affects_selected and version.publish_status in {"effective", "superseded"})
    if item.kind == "classification":
        db.get(KnowledgeBase, db.get(Document, version.document_id).knowledge_base_id).acl_revision += 1
    db.flush()
    if item.kind == "key_content":
        from .parsing.processing import synchronize_source_status
        synchronize_source_status(db, db.get(ParseRun, item.scope.get("parse_run_id")))
    return item


def current_confirmations(db: Session, version: DocumentVersion, kind: str) -> list[BusinessConfirmation]:
    return [x for x in db.scalars(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == version.id, BusinessConfirmation.kind == kind)).all() if confirmation_is_current(db, x)]


def _review_binding(db: Session, version: DocumentVersion, run: ParseRun, chunk: ChunkSet, refs: list[str]) -> str:
    if run.ir_schema_version:
        from .chunk_jobs import chunk_projection_fingerprint
        from .parsing.processing import projection_fingerprint, source_verification
        return digest({"source": source_binding(db, version), "run_id": run.id, "ir": run.ir_artifact_id,
            "source_projection": projection_fingerprint(db, run.id), "source_qc": run.stage_checkpoint.get("outputs", {}).get("source_qc"),
            "chunk": [chunk.id, chunk.config_hash, chunk.quality_artifact_id, chunk_projection_fingerprint(db, chunk)],
            "blocks": content_binding(db, run.id), "verification": source_verification(db, run, refs),
            "confirmations": [[id, db.get(BusinessConfirmation, id).revision, db.get(BusinessConfirmation, id).dependency_fingerprint] for id in sorted(refs)]})
    return digest({"source": source_binding(db, version), "run": [run.id, run.status, run.output_revision],
        "chunk": [chunk.id, chunk.status, chunk.splitter_version], "blocks": content_binding(db, run.id),
        "confirmations": [{"id": id, "fingerprint": db.get(BusinessConfirmation, id).dependency_fingerprint,
                           "revision": db.get(BusinessConfirmation, id).revision} for id in sorted(refs)]})


def create_review_set(db: Session, actor: User, version: DocumentVersion, payload: Any) -> ReviewSet:
    data = _data(payload)
    manage(db, actor, db.get(Document, version.document_id))
    candidate_run = db.get(ParseRun, data.get("parse_run_id"))
    if candidate_run and candidate_run.ir_schema_version:
        return _create_ir_review_set(db, actor, version, candidate_run, data)
    if version.publish_status != "draft":
        raise _error("PUBLISHED_BATCH_IMMUTABLE", "已发布版本需新校正批次及新修订，不可原位核验")
    run, chunk = db.get(ParseRun, data.get("parse_run_id")), db.get(ChunkSet, data.get("chunk_set_id"))
    if not run or run.document_version_id != version.id or run.status != "succeeded" or not chunk or chunk.parse_run_id != run.id or chunk.status != "ready":
        raise _error("INVALID_REVIEW_BATCH", "核验必须绑定同版本成功解析及ready切分集合")
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id)).all()
    # Every block is required: warnings, captions, footnotes and linked definitions
    # cannot be removed from coverage by the client.
    required = {x.id for x in blocks}
    submitted = data.get("reviewed_block_refs", [])
    reviewed = {_ref(x) for x in submitted}
    if not required or reviewed != required or any(x.review_status != "approved" or not x.locator for x in blocks):
        raise _error("REVIEW_COVERAGE_INCOMPLETE", "必须核验本批次全部块及其原文定位（含警告和脚注）")
    submitted_by_id = {_ref(x): x for x in submitted if isinstance(x, dict)}
    if any(not isinstance(submitted_by_id.get(x.id), dict) or submitted_by_id[x.id].get("revision") != x.revision or submitted_by_id[x.id].get("content_hash") != block_binding(x) for x in blocks):
        raise _error("REVIEW_COVERAGE_INCOMPLETE", "必检引用必须包含当前块revision及content_hash")
    refs = [_ref(x) for x in data.get("professional_confirmation_refs", [])]
    covered = set()
    for id in refs:
        item = db.get(BusinessConfirmation, id)
        if not item or item.kind != "key_content" or item.document_version_id != version.id or item.scope.get("parse_run_id") != run.id or item.conclusion != "accepted" or not confirmation_is_current(db, item):
            raise _error("CRITICAL_CONFIRMATION_MISSING", "专业确认须匹配本批次精确内容和定位")
        covered.update(item.scope.get("block_bindings", {}))
    critical = {x.id for x in blocks if x.kind in {"warning", "footnote", "formula", "table", "caption"} or (x.structured_content or {}).get("critical")}
    if not critical <= covered:
        raise _error("CRITICAL_CONFIRMATION_MISSING", "警告、脚注、公式与表格所需专业确认未齐全")
    row = ReviewSet(document_version_id=version.id, parse_run_id=run.id, chunk_set_id=chunk.id,
        required_refs=sorted(required), reviewed_refs=sorted(reviewed), professional_confirmation_refs=refs,
        block_bindings={x.id: {"revision": x.revision, "content_hash": block_binding(x)} for x in blocks},
        binding_fingerprint=_review_binding(db, version, run, chunk, refs), reviewer_id=actor.id)
    db.add(row)
    db.flush()
    version.selected_chunk_set_id, version.selected_review_set_id = chunk.id, row.id
    _changed(db, actor, version, "review_set.created")
    db.flush()
    return row


def review_set_is_current(db: Session, version: DocumentVersion, review: ReviewSet | None, *, require_selected=True) -> bool:
    if not review or review.document_version_id != version.id or (require_selected and review.chunk_set_id != version.selected_chunk_set_id):
        return False
    run, chunk = db.get(ParseRun, review.parse_run_id, populate_existing=True), db.get(ChunkSet, review.chunk_set_id, populate_existing=True)
    if run and run.ir_schema_version:
        return _ir_review_is_current(db, version, run, chunk, review)
    if not run or run.status != "succeeded" or not chunk or chunk.status != "ready" or chunk.parse_run_id != run.id:
        return False
    blocks = content_binding(db, run.id)
    if set(blocks) != set(review.required_refs) or set(review.reviewed_refs) != set(blocks) or any(x["review"] != "approved" for x in blocks.values()):
        return False
    if set(review.block_bindings) != set(blocks) or any(review.block_bindings[id].get("content_hash") != value["hash"] for id, value in blocks.items()):
        return False
    if any(not db.get(BusinessConfirmation, id) or not confirmation_is_current(db, db.get(BusinessConfirmation, id)) for id in review.professional_confirmation_refs):
        return False
    return review.binding_fingerprint == _review_binding(db, version, run, chunk, review.professional_confirmation_refs)


def _create_ir_review_set(db, actor, version, run, data):
    from .chunk_jobs import chunk_set_qc_is_current
    from .parsing.processing import source_verification, synchronize_source_status
    chunk = db.get(ChunkSet, data.get("chunk_set_id"))
    if (run.document_version_id != version.id or version.publish_status not in {"draft", "effective"}
            or not chunk or chunk.parse_run_id != run.id or not chunk_set_qc_is_current(db, chunk)):
        raise _error("INVALID_REVIEW_BATCH", "候选须为同版本、源及集合技术检查有效的已完成切分")
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id, ContentBlock.structure_role == "leaf")).all()
    required = {row.id for row in blocks}
    submitted = data.get("reviewed_block_refs", [])
    if not isinstance(submitted, list) or any(not isinstance(item, dict) for item in submitted):
        raise _error("REVIEW_COVERAGE_INCOMPLETE", "源叶核验引用必须含当前修订和指纹")
    by_id = {_ref(item): item for item in submitted}
    if (not required or set(by_id) != required or len(submitted) != len(by_id)
            or any(row.review_status != "approved" or by_id[row.id].get("revision") != row.revision
                   or by_id[row.id].get("content_hash") != block_binding(row) for row in blocks)):
        raise _error("REVIEW_COVERAGE_INCOMPLETE", "候选须覆盖全部源叶；容器和清点处置由服务器另行核对")
    refs = [_ref(item) for item in data.get("professional_confirmation_refs", [])]
    for id in refs:
        item = db.get(BusinessConfirmation, id)
        if (not item or item.document_version_id != version.id or item.kind != "key_content"
                or item.scope.get("parse_run_id") != run.id or item.conclusion != "accepted"
                or not confirmation_is_current(db, item)):
            raise _error("CRITICAL_CONFIRMATION_MISSING", "候选的每项专业依据均须当前有效并属于本源")
    if len(set(refs)) != len(refs) or source_verification(db, run, refs)["state"] != "verified":
        raise _error("CRITICAL_CONFIRMATION_MISSING", "源关键事实、关系或视觉处置的当前人工依据不完整")
    synchronize_source_status(db, run)
    row = ReviewSet(document_version_id=version.id, parse_run_id=run.id, chunk_set_id=chunk.id,
        required_refs=sorted(required), reviewed_refs=sorted(required), professional_confirmation_refs=refs,
        block_bindings={item.id: {"revision": item.revision, "content_hash": block_binding(item)} for item in blocks},
        binding_fingerprint=_review_binding(db, version, run, chunk, refs), reviewer_id=actor.id)
    db.add(row)
    from .services import audit
    db.flush()
    reason = data.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 500):
        raise _error("INVALID_REVIEW_REASON", "核验登记说明最长500字", 422)
    audit(db, actor.id, "review_set.candidate_recorded", "review_set", row.id, reason=reason)
    _changed(db, actor, version, "review_set.candidate_created", affects_formal=False)
    db.flush()
    return row


def _ir_review_is_current(db, version, run, chunk, review):
    from .chunk_jobs import chunk_set_qc_is_current
    from .parsing.processing import source_verification
    if (not chunk or chunk.parse_run_id != run.id or run.document_version_id != version.id
            or not chunk_set_qc_is_current(db, chunk)
            or source_verification(db, run, review.professional_confirmation_refs)["state"] != "verified"):
        return False
    leaves = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == run.id, ContentBlock.structure_role == "leaf")).all()
    if (set(review.required_refs) != {row.id for row in leaves} or set(review.reviewed_refs) != set(review.required_refs)
            or set(review.block_bindings) != set(review.required_refs)
            or any(row.review_status != "approved" or review.block_bindings[row.id] != {"revision": row.revision,
                      "content_hash": block_binding(row)} for row in leaves)):
        return False
    return review.binding_fingerprint == _review_binding(db, version, run, chunk, review.professional_confirmation_refs)


def select_source_candidate(db, actor, version, payload):
    manage(db, actor, db.get(Document, version.document_id))
    data = _data(payload)
    if set(data) != {"chunk_set_id", "review_set_id"} or version.publish_status != "draft":
        raise _error("SOURCE_SELECTION_REQUIRES_DRAFT", "草稿显式选用精确候选；已发布版本请走来源重绑")
    review = db.get(ReviewSet, data["review_set_id"])
    if (not review or review.chunk_set_id != data["chunk_set_id"]
            or not review_set_is_current(db, version, review, require_selected=False)):
        raise _error("REVIEW_COVERAGE_INCOMPLETE", "候选已失效或核验不完整")
    version.selected_chunk_set_id, version.selected_review_set_id = review.chunk_set_id, review.id
    _changed(db, actor, version, "version.source_selected", affects_formal=False)
    db.flush()
    return version


def _valid_rebind_chain(db, version, event):
    seen, root_id = set(), event.bound_revisions.get("root_publication_event_id")
    source = source_binding(db, version)
    while event and event.id not in seen and len(seen) < 64:
        seen.add(event.id)
        if event.document_version_id != version.id or event.bound_revisions.get("qualification", {}).get("source") != source:
            return False
        if event.action == "publish":
            return event.id == root_id
        if event.action != "source_rebind" or event.bound_revisions.get("root_publication_event_id") != root_id:
            return False
        event = db.get(PublicationEvent, event.bound_revisions.get("previous_source_event_id"))
    return False


def _source_event(db, version):
    return db.scalar(select(PublicationEvent).where(PublicationEvent.document_version_id == version.id,
        PublicationEvent.action.in_(("publish", "source_rebind"))).order_by(PublicationEvent.created_at.desc(), PublicationEvent.id.desc()))


def _rebind_candidate_snapshot(db, version, data):
    review = db.get(ReviewSet, data.get("review_set_id"))
    chunk = db.get(ChunkSet, data.get("chunk_set_id"))
    return {"chunk_set_id": chunk.id if chunk else None, "review_set_id": review.id if review else None,
            "review_fingerprint": review.binding_fingerprint if review else None,
            "current": bool(review and chunk and review.chunk_set_id == chunk.id
                             and review_set_is_current(db, version, review, require_selected=False))}


def _rebind_blockers(db, version, data):
    from .models import BlockRelation
    blockers = []
    candidate = _rebind_candidate_snapshot(db, version, data)
    if version.publish_status != "effective":
        blockers.append("VERSION_NOT_EFFECTIVE")
    if data.get("mode") not in {"refresh", "quality_repair"}:
        blockers.append("INVALID_REBIND_MODE")
    if not candidate["current"]:
        blockers.append("CANDIDATE_NOT_VERIFIED")
    event = _source_event(db, version)
    if not event or (event.action == "source_rebind" and not _valid_rebind_chain(db, version, event)):
        blockers.append("PUBLISH_IDENTITY_INVALID")
    root = db.get(PublicationEvent, event.bound_revisions.get("root_publication_event_id")) if event and event.action == "source_rebind" else event
    document = db.get(Document, version.document_id)
    if (not root or root.action != "publish" or root.document_version_id != version.id
            or root.bound_revisions.get("qualification", {}).get("source") != source_binding(db, version)
            or root.bound_revisions.get("qualification", {}).get("document_rules") != [document.access_level, document.category, document.need_class]):
        blockers.append("PUBLISH_BUSINESS_SCOPE_CHANGED")
    if not any(item.conclusion == "applicable" for item in current_confirmations(db, version, "applicability")):
        blockers.append("BUSINESS_CONFIRMATION_PENDING")
    if not any(item.conclusion == "classified" for item in current_confirmations(db, version, "classification")):
        blockers.append("CLASSIFICATION_PENDING")
    if (any(item.conclusion in {"conflict", "not_applicable"} for item in current_confirmations(db, version, "applicability"))
            or any(item.conclusion == "conflict" for item in current_confirmations(db, version, "classification"))):
        blockers.append("BUSINESS_SCOPE_CONFLICT")
    holds = db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == version.id,
                DocumentUseHold.released_at.is_(None))).all()
    if data.get("mode") == "refresh":
        if holds or not publication_is_current(db, version):
            blockers.append("CURRENT_SOURCE_NOT_VALID")
    elif data.get("mode") == "quality_repair":
        old_chunk = db.get(ChunkSet, version.selected_chunk_set_id) if version.selected_chunk_set_id else None
        old_run = db.get(ParseRun, old_chunk.parse_run_id) if old_chunk else None
        refs = {id for id in (version.selected_chunk_set_id, version.selected_review_set_id, old_run.id if old_run else None) if id}
        if old_run:
            refs.update(db.scalars(select(ContentBlock.id).where(ContentBlock.parse_run_id == old_run.id)))
            refs.update(db.scalars(select(BlockRelation.id).where(BlockRelation.parse_run_id == old_run.id)))
            refs.update(value for value in old_run.stage_checkpoint.get("outputs", {}).values() if isinstance(value, str))
        review = db.get(ReviewSet, data.get("review_set_id"))
        repairs = set()
        if review:
            for id in review.professional_confirmation_refs:
                proof = db.get(BusinessConfirmation, id)
                if proof and confirmation_is_current(db, proof) and proof.conclusion == "accepted":
                    repairs.update(proof.scope.get("repair_of_hold_refs", []))
        for hold in holds:
            if (hold.kind != "quality" or not hold.affected_refs or set(hold.affected_refs) - refs
                    or hold.id not in repairs):
                blockers.append("QUALITY_HOLD_REPAIR_UNPROVEN")
    return sorted(set(blockers))


def _create_rebind_preview(db, actor, document, version, data):
    allowed = {"action", "mode", "chunk_set_id", "review_set_id"}
    if set(data) != allowed:
        raise _error("INVALID_ACTION", "来源重绑须指定模式、候选集合及核验集", 422)
    blockers = _rebind_blockers(db, version, data)
    holds = db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == version.id,
                DocumentUseHold.released_at.is_(None))).all()
    impact = {**data, "old_chunk_set_id": version.selected_chunk_set_id, "old_review_set_id": version.selected_review_set_id,
        "quality_hold_refs": [row.id for row in holds], "repair_basis": {row.id: {"revision": row.revision, "affected_refs": row.affected_refs} for row in holds},
        "index_readiness": "requires_rebuild_after_source_rebind"}
    vector = revision_vector(db, actor, document, version)
    vector["rebind_candidate"] = _rebind_candidate_snapshot(db, version, impact)
    row = PublicationPreview(actor_id=actor.id, document_version_id=version.id, action="source_rebind",
        bound_revisions=vector, allowed=not blockers, blocked_reasons=blockers, impact=impact,
        expires_at=utc_now() + timedelta(minutes=5))
    db.add(row)
    db.flush()
    return row


def rebind_source_from_preview(db, actor, document, version, payload):
    row = _consume_preview(db, actor, document, version, payload, "source_rebind")
    previous = _source_event(db, version)
    root_id = previous.bound_revisions.get("root_publication_event_id") if previous.action == "source_rebind" else previous.id
    review = db.get(ReviewSet, row.impact["review_set_id"])
    repaired = []
    for id in row.impact["quality_hold_refs"]:
        hold = db.get(DocumentUseHold, id)
        if not hold or hold.released_at or hold.revision != row.impact["repair_basis"][id]["revision"]:
            raise _error("PUBLICATION_PREVIEW_STALE", "暂停清单已变化，请重新预检")
        hold.released_at, hold.released_by = utc_now(), actor.id
        hold.resolution_refs, hold.revision = [review.id, *review.professional_confirmation_refs], hold.revision + 1
        repaired.append(hold.id)
    version.selected_chunk_set_id, version.selected_review_set_id = review.chunk_set_id, review.id
    row.consumed_at = utc_now()
    event = PublicationEvent(actor_id=actor.id, document_version_id=version.id, preview_id=row.id, action="source_rebind",
        bound_revisions={**row.bound_revisions, "root_publication_event_id": root_id, "previous_source_event_id": previous.id,
            "mode": row.impact["mode"], "repaired_hold_refs": repaired}, previous_current_ids=[version.id])
    db.add(event)
    _changed(db, actor, version, "version.source_rebound", affects_formal=True)
    db.flush()
    event.bound_revisions = {**event.bound_revisions, "qualification": qualification_snapshot(db, version)}
    db.flush()
    return {"operation_receipt_ref": event.id, "index_readiness": "requires_rebuild", "next_actions": ["build_and_activate_index"],
            "selected_chunk_set_id": version.selected_chunk_set_id, "selected_review_set_id": version.selected_review_set_id}


def publication_blockers(db: Session, version: DocumentVersion) -> list[str]:
    version = db.get(DocumentVersion, version.id, populate_existing=True)
    document = db.get(Document, version.document_id, populate_existing=True)
    file = db.get(FileObject, version.source_file_id, populate_existing=True)
    blockers = []
    if not file or file.status != "available":
        blockers.append("SOURCE_UNAVAILABLE")
    if document.deleted_at or not all(getattr(document, x) for x in ("access_level", "category", "need_class")):
        blockers.append("ACCESS_RULES_INCOMPLETE")
    if not version.model_scope:
        blockers.append("MODEL_SCOPE_MISSING")
    if not version.source_metadata.get("channel") or not version.source_metadata.get("source_description"):
        blockers.append("SOURCE_METADATA_INCOMPLETE")
    applicable = current_confirmations(db, version, "applicability")
    if not applicable or any(x.conclusion != "applicable" for x in applicable):
        blockers.append("BUSINESS_CONFIRMATION_PENDING")
    classification = current_confirmations(db, version, "classification")
    if not classification or any(x.conclusion != "classified" for x in classification) or len({x.scope.get("access_level") for x in classification}) != 1:
        blockers.append("CLASSIFICATION_CONFIRMATION_PENDING")
    if not review_set_is_current(db, version, db.get(ReviewSet, version.selected_review_set_id) if version.selected_review_set_id else None):
        blockers.append("REVIEW_COVERAGE_INCOMPLETE")
    if db.scalar(select(DocumentUseHold.id).where(DocumentUseHold.document_version_id == version.id, DocumentUseHold.released_at.is_(None))):
        blockers.append("DOCUMENT_USE_HELD")
    return blockers


def publication_is_current(db: Session, version: DocumentVersion, historical: bool = False) -> bool:
    # Read-only authorization predicate: it never writes cached status columns.
    version = db.get(DocumentVersion, version.id, populate_existing=True)
    if version.publish_status not in ({"effective", "superseded"} if historical else {"effective"}) or publication_blockers(db, version):
        return False
    event = db.scalar(select(PublicationEvent).where(PublicationEvent.document_version_id == version.id,
        PublicationEvent.action.in_(("publish", "source_rebind"))).order_by(PublicationEvent.created_at.desc(), PublicationEvent.id.desc()))
    if not event:
        return False
    if event.action == "source_rebind" and not _valid_rebind_chain(db, version, event):
        return False
    # Unselected re-parsing is a new derivative, not a replacement of the selected
    # evidence. Bind only the selected facts and a hold fence, not every edit.
    expected = event.bound_revisions.get("qualification")
    return expected is not None and expected == qualification_snapshot(db, version)


def qualification_snapshot(db: Session, version: DocumentVersion) -> dict:
    document = db.get(Document, version.document_id, populate_existing=True)
    review = db.get(ReviewSet, version.selected_review_set_id) if version.selected_review_set_id else None
    confirmations = current_confirmations(db, version, "applicability") + current_confirmations(db, version, "classification")
    if review:
        confirmations += [db.get(BusinessConfirmation, ref) for ref in review.professional_confirmation_refs]
    holds = db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == version.id).order_by(DocumentUseHold.id)).all()
    return {"source": source_binding(db, version), "selected": [version.selected_chunk_set_id, version.selected_review_set_id],
        "review": review.binding_fingerprint if review else None,
        "confirmations": sorted([[x.id, x.revision, x.dependency_fingerprint] for x in confirmations if x], key=lambda x: x[0]),
        # Current ACL mode is an authorization condition, not a business
        # publication fact. It is rechecked for every read/use and remains in
        # preview's revision vector; tightening it must not erase valid facts.
        "document_rules": [document.access_level, document.category, document.need_class],
        # Retain released holds in this fence: release alone never reinstates a
        # prior publication; a subsequent explicit publication is necessary.
        "holds": [[x.id, x.revision, _time(x.released_at)] for x in holds]}


def revision_vector(db: Session, actor: User, document: Document, version: DocumentVersion) -> dict:
    kb = db.get(KnowledgeBase, document.knowledge_base_id)
    confirmations = db.scalars(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == version.id).order_by(BusinessConfirmation.id)).all()
    holds = db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == version.id).order_by(DocumentUseHold.id)).all()
    grants = db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == actor.id)).all()
    return {"document": document.revision, "version": version.revision, "actor": actor.authz_revision,
        "kb": [kb.acl_revision, kb.content_revision, kb.status], "source": source_binding(db, version),
        "selected": [version.selected_chunk_set_id, version.selected_review_set_id],
        "confirmations": [[x.id, x.revision, confirmation_is_current(db, x)] for x in confirmations],
        "review_current": review_set_is_current(db, version, db.get(ReviewSet, version.selected_review_set_id)) if version.selected_review_set_id else False,
        "holds": [[x.id, x.revision, _time(x.released_at)] for x in holds],
        "acl": digest([[x.id, x.approval_id, x.operation, x.knowledge_base_id, x.document_id, x.level, x.category, x.need_class,
            _time(x.valid_from), _time(x.valid_until), _time(x.revoked_at), approval_is_current(db, db.get(BusinessApproval, x.approval_id)) if x.approval_id else False] for x in grants]),
        "document_rules": [document.access_level, document.category, document.need_class, document.access_mode, _time(document.deleted_at)]}


def create_publication_preview(db: Session, actor: User, document: Document, version: DocumentVersion, payload: Any) -> PublicationPreview:
    manage(db, actor, document)
    action = _data(payload).get("action")
    if action == "source_rebind":
        return _create_rebind_preview(db, actor, document, version, _data(payload))
    if action not in {"publish", "withdraw"}:
        raise _error("INVALID_ACTION", "预检动作无效", 422)
    blockers = publication_blockers(db, version) if action == "publish" else []
    if action == "publish" and version.publish_status != "draft":
        blockers.append("VERSION_NOT_DRAFT")
    if action == "withdraw" and version.publish_status not in {"effective", "superseded"}:
        blockers.append("VERSION_NOT_PUBLISHED")
    row = PublicationPreview(actor_id=actor.id, document_version_id=version.id, action=action,
        bound_revisions=revision_vector(db, actor, document, version), allowed=not blockers,
        blocked_reasons=blockers, impact={"target_version_id": version.id, "previous_current_ids": db.scalars(select(DocumentVersion.id).where(DocumentVersion.document_id == document.id, DocumentVersion.publish_status == "effective")).all(),
            "affected_use_kinds": ["answer", "calculate", "report"], "index_readiness": "requires_rebuild_after_publication", "feedback_count": None},
        expires_at=utc_now() + timedelta(minutes=5))
    db.add(row)
    db.flush()
    return row


def _consume_preview(db: Session, actor: User, document: Document, version: DocumentVersion, payload: Any, action: str) -> PublicationPreview:
    db.flush()
    # Compare-and-swap serializes publication against other document writers on
    # SQLite; SELECT FOR UPDATE supplies the row lock on PostgreSQL.
    db.scalar(select(Document).where(Document.id == document.id).with_for_update())
    manage(db, actor, document)
    row = db.get(PublicationPreview, _data(payload).get("preview_id") or _data(payload).get("publication_preview_ref"))
    current_vector = revision_vector(db, actor, document, version)
    if row and action == "source_rebind":
        current_vector["rebind_candidate"] = _rebind_candidate_snapshot(db, version, row.impact)
    if not row or row.actor_id != actor.id or row.document_version_id != version.id or row.action != action or row.consumed_at or _utc(row.expires_at) <= utc_now() or not row.allowed or row.bound_revisions != current_vector:
        raise _error("PUBLICATION_PREVIEW_STALE", "预检已过期或依赖改变，请重新预检")
    if action == "publish" and (version.publish_status != "draft" or publication_blockers(db, version)):
        raise _error("PUBLICATION_PREVIEW_STALE", "发布资格已变化，请重新预检")
    if action == "source_rebind" and _rebind_blockers(db, version, row.impact):
        raise _error("PUBLICATION_PREVIEW_STALE", "候选或修复依据已变化，请重新预检")
    result = db.execute(update(Document).where(Document.id == document.id, Document.revision == row.bound_revisions["document"]).values(revision=Document.revision + 1).execution_options(synchronize_session=False))
    if result.rowcount != 1:
        raise _error("PRECONDITION_FAILED", "文档已改变", 412)
    db.refresh(document)
    return row


def publish_from_preview(db: Session, actor: User, document: Document, version: DocumentVersion, payload: Any) -> dict:
    row = _consume_preview(db, actor, document, version, payload, "publish")
    prior = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id, DocumentVersion.publish_status == "effective")).all()
    for old in prior:
        old.publish_status = "superseded"
        old.revision += 1
    version.publish_status = "effective"
    version.effective_at = _date(_data(payload).get("effective_at"))
    version.applicability_status, version.content_review_status = "confirmed", "approved"
    row.consumed_at = utc_now()
    event = PublicationEvent(actor_id=actor.id, document_version_id=version.id, preview_id=row.id, action="publish",
        bound_revisions=row.bound_revisions, previous_current_ids=[x.id for x in prior])
    db.add(event)
    _changed(db, actor, version, "version.published", affects_formal=True)
    # HTTP sessions deliberately disable autoflush. Qualification predicates
    # re-read current database facts, so flush the authorized state transition
    # inside this transaction before computing the immutable event snapshot.
    # Without this, populate_existing would replace effective with old draft.
    db.flush()
    event.bound_revisions = {**event.bound_revisions, "published_version_revision": version.revision,
                            "qualification": qualification_snapshot(db, version)}
    db.flush()
    return {"operation_receipt_ref": event.id, "index_readiness": "requires_rebuild", "next_actions": ["build_and_activate_index"]}


def withdraw_from_preview(db: Session, actor: User, document: Document, version: DocumentVersion, payload: Any) -> dict:
    row = _consume_preview(db, actor, document, version, payload, "withdraw")
    if version.publish_status not in {"effective", "superseded"}:
        raise _error("VERSION_NOT_PUBLISHED", "只能撤回已发布或历史版本")
    version.publish_status = "withdrawn"
    row.consumed_at = utc_now()
    event = PublicationEvent(actor_id=actor.id, document_version_id=version.id, preview_id=row.id, action="withdraw",
        reason=_data(payload).get("reason"), bound_revisions=row.bound_revisions,
        previous_current_ids=[version.id] if version.id in row.impact.get("previous_current_ids", []) else [])
    db.add(event)
    _changed(db, actor, version, "version.withdrawn", affects_formal=True)
    db.flush()
    return {"operation_receipt_ref": event.id, "index_readiness": "stale", "next_actions": []}


def create_hold(db: Session, actor: User, version: DocumentVersion, payload: Any) -> DocumentUseHold:
    manage(db, actor, db.get(Document, version.document_id))
    data = _data(payload)
    if data.get("kind") not in {"quality", "conflict"} or not data.get("reason"):
        raise _error("INVALID_HOLD", "暂停类型及原因必填", 422)
    row = DocumentUseHold(document_version_id=version.id, kind=data["kind"], reason=data["reason"], affected_refs=data.get("affected_refs", []), created_by=actor.id)
    db.add(row)
    _changed(db, actor, version, "version.held")
    db.flush()
    return row


def release_hold(db: Session, actor: User, hold: DocumentUseHold, payload: Any) -> DocumentUseHold:
    version = db.get(DocumentVersion, hold.document_version_id)
    manage(db, actor, db.get(Document, version.document_id))
    data = _data(payload)
    refs = [_ref(x) for x in data.get("resolution_refs", [])]
    if hold.released_at or not data.get("reason") or not refs:
        raise _error("HOLD_RESOLUTION_REQUIRED", "必须提供当前有效的消解依据")
    if hold.kind == "quality":
        if not any((review := db.get(ReviewSet, ref)) and _utc(review.reviewed_at) >= _utc(hold.created_at) and review_set_is_current(db, version, review, require_selected=False) for ref in refs):
            raise _error("REVIEW_COVERAGE_INCOMPLETE", "质量暂停需对应合格核验集")
    elif not any((item := db.get(BusinessConfirmation, ref)) and item.document_version_id == version.id and item.kind == "applicability" and item.conclusion == "applicable" and confirmation_is_current(db, item) and _utc(item.recorded_at) >= _utc(hold.created_at) for ref in refs):
        raise _error("BUSINESS_CONFIRMATION_PENDING", "业务冲突需暂停后登记的真实消解确认")
    hold.released_at, hold.released_by, hold.resolution_refs = utc_now(), actor.id, refs
    hold.revision += 1
    _changed(db, actor, version, "version.hold_released")
    db.flush()
    return hold


def set_reference_access(db: Session, actor: User, version: DocumentVersion, payload: Any) -> DocumentVersion:
    document = db.get(Document, version.document_id)
    manage(db, actor, document)
    data = _data(payload)
    if data.get("state") not in {"open", "closed"} or not data.get("reason"):
        raise _error("INVALID_REFERENCE_STATE", "参考状态及原因必填", 422)
    if version.publish_status != "draft":
        raise _error("VERSION_NOT_DRAFT", "参考开放仅适用于精确草稿")
    file = db.get(FileObject, version.source_file_id)
    if data["state"] == "open" and (not file or file.status != "available" or not all(getattr(document, k) for k in ("access_level", "category", "need_class"))):
        raise _error("ACCESS_RULES_INCOMPLETE", "原件和等级、类别、访问规则必须明确")
    version.reference_access_state = data["state"]
    version.reference_access_revision += 1
    db.get(KnowledgeBase, document.knowledge_base_id).acl_revision += 1
    _changed(db, actor, version, "version.reference_access_changed")
    db.flush()
    return version


def reference_is_readable(db: Session, actor: User, document: Document, version: DocumentVersion, *, purpose: str = "human_inline") -> bool:
    from .services import can_access_document
    version = db.get(DocumentVersion, version.id, populate_existing=True)
    conditions = version_access_conditions(db, version)
    file = db.get(FileObject, version.source_file_id, populate_existing=True)
    return bool(purpose == "human_inline" and version.document_id == document.id and version.publish_status == "draft" and
        version.reference_access_state == "open" and file and file.status == "available" and can_access_document(db, actor, document) and
        conditions and _tuple_allowed(db, actor, document, "document.read", conditions))


def publication_dependency_snapshot(db: Session, version: DocumentVersion) -> dict:
    """Safe exact dependency identity for runtime projection invalidation."""
    document = db.get(Document, version.document_id, populate_existing=True)
    kb = db.get(KnowledgeBase, document.knowledge_base_id, populate_existing=True)
    confirmations = db.scalars(select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == version.id).order_by(BusinessConfirmation.id)).all()
    review = db.get(ReviewSet, version.selected_review_set_id) if version.selected_review_set_id else None
    holds = db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == version.id).order_by(DocumentUseHold.id)).all()
    return {"source": source_binding(db, version), "version": [version.id, version.revision, version.publish_status],
        "document": [document.id, document.revision, document.access_level, document.category, document.need_class],
        "kb": [kb.id, kb.acl_revision, kb.content_revision, kb.active_generation_id],
        "confirmations": [[x.id, x.revision, x.supersedes_id, x.dependency_fingerprint, confirmation_is_current(db, x)] for x in confirmations],
        "review": [review.id, review.binding_fingerprint, review_set_is_current(db, version, review)] if review else None,
        "content": content_binding(db, review.parse_run_id) if review else {},
        "holds": [[x.id, x.revision, _time(x.released_at)] for x in holds]}


def version_access_conditions(db: Session, version: DocumentVersion) -> dict | None:
    """Version classification supplements, and never removes, document rules."""
    confirmations = current_confirmations(db, version, "classification")
    if not confirmations:
        if version.publish_status == "draft":
            document = db.get(Document, version.document_id)
            return {"level": document.access_level, "category": document.category, "need_class": document.need_class}
        return None
    conditions = [{"level": x.scope.get("access_level"), "category": x.scope.get("category"), "need_class": x.scope.get("need_class")} for x in confirmations]
    if any(x.conclusion != "classified" for x in confirmations) or len({digest(x) for x in conditions}) != 1 or not all(conditions[0].values()):
        return None
    return conditions[0]


def can_read_version(db: Session, actor: User, version: DocumentVersion, operation: str = "document.read", *, reference: bool = False, historical: bool = False) -> bool:
    from .services import can_access_document
    document = db.get(Document, version.document_id, populate_existing=True)
    if can_access_document(db, actor, document, "knowledge.manage") and operation == "document.read" and version.publish_status == "draft" and not reference:
        return True
    if reference:
        if not reference_is_readable(db, actor, document, version):
            return False
    elif not publication_is_current(db, version, historical=historical):
        return False
    conditions = version_access_conditions(db, version)
    return bool(conditions and can_access_document(db, actor, document, operation) and _tuple_allowed(db, actor, document, operation, conditions))


def query_has_version_conflict(db: Session, actor: User, model: str, kb_ids: list[str], context: dict | None) -> bool:
    """Report only conflicts on versions within the caller's current query scope."""
    from .services import can_access_document

    held = select(DocumentUseHold.id).where(
        DocumentUseHold.document_version_id == DocumentVersion.id,
        DocumentUseHold.kind == "conflict", DocumentUseHold.released_at.is_(None),
    ).exists()
    disputed = select(BusinessConfirmation.id).where(
        BusinessConfirmation.document_version_id == DocumentVersion.id,
        BusinessConfirmation.kind == "applicability", BusinessConfirmation.conclusion == "conflict",
        BusinessConfirmation.verification_state == "verified",
    ).exists()
    candidates = select(DocumentVersion, Document, held.label("held")).join(
        Document, Document.id == DocumentVersion.document_id,
    ).where(DocumentVersion.publish_status == "effective", held | disputed)
    if kb_ids:
        candidates = candidates.where(Document.knowledge_base_id.in_(kb_ids))
    for version, document, is_held in db.execute(candidates.execution_options(populate_existing=True)):
        if model not in version.model_scope:
            continue
        if not all(can_access_document(db, actor, document, operation, context=context)
                   for operation in ("document.read", "knowledge.query")):
            continue
        conditions = version_access_conditions(db, version)
        if not conditions or not all(_tuple_allowed(db, actor, document, operation, conditions)
                                     for operation in ("document.read", "knowledge.query")):
            continue
        if is_held or any(item.conclusion == "conflict" for item in current_confirmations(db, version, "applicability")):
            return True
    return False


def can_read_original_version(db: Session, actor: User, version: DocumentVersion) -> bool:
    """Human original reference is separate from formal evidence eligibility.

    A withdrawn published original may be read under current document and
    exact-version classification permissions, but cannot enter a new query,
    saved answer or model context through this predicate.
    """
    from .services import can_access_document
    version = db.get(DocumentVersion, version.id, populate_existing=True)
    document = db.get(Document, version.document_id, populate_existing=True)
    if can_access_document(db, actor, document, "knowledge.manage"):
        return True
    if version.publish_status == "draft":
        return reference_is_readable(db, actor, document, version)
    if version.publish_status not in {"effective", "superseded", "withdrawn"}:
        return False
    conditions = version_access_conditions(db, version)
    return bool(conditions and can_access_document(db, actor, document, "document.read")
                and _tuple_allowed(db, actor, document, "document.read", conditions))


def _tuple_allowed(db: Session, actor: User, document: Document, operation: str, conditions: dict) -> bool:
    from .services import approval_allows_grant, has_permission, _grant_valid
    if not has_permission(actor, operation):
        return False
    now = utc_now()
    for grant in db.scalars(select(AuthorizationGrant).where(AuthorizationGrant.subject_user_id == actor.id).execution_options(populate_existing=True)).all():
        approval = db.get(BusinessApproval, grant.approval_id, populate_existing=True) if grant.approval_id else None
        if not _grant_valid(grant, operation, now) or not approval_is_current(db, approval) or not approval_allows_grant(approval, grant, now):
            continue
        if any(getattr(grant, key) != value for key, value in conditions.items()):
            continue
        if grant.document_id == document.id or (document.access_mode == "inherit" and grant.document_id is None and grant.knowledge_base_id == document.knowledge_base_id):
            return True
    return False
