"""Scoped dialogue facts, logical tool receipts and query delivery records."""
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from .models import Base, new_id, utc_now


class Conversation(Base):
    __tablename__ = "conversations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ConversationMessage(Base):
    __tablename__ = "conversation_messages"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    author_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(20), default="user")
    content: Mapped[str] = mapped_column(Text)
    purpose: Mapped[str] = mapped_column(String(30))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContextFact(Base):
    __tablename__ = "context_facts"
    __table_args__ = (UniqueConstraint("conversation_id", "fact_key", "revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    conversation_id: Mapped[str] = mapped_column(ForeignKey("conversations.id"), index=True)
    fact_key: Mapped[str] = mapped_column(String(80))
    revision: Mapped[int] = mapped_column(Integer)
    value: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    scope: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    source_message_id: Mapped[str] = mapped_column(ForeignKey("conversation_messages.id"))
    state: Mapped[str] = mapped_column(String(20), default="confirmed")


class TaskInputRequest(Base):
    __tablename__ = "task_input_requests"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    execution_revision: Mapped[int] = mapped_column(Integer)
    context_revision: Mapped[int] = mapped_column(Integer, default=0)
    requested_field: Mapped[str] = mapped_column(String(40))
    reason: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="open")
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class TaskInputResolution(Base):
    __tablename__ = "task_input_resolutions"
    __table_args__ = (UniqueConstraint("request_id"), UniqueConstraint("response_message_id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"))
    request_id: Mapped[str] = mapped_column(ForeignKey("task_input_requests.id"))
    response_message_id: Mapped[str] = mapped_column(ForeignKey("conversation_messages.id"))
    responded_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    value: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ToolInvocation(Base):
    __tablename__ = "tool_invocations"
    __table_args__ = (UniqueConstraint("actor_user_id", "tool_name", "logical_action_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    tool_name: Mapped[str] = mapped_column(String(80))
    logical_action_key: Mapped[str] = mapped_column(String(200))
    args_hash: Mapped[str] = mapped_column(String(64))
    execution_revision: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(30), default="processing")
    output: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    evidence_ids: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class QueryAnswerSnapshot(Base):
    __tablename__ = "answer_snapshots"
    __table_args__ = (UniqueConstraint("task_id", "execution_revision"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    execution_revision: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON))
    evidence_ids: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON))
    validation_ref: Mapped[str] = mapped_column(ForeignKey("tool_invocations.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ToolCallBinding(Base):
    __tablename__ = "agent_tool_call_bindings"
    __table_args__ = (UniqueConstraint("run_id", "pi_tool_call_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    pi_tool_call_id: Mapped[str] = mapped_column(String(200))
    invocation_id: Mapped[str] = mapped_column(ForeignKey("tool_invocations.id"))


class RuntimeJournal(Base):
    __tablename__ = "runtime_journals"
    __table_args__ = (UniqueConstraint("run_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("agent_session_bindings.id"), index=True)
    generation: Mapped[int] = mapped_column(Integer)
    pi_session_id: Mapped[str] = mapped_column(String(36))
    working_key: Mapped[str] = mapped_column(String(500))
    snapshot_file_id: Mapped[str] = mapped_column(ForeignKey("file_objects.id"))
    content_sha256: Mapped[str] = mapped_column(String(64))
    version_ids: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    source_bindings: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    source_binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    runtime_compatibility: Mapped[dict[str, Any] | None] = mapped_column(JSON(none_as_null=True), nullable=True)
    recovery_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(30), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
