"""Synthetic coverage for the formal organization and source rendering gate."""
from dataclasses import replace
from hashlib import sha256
import json

import pytest

from .answer_plan import AnswerEvidence, QueryContract, SupportGroup, SupportMember, validate_and_render_answer_plan


def _evidence(evidence_ref, text, start, **overrides):
    fields = {
        "document_id": "manual-a", "document_version_id": "manual-version-7", "document_title": "合成设备使用手册",
        "version_label": "R7", "parse_run_id": "selected-run", "ir_hash": "f" * 64, "chunk_set_id": "selected-set",
        "chunk_id": "chunk-" + evidence_ref, "block_id": "block-" + evidence_ref,
        "source_binding_hash": sha256(evidence_ref.encode()).hexdigest(), "file_page": 9, "printed_page": "iv",
        "text": text, "block_start": start, "block_end": start + len(text), "section": "设备启动",
    }
    return AnswerEvidence(**(fields | overrides))


def _item(kind, group_ids, evidence_ids):
    renderer = "source-procedure/v1" if kind == "procedure_group" else "source-fact/v1"
    return {"kind": kind, "support_group_refs": list(group_ids), "evidence_refs": list(evidence_ids), "renderer_key": renderer}


def _plan(*sections):
    return {"schema_version": "answer-plan/1",
            "sections": [{"heading_code": heading, "items": list(items)} for heading, items in sections]}


@pytest.fixture
def manual():
    evidence = {
        "header": _evidence("header", "合成设备启动流程", 0),
        "condition": _evidence("condition", "仅在停止状态下开始。", 8),
        "step-one": _evidence("step-one", "按下 ▶️ 启动键。", 21),
        "warning": _evidence("warning", "指示灯为红色时，不得继续。", 0),
        "step-two": _evidence("step-two", "确认指示灯为绿色。", 12, file_page=10, printed_page="v"),
        "fact": _evidence("fact", "运行气压为 3.5 kPa。", 3, file_page=10, printed_page="v"),
        "definition": _evidence("definition", "气压指进气端的表压。", 33, file_page=10, printed_page="v"),
        "unrelated": _evidence("unrelated", "附件储存在运输箱。", 4),
    }

    def member(ref, role, step=None):
        return SupportMember(ref, role, evidence[ref].source_binding_hash, step)

    procedure = SupportGroup("procedure_group", "SYN-A", "SYN-A:confirmed", ("operation",),
                             (member("header", "header"), member("condition", "condition"), member("step-one", "body", 1),
                              member("warning", "warning"), member("step-two", "body", 2)), (), True)
    fact = SupportGroup("fact_group", "SYN-A", "SYN-A:confirmed", ("maintenance",),
                        (member("definition", "definition"), member("fact", "body")), (), True)
    groups = {"start": procedure, "pressure": fact, "other": replace(fact, members=(member("unrelated", "body"),))}
    contract = QueryContract("SYN-A", "SYN-A:confirmed", {"startup-requested": ("start",), "pressure-requested": ("pressure",)})
    plan = _plan((
        "operation", [_item("procedure_group", ("start",), ("step-two", "warning", "step-one", "condition", "header"))],
    ), ("maintenance", [_item("fact_group", ("pressure",), ("fact", "definition"))]))
    return groups, evidence, contract, plan


def _render(manual, plan=None):
    groups, evidence, contract, default = manual
    return validate_and_render_answer_plan(default if plan is None else plan,
                                           support_groups=groups, evidence=evidence, query_contract=contract)


def _held(result, code, *, rejected=False):
    assert result["status"] == ("rejected" if rejected else "needs_review")
    assert result["text"] is None and result["sections"] == result["claims"] == []
    assert result["report"]["reasons"][0]["code"] == code


def test_formal_text_uses_source_order_and_preserves_conditions_units_warnings_and_locations(manual):
    _, evidence, _, plan = manual
    result = _render(manual, json.dumps(plan, ensure_ascii=False).encode("utf-8"))
    assert result["status"] == "formal"
    assert result["text"].splitlines() == [
        "操作", "合成设备启动流程〔1〕", "条件： 仅在停止状态下开始。〔1〕", "步骤 1： 按下 ▶️ 启动键。〔1〕",
        "警告： 指示灯为红色时，不得继续。〔1〕", "步骤 2： 确认指示灯为绿色。〔2〕",
        "", "维护", "定义： 气压指进气端的表压。〔2〕", "运行气压为 3.5 kPa。〔2〕",
        "", "来源", "〔1〕合成设备使用手册 · R7 · 文件第 9 页（印刷页 iv） · 设备启动",
        "〔2〕合成设备使用手册 · R7 · 文件第 10 页（印刷页 v） · 设备启动",
    ]
    first, second = result["claims"]
    assert first["evidence_refs"] == ["header", "condition", "step-one", "warning", "step-two"]
    assert second["support_group_refs"] == ["pressure"] and second["evidence_refs"] == ["definition", "fact"]
    step = first["source_spans"][2]
    assert (step["block_start"], step["block_end"], step["block_id"]) == (21, 31, "block-step-one")
    assert result["report"]["claim_checks"][0]["evidence_bindings"][2] == {
        "evidence_ref": "step-one", "source_binding_hash": evidence["step-one"].source_binding_hash,
    }
    assert result["report"]["missing_item_refs"] == []
    assert "附件储存" not in result["text"], "unrelated retrieved groups are not required coverage"


