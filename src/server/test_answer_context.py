"""Task-scoped support mapping and independent, deterministic request coverage."""
from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace

from .answer_context import build_answer_context, freeze_request_items
from .answer_delivery import delivery_coverage
from .answer_plan import RENDERER_KEYS, validate_and_render_answer_plan


MODEL = "DEMO-1"


def _task(query, *, legacy=False):
    snapshot = {"query": query, "model": MODEL}
    scope = {"model": MODEL, "level": "internal", "category": "manual", "need_class": "maintenance"}
    if not legacy:
        scope["request_items"] = freeze_request_items(query, MODEL)
    return SimpleNamespace(id="task-1", execution_revision=2, input_snapshot=snapshot,
                           effective_input={**snapshot, **scope}, accepted_scope=scope)


def _rows(group_ref, kind, structure_scope, text_members, *, task_id="task-1", version_label="R7", predecessors=()):
    rows, members = [], []
    for number, (role, text, step) in enumerate(text_members, 1):
        original_id = f"{group_ref}-fragment-{number}"
        chunk, block = f"{group_ref}-child-{number}", f"{group_ref}-block-{number}"
        binding = sha256(original_id.encode()).hexdigest()
        evidence_kind = "text" if text is not None else "region"
        fragment = {"id": original_id, "document_id": "manual", "document_title": "合成设备手册",
                    "document_version_id": "manual-version-7", "version_label": version_label, "parse_run_id": "selected-run",
                    "ir_hash": "d" * 64, "chunk_set_id": "selected-collection", "chunk_id": chunk, "block_id": block,
                    "block_start": 0 if text is not None else None, "block_end": len(text) if text is not None else None,
                    "evidence_kind": evidence_kind, "role": role, "step_number": step, "printed_page": "iv",
                    "region_ref": "/source/region/0" if text is None else None,
                    "asset_ref": "actual-asset" if text is None else None, "dependencies": {"group_ref": group_ref}}
        rows.append(SimpleNamespace(id=f"current-{original_id}", task_id=task_id, execution_revision=2,
            document_version_id=fragment["document_version_id"], block_id=block, chunk_set_id=fragment["chunk_set_id"],
            chunk_id=chunk, quote_snapshot=text or "", locator={"file_page": 7}, evidence_kind=evidence_kind,
            source_binding_hash=binding, source_context={"fragment": fragment}))
        members.append({"evidence_ref": original_id, "role": role, "step_number": step, "source_binding_hash": binding})
    rows[0].source_context["group"] = {"id": group_ref, "kind": kind, "scope_key": structure_scope, "complete": True,
                                         "members": members, "predecessor_refs": list(predecessors) if predecessors is not None else None}
    return rows


def _render(context, *placements):
    plan = {"schema_version": "answer-plan/1", "sections": []}
    for heading, group_ref in placements:
        group = context["support_groups"][group_ref]
        item = {"kind": group.kind, "support_group_refs": [group_ref],
                "evidence_refs": [member.evidence_ref for member in group.members], "renderer_key": RENDERER_KEYS[group.kind]}
        existing = next((section for section in plan["sections"] if section["heading_code"] == heading), None)
        if existing:
            existing["items"].append(item)
        else:
            plan["sections"].append({"heading_code": heading, "items": [item]})
    return validate_and_render_answer_plan(plan, evidence=context["evidence"], support_groups=context["support_groups"],
                                           query_contract=context["query_contract"])


def test_original_requests_each_get_evidenced_group_mapping_across_known_structure_scopes():
    task = _task("请说明 DEMO-1 开机步骤，以及如何更换滤芯。维护时注意什么？")
    startup = _rows("start", "procedure_group", "source-section-start", [
        ("header", "启动程序", None), ("body", "按下绿色按钮。", 1), ("warning", "气压不足时不得启动。", 1),
        ("body", "核对状态灯。", 2),
    ])
    maintenance = _rows("replace", "procedure_group", "source-section-maint", [
        ("header", "滤芯更换", None), ("body", "更换过滤器中的滤芯。", 1),
    ])
    caution = _rows("caution", "fact_group", "source-section-caution", [
        ("body", "维护时不得接触带电部件。", None),
    ])
    unrelated = _rows("unrelated", "fact_group", "source-section-freight", [("body", "运输包装清单。", None)])
    context = build_answer_context(task, [*reversed(startup), *maintenance, *caution, *unrelated])
    assert context["coverage"]["contract_state"] == "frozen" and context["coverage"]["missing_item_refs"] == []
    assert [item["matched_group_refs"] for item in context["coverage"]["items"]] == [["start"], ["replace"], ["caution"]]
    assert set(context["support_groups"]) == {"start", "replace", "caution"}
    projected = next(item for item in context["model_data"]["support_groups"] if item["id"] == "start")
    assert projected["members"][0]["evidence_ref"] == "current-start-fragment-1"
    assert projected["members"][2]["role"] == "warning" and projected["members"][2]["step_number"] is None
    formal = _render(context, ("operation", "start"), ("maintenance", "replace"), ("precautions", "caution"))
    assert formal["status"] == "formal"
    assert [claim["structure_scope"] for claim in formal["claims"]] == [
        "source-section-start", "source-section-maint", "source-section-caution"]
    assert all(ref.startswith("current-") for claim in formal["claims"] for ref in claim["evidence_refs"])
    assert formal["text"].index("气压不足时不得启动") < formal["text"].index("核对状态灯")
    assert "运输包装" not in formal["text"]
    wrong_contract = replace(context["query_contract"], allowed_group_scopes={"start": "replacement-section"})
    assert validate_and_render_answer_plan(
        {"schema_version": "answer-plan/1", "sections": [{"heading_code": "operation", "items": [
            {"kind": "procedure_group", "renderer_key": "source-procedure/v1", "support_group_refs": ["start"],
             "evidence_refs": [member.evidence_ref for member in context["support_groups"]["start"].members]}]}]},
        evidence=context["evidence"], support_groups=context["support_groups"], query_contract=wrong_contract,
    )["report"]["reasons"][0]["code"] == "applicability_mismatch"


