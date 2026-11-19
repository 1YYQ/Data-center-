"""HTTP adapters for local governance; none of these routes creates a trust root."""
from __future__ import annotations

import json
from typing import Any
from fastapi import Depends, FastAPI, File, Form, Request, Response, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from .db import get_db
from .models import BusinessApproval, Document, DocumentVersion, FileObject, User
from .governance_models import BusinessConfirmation, DocumentUseHold, GovernanceEvidence, PublicationEvent, ReviewSet, VerificationEvent
from . import governance as g


def _iso(value):
    return value.isoformat() if value else None


def approval_view(db: Session, row: BusinessApproval) -> dict:
    from .governance_models import ApprovalRevocation
    revocation = db.scalar(select(ApprovalRevocation).where(ApprovalRevocation.approval_id == row.id))
    return {"id": row.id, "status": row.status, "current": g.approval_is_current(db, row), "applicant": row.applicant,
        "revoked": revocation is not None, "revocation_id": revocation.id if revocation else None,
        "actual_approver": row.actual_approver_snapshot, "decision": row.actual_decision, "approved_at": _iso(row.approved_at),
        "evidence_locator": row.evidence_locator,
        "recorded_by": row.recorded_by, "recorded_at": _iso(row.recorded_at), "scope": row.scope, "operations": row.operations,
        "valid_from": _iso(row.valid_from), "valid_until": _iso(row.valid_until), "proof_refs": row.proof_refs,
        "verification_state": row.verification_state, "verified_by": row.verified_by, "verified_at": _iso(row.verified_at),
        "dependency_fingerprint": row.dependency_fingerprint, "etag": f'"{row.revision}"'}


def confirmation_view(db: Session, row: BusinessConfirmation) -> dict:
    event = g._latest_verification(db, "confirmation", row.id)
    return {"id": row.id, "version_id": row.document_version_id, "kind": row.kind,
        "actual_confirmer": row.actual_confirmer, "confirmed_at": _iso(row.confirmed_at), "scope": row.scope,
        "conclusion": row.conclusion, "proof_refs": row.proof_refs, "supersedes_confirmation_id": row.supersedes_id,
        "recorded_by": row.recorded_by, "recorded_at": _iso(row.recorded_at), "verification_state": row.verification_state,
        "current": g.confirmation_is_current(db, row), "verified_by": event.actor_id if event else None,
        "verified_at": _iso(event.created_at) if event else None, "dependency_fingerprint": row.dependency_fingerprint,
        "etag": f'"{row.revision}"'}


def review_view(db: Session, row: ReviewSet) -> dict:
    from .models import ParseRun
    version = db.get(DocumentVersion, row.document_version_id)
    run = db.get(ParseRun, row.parse_run_id)
    return {"id": row.id, "version_id": row.document_version_id, "parse_run_id": row.parse_run_id,
        "chunk_set_id": row.chunk_set_id, "required_coverage": row.required_refs, "reviewed_block_refs": row.reviewed_refs,
        "block_bindings": row.block_bindings,
        "professional_confirmation_refs": row.professional_confirmation_refs,
        "coverage_state": "complete" if g.review_set_is_current(db, version, row, require_selected=False) else "stale",
        "is_ir": bool(run and run.ir_schema_version), "source_qc_ref": run.stage_checkpoint.get("outputs", {}).get("source_qc") if run else None,
        "selected": version.selected_review_set_id == row.id,
        "missing_refs": sorted(set(row.required_refs) - set(row.reviewed_refs)), "binding_fingerprint": row.binding_fingerprint,
        "reviewer": row.reviewer_id, "reviewed_at": _iso(row.reviewed_at), "etag": '"0"'}


def hold_view(db: Session, row: DocumentUseHold) -> dict:
    return {"id": row.id, "version_id": row.document_version_id, "kind": row.kind, "reason": row.reason,
        "affected_refs": row.affected_refs, "created_by": row.created_by, "created_at": _iso(row.created_at),
        "released_at": _iso(row.released_at), "released_by": row.released_by, "resolution_refs": row.resolution_refs,
        "etag": f'"{row.revision}"'}


