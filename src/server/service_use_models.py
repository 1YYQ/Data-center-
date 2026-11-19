"""Local synthetic business-use pins, purpose policy and durable call receipts.

None of these records grants access to enterprise documents or a vendor account.
"""
from __future__ import annotations
from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, event, inspect
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON
from .models import Base, new_id, utc_now


class ServiceSelection(Base):
    __tablename__ = "service_selections"
    purpose: Mapped[str] = mapped_column(String(40), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    epoch: Mapped[int] = mapped_column(Integer, default=0)
    revision_id: Mapped[str | None] = mapped_column(ForeignKey("service_revisions.id"), nullable=True)
    test_run_id: Mapped[str | None] = mapped_column(ForeignKey("service_test_runs.id"), nullable=True)
    binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)


class ServiceSelectionPreview(Base):
    __tablename__ = "service_selection_previews"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    purpose: Mapped[str] = mapped_column(String(40))
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(20))
    selection_revision: Mapped[int] = mapped_column(Integer)
    revision_id: Mapped[str | None] = mapped_column(ForeignKey("service_revisions.id"), nullable=True)
    test_run_id: Mapped[str | None] = mapped_column(ForeignKey("service_test_runs.id"), nullable=True)
    binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    profile_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed: Mapped[bool] = mapped_column(Boolean, default=False)


class SyntheticServiceInput(Base):
    __tablename__ = "synthetic_service_inputs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    purpose: Mapped[str] = mapped_column(String(40))
    resource_key: Mapped[str] = mapped_column(String(160))
    input_hash: Mapped[str] = mapped_column(String(64))
    provenance: Mapped[str] = mapped_column(String(80))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class ServiceUsePin(Base):
    __tablename__ = "service_use_pins"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    purpose: Mapped[str] = mapped_column(String(40))
    resource_key: Mapped[str] = mapped_column(String(160))
    revision_id: Mapped[str] = mapped_column(ForeignKey("service_revisions.id"))
    test_run_id: Mapped[str] = mapped_column(ForeignKey("service_test_runs.id"))
    binding_hash: Mapped[str] = mapped_column(String(64))
    selection_epoch: Mapped[int] = mapped_column(Integer)
    profile_epoch: Mapped[int] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    max_calls: Mapped[int] = mapped_column(Integer, default=12)
    calls_used: Mapped[int] = mapped_column(Integer, default=0)


class ServiceTaskSelection(Base):
    __tablename__ = "service_task_selections"
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), primary_key=True)
    required: Mapped[bool] = mapped_column(Boolean, default=True)
    pin_ids: Mapped[dict] = mapped_column(JSON, default=dict)


class ServiceUseCall(Base):
    __tablename__ = "service_use_calls"
    __table_args__ = (UniqueConstraint("pin_id", "request_key"), UniqueConstraint("pin_id", "ordinal"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    pin_id: Mapped[str] = mapped_column(ForeignKey("service_use_pins.id"), index=True)
    request_key: Mapped[str] = mapped_column(String(200))
    ordinal: Mapped[int] = mapped_column(Integer)
    method: Mapped[str] = mapped_column(String(8))
    purpose: Mapped[str] = mapped_column(String(20))
    target_hash: Mapped[str] = mapped_column(String(64))
    payload_hash: Mapped[str] = mapped_column(String(64))
    input_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(30), default="dispatching")
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    safe_error: Mapped[str | None] = mapped_column(String(80), nullable=True)
    usage: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    settled_cost: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(ServiceUsePin, "before_update")
def _immutable_pin(mapper, connection, target):
    if any(inspect(target).attrs[c.name].history.has_changes() for c in target.__table__.columns if c.name != "calls_used"):
        raise ValueError("Accepted service-use pins are immutable")


@event.listens_for(ServiceTaskSelection, "before_update")
def _immutable_task_selection(mapper, connection, target):
    raise ValueError("Accepted task selections are immutable")


@event.listens_for(SyntheticServiceInput, "before_update")
def _immutable_input(mapper, connection, target):
    if any(inspect(target).attrs[c.name].history.has_changes() for c in target.__table__.columns if c.name != "revoked"):
        raise ValueError("Synthetic input scope cannot be enlarged")
