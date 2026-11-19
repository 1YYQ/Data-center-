"""Service self-test records. These records grant no enterprise data access."""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON
from .models import Base, new_id, utc_now


class ServiceProfile(Base):
    __tablename__ = "service_profiles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(120))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    use_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    active_revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))


class ServiceRevision(Base):
    __tablename__ = "service_revisions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(ForeignKey("service_profiles.id"), index=True)
    endpoint_ref: Mapped[str] = mapped_column(String(80))
    adapter: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(100))
    configuration_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(ServiceRevision, "before_update")
def _immutable_revision(mapper, connection, target):
    raise ValueError("Service revisions are immutable")


class ServiceTestRun(Base):
    __tablename__ = "service_test_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    revision_id: Mapped[str] = mapped_column(ForeignKey("service_revisions.id"), index=True)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    fixture_id: Mapped[str] = mapped_column(String(80))
    binding_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="pending")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    checkpoint: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    safe_error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ServiceChangePreview(Base):
    __tablename__ = "service_change_previews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    profile_id: Mapped[str] = mapped_column(ForeignKey("service_profiles.id"))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))
    revision_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    profile_revision: Mapped[int] = mapped_column(Integer)
    binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    test_run_id: Mapped[str | None] = mapped_column(ForeignKey("service_test_runs.id"), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class ServiceRequestCall(Base):
    __tablename__ = "service_request_calls"
    __table_args__ = (UniqueConstraint("test_run_id", "ordinal"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    test_run_id: Mapped[str] = mapped_column(ForeignKey("service_test_runs.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(8))
    purpose: Mapped[str] = mapped_column(String(20))
    target_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="dispatching")
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