def test_omitting_warning_or_replacing_citation_with_identical_words_withholds_the_whole_answer(manual):
    groups, evidence, _, plan = manual
    evidence["other-warning"] = _evidence("other-warning", evidence["warning"].text, 0)
    refs = plan["sections"][0]["items"][0]["evidence_refs"]
    refs.remove("warning")
    result = _render(manual)
    _held(result, "evidence_membership_mismatch")
    assert result["report"]["reasons"][0]["missing_evidence_refs"] == ["warning"]
    refs.append("other-warning")
    _held(_render(manual), "evidence_membership_mismatch")
    refs[-1] = "warning"
    evidence["warning"] = evidence["other-warning"]
    _held(_render(manual), "evidence_binding_mismatch")
    assert groups["start"].members[3].evidence_ref == "warning"


@pytest.mark.parametrize("extra", [
    {"text": "可略过红色指示灯警告。"}, {"formal": True}, {"template": "{{ evidence }}"}, {"source_page": 91},
])
def test_candidates_cannot_supply_text_approval_template_or_source_metadata(manual, extra):
    manual[3]["sections"][0]["items"][0].update(extra)
    _held(_render(manual), "plan_schema_invalid", rejected=True)


def test_free_text_duplicate_json_keys_unsupported_renderer_and_fabricated_sources_are_rejected(manual):
    _held(_render(manual, "全部完成。"), "plan_schema_invalid", rejected=True)
    _held(_render(manual, '{"schema_version":"answer-plan/1","schema_version":"answer-plan/1","sections":[]}'),
          "plan_schema_invalid", rejected=True)
    plan = manual[3]
    item = plan["sections"][0]["items"][0]
    item["renderer_key"] = "source-fact/v1"
    _held(_render(manual), "invalid_item_shape", rejected=True)
    item["renderer_key"] = "source-procedure/v1"
    plan["sections"].append({"heading_code": "sources", "items": [item]})
    _held(_render(manual), "invalid_section", rejected=True)
    plan["sections"][-1]["items"] = []
    assert _render(manual)["status"] == "formal"


def test_missing_required_item_and_unknown_coverage_do_not_render_the_valid_partial_answer(manual):
    groups, evidence, contract, plan = manual
    plan["sections"].pop()
    result = _render(manual)
    _held(result, "required_items_uncovered")
    assert result["report"]["missing_item_refs"] == ["pressure-requested"]
    _held(_render((groups, evidence, replace(contract, required_items={}), plan)), "query_contract_unverified")
    _held(_render((groups, evidence, replace(contract, scope_key=None), plan)), "query_contract_unverified")


def test_repeating_group_and_publishing_the_same_primary_source_under_a_second_id_are_held(manual):
    groups, evidence, _, plan = manual
    items = plan["sections"][1]["items"]
    items.append(items[0])
    _held(_render(manual), "duplicate_support_group")
    items.pop()
    evidence["fact-alias"] = evidence["fact"]
    groups["alias"] = replace(groups["pressure"], members=(
        SupportMember("fact-alias", "body", evidence["fact"].source_binding_hash),
    ))
    items.append(_item("fact_group", ("alias",), ("fact-alias",)))
    _held(_render(manual), "duplicate_primary_span")


def test_cross_version_scope_and_unknown_relationships_are_not_promoted(manual):
    groups, evidence, _, plan = manual
    original = groups["start"]
    groups["start"] = replace(original, scope_key="SYN-A:unknown")
    _held(_render(manual), "applicability_mismatch")
    groups["start"] = replace(original, predecessor_refs=None)
    _held(_render(manual), "group_order_unknown")
    groups["start"] = replace(original, predecessor_refs=("pressure",))
    _held(_render(manual), "required_predecessor_not_rendered")
    plan["sections"].reverse()
    assert _render(manual)["status"] == "formal"
    for ref in ("definition", "fact"):
        evidence[ref] = replace(evidence[ref], document_version_id="old-version")
    _held(_render(manual), "mixed_document_versions")
    evidence["definition"] = replace(evidence["definition"], document_version_id="manual-version-7")
    _held(_render(manual), "mixed_group_origins")


def test_region_only_and_unicode_byte_offsets_cannot_support_formal_text(manual):
    _, evidence, _, _ = manual
    step = evidence["step-one"]
    evidence["step-one"] = replace(step, block_end=step.block_start + len(step.text.encode()))
    _held(_render(manual), "evidence_binding_mismatch")
    evidence["step-one"] = replace(step, text=None, block_start=None, block_end=None,
                                   region_ref="/locator/regions/0", asset_ref="actual-region-asset")
    result = _render(manual)
    _held(result, "region_review_required")
    assert result["report"]["reasons"][0]["navigation_evidence_refs"] == ["step-one"]
