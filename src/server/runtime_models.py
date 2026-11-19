"""Private runtime records. They are execution evidence, never authorization caches."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, String, Text, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .models import Base, new_id, utc_now


BRIDGE_PROFILE = {
    "protocol": "data-center-json-lines/1",
    "worker_methods": ["session.open", "run.prompt", "run.abort"],
    "host_methods": ["tool.invoke", "run.event"],
    "operations": ["answer"],
    "max_message_bytes": 1048576,
    "max_pending_messages": 16,
}
SESSION_SETTINGS = {
    "compaction": {"enabled": False, "reserveTokens": 128, "keepRecentTokens": 128},
    "retry": {"enabled": False, "maxRetries": 0, "provider": {"maxRetries": 0}},
    "cacheWarming": "off",
}
SESSION_RESOURCES = {
    "extensions": [], "skills": [], "prompts": [], "themes": [], "context_files": [],
    "builtin_tools": [], "disabled_builtin_extensions": ["mcp", "codemode"],
    "system_prompt": "Controlled local execution. Business context comes only from the private authorized projection.",
}
GATEWAY_POLICY = {"native_timeout_ms": 20000, "upstream_timeout_seconds": 5}
BAILIAN_GATEWAY_POLICY = {"native_timeout_ms": 25000, "upstream_timeout_seconds": 12}
JOURNAL_SOURCE_SCHEMA = "dc.runtime-journal-sources/1"
JOURNAL_COMPATIBILITY_SCHEMA = "dc.runtime-journal-profile/1"
MAX_RUNTIME_JOURNAL_BYTES = 16 * 1024 * 1024


class RuntimeProfile(Base):
    __tablename__ = "agent_runtime_profiles"
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    sdk_version: Mapped[str] = mapped_column(String(30))
    sdk_source_revision: Mapped[str] = mapped_column(String(64))
    artifact_manifest_hash: Mapped[str] = mapped_column(String(64))
    adapter: Mapped[str] = mapped_column(String(100))
    adapter_fingerprint: Mapped[str] = mapped_column(String(64))
    tool_set_hash: Mapped[str] = mapped_column(String(64))
    configuration_hash: Mapped[str] = mapped_column(String(64))
    configuration: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


@event.listens_for(RuntimeProfile, "before_delete")
@event.listens_for(RuntimeProfile, "before_update")
def _immutable_profile(mapper, connection, target):
    raise ValueError("Runtime profiles are immutable; publish a new revision")


class TaskBudget(Base):
    __tablename__ = "task_budgets"
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), primary_key=True)
    max_calls: Mapped[int] = mapped_column(Integer)
    max_reserved_tokens: Mapped[int] = mapped_column(Integer)
    reserved_calls: Mapped[int] = mapped_column(Integer, default=0)
    reserved_tokens: Mapped[int] = mapped_column(Integer, default=0)
    revision: Mapped[int] = mapped_column(Integer, default=0)


class AgentSessionBinding(Base):
    __tablename__ = "agent_session_bindings"
    __table_args__ = (UniqueConstraint("task_id"), ForeignKeyConstraint(["runtime_profile_id", "runtime_profile_revision"], ["agent_runtime_profiles.id", "agent_runtime_profiles.revision"]))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    runtime_profile_id: Mapped[str] = mapped_column(String(80))
    runtime_profile_revision: Mapped[int] = mapped_column(Integer)
    pi_session_id: Mapped[str] = mapped_column(String(36), unique=True, default=new_id)
    state: Mapped[str] = mapped_column(String(30), default="ready")
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    session_binding_id: Mapped[str] = mapped_column(ForeignKey("agent_session_bindings.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    origin_session_id: Mapped[str] = mapped_column(ForeignKey("auth_sessions.id"))
    step_id: Mapped[str] = mapped_column(ForeignKey("task_steps.id"))
    step_lease_epoch: Mapped[int] = mapped_column(Integer)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("task_attempts.id"))
    authorization_basis: Mapped[str] = mapped_column(String(30), default="accepted_task")
    execution_revision: Mapped[int] = mapped_column(Integer)
    lease_epoch: Mapped[int] = mapped_column(Integer)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    state: Mapped[str] = mapped_column(String(30), default="active")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ModelInputProjection(Base):
    __tablename__ = "model_input_projections"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    operation: Mapped[str] = mapped_column(String(30))
    business_purpose: Mapped[str] = mapped_column(String(30))
    context_json: Mapped[str] = mapped_column(Text)
    context_digest: Mapped[str] = mapped_column(String(64))
    dependency_digest: Mapped[str] = mapped_column(String(64))
    dependencies: Mapped[dict[str, Any]] = mapped_column(JSON)
    profile_configuration_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ModelCallAdmission(Base):
    __tablename__ = "model_call_admissions"
    __table_args__ = (UniqueConstraint("run_id", "request_key"), UniqueConstraint("projection_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"), index=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    projection_id: Mapped[str] = mapped_column(ForeignKey("model_input_projections.id"))
    request_key: Mapped[str] = mapped_column(String(200))
    context_digest: Mapped[str] = mapped_column(String(64))
    payload_digest: Mapped[str] = mapped_column(String(64))
    adapter: Mapped[str] = mapped_column(String(100))
    lease_epoch: Mapped[int] = mapped_column(Integer)
    reserved_tokens: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30), default="reserved", index=True)
    usage_state: Mapped[str] = mapped_column(String(30), default="unknown")
    usage_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    response_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
