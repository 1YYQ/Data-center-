"""Small, explicit relational model for the first device-query release.

The production plan targets PostgreSQL.  SQLite is supported here so the full
workflow can be exercised locally without external infrastructure; the schema
uses portable columns and JSON snapshots for provider-specific data.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import Boolean, CheckConstraint, DateTime, Float, ForeignKey, ForeignKeyConstraint, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.mutable import MutableDict, MutableList
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON


def new_id() -> str:
    return str(uuid4())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    display_name: Mapped[str] = mapped_column(String(200))
    username: Mapped[str] = mapped_column(String(200), unique=True, index=True)
    status: Mapped[str] = mapped_column(String(20), default="disabled", index=True)
    credential_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    credential_state: Mapped[str] = mapped_column(String(20), default="pending")
    credential_revision: Mapped[int] = mapped_column(Integer, default=0)
    authz_revision: Mapped[int] = mapped_column(Integer, default=0)
    roles: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    token_hash: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(128))
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    authz_revision: Mapped[int] = mapped_column(Integer, default=0)


class Role(Base):
    __tablename__ = "roles"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(120))


class Permission(Base):
    __tablename__ = "permissions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    code: Mapped[str] = mapped_column(String(120), unique=True)
    description: Mapped[str] = mapped_column(String(300))


class AuthorizationGrant(Base):
    __tablename__ = "authorization_grants"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    subject_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    knowledge_base_id: Mapped[str | None] = mapped_column(ForeignKey("knowledge_bases.id"), nullable=True, index=True)
    document_id: Mapped[str | None] = mapped_column(ForeignKey("documents.id"), nullable=True, index=True)
    operation: Mapped[str] = mapped_column(String(120), index=True)
    level: Mapped[str | None] = mapped_column(String(80), nullable=True)
    category: Mapped[str | None] = mapped_column(String(80), nullable=True)
    need_class: Mapped[str | None] = mapped_column(String(80), nullable=True)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    approval_id: Mapped[str | None] = mapped_column(ForeignKey("business_approvals.id"), nullable=True)
    granted_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)


class BusinessApproval(Base):
    __tablename__ = "business_approvals"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    applicant: Mapped[str] = mapped_column(String(36))
    approver: Mapped[str | None] = mapped_column(String(36), nullable=True)
    recorded_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verification_state: Mapped[str] = mapped_column(String(30), default="unverified", server_default="unverified")
    actual_approver_snapshot: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    actual_decision: Mapped[str | None] = mapped_column(String(30), nullable=True)
    proof_refs: Mapped[list[Any]] = mapped_column(MutableList.as_mutable(JSON), default=list, server_default="[]")
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    verification_revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    verified_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dependency_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    scope: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    operations: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    valid_from: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    evidence_locator: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending")


class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    content_revision: Mapped[int] = mapped_column(Integer, default=0)
    acl_revision: Mapped[int] = mapped_column(Integer, default=0)
    active_generation_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FileObject(Base):
    __tablename__ = "file_objects"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    storage_name: Mapped[str] = mapped_column(String(80), default="local")
    object_key: Mapped[str] = mapped_column(String(500), unique=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    byte_size: Mapped[int] = mapped_column(Integer)
    mime_type: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    created_by: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Document(Base):
    __tablename__ = "documents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    title: Mapped[str] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    access_level: Mapped[str | None] = mapped_column(String(80), nullable=True)
    need_class: Mapped[str | None] = mapped_column(String(80), nullable=True)
    access_mode: Mapped[str] = mapped_column(String(30), default="inherit")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DocumentVersion(Base):
    __tablename__ = "document_versions"
    __table_args__ = (UniqueConstraint("document_id", "version_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_id: Mapped[str] = mapped_column(ForeignKey("documents.id"), index=True)
    version_no: Mapped[int] = mapped_column(Integer)
    revision_label: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_scope: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    applicability_status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    content_review_status: Mapped[str] = mapped_column(String(20), default="unreviewed", index=True)
    publish_status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    source_file_id: Mapped[str] = mapped_column(ForeignKey("file_objects.id"))
    selected_chunk_set_id: Mapped[str | None] = mapped_column(ForeignKey("chunk_sets.id"), nullable=True)
    selected_review_set_id: Mapped[str | None] = mapped_column(ForeignKey("review_sets.id"), nullable=True)
    source_metadata: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    reference_access_state: Mapped[str] = mapped_column(String(20), default="closed", server_default="closed")
    reference_access_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    effective_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ParseRun(Base):
    __tablename__ = "parse_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    origin: Mapped[str] = mapped_column(String(30), default="provider")
    provider_task_id: Mapped[str | None] = mapped_column(String(200), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    output_revision: Mapped[str] = mapped_column(String(40), default="local-1")
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    parent_run_id: Mapped[str | None] = mapped_column(ForeignKey("parse_runs.id"), nullable=True)
    provider: Mapped[str | None] = mapped_column(String(80), nullable=True)
    engine_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    model_revision: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_config: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    actual_profile: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    raw_contract: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    adapter_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    recovery_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    ir_schema_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    input_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ir_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("parse_artifacts.id"), nullable=True)
    processing_stage: Mapped[str] = mapped_column(String(30), default="ingestion", server_default="ingestion")
    stage_checkpoint: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ParseArtifact(Base):
    __tablename__ = "parse_artifacts"
    __table_args__ = (UniqueConstraint("parse_run_id", "logical_name"), UniqueConstraint("parse_run_id", "id"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    file_id: Mapped[str] = mapped_column(ForeignKey("file_objects.id"))
    kind: Mapped[str] = mapped_column(String(40))
    logical_name: Mapped[str] = mapped_column(String(500))
    producer: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    source_locator: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    derived_from_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("parse_artifacts.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ContentBlock(Base):
    __tablename__ = "content_blocks"
    __table_args__ = (
        UniqueConstraint("parse_run_id", "id", name="uq_content_blocks_run_id"),
        UniqueConstraint("parse_run_id", "ordinal", name="uq_content_blocks_run_ordinal"),
        CheckConstraint("parent_block_id IS NULL OR parent_block_id <> id", name="ck_content_blocks_not_own_parent"),
        ForeignKeyConstraint(["parse_run_id", "file_page"], ["document_pages.parse_run_id", "document_pages.file_page"],
                             name="fk_content_blocks_page").ddl_if(dialect="postgresql"),
        ForeignKeyConstraint(["parse_run_id", "parent_block_id"], ["content_blocks.parse_run_id", "content_blocks.id"],
                             name="fk_content_blocks_parent").ddl_if(dialect="postgresql"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    ordinal: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(40), default="paragraph")
    text_content: Mapped[str] = mapped_column(Text, default="")
    structured_content: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    locator: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    review_status: Mapped[str] = mapped_column(String(20), default="unreviewed", index=True)
    review_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    file_page: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_kind: Mapped[str | None] = mapped_column(String(30), nullable=True)
    raw_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    source_block_ref: Mapped[str | None] = mapped_column(ForeignKey("content_blocks.id"), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_block_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reading_order: Mapped[int | None] = mapped_column(Integer, nullable=True)
    heading_level: Mapped[int | None] = mapped_column(Integer, nullable=True)
    structure_role: Mapped[str | None] = mapped_column(String(20), nullable=True)


class BlockRelation(Base):
    __tablename__ = "block_relations"
    __table_args__ = (
        UniqueConstraint("parse_run_id", "from_block_id", "from_part", "relation_type", "to_block_id", "to_part", name="uq_block_relation_endpoints"),
        ForeignKeyConstraint(["parse_run_id", "from_block_id"], ["content_blocks.parse_run_id", "content_blocks.id"], name="fk_relation_from"),
        ForeignKeyConstraint(["parse_run_id", "to_block_id"], ["content_blocks.parse_run_id", "content_blocks.id"], name="fk_relation_to"),
        CheckConstraint("from_block_id <> to_block_id OR from_part <> to_part", name="ck_relation_not_same_endpoint"),
        Index("ix_relations_from", "parse_run_id", "from_block_id", "relation_type"),
        Index("ix_relations_to", "parse_run_id", "to_block_id", "relation_type"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"))
    from_block_id: Mapped[str] = mapped_column(String(36))
    to_block_id: Mapped[str] = mapped_column(String(36))
    from_part: Mapped[str] = mapped_column(Text, default="", server_default="")
    to_part: Mapped[str] = mapped_column(Text, default="", server_default="")
    relation_type: Mapped[str] = mapped_column(String(30))
    basis: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)


class DocumentPage(Base):
    __tablename__ = "document_pages"
    __table_args__ = (CheckConstraint("file_page >= 1", name="ck_document_pages_positive"),)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), primary_key=True)
    file_page: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_box_pt: Mapped[list | None] = mapped_column(JSON, nullable=True)
    crop_box_pt: Mapped[list | None] = mapped_column(JSON, nullable=True)
    rotation: Mapped[int | None] = mapped_column(Integer, nullable=True)
    user_unit: Mapped[float | None] = mapped_column(Float, nullable=True)
    width_pt: Mapped[float | None] = mapped_column(Float, nullable=True)
    height_pt: Mapped[float | None] = mapped_column(Float, nullable=True)
    printed_page: Mapped[str | None] = mapped_column(String(80), nullable=True)
    printed_page_basis: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    page_class: Mapped[str] = mapped_column(String(20), default="unknown")
    state: Mapped[str] = mapped_column(String(20), default="pending")
    coverage: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    issue_refs: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)
    transforms: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    raw_ref: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class ChunkSet(Base):
    __tablename__ = "chunk_sets"
    __table_args__ = (UniqueConstraint("parse_run_id", "config_hash", name="uq_chunk_sets_input"),
                     UniqueConstraint("id", "parse_run_id", name="uq_chunk_sets_run"))
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"), index=True)
    splitter_version: Mapped[str] = mapped_column(String(60), default="local-keyword-v1")
    status: Mapped[str] = mapped_column(String(20), default="ready")
    splitter_config: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    config_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ir_content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    tokenizer_revision: Mapped[str | None] = mapped_column(String(150), nullable=True)
    quality_artifact_id: Mapped[str | None] = mapped_column(ForeignKey("parse_artifacts.id"), nullable=True)
    stage_checkpoint: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")


class Chunk(Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("chunk_set_id", "id", name="uq_chunks_set_id"),
        UniqueConstraint("chunk_set_id", "ordinal", name="uq_chunks_ordinal"),
        UniqueConstraint("id", "chunk_set_id", "parse_run_id", name="uq_chunks_source"),
        ForeignKeyConstraint(["chunk_set_id", "parse_run_id"], ["chunk_sets.id", "chunk_sets.parse_run_id"], name="fk_chunks_run"),
        ForeignKeyConstraint(["chunk_set_id", "parent_chunk_id"], ["chunks.chunk_set_id", "chunks.id"], name="fk_chunks_parent"),
        ForeignKeyConstraint(["parse_run_id", "structure_root_id"], ["content_blocks.parse_run_id", "content_blocks.id"], name="fk_chunks_structure"),
        CheckConstraint("parent_chunk_id IS NULL OR parent_chunk_id <> id", name="ck_chunks_not_self"),
        CheckConstraint("level IN ('child','parent') AND (level <> 'parent' OR parent_chunk_id IS NULL)", name="ck_chunks_level"),
        Index("ix_chunks_parent", "chunk_set_id", "parent_chunk_id"),
    )
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    chunk_set_id: Mapped[str] = mapped_column(ForeignKey("chunk_sets.id"))
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"))
    ordinal: Mapped[int] = mapped_column(Integer)
    content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64))
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    level: Mapped[str] = mapped_column(String(10))
    parent_chunk_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    structure_root_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    kind: Mapped[str] = mapped_column(String(30))
    eligibility: Mapped[str] = mapped_column(String(30))
    required_relation_ids: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)
    assembly: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)
    support_group_refs: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)


class ChunkBlock(Base):
    __tablename__ = "chunk_blocks"
    __table_args__ = (
        ForeignKeyConstraint(["chunk_id", "chunk_set_id", "parse_run_id"], ["chunks.id", "chunks.chunk_set_id", "chunks.parse_run_id"], name="fk_mapping_chunk"),
        ForeignKeyConstraint(["parse_run_id", "block_id"], ["content_blocks.parse_run_id", "content_blocks.id"], name="fk_mapping_block"),
        Index("ix_mapping_block", "block_id", "chunk_id"),
    )
    chunk_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    block_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chunk_set_id: Mapped[str] = mapped_column(String(36))
    parse_run_id: Mapped[str] = mapped_column(String(36))
    ordinal: Mapped[int] = mapped_column(Integer)
    segments: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list)


class IndexGeneration(Base):
    __tablename__ = "index_generations"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    knowledge_base_id: Mapped[str] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    embedding_profile_id: Mapped[str] = mapped_column(String(100), default="local-keyword")
    source_revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="building", index=True)
    expected_chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    build_report: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class IndexSource(Base):
    __tablename__ = "index_sources"
    __table_args__ = (UniqueConstraint("index_generation_id", "document_version_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    index_generation_id: Mapped[str] = mapped_column(ForeignKey("index_generations.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"), index=True)
    parse_run_id: Mapped[str] = mapped_column(ForeignKey("parse_runs.id"))
    chunk_set_id: Mapped[str] = mapped_column(ForeignKey("chunk_sets.id"))


class QueryTask(Base):
    __tablename__ = "query_tasks"
    __table_args__ = (UniqueConstraint("owner_user_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="query")
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    effective_input: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    execution_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    auth_session_id: Mapped[str | None] = mapped_column(ForeignKey("auth_sessions.id"), nullable=True)
    authorization_basis: Mapped[str] = mapped_column(String(30), default="accepted_task", server_default="accepted_task")
    accepted_scope: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")
    conversation_id: Mapped[str | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    source_message_id: Mapped[str | None] = mapped_column(ForeignKey("conversation_messages.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    next_action: Mapped[str | None] = mapped_column(String(200), nullable=True)
    result_summary: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    warnings: Mapped[list[str]] = mapped_column(MutableList.as_mutable(JSON), default=list)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskStep(Base):
    __tablename__ = "task_steps"
    __table_args__ = (UniqueConstraint("task_id", "step_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    step_key: Mapped[str] = mapped_column(String(100))
    position: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_epoch: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    __table_args__ = (UniqueConstraint("step_id", "attempt_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    step_id: Mapped[str] = mapped_column(ForeignKey("task_steps.id"), index=True)
    attempt_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    diagnostic: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)


class TaskOutbox(Base):
    __tablename__ = "task_outbox"
    __table_args__ = (UniqueConstraint("step_id", "dispatch_no"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    step_id: Mapped[str] = mapped_column(ForeignKey("task_steps.id"), index=True)
    dispatch_no: Mapped[int] = mapped_column(Integer, default=1)
    event_type: Mapped[str] = mapped_column(String(80), default="execute_step")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, default=0)


class TaskEvidence(Base):
    __tablename__ = "task_evidence"
    __table_args__ = (ForeignKeyConstraint(["chunk_set_id", "chunk_id"], ["chunks.chunk_set_id", "chunks.id"], name="fk_evidence_exact_chunk")
                     .ddl_if(dialect="postgresql"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    document_version_id: Mapped[str] = mapped_column(ForeignKey("document_versions.id"))
    block_id: Mapped[str] = mapped_column(ForeignKey("content_blocks.id"))
    quote_snapshot: Mapped[str] = mapped_column(Text)
    locator: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    review_at_run: Mapped[str] = mapped_column(String(20), default="unreviewed")
    execution_revision: Mapped[int] = mapped_column(Integer, default=1, server_default="1")
    chunk_id: Mapped[str | None] = mapped_column(ForeignKey("chunks.id"), nullable=True)
    chunk_set_id: Mapped[str | None] = mapped_column(ForeignKey("chunk_sets.id"), nullable=True)
    evidence_kind: Mapped[str] = mapped_column(String(20), default="text", server_default="text")
    source_spans: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list, server_default="[]")
    asset_refs: Mapped[list] = mapped_column(MutableList.as_mutable(JSON), default=list, server_default="[]")
    source_binding_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_context: Mapped[dict] = mapped_column(MutableDict.as_mutable(JSON), default=dict, server_default="{}")


class TaskShare(Base):
    __tablename__ = "task_shares"
    __table_args__ = (UniqueConstraint("task_id", "recipient_user_id"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    answer_snapshot_id: Mapped[str | None] = mapped_column(ForeignKey("answer_snapshots.id"), nullable=True)
    recipient_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    granted_by: Mapped[str] = mapped_column(ForeignKey("users.id"))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")


class TaskArtifact(Base):
    __tablename__ = "task_artifacts"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(ForeignKey("query_tasks.id"), index=True)
    kind: Mapped[str] = mapped_column(String(80))
    validation_status: Mapped[str] = mapped_column(String(30), default="not_verified")
    file_object_id: Mapped[str | None] = mapped_column(ForeignKey("file_objects.id"), nullable=True)


class AuditEvent(Base):
    __tablename__ = "audit_events"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(120), index=True)
    resource_type: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ApiIdempotencyRecord(Base):
    __tablename__ = "api_idempotency_records"
    __table_args__ = (UniqueConstraint("actor_user_id", "idempotency_key"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    actor_user_id: Mapped[str] = mapped_column(ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    method: Mapped[str] = mapped_column(String(10))
    request_path: Mapped[str] = mapped_column(String(300))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="processing")
    resource_refs: Mapped[dict[str, Any]] = mapped_column(MutableDict.as_mutable(JSON), default=dict)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)


class ProviderCall(Base):
    __tablename__ = "provider_calls"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    provider: Mapped[str] = mapped_column(String(100), index=True)
    operation: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(30))
    data_scope: Mapped[str] = mapped_column(String(200), default="none")
    cost_units: Mapped[float] = mapped_column(default=0)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# Register bounded domain tables with the same metadata before migrations/tests.
# Module imports also support entry through one of those modules without a
# partially initialized class import cycle.
from . import execution_models, governance_models, runtime_models, service_models, service_use_models  # noqa: E402,F401
from . import parse_jobs  # noqa: E402,F401
from . import embedding_jobs  # noqa: E402,F401
