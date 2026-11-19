"""Append-only local governance facts. Trust roots have no HTTP creation route."""
from __future__ import annotations

from datetime import datetime
from typing import Any
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON
from .models import Base, new_id, utc_now


class AuthorityBasis(Base):
    __tablename__ = "authority_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    identity: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    purposes: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    scope: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    source_reference: Mapped[str] = mapped_column(Text)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class GovernanceEvidence(Base):
    __tablename__ = "governance_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_kind: Mapped[str] = mapped_column(String(50))
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    file_object_id: Mapped[str | None] = mapped_column(ForeignKey("file_objects.id"), nullable=True)
    source_reference: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    assertion: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    # Set only by controlled local initialization, never by public payloads.
    attested: Mapped[bool] = mapped_column(default=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class VerificationEvent(Base):
    __tablename__ = "governance_verification_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_kind: Mapped[str] = mapped_column(String(50), index=True)
    owner_id: Mapped[str] = mapped_column(String(36), index=True)
    decision: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)
    checked_refs: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    authority_basis_id: Mapped[str | None] = mapped_column(ForeignKey("authority_bases.id"), nullable=True)
    dependency_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    revision: Mapped[int] = mapped_column(Integer)


class ApprovalSupersession(Base):
    __tablename__ = "approval_supersessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    old_approval_id: Mapped[str] = mapped_column(ForeignKey("business_approvals.id"), unique=True)
    new_approval_id: Mapped[str] = mapped_column(ForeignKey("business_approvals.id"), unique=True)
    recorded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ApprovalRevocation(Base):
    __tablename__ = "approval_revocations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    approval_id: Mapped[str] = mapped_column(ForeignKey("business_approvals.id"), index=True)
    evidence_id: Mapped[str] = mapped_column(ForeignKey("governance_evidence.id"))
    authority_basis_id: Mapped[str] = mapped_column(ForeignKey("authority_bases.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reason: Mapped[str] = mapped_column(Text)
    dependency_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class BusinessConfirmation(Base):
    __tablename__ = "document_business_confirmations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    actual_confirmer: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    conclusion: Mapped[str] = mapped_column(String(40))
    proof_refs: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    supersedes_id: Mapped[str | None] = mapped_column(ForeignKey("document_business_confirmations.id"), nullable=True)
    recorded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    verification_state: Mapped[str] = mapped_column(String(30), default="pending")
    dependency_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class ReviewSet(Base):
    __tablename__ = "review_sets"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"))
    chunk_set_id: Mapped[str] = mapped_column(ForeignKey("chunk_sets.id"))
    required_refs: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    reviewed_refs: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    block_bindings: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    professional_confirmation_refs: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    binding_fingerprint: Mapped[str] = mapped_column(String(64))
    reviewer_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    reviewed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DocumentUseHold(Base):
    __tablename__ = "document_use_holds"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    reason: Mapped[str] = mapped_column(Text)
    affected_refs: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    released_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolution_refs: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class PublicationPreview(Base):
    __tablename__ = "publication_previews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    action: Mapped[str] = mapped_column(String(20))
    bound_revisions: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    allowed: Mapped[bool] = mapped_column(default=False)
    blocked_reasons: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    impact: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PublicationEvent(Base):
    __tablename__ = "document_publication_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    preview_id: Mapped[str] = mapped_column(ForeignKey("publication_previews.id"), unique=True)
    action: Mapped[str] = mapped_column(String(20))
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    bound_revisions: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    previous_current_ids: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class GovernanceOutbox(Base):
    __tablename__ = "governance_outbox"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    event_kind: Mapped[str] = mapped_column(String(100))
    resource_id: Mapped[str] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