def preview_view(db: Session, row) -> dict:
    from .services import etag
    return {"preview_id": row.id, "action": row.action, "allowed": row.allowed, "blocked_reasons": row.blocked_reasons,
        "bound_revisions": row.bound_revisions, "impact": row.impact, "expires_at": _iso(row.expires_at),
        "document_etag": etag(row.bound_revisions["document"])}


def register_governance_routes(app: FastAPI) -> None:
    from .services import (SafeError, current_session, has_permission, require_csrf, check_if_match,
        idempotency_record, complete_idempotency, fence_management_authority, audit, save_upload, file_path)

    def actor(request, db, write=False, admin=False):
        user, session = current_session(request, db, request.app.state.settings)
        if write:
            require_csrf(request, session)
        if admin and not has_permission(user, "access.manage"):
            raise SafeError("FORBIDDEN", "需要授权管理能力", 403)
        return user

    def objects(db, user, d, v):
        document, version = db.get(Document, d), db.get(DocumentVersion, v)
        if not document or not version or version.document_id != document.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        g.manage(db, user, document)
        return document, version

    def fence_document(db, user, document, version):
        # All document governance writes share the tombstone lock with DELETE.
        # Claim the parent before a Version/confirmation/hold CAS; taking a
        # child lock first can deadlock against publication's Doc -> Version.
        claimed = db.execute(update(Document).where(Document.id == document.id, Document.deleted_at.is_(None))
            .values(revision=Document.revision).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        db.refresh(document)
        db.refresh(version)
        if version.document_id != document.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        g.manage(db, user, document)

    def mutation(request, db, user, payload, create, model, view, status=200, revision_row=None, document_context=None):
        record, replay = idempotency_record(db, user, request, request.headers.get("Idempotency-Key", ""),
            json.dumps(payload, sort_keys=True, default=str).encode())
        if document_context is not None:
            # Receipt -> Doc -> child is the same order as logical deletion.
            # Replays also recheck current scope/tombstone before returning.
            fence_document(db, user, *document_context)
        if replay:
            result = db.get(model, record.resource_refs["governance_id"])
        else:
            if revision_row is not None:
                expected = revision_row.revision
                check_if_match(request.headers.get("If-Match"), expected)
                entity = type(revision_row)
                # Lock/claim the checked revision in the same transaction as
                # the mutation. A SELECT plus Python comparison loses updates
                # when two requests both read the old revision. No-op UPDATE
                # works on SQLite too; the domain operation increments once.
                claimed = db.execute(update(entity).where(entity.id == revision_row.id, entity.revision == expected)
                    .values(revision=entity.revision).execution_options(synchronize_session=False))
                if claimed.rowcount != 1:
                    raise SafeError("PRECONDITION_FAILED", "资源已发生变化，请刷新后重试", 412)
                db.refresh(revision_row)
            result = create()
            db.flush()
            audit(db, user.id, "governance." + model.__tablename__, model.__tablename__, result.id)
        if document_context is not None:
            document = document_context[0]
            user = fence_management_authority(db, user, document.knowledge_base_id,
                document_id=document.id, request=request)
            g.manage(db, user, document)
        if not replay:
            complete_idempotency(db, record, status_code=status, refs={"governance_id": result.id})
            db.commit()
        return view(db, result)

    def page(rows, cursor, limit):
        if cursor:
            indexes = [i for i, row in enumerate(rows) if row.id == cursor]
            rows = rows[indexes[0] + 1:] if indexes else []
        selected = rows[:max(1, min(limit, 100))]
        return selected, selected[-1].id if len(rows) > len(selected) and selected else None

    @app.get("/admin/business-approvals")
    def approvals(request: Request, cursor: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
        user = actor(request, db, admin=True)
        rows, next_cursor = page([x for x in db.scalars(select(BusinessApproval).order_by(BusinessApproval.id)).all() if g.approval_is_manageable(db, user, x)], cursor, limit)
        return {"items": [approval_view(db, x) for x in rows], "next_cursor": next_cursor}

    @app.get("/admin/business-approvals/{approval_id}")
    def approval(approval_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
        user = actor(request, db, admin=True)
        row = db.get(BusinessApproval, approval_id)
        if not row or not g.approval_is_manageable(db, user, row):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        response.headers["ETag"] = f'"{row.revision}"'
        return approval_view(db, row)

    @app.post("/admin/business-approvals/{approval_id}/verification")
    def approval_verification(approval_id: str, payload: dict[str, Any], request: Request, response: Response, db: Session = Depends(get_db)):
        user = actor(request, db, write=True, admin=True)
        row = db.get(BusinessApproval, approval_id)
        if not row or not g.approval_is_manageable(db, user, row):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        result = mutation(request, db, user, payload, lambda: g.verify_approval(db, user, row, payload), BusinessApproval, approval_view, revision_row=row)
        response.headers["ETag"] = result["etag"]
        return result

    @app.get("/admin/business-approvals/{approval_id}/verification-options")
    def approval_options(approval_id: str, request: Request, db: Session = Depends(get_db)):
        from .governance_models import AuthorityBasis
        from .models import utc_now
        from .services import _utc
        user = actor(request, db, admin=True)
        row = db.get(BusinessApproval, approval_id)
        if not row or not g.approval_is_manageable(db, user, row):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        identity = row.actual_approver_snapshot or {}
        basis = db.get(AuthorityBasis, identity.get("authority_basis_ref")) if identity.get("authority_basis_ref") else None
        now = utc_now()
        eligible = bool(basis and not basis.revoked_at and _utc(basis.valid_from) <= now
            and (not basis.valid_until or _utc(basis.valid_until) > now)
            and "access_approval" in basis.purposes and basis.scope
            and all(row.scope.get(k) == v for k, v in basis.scope.items())
            and all(identity.get(k) == v for k, v in basis.identity.items()))
        evidence = db.scalars(select(GovernanceEvidence).where(GovernanceEvidence.owner_kind == "approval",
            GovernanceEvidence.owner_id == row.id, GovernanceEvidence.revoked_at.is_(None))).all()
        return {"authority_bases": [{"id": basis.id, "identity_label": basis.identity.get("identity_label"),
            "responsibility": basis.identity.get("responsibility")} ] if eligible else [],
            "evidence": [{"id": x.id, "attested": x.attested, "has_file": bool(x.file_object_id),
                "source_description": x.source_reference.get("source_description"),
                "linked": x.id in (row.proof_refs or [])} for x in evidence],
            "proof_refs": row.proof_refs or [], "revision": row.revision}

    @app.patch("/admin/business-approvals/{approval_id}/evidence-refs")
    def approval_evidence_refs(approval_id: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True, admin=True)
        row = db.get(BusinessApproval, approval_id)
        if not row or not g.approval_is_manageable(db, user, row):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        def link():
            refs = payload.get("proof_refs")
            if set(payload) != {"proof_refs"} or not isinstance(refs, list) or len(refs) > 50 or any(not isinstance(x, str) for x in refs):
                raise SafeError("INVALID_EVIDENCE_REFS", "请选择本记录的依据材料", 422)
            if row.verification_state == "verified" or row.status != "pending":
                raise SafeError("IMMUTABLE_APPROVAL", "已核实记录须另行登记替代事实", 409)
            for ref in refs:
                proof = db.get(GovernanceEvidence, ref)
                if not proof or proof.owner_kind != "approval" or proof.owner_id != row.id or proof.revoked_at:
                    raise SafeError("INVALID_EVIDENCE_REFS", "材料不属于当前记录或已失效", 422)
            changed = db.execute(update(BusinessApproval).where(BusinessApproval.id == row.id,
                BusinessApproval.revision == row.revision, BusinessApproval.status == "pending",
                BusinessApproval.verification_state != "verified")
                .values(proof_refs=list(dict.fromkeys(refs)), revision=BusinessApproval.revision + 1)
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise SafeError("PRECONDITION_FAILED", "记录已改变，请刷新后重试", 412)
            db.refresh(row)
            return row
        return mutation(request, db, user, payload, link, BusinessApproval, approval_view, revision_row=row)

    @app.post("/admin/business-approvals/{approval_id}/revocations", status_code=201)
    def approval_revocation(approval_id: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        from .governance_models import ApprovalRevocation
        user = actor(request, db, write=True, admin=True)
        row = db.get(BusinessApproval, approval_id)
        if not row or not g.approval_is_manageable(db, user, row):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return mutation(request, db, user, payload, lambda: g.revoke_approval(db, user, row, payload), ApprovalRevocation,
            lambda db, event: {"id": event.id, "approval_id": event.approval_id, "current": g.approval_is_current(db, db.get(BusinessApproval, event.approval_id))}, status=201, revision_row=row)

    @app.get("/documents/{d}/versions/{v}/source-metadata")
    def get_source_metadata(d: str, v: str, request: Request, response: Response, db: Session = Depends(get_db)):
        user = actor(request, db)
        document, version = objects(db, user, d, v)
        response.headers["ETag"] = f'"{version.revision}"'
        return {"version_id": version.id, "source_metadata": version.source_metadata or {},
                "revision": version.revision, "etag": f'"{version.revision}"'}

    @app.patch("/documents/{d}/versions/{v}/source-metadata")
    def source_metadata(d: str, v: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        allowed = {"channel", "source_description", "received_at", "source_party", "traceable_ref"}
        if set(payload) - allowed:
            raise SafeError("INVALID_SOURCE_METADATA", "仅接收来源登记字段", 422)
        def change():
            version.source_metadata = payload
            g._changed(db, user, version, "version.source_metadata_changed")
            return version
        return mutation(request, db, user, payload, change, DocumentVersion,
            lambda db, x: {"version_id": x.id, "source_metadata": x.source_metadata, "etag": f'"{x.revision}"'},
            revision_row=version, document_context=(document, version))

    @app.get("/documents/{d}/versions/{v}/confirmation-options")
    def confirmation_options(d: str, v: str, request: Request, db: Session = Depends(get_db)):
        from .models import ParseRun, ContentBlock, utc_now
        from .governance_models import AuthorityBasis
        from .services import _utc
        user = actor(request, db)
        document, version = objects(db, user, d, v)
        binding = g.source_binding(db, version)
        runs = db.scalars(select(ParseRun).where(ParseRun.document_version_id == v)).all()
        run_bindings = {run.id: {block.id: g.block_binding(block) for block in db.scalars(
            select(ContentBlock).where(ContentBlock.parse_run_id == run.id)).all()} for run in runs}
        from .parsing.processing import source_confirmation_scope
        source_scopes = {run.id: source_confirmation_scope(db, run) for run in runs if run.ir_artifact_id}
        authorities = []
        for basis in db.scalars(select(AuthorityBasis)).all():
            if basis.revoked_at or _utc(basis.valid_from) > utc_now() or (basis.valid_until and _utc(basis.valid_until) <= utc_now()):
                continue
            scope = basis.scope or {}
            if not basis.source_reference or not (scope.get("version_id") == v or scope.get("knowledge_base_id") == document.knowledge_base_id):
                continue
            if any(scope[k] != value for k, value in binding.items() if k in scope):
                continue
            if scope.get("parse_run_id") and scope["parse_run_id"] not in run_bindings:
                continue
            purposes = [x for x in basis.purposes if x in {"applicability", "classification", "key_content"}]
            if purposes:
                authorities.append({"id": basis.id, "identity_label": basis.identity.get("identity_label"),
                    "responsibility": basis.identity.get("responsibility"), "identity": dict(basis.identity),
                    "purposes": purposes, "scope": scope})
        quality_holds = db.scalars(select(DocumentUseHold).where(DocumentUseHold.document_version_id == v,
                    DocumentUseHold.kind == "quality", DocumentUseHold.released_at.is_(None))).all()
        return {"source_binding": binding, "quality_holds": [{"id": row.id, "reason": row.reason,
                    "affected_refs": row.affected_refs, "revision": row.revision} for row in quality_holds],
            "classification": {"access_level": document.access_level,
            "category": document.category, "need_class": document.need_class},
            "run_block_bindings": run_bindings, "run_source_bindings": source_scopes,
            "authority_bases": authorities, "revision": version.revision}

    @app.get("/documents/{d}/versions/{v}/business-confirmations/{c}/verification-options")
    def confirmation_evidence_options(d: str, v: str, c: str, request: Request, db: Session = Depends(get_db)):
        user = actor(request, db)
        objects(db, user, d, v)
        row = db.get(BusinessConfirmation, c)
        if not row or row.document_version_id != v:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        evidence = db.scalars(select(GovernanceEvidence).where(GovernanceEvidence.owner_kind == "version",
            GovernanceEvidence.owner_id == v, GovernanceEvidence.revoked_at.is_(None))).all()
        return {"proof_refs": row.proof_refs, "revision": row.revision,
            "evidence": [{"id": x.id, "attested": x.attested, "has_file": bool(x.file_object_id), "source_description": x.source_reference.get("source_description"),
                "linked": x.id in row.proof_refs} for x in evidence]}

    @app.patch("/documents/{d}/versions/{v}/business-confirmations/{c}/evidence-refs")
    def confirmation_evidence_refs(d: str, v: str, c: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        row = db.get(BusinessConfirmation, c)
        if not row or row.document_version_id != v:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        def link():
            refs = payload.get("proof_refs")
            if set(payload) != {"proof_refs"} or not isinstance(refs, list) or len(refs) > 50 or any(not isinstance(x, str) for x in refs):
                raise SafeError("INVALID_EVIDENCE_REFS", "请选择本记录的依据材料", 422)
            if row.verification_state != "pending":
                raise SafeError("IMMUTABLE_CONFIRMATION", "已核查事实须另行登记替代记录", 409)
            for ref in refs:
                proof = db.get(GovernanceEvidence, ref)
                if not proof or proof.owner_kind != "version" or proof.owner_id != v or proof.revoked_at:
                    raise SafeError("INVALID_EVIDENCE_REFS", "材料不属于当前版本或已失效", 422)
            changed = db.execute(update(BusinessConfirmation).where(BusinessConfirmation.id == c,
                BusinessConfirmation.revision == row.revision, BusinessConfirmation.verification_state == "pending")
                .values(proof_refs=list(dict.fromkeys(refs)), revision=BusinessConfirmation.revision + 1)
                .execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise SafeError("PRECONDITION_FAILED", "记录已改变，请刷新后重试", 412)
            db.refresh(row)
            return row
        return mutation(request, db, user, payload, link, BusinessConfirmation, confirmation_view,
            revision_row=row, document_context=(document, version))

    @app.get("/documents/{d}/versions/{v}/business-confirmations")
    def confirmations(d: str, v: str, request: Request, kind: str | None = None, state: str | None = None, cursor: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
        user = actor(request, db)
        objects(db, user, d, v)
        query = select(BusinessConfirmation).where(BusinessConfirmation.document_version_id == v)
        if kind:
            query = query.where(BusinessConfirmation.kind == kind)
        if state:
            query = query.where(BusinessConfirmation.verification_state == state)
        rows, next_cursor = page(db.scalars(query.order_by(BusinessConfirmation.id)).all(), cursor, limit)
        return {"items": [confirmation_view(db, x) for x in rows], "next_cursor": next_cursor}

    @app.get("/documents/{d}/versions/{v}/business-confirmations/{c}")
    def confirmation(d: str, v: str, c: str, request: Request, response: Response, db: Session = Depends(get_db)):
        user = actor(request, db)
        objects(db, user, d, v)
        row = db.get(BusinessConfirmation, c)
        if not row or row.document_version_id != v:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        response.headers["ETag"] = f'"{row.revision}"'
        return confirmation_view(db, row)

    @app.post("/documents/{d}/versions/{v}/business-confirmations", status_code=201)
    def confirmation_create(d: str, v: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        return mutation(request, db, user, payload, lambda: g.register_confirmation(db, user, version, payload),
            BusinessConfirmation, confirmation_view, status=201, document_context=(document, version))

    @app.post("/documents/{d}/versions/{v}/business-confirmations/{c}/verification")
    def confirmation_verify(d: str, v: str, c: str, payload: dict[str, Any], request: Request, response: Response, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        row = db.get(BusinessConfirmation, c)
        if not row or row.document_version_id != v:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        result = mutation(request, db, user, payload, lambda: g.verify_confirmation(db, user, row, payload),
            BusinessConfirmation, confirmation_view, revision_row=row, document_context=(document, version))
        response.headers["ETag"] = result["etag"]
        return result

    @app.get("/documents/{d}/versions/{v}/review-sets")
    def reviews(d: str, v: str, request: Request, cursor: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
        user = actor(request, db)
        objects(db, user, d, v)
        rows, next_cursor = page(db.scalars(select(ReviewSet).where(ReviewSet.document_version_id == v).order_by(ReviewSet.id)).all(), cursor, limit)
        return {"items": [review_view(db, x) for x in rows], "next_cursor": next_cursor}

    @app.get("/documents/{d}/versions/{v}/review-sets/{r}")
    def review(d: str, v: str, r: str, request: Request, response: Response, db: Session = Depends(get_db)):
        user = actor(request, db)
        objects(db, user, d, v)
        row = db.get(ReviewSet, r)
        if not row or row.document_version_id != v:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        response.headers["ETag"] = '"0"'
        return review_view(db, row)

    @app.post("/documents/{d}/versions/{v}/review-sets", status_code=201)
    def review_create(d: str, v: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        return mutation(request, db, user, payload, lambda: g.create_review_set(db, user, version, payload),
            ReviewSet, review_view, status=201, revision_row=version, document_context=(document, version))

    @app.post("/documents/{d}/versions/{v}/publication-preview")
    def publication_preview(d: str, v: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        from .governance_models import PublicationPreview
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        return mutation(request, db, user, payload, lambda: g.create_publication_preview(db, user, document, version, payload),
            PublicationPreview, preview_view, document_context=(document, version))

    @app.post("/documents/{d}/versions/{v}/source-selection")
    def source_selection(d: str, v: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        return mutation(request, db, user, payload, lambda: g.select_source_candidate(db, user, version, payload), DocumentVersion,
            lambda db, row: {"version_id": row.id, "selected_chunk_set_id": row.selected_chunk_set_id,
                "selected_review_set_id": row.selected_review_set_id, "etag": f'"{row.revision}"'},
            revision_row=version, document_context=(document, version))

    @app.post("/documents/{d}/versions/{v}/source-rebind")
    def source_rebind(d: str, v: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        if set(payload) != {"preview_id"} or not isinstance(payload["preview_id"], str):
            raise SafeError("INVALID_INPUT", "请提交来源预检标识", 422)
        def rebind():
            result = g.rebind_source_from_preview(db, user, document, version, payload)
            return db.get(PublicationEvent, result["operation_receipt_ref"])
        return mutation(request, db, user, payload, rebind, PublicationEvent,
            lambda db, row: {"operation_receipt_ref": row.id, "index_readiness": "requires_rebuild",
                "selected_chunk_set_id": row.bound_revisions["qualification"]["selected"][0],
                "selected_review_set_id": row.bound_revisions["qualification"]["selected"][1]},
            revision_row=document, document_context=(document, version))

    @app.post("/documents/{d}/versions/{v}/holds", status_code=201)
    def hold_create(d: str, v: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        return mutation(request, db, user, payload, lambda: g.create_hold(db, user, version, payload),
            DocumentUseHold, hold_view, status=201, revision_row=version, document_context=(document, version))

    @app.post("/documents/{d}/versions/{v}/holds/{h}/release")
    def hold_release(d: str, v: str, h: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        row = db.get(DocumentUseHold, h)
        if not row or row.document_version_id != v:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return mutation(request, db, user, payload, lambda: g.release_hold(db, user, row, payload),
            DocumentUseHold, hold_view, revision_row=row, document_context=(document, version))

    @app.put("/documents/{d}/versions/{v}/reference-access")
    def reference_access(d: str, v: str, payload: dict[str, Any], request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        return mutation(request, db, user, payload, lambda: g.set_reference_access(db, user, version, payload), DocumentVersion,
            lambda db, x: {"version_id": x.id, "reference_access_state": x.reference_access_state, "read_mode": "reference_only" if x.reference_access_state == "open" else "closed", "etag": f'"{x.revision}"'},
            revision_row=version, document_context=(document, version))

    def upload_evidence(request, db, user, owner_kind, owner_id, metadata, upload, document_context=None):
        if not metadata.get("source_description"):
            raise SafeError("SOURCE_REQUIRED", "依据来源描述必填", 422)
        raw = upload.file.read(request.app.state.settings.max_upload_bytes + 1)
        body = {"metadata": metadata, "sha256": g.digest(raw.hex()), "filename": upload.filename}
        def create():
            file = save_upload([raw], upload.filename or "evidence.pdf", upload.content_type or "application/octet-stream", user, request.app.state.settings)
            db.add(file)
            db.flush()
            row = GovernanceEvidence(owner_kind=owner_kind, owner_id=owner_id, file_object_id=file.id,
                source_reference={"source_description": metadata["source_description"], "purpose": metadata.get("purpose"), "sha256": file.sha256},
                created_by=user.id, attested=False)
            db.add(row)
            return row
        return mutation(request, db, user, body, create, GovernanceEvidence,
            lambda db, row: {"id": row.id, "owner_kind": row.owner_kind, "owner_id": row.owner_id, "verification_state": "unverified"},
            status=201, document_context=document_context)

    @app.post("/documents/{d}/versions/{v}/confirmation-evidence", status_code=201)
    def confirmation_evidence(d: str, v: str, request: Request, file: UploadFile = File(...), metadata: str = Form(...), db: Session = Depends(get_db)):
        user = actor(request, db, write=True)
        document, version = objects(db, user, d, v)
        try:
            data = json.loads(metadata)
        except ValueError:
            raise SafeError("INVALID_METADATA", "依据元数据无效", 422)
        return upload_evidence(request, db, user, "version", v, data, file, document_context=(document, version))

    @app.post("/admin/approval-evidence", status_code=201)
    def approval_evidence(request: Request, file: UploadFile = File(...), metadata: str = Form(...), db: Session = Depends(get_db)):
        user = actor(request, db, write=True, admin=True)
        try:
            data = json.loads(metadata)
        except ValueError:
            raise SafeError("INVALID_METADATA", "依据元数据无效", 422)
        row = db.get(BusinessApproval, data.get("approval_id"))
        if not row or not g.approval_is_manageable(db, user, row):
            raise SafeError("NOT_FOUND", "需绑定已有待核查审批", 404)
        return upload_evidence(request, db, user, "approval", row.id, data, file)

    def evidence_file(db, request, row):
        if not row or not row.file_object_id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        file = db.get(FileObject, row.file_object_id)
        if not file or file.status != "available":
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return FileResponse(file_path(file, request.app.state.settings), media_type=file.mime_type,
            headers={"Content-Disposition": 'inline; filename="evidence.pdf"', "Cache-Control": "private, no-store"})

    @app.get("/documents/{d}/versions/{v}/confirmation-evidence/{e}/file")
    def confirmation_evidence_file(d: str, v: str, e: str, request: Request, db: Session = Depends(get_db)):
        user = actor(request, db)
        objects(db, user, d, v)
        row = db.get(GovernanceEvidence, e)
        if not row or row.owner_kind != "version" or row.owner_id != v:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return evidence_file(db, request, row)

    @app.get("/admin/approval-evidence/{e}/file")
    def approval_evidence_file(e: str, request: Request, db: Session = Depends(get_db)):
        user = actor(request, db, admin=True)
        row = db.get(GovernanceEvidence, e)
        approval = db.get(BusinessApproval, row.owner_id) if row else None
        if not row or row.owner_kind != "approval" or not approval or not g.approval_is_manageable(db, user, approval):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return evidence_file(db, request, row)

    @app.get("/knowledge/documents/{d}/versions/{v}/history")
    def history(d: str, v: str, request: Request, cursor: str | None = None, limit: int = 50, db: Session = Depends(get_db)):
        from .models import AuditEvent
        user = actor(request, db)
        objects(db, user, d, v)
        rows, next_cursor = page(db.scalars(select(AuditEvent).where(AuditEvent.resource_type == "document_version", AuditEvent.resource_id == v).order_by(AuditEvent.created_at, AuditEvent.id)).all(), cursor, limit)
        return {"items": [{"id": row.id, "actor": row.actor_user_id, "time": _iso(row.created_at), "action": row.action, "reason": row.reason} for row in rows], "next_cursor": next_cursor}
