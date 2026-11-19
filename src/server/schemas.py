"""HTTP payloads for the first-release API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, StringConstraints


class BootstrapInput(BaseModel):
    username: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=10)


class LoginInput(BaseModel):
    username: str
    password: str


class UserCreateInput(BaseModel):
    username: str = Field(min_length=3, max_length=200)
    display_name: str = Field(min_length=1, max_length=200)
    roles: list[str] = Field(default_factory=lambda: ["employee"])


class UserPatchInput(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    status: Literal["active", "disabled"] | None = None


class CredentialInput(BaseModel):
    password: str = Field(min_length=10)
    reason: str | None = None


class RolesInput(BaseModel):
    role_codes: list[str]


class GrantInput(BaseModel):
    user_id: str
    knowledge_base_id: str | None = None
    document_id: str | None = None
    operation: str
    level: str | None = None
    category: str | None = None
    need_class: str | None = None
    valid_until: datetime | None = None
    approval_id: str | None = None


class KnowledgeBaseInput(BaseModel):
    model_config = {"extra": "forbid"}
    name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class SourceMetadataInput(BaseModel):
    model_config = {"extra": "forbid"}
    channel: str | None = Field(default=None, max_length=200)
    source_description: str | None = Field(default=None, max_length=2000)
    received_at: datetime | None = None
    source_party: str | None = Field(default=None, max_length=500)
    traceable_ref: str | None = Field(default=None, max_length=2000)


class NewDocumentInput(BaseModel):
    model_config = {"extra": "forbid"}
    title: str | None = Field(default=None, max_length=300)
    category: str | None = Field(default=None, max_length=100)
    model_scope: list[str] = Field(default_factory=list, max_length=100)
    revision_label: str | None = Field(default=None, max_length=200)
    source_metadata: SourceMetadataInput = Field(default_factory=SourceMetadataInput)


class DocumentDeleteInput(BaseModel):
    model_config = {"extra": "forbid"}
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)]


class VersionPatchInput(BaseModel):
    model_config = {"extra": "forbid"}
    revision_label: str | None = None


class PublishInput(BaseModel):
    model_config = {"extra": "forbid"}
    effective_at: datetime | None = None
    publication_preview_ref: str | None = None
    preview_id: str | None = None


class WithdrawInput(BaseModel):
    model_config = {"extra": "forbid"}
    reason: str = Field(min_length=1, max_length=500)
    publication_preview_ref: str | None = None
    preview_id: str | None = None


class BlockReviewInput(BaseModel):
    review_status: Literal["approved", "rejected", "unreviewed"]
    reason: str | None = None


class QueryInput(BaseModel):
    service_mode: Literal["local", "selected_service"] = "local"
    model_config = {"extra": "forbid"}
    query: str = Field(min_length=1, max_length=5000)
    knowledge_base_ids: list[str] = Field(default_factory=list)
    model: str | None = None
    mode: Literal["retrieve", "answer"] = "retrieve"
    level: str | None = None
    category: str | None = None
    need_class: str | None = None
    conversation_id: str | None = None
    context_revision: int | None = Field(default=None, ge=0)


class BusinessApprovalInput(BaseModel):
    model_config = {"extra": "forbid"}
    applicant: str
    scope: dict[str, Any] = Field(default_factory=dict)
    operations: list[str] = Field(default_factory=list)
    valid_until: datetime | None = None
    evidence_locator: str | None = None
    actual_approver: dict[str, Any] | None = None
    decision: Literal["approved", "rejected"] | None = None
    approved_at: datetime | None = None
    valid_from: datetime | None = None
    proof_refs: list[Any] = Field(default_factory=list)
    supersedes_id: str | None = None


class ShareEntry(BaseModel):
    user_id: str
    expires_at: datetime | None = None


class ShareInput(BaseModel):
    model_config = {"extra": "forbid"}
    answer_snapshot_id: str | None = None
    entries: list[ShareEntry] = Field(max_length=100)


class CancelInput(BaseModel):
    reason: str = Field(min_length=1, max_length=500)


class RetryInput(BaseModel):
    reason: str = Field(default="用户请求重试", max_length=500)


class IndexInput(BaseModel):
    embedding_profile_id: Literal["local-keyword"] = "local-keyword"


class ParseInput(BaseModel):
    model_config = {"extra": "forbid"}
    mode: Literal["local", "selected_service"] = "local"


class ProviderCallInput(BaseModel):
    provider: str = Field(min_length=1, max_length=100)
    operation: str = Field(min_length=1, max_length=100)
    status: Literal["approved", "blocked", "succeeded", "failed"]
    data_scope: str = "none"
    cost_units: float = Field(default=0, ge=0)
    error_code: str | None = None


class BackupDrillInput(BaseModel):
    kind: Literal["backup", "restore", "deletion"]
    result: Literal["passed", "failed", "not_verified"] = "not_verified"
    notes: str | None = Field(default=None, max_length=500)


class GraphicKeyInput(BaseModel):
    label: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=80)]
    operation: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=300)]
    bbox_normalized: tuple[float, float, float, float]


class GraphicCellInput(BaseModel):
    row: int = Field(ge=0, strict=True)
    column: int = Field(ge=0, strict=True)
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=6000)]
    keys: list[GraphicKeyInput] = Field(min_length=1, max_length=24)


class GraphicTableInput(BaseModel):
    block_id: str = Field(min_length=1, max_length=36)
    source_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    cells: list[GraphicCellInput] = Field(min_length=1, max_length=100)


class CorrectionInput(BaseModel):
    model_config = {"extra": "forbid"}
    blocks: list[dict[str, Any]] = Field(default_factory=list, max_length=1000)
    graphic_tables: list[GraphicTableInput] = Field(default_factory=list, max_length=30)
