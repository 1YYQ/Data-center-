"""Validate a model's organization plan against server-owned, source-bound evidence."""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


PLAN_VERSION = "answer-plan/1"
RENDERER_VERSION = "answer-renderer/1"
HEADING_LABELS = {"operation": "操作", "maintenance": "维护", "precautions": "注意事项", "sources": "来源"}
RENDERER_KEYS = {"fact_group": "source-fact/v1", "procedure_group": "source-procedure/v1"}
MEMBER_LABELS = {"body": "", "header": "", "context": "", "condition": "条件", "warning": "警告", "definition": "定义"}

Heading = Literal["operation", "maintenance", "precautions", "sources"]
GroupKind = Literal["fact_group", "procedure_group"]
MemberRole = Literal["body", "context", "condition", "warning", "header", "definition"]
Reference = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")]


class _PlanItem(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    kind: GroupKind
    evidence_refs: list[Reference] = Field(min_length=1, max_length=128)
    support_group_refs: list[Reference] = Field(min_length=1, max_length=32)
    renderer_key: Literal["source-fact/v1", "source-procedure/v1"]


class _PlanSection(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    heading_code: Heading
    items: list[_PlanItem] = Field(max_length=48)


class AnswerPlan(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    schema_version: Literal["answer-plan/1"]
    sections: list[_PlanSection] = Field(min_length=1, max_length=4)


@dataclass(frozen=True, slots=True)
class QueryContract:
    """Confirmed task scope and server-determined alternatives for each required item."""

    model: str
    scope_key: str | None
    required_items: Mapping[str, tuple[str, ...]]
    allowed_group_scopes: Mapping[str, str] | None = None


@dataclass(frozen=True, slots=True)
class SupportMember:
    evidence_ref: str
    role: MemberRole
    source_binding_hash: str
    step_number: int | None = None


@dataclass(frozen=True, slots=True)
class SupportGroup:
    kind: GroupKind
    model: str
    scope_key: str | None
    allowed_headings: tuple[Heading, ...]
    members: tuple[SupportMember, ...]
    predecessor_refs: tuple[str, ...] | None
    complete: bool


@dataclass(frozen=True, slots=True)
class AnswerEvidence:
    """An admitted evidence projection. Text offsets are Unicode code points, [start, end)."""

    document_id: str
    document_version_id: str
    document_title: str
    version_label: str | None
    parse_run_id: str
    ir_hash: str
    chunk_set_id: str
    chunk_id: str
    block_id: str
    source_binding_hash: str
    file_page: int
    text: str | None = None
    block_start: int | None = None
    block_end: int | None = None
    printed_page: str | None = None
    section: str | None = None
    region_ref: str | None = None
    asset_ref: str | None = None

    @property
    def origin(self) -> tuple[str, ...]:
        return self.document_id, self.document_version_id, self.parse_run_id, self.ir_hash, self.chunk_set_id


class _Unverified(ValueError):
    def __init__(self, code: str, **details):
        super().__init__(code)
        self.reason = {"code": code, **details}


def _unique_members(pairs: list[tuple[str, object]]) -> dict:
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("Duplicate JSON member")
    return result


def _parse(plan: object) -> AnswerPlan:
    if isinstance(plan, bytes):
        plan = plan.decode("utf-8")
    if isinstance(plan, str):
        if len(plan.encode("utf-8")) > 65536:
            raise ValueError("Plan exceeds input budget")
        plan = json.loads(plan, object_pairs_hook=_unique_members)
    if type(plan) is not dict:
        raise ValueError("Plan must be a JSON object")
    return AnswerPlan.model_validate(plan)


def _check_member(group_ref: str, member: SupportMember, evidence: AnswerEvidence | None) -> AnswerEvidence:
    def fail():
        raise _Unverified("evidence_binding_mismatch", group_ref=group_ref, evidence_ref=member.evidence_ref)

    if (not isinstance(evidence, AnswerEvidence) or member.role not in MEMBER_LABELS
            or member.source_binding_hash != evidence.source_binding_hash
            or not re.fullmatch(r"[a-f0-9]{64}", evidence.source_binding_hash)
            or not re.fullmatch(r"[a-f0-9]{64}", evidence.ir_hash)
            or any(not field for field in (*evidence.origin, evidence.chunk_id, evidence.block_id, evidence.document_title))
            or evidence.version_label is not None and (not isinstance(evidence.version_label, str) or not evidence.version_label.strip())
            or type(evidence.file_page) is not int or evidence.file_page < 1):
        fail()
    if evidence.text is None:
        if not evidence.region_ref or evidence.block_start is not None or evidence.block_end is not None:
            fail()
        return evidence
    start, end = evidence.block_start, evidence.block_end
    if (not isinstance(evidence.text, str) or not evidence.text.strip() or type(start) is not int or type(end) is not int
            or not 0 <= start < end or end - start != len(evidence.text)):
        fail()
    return evidence


def _check_group(group_ref: str, group: SupportGroup, heading: Heading, kind: GroupKind,
                 evidence: Mapping[str, AnswerEvidence], contract: QueryContract,
                 seen_groups: set[str]) -> tuple[tuple[SupportMember, AnswerEvidence], ...]:
    if group.complete is not True or not group.members or not any(member.role == "body" for member in group.members):
        raise _Unverified("incomplete_support_group", group_ref=group_ref)
    if group.kind != kind or heading not in group.allowed_headings:
        raise _Unverified("incompatible_group_placement", group_ref=group_ref)
    admitted_scope = contract.scope_key if contract.allowed_group_scopes is None else contract.allowed_group_scopes.get(group_ref)
    if group.model != contract.model or not admitted_scope or group.scope_key != admitted_scope:
        raise _Unverified("applicability_mismatch", group_ref=group_ref)
    if group.predecessor_refs is None:
        raise _Unverified("group_order_unknown", group_ref=group_ref)
    if missing := sorted(set(group.predecessor_refs) - seen_groups):
        raise _Unverified("required_predecessor_not_rendered", group_ref=group_ref, required_group_refs=missing)
    if len({member.evidence_ref for member in group.members}) != len(group.members):
        raise _Unverified("duplicate_group_member", group_ref=group_ref)
    resolved = tuple((member, _check_member(group_ref, member, evidence.get(member.evidence_ref))) for member in group.members)
    if len({item.origin for _, item in resolved}) != 1:
        raise _Unverified("mixed_group_origins", group_ref=group_ref)
    if regions := [member.evidence_ref for member, item in resolved if item.text is None]:
        raise _Unverified("region_review_required", group_ref=group_ref, navigation_evidence_refs=regions)
    step_numbers = []
    for member, _ in resolved:
        is_step = kind == "procedure_group" and member.role == "body"
        number = member.step_number
        if (is_step and (type(number) is not int or number < 1)) or (not is_step and number is not None):
            raise _Unverified("procedure_order_unverified", group_ref=group_ref)
        if is_step:
            step_numbers.append(number)
    if step_numbers != sorted(step_numbers):
        raise _Unverified("procedure_order_unverified", group_ref=group_ref)
    return resolved


def _coverage(contract: QueryContract, selected: set[str]) -> list[dict]:
    return [{"item_ref": item, "matched_group_refs": sorted(set(alternatives) & selected)}
            for item, alternatives in contract.required_items.items()]


def _result(status: str, *, coverage: list[dict] | None = None, reason: dict | None = None) -> dict:
    coverage = coverage or []
    return {"status": status, "text": None, "sections": [], "claims": [], "sources": [],
            "report": {"schema_version": "answer-validation/1", "plan_version": PLAN_VERSION,
                       "renderer_version": RENDERER_VERSION, "status": status, "coverage": coverage,
                       "claim_checks": [],
                       "missing_item_refs": [entry["item_ref"] for entry in coverage if not entry["matched_group_refs"]],
                       "reasons": [reason] if reason else []}}


def _span(member: SupportMember, evidence: AnswerEvidence) -> dict:
    return {"evidence_ref": member.evidence_ref, "role": member.role, "step_number": member.step_number,
            "document_id": evidence.document_id, "document_version_id": evidence.document_version_id,
            "parse_run_id": evidence.parse_run_id, "ir_hash": evidence.ir_hash, "chunk_set_id": evidence.chunk_set_id,
            "chunk_id": evidence.chunk_id, "block_id": evidence.block_id, "block_start": evidence.block_start,
            "block_end": evidence.block_end, "file_page": evidence.file_page, "printed_page": evidence.printed_page,
            "section": evidence.section, "region_ref": evidence.region_ref, "asset_ref": evidence.asset_ref,
            "source_binding_hash": evidence.source_binding_hash}


def _render(ordered: Mapping[Heading, list], coverage: list[dict]) -> dict:
    result = _result("formal", coverage=coverage)
    source_pages: dict[tuple, dict] = {}
    sections_text = []
    for heading, groups in ordered.items():
        section = {"heading_code": heading, "heading": HEADING_LABELS[heading], "claim_ids": []}
        group_texts = []
        for group_ref, kind, renderer_key, structure_scope, resolved in groups:
            claim_id = f"claim-{len(result['claims']) + 1}"
            fragments = []
            for member, item in resolved:
                page_key = (*item.origin, item.file_page, item.printed_page, item.section)
                if page_key not in source_pages:
                    source_pages[page_key] = {
                        "source_number": len(source_pages) + 1, "document_id": item.document_id,
                        "document_version_id": item.document_version_id, "document_title": item.document_title,
                        "version_label": item.version_label, "file_page": item.file_page,
                        "printed_page": item.printed_page, "section": item.section, "evidence_refs": [],
                    }
                page = source_pages[page_key]
                if member.evidence_ref not in page["evidence_refs"]:
                    page["evidence_refs"].append(member.evidence_ref)
                label = f"步骤 {member.step_number}" if member.step_number is not None else MEMBER_LABELS[member.role]
                prefix = f"{label}： " if label else ""
                fragments.append(f"{prefix}{item.text}〔{page['source_number']}〕")
            text = "\n".join(fragments)
            result["claims"].append({
                "claim_id": claim_id, "kind": kind, "text": text, "renderer_key": renderer_key, "structure_scope": structure_scope,
                "support_group_refs": [group_ref], "evidence_refs": [member.evidence_ref for member, _ in resolved],
                "source_spans": [_span(member, item) for member, item in resolved],
            })
            result["report"]["claim_checks"].append({
                "claim_id": claim_id, "support_group_ref": group_ref, "status": "supported", "structure_scope": structure_scope,
                "rendered_text_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "evidence_bindings": [{"evidence_ref": member.evidence_ref, "source_binding_hash": item.source_binding_hash}
                                      for member, item in resolved],
            })
            section["claim_ids"].append(claim_id)
            group_texts.append(text)
        result["sections"].append(section)
        sections_text.append(section["heading"] + "\n" + "\n\n".join(group_texts))
    result["sources"] = list(source_pages.values())
    source_lines = []
    for source in result["sources"]:
        mark = f"〔{source['source_number']}〕"
        page = f"文件第 {source['file_page']} 页"
        if source["printed_page"]:
            page += f"（印刷页 {source['printed_page']}）"
        if source["section"]:
            page += f" · {source['section']}"
        version_label = source["version_label"] or "版本标识待确认"
        source_lines.append(f"{mark}{source['document_title']} · {version_label} · {page}")
    result["sections"].append({"heading_code": "sources", "heading": HEADING_LABELS["sources"], "claim_ids": []})
    sections_text.append(HEADING_LABELS["sources"] + "\n" + "\n".join(source_lines))
    result["text"] = "\n\n".join(sections_text)
    return result


def validate_and_render_answer_plan(
    plan: object, *,
    support_groups: Mapping[str, SupportGroup],
    evidence: Mapping[str, AnswerEvidence],
    query_contract: QueryContract,
) -> dict:
    """Return formal text only for a complete plan over the current admitted projection.

    Callers own candidate provenance and recheck the live publication, authority,
    and generation before using the result or freezing a formal snapshot.
    """
    try:
        candidate = _parse(plan)
    except (ValidationError, ValueError, RecursionError):
        return _result("rejected", reason={"code": "plan_schema_invalid"})
    if not query_contract.model or not query_contract.scope_key or not query_contract.required_items:
        return _result("needs_review", reason={"code": "query_contract_unverified"})
    ordered: dict[Heading, list] = {}
    headings: set[Heading] = set()
    selected: set[str] = set()
    origins: dict[str, tuple[str, ...]] = {}
    primary_spans: set[tuple] = set()
    try:
        for section in candidate.sections:
            heading = section.heading_code
            invalid_items = bool(section.items) if heading == "sources" else not section.items
            if heading in headings or invalid_items:
                return _result("rejected", reason={"code": "invalid_section", "heading_code": heading})
            headings.add(heading)
            if heading == "sources":
                continue
            ordered[heading] = []
            for item in section.items:
                if (item.renderer_key != RENDERER_KEYS[item.kind]
                        or len(set(item.support_group_refs)) != len(item.support_group_refs)
                        or len(set(item.evidence_refs)) != len(item.evidence_refs)):
                    return _result("rejected", reason={"code": "invalid_item_shape"})
                groups = []
                for group_ref in item.support_group_refs:
                    if group_ref in selected:
                        raise _Unverified("duplicate_support_group", group_ref=group_ref)
                    group = support_groups.get(group_ref)
                    if not isinstance(group, SupportGroup):
                        raise _Unverified("unknown_support_group", group_ref=group_ref)
                    groups.append((group_ref, group))
                expected = {member.evidence_ref for _, group in groups for member in group.members}
                if set(item.evidence_refs) != expected:
                    raise _Unverified("evidence_membership_mismatch", missing_evidence_refs=sorted(expected - set(item.evidence_refs)))
                for group_ref, group in groups:
                    resolved = _check_group(group_ref, group, heading, item.kind, evidence, query_contract, selected)
                    origin = resolved[0][1].origin
                    if origins.setdefault(origin[0], origin) != origin:
                        raise _Unverified("mixed_document_versions", group_ref=group_ref)
                    for member, source in resolved:
                        if member.role == "body":
                            span = (*origin, source.block_id, source.block_start, source.block_end)
                            if span in primary_spans:
                                raise _Unverified("duplicate_primary_span", group_ref=group_ref)
                            primary_spans.add(span)
                    selected.add(group_ref)
                    ordered[heading].append((group_ref, item.kind, item.renderer_key, group.scope_key, resolved))
    except _Unverified as error:
        return _result("needs_review", coverage=_coverage(query_contract, selected), reason=error.reason)
    coverage = _coverage(query_contract, selected)
    if any(not entry["matched_group_refs"] for entry in coverage):
        return _result("needs_review", coverage=coverage, reason={"code": "required_items_uncovered"})
    return _render(ordered, coverage)
