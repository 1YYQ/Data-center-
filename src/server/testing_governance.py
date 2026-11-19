"""Explicit synthetic fixtures only; never called by application initialization.

These helpers establish a local test witness before exercising the same
registration/verification/publication services as HTTP. They do not attest any
enterprise record or migrate legacy approvals to trusted facts.
"""
from datetime import timedelta
from sqlalchemy import select
from .models import BusinessApproval, ChunkSet, ContentBlock, Document, DocumentVersion, ParseRun, User, utc_now
from .governance_models import AuthorityBasis, GovernanceEvidence
from . import governance as g


def _authority(db, *, purpose, scope):
    row = AuthorityBasis(identity={"identity_label": "SYNTHETIC EXTERNAL WITNESS", "responsibility": "synthetic fixture authority"},
        purposes=[purpose], scope=scope, source_reference="synthetic://controlled-test-initialization",
        valid_from=utc_now() - timedelta(days=365), valid_until=utc_now() + timedelta(days=365))
    db.add(row)
    db.flush()
    return row, {**row.identity, "authority_basis_ref": row.id}


def _proof(db, actor, owner_kind, owner_id, assertion):
    row = GovernanceEvidence(owner_kind=owner_kind, owner_id=owner_id,
        source_reference={"source_description": "SYNTHETIC deterministic fixture witness", "traceable_ref": "synthetic://fixture"},
        assertion=assertion, attested=True, created_by=actor.id)
    db.add(row)
    db.flush()
    return row


def seed_verified_approval(db, approval: BusinessApproval):
    """Add explicit synthetic external facts to an existing synthetic approval."""
    db.flush()
    actor = db.get(User, approval.recorded_by or approval.applicant)
    from uuid import uuid4
    verifier = User(username="synthetic-verifier-" + uuid4().hex, display_name="SYNTHETIC scoped registrar", status="active", roles=["system_admin"])
    db.add(verifier)
    db.flush()
    db.add(AuthorityBasis(identity={"user_ref": verifier.id, "identity_label": "SYNTHETIC scoped registrar", "responsibility": "fixture verification"},
        purposes=["access.manage"], scope=dict(approval.scope), source_reference="synthetic://controlled-platform-assignment",
        valid_from=utc_now() - timedelta(days=1), valid_until=utc_now() + timedelta(days=365)))
    db.flush()
    basis, identity = _authority(db, purpose="access_approval", scope=dict(approval.scope))
    approval.actual_approver_snapshot, approval.actual_decision = identity, "approved"
    approval.approved_at = utc_now() - timedelta(minutes=1)
    approval.valid_from = approval.valid_from or utc_now() - timedelta(minutes=1)
    approval.valid_until = approval.valid_until or utc_now() + timedelta(days=30)
    approval.recorded_by = actor.id
    approval.status, approval.verification_state = "pending", "unverified"
    db.flush()
    proof = _proof(db, actor, "approval", approval.id, g._approval_assertion(approval))
    approval.proof_refs = [proof.id]
    db.flush()
    g.verify_approval(db, verifier, approval, {"decision": "verified", "reason": "synthetic witness matched", "checked_evidence_refs": [proof.id], "authority_basis_ref": basis.id})
    db.flush()
    return approval


def seed_confirmation(db, actor, version, kind, conclusion, scope, *, supersedes=None):
    basis, identity = _authority(db, purpose=kind, scope={k: scope[k] for k in ("version_id", "source_sha256", "scope_hash")})
    item = g.register_confirmation(db, actor, version, {"kind": kind, "actual_confirmer": identity,
        "confirmed_at": utc_now() - timedelta(seconds=1), "conclusion": conclusion, "scope": scope,
        "proof_refs": [], "supersedes_confirmation_id": supersedes})
    proof = _proof(db, actor, "version", version.id, g._confirmation_assertion(item))
    item.proof_refs = [proof.id]
    db.flush()
    g.verify_confirmation(db, actor, item, {"decision": "verified", "reason": "synthetic external statement matched",
        "checked_proof_refs": [proof.id], "authority_basis_ref": basis.id})
    db.flush()
    return item


def seed_verified_version(db, actor, document: Document, version: DocumentVersion, parse_run: ParseRun, chunk_set_id: str):
    if version.publish_status != "draft":
        raise ValueError("Synthetic publication helper requires a draft; never upgrades published legacy data")
    version.source_metadata = {"channel": "synthetic_fixture", "source_description": "SYNTHETIC local test original"}
    db.flush()
    blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == parse_run.id)).all()
    for block in blocks:
        if not block.locator:
            block.locator = {"section": "synthetic fixture section", "ordinal": block.ordinal}
        g.review_content_block(db, actor, parse_run, block, "approved", "synthetic local content checked")
    binding = g.source_binding(db, version)
    seed_confirmation(db, actor, version, "applicability", "applicable", binding)
    seed_confirmation(db, actor, version, "classification", "classified", {**binding,
        "access_level": document.access_level, "category": document.category, "need_class": document.need_class, "rule_version": "synthetic-rule-v1"})
    professional = seed_confirmation(db, actor, version, "key_content", "accepted", {**binding, "parse_run_id": parse_run.id,
        "block_bindings": {x.id: g.block_binding(x) for x in blocks}})
    db.flush()
    g.create_review_set(db, actor, version, {"parse_run_id": parse_run.id, "chunk_set_id": chunk_set_id,
        "reviewed_block_refs": [{"id": x.id, "revision": x.revision, "content_hash": g.block_binding(x)} for x in blocks], "professional_confirmation_refs": [professional.id], "reason": "synthetic complete review"})
    db.flush()
    preview = g.create_publication_preview(db, actor, document, version, {"action": "publish"})
    db.flush()
    result = g.publish_from_preview(db, actor, document, version, {"preview_id": preview.id})
    db.flush()
    return result