def test_numeric_request_needs_literal_subject_and_quantity_and_does_not_cover_other_requested_facts():
    task = _task("DEMO-1 的气压是多少，最高温度是多少？")
    pressure = _rows("pressure", "fact_group", "specs", [("body", "气压为 5 Pa。", None)])
    false_temperature = _rows("temperature", "fact_group", "manual-specs", [
        ("body", "最高温度需要确认；运行时长为 5 小时。", None),
    ])
    ctx = build_answer_context(task, [*pressure, *false_temperature])
    assert ctx["query_contract"].required_items == {"request-1": ("pressure",), "request-2": ()}
    assert ctx["coverage"]["missing_item_refs"] == ["request-2"]
    report = _render(ctx, ("operation", "pressure"))
    assert report["status"] == "needs_review" and report["text"] is None
    assert report["report"]["missing_item_refs"] == ["request-2"]


def test_numeric_requests_never_borrow_the_neighbouring_fields_value_even_in_one_sentence():
    task = _task("气压是多少；最高温度是多少")
    pressure = _rows("pressure", "fact_group", "specs", [
        ("body", "气压为 5 Pa，最高温度待确认。", None),
    ])
    ctx = build_answer_context(task, pressure)
    assert ctx["query_contract"].required_items == {"request-1": ("pressure",), "request-2": ()}
    assert {(item["label"], item["state"]) for item in ctx["coverage"]["items"]} == {
        ("气压是多少", "covered"), ("最高温度是多少", "missing")}
    result = _render(ctx, ("operation", "pressure"))
    assert result["status"] == "needs_review" and result["text"] is None
    assert result["report"]["missing_item_refs"] == ["request-2"]


def test_unknown_or_incompatible_quantity_cannot_satisfy_its_subject():
    task = _task("气压是多少；最高温度是多少")
    rows = [*_rows("unknown", "fact_group", "checks", [("body", "气压无法确定，校验需 5 小时。", None)]),
            *_rows("wrong-unit", "fact_group", "checks", [("body", "气压为 5 小时。", None)]),
            *_rows("temperature", "fact_group", "specs", [("body", "最高温度为 −5 ℃。", None)])]
    ctx = build_answer_context(task, rows)
    assert ctx["query_contract"].required_items == {"request-1": (), "request-2": ("temperature",)}
    result = _render(ctx, ("operation", "temperature"))
    assert result["status"] == "needs_review" and result["text"] is None
    assert result["report"]["missing_item_refs"] == ["request-1"]


def test_combined_heading_with_only_startup_steps_leaves_shutdown_missing():
    task = _task("开机和关机步骤")
    startup = _rows("startup", "procedure_group", "operations", [
        ("header", "开机和关机", None), ("body", "开机前按下绿色按钮。", 1),
    ])
    ctx = build_answer_context(task, startup)
    assert ctx["query_contract"].required_items == {"request-1": ("startup",), "request-2": ()}
    result = _render(ctx, ("operation", "startup"))
    assert result["status"] == "needs_review" and result["text"] is None
    assert result["report"]["missing_item_refs"] == ["request-2"]


def test_delivery_coverage_keeps_selected_missing_and_unverified_requests_distinct():
    task = _task("气压是多少；最高温度是多少")
    rows = [*_rows("pressure", "fact_group", "specs", [("body", "气压为 5 Pa。", None)]),
            *_rows("temperature", "fact_group", "specs", [("body", "最高温度为 25 ℃。", None)])]
    ctx = build_answer_context(task, rows)
    available = ctx["coverage"]
    assert available["missing_count"] == 0
    partial = _render(ctx, ("operation", "pressure"))
    coverage = delivery_coverage(available, partial, origin_verified=True)
    assert partial["status"] == "needs_review" and coverage["missing_item_refs"] == ["request-2"]
    assert [(item["label"], item["state"]) for item in coverage["items"]] == [
        ("气压是多少", "covered"), ("最高温度是多少", "missing")]
    formal = _render(ctx, ("operation", "pressure"), ("operation", "temperature"))
    assert formal["status"] == "formal"
    assert delivery_coverage(available, formal, origin_verified=True)["missing_count"] == 0
    invalid_schema = _render(ctx)
    for result, verified in ((formal, False), (invalid_schema, True)):
        coverage = delivery_coverage(available, result, origin_verified=verified)
        assert coverage["missing_count"] == 2
        assert all(item["state"] == "needs_review" and not item["matched_group_refs"] for item in coverage["items"])


def test_model_removal_uses_exact_identity_and_legacy_contract_never_uses_rewritten_query():
    frozen = freeze_request_items("please tell me DEMO-10 pressure", MODEL)
    assert "demo-10" in frozen["items"][0]["terms"]
    task = _task("温度是多少", legacy=True)
    task.effective_input["query"] = "气压是多少"
    rows = _rows("pressure", "fact_group", "specs", [("body", "气压为 5 Pa。", None)])
    ctx = build_answer_context(task, rows)
    assert ctx["coverage"]["contract_state"] == "legacy_derived"
    assert ctx["coverage"]["missing_count"] == 1 and "温度" in ctx["coverage"]["items"][0]["label"]
    task.accepted_scope["request_items"] = freeze_request_items("气压是多少", MODEL)
    ctx = build_answer_context(task, rows)
    assert ctx["coverage"]["contract_state"] == "invalid_frozen" and ctx["coverage"]["missing_count"] == 1


def test_missing_or_duplicate_evidence_never_completes_a_group_with_a_foreign_row():
    task = _task("如何开机")
    rows = _rows("startup", "procedure_group", "operations", [
        ("header", "开机操作", None), ("body", "按下绿色按钮。", 1),
    ])
    assert build_answer_context(task, rows)["coverage"]["missing_count"] == 0
    foreign = SimpleNamespace(**(vars(rows[-1]) | {"id": "other-task-row", "task_id": "somewhere-else"}))
    assert build_answer_context(task, [rows[0], foreign])["coverage"]["missing_count"] == 1
    duplicate = SimpleNamespace(**(vars(rows[-1]) | {"id": "ambiguous-task-row"}))
    assert build_answer_context(task, [*rows, duplicate])["coverage"]["missing_count"] == 1
    rows[-1].execution_revision = 1
    assert build_answer_context(task, rows)["coverage"]["missing_count"] == 1


def test_unknown_version_label_preserves_real_historical_reference_without_inventing_a_revision():
    task = _task("气压是多少")
    rows = _rows("pressure", "fact_group", "specs", [("body", "气压为 5 Pa。", None)], version_label=None)
    ctx = build_answer_context(task, rows)
    result = _render(ctx, ("operation", "pressure"))
    assert result["status"] == "formal"
    assert result["sources"][0]["document_version_id"] == "manual-version-7"
    assert result["sources"][0]["version_label"] is None and "版本标识待确认" in result["text"]


def test_predecessor_and_region_are_required_even_when_the_main_text_matches():
    task = _task("如何开机")
    entry = _rows("before", "fact_group", "conditions", [("body", "确认已落实锁定条件。", None)])
    start = _rows("startup", "procedure_group", "startup", [("body", "开机前按下绿色按钮。", 1)], predecessors=("before",))
    assert build_answer_context(task, start)["coverage"]["missing_count"] == 1
    ctx = build_answer_context(task, [*start, *entry])
    assert _render(ctx, ("operation", "startup"), ("operation", "before"))["status"] == "needs_review"
    assert _render(ctx, ("operation", "before"), ("operation", "startup"))["status"] == "formal"
    region = _rows("startup", "procedure_group", "startup", [("body", "开机前按下绿色按钮。", 1), ("context", None, 1)])
    assert build_answer_context(task, region)["coverage"]["missing_count"] == 1


def test_warning_only_overlap_or_changed_confirmed_dimensions_cannot_cover_an_operation():
    task = _task("开机和维护步骤")
    mentions_both = _rows("safety", "fact_group", "safety", [("body", "开机和维护阶段都不得触碰带电部件。", None)])
    assert build_answer_context(task, mentions_both)["coverage"]["missing_count"] == 2
    start = _rows("startup", "procedure_group", "start", [("body", "开机时按下绿色按钮。", 1)])
    assert build_answer_context(task, start)["coverage"]["missing_count"] == 1
    task.effective_input["category"] = "other"
    assert build_answer_context(task, start)["coverage"]["missing_count"] == 2


def test_later_confirmed_model_binds_its_literal_without_rewriting_the_frozen_request():
    task = _task("DEMO-1 如何开机")
    task.input_snapshot["model"] = None
    task.accepted_scope["model"] = None
    frozen = freeze_request_items(task.input_snapshot["query"], None)
    task.accepted_scope["request_items"] = frozen
    assert frozen["items"][0]["terms"] == ["demo-1"]
    start = _rows("startup", "procedure_group", "start", [("body", "开机时按下绿色按钮。", 1)])
    assert build_answer_context(task, start)["coverage"]["missing_count"] == 0
    assert task.accepted_scope["request_items"]["items"][0]["terms"] == ["demo-1"]
    task.effective_input["model"] = "DEMO-10"
    assert build_answer_context(task, start)["coverage"]["missing_count"] == 1
