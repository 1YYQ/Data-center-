"""Focused checks for child/parent evidence grouping and exact source mappings."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from .parsing.chunking import PROFILE, SPLITTER_VERSION, TOKENIZER_SHA256, build_chunks, load_tokenizer
from .parsing.local_pipeline import ParseContractError
from .parsing.structure import recover_structure
from .test_document_structure import _figure, _node, _source, _table


class _SyntheticCodepointCounter:
    """Controlled budget fixture. The actual BGE-M3 model is tested separately."""

    def encode(self, text, *, add_special_tokens):
        assert add_special_tokens is True
        return SimpleNamespace(ids=[0] * (len(text) + 2))


def _profile(**overrides):
    return {**PROFILE, "structured_ir_sha256": "a" * 64, "source_qc_sha256": "b" * 64,
            "source_qc_ref": str(uuid4()), **overrides}


def _build(normal, **overrides):
    ir = recover_structure(normal, normalized_artifact_id=str(uuid4()))
    inputs = {"chunk_set_id": str(uuid4()), "profile": _profile(**overrides), "tokenizer": _SyntheticCodepointCounter()}
    before = deepcopy(ir)
    result = build_chunks(ir, **inputs)
    assert ir == before and result == build_chunks(ir, **inputs)
    _assert_mappings(result, ir)
    return result, ir


def _assert_mappings(result, ir):
    by_block = {item["id"]: item for item in ir["blocks"]}
    by_chunk = {item["id"]: item for item in result["chunks"]}
    for chunk in result["chunks"]:
        if chunk["level"] == "parent":
            assert chunk["content"] is None and chunk["parent_chunk_id"] is None and chunk["token_count"] is None
            continue
        if chunk["parent_chunk_id"]:
            parent = by_chunk[chunk["parent_chunk_id"]]
            assert parent["chunk_set_id"] == chunk["chunk_set_id"] and parent["level"] == "parent"
        assembled = [(span["chunk_start"], span["chunk_end"], span["text"]) for span in chunk["assembly"]]
        for row in result["chunk_blocks"]:
            if row["chunk_id"] != chunk["id"]:
                continue
            assert row["chunk_set_id"] == result["chunk_set_id"] and row["parse_run_id"] == ir["parse_run_id"]
            for span in row["segments"]:
                if span["kind"] == "region":
                    assert {"asset_ref", "locator_ref"} <= span.keys() and "chunk_start" not in span
                else:
                    text = by_block[row["block_id"]]["text_content"][span["block_start"]:span["block_end"]]
                    assembled.append((span["chunk_start"], span["chunk_end"], text))
        offset = 0
        for start, end, text in sorted(assembled):
            assert offset == start < end and text == chunk["content"][start:end]
            offset = end
        assert offset == len(chunk["content"])
        if chunk["eligibility"] == "candidate":
            assert chunk["token_count"] <= result["quality"]["max_tokens"]
            assert chunk["utf8_byte_count"] <= result["quality"]["max_utf8_bytes"]
    represented = {relation_id for item in result["support_groups"] for relation_id in item["required_relation_ids"]}
    assert represented <= {item["id"] for item in ir["relations"]}


def test_procedure_preserves_full_order_conditions_warnings_and_caption_region():
    normal, _ = _source([
        _node("操作步骤", "doc_title", level=1), _node("步骤1：按下主键，见图2。"),
        _node("步骤1 前置条件：断开输入。"), _node("步骤1 警告：保持护罩。"),
        _node("步骤2：关闭合成测试台并记录观察结果。"), _figure("图2 主键位置"),
    ], assets={"images/figure.png": str(uuid4())})
    result, ir = _build(normal, embedding_max_tokens=60)
    group = next(group for group in result["support_groups"] if group["kind"] == "procedure_group")
    assert group["complete"] and group["predecessor_refs"] == []
    assert [step["step_number"] for step in group["ordered_step_refs"]] == [1, 2]
    assert len(group["child_chunk_ids"]) == 2
    assert {member["role"] for member in group["members"]} >= {"body", "context", "condition", "warning"}
    assert any(member["kind"] == "region" and member["step_number"] == 1 for member in group["members"])
    assert {relation["relation_type"] for relation in ir["relations"] if relation["id"] in group["required_relation_ids"]} == {
        "warns", "requires", "caption_of", "refers_to"}
    assert result["quality"]["status"] == "passed"


def test_table_rowspan_preserves_atom_and_repeats_real_header_with_assembly():
    html = ('<table><thead><tr><th>项</th><th>单位</th></tr></thead><tbody>'
            '<tr><td rowspan="2">甲😀</td><td>Aaaaaa</td></tr><tr><td>Bbbbbb</td></tr>'
            '<tr><td>乙😀</td><td>Cccccc</td></tr></tbody></table>')
    normal, _ = _source([_table("表1", html)])
    result, _ = _build(normal, embedding_max_tokens=28)
    children = [chunk for chunk in result["chunks"] if chunk["level"] == "child"]
    assert len(children) == 2 and result["quality"]["status"] == "passed"
    assert "甲😀" in children[0]["content"] and "Bbbbbb" in children[0]["content"]
    assert "乙😀" in children[1]["content"] and "Aaaaaa" not in children[1]["content"]
    for child in children:
        mappings = [span for row in result["chunk_blocks"] if row["chunk_id"] == child["id"] for span in row["segments"]]
        assert any(span.get("context_kind") == "table_header" and span["role"] == "context" for span in mappings)
        assert any(span["kind"] == "table_cell_separator" for span in child["assembly"])


def test_continuation_with_changed_source_headers_remains_navigation_only():
    left = "<table><tr><th>温度 ℃</th></tr><tr><td>12</td></tr></table>"
    right = "<table><tr><th>压力 kPa</th></tr><tr><td>88</td></tr></table>"
    normal, _ = _source([_table("表1", left)], [_table("表1（续）", right, continues_prev=True)])
    result, _ = _build(normal)
    group = next(group for group in result["support_groups"] if group.get("reason") == "CHUNK_TABLE_CONTINUATION_UNRESOLVED")
    assert not group["complete"] and group["predecessor_refs"] is None
    child_ids = set(group["child_chunk_ids"])
    assert all(item["eligibility"] == "navigation_only" for item in result["chunks"] if item["id"] in child_ids)
    assert result["quality"]["status"] == "needs_review"


def test_large_formula_evidence_keeps_complete_definitions_required_and_splits_only_support_items():
    normal, _ = _source([
        _node("a + b", "equation", equation_label="(1)"),
        _node("公式(1)中，a 表示" + "合成定义" * 9 + "。"),
        _node("公式(1)中，b 表示" + "独立定义" * 9 + "。"),
        _node("公式(1)适用条件：合成常温。"),
    ])
    result, _ = _build(normal, embedding_max_tokens=60)
    group = result["support_groups"][0]
    assert group["complete"]
    children = [item for item in result["chunks"] if item["level"] == "child"]
    assert [item["kind"] for item in children] == ["formula", "formula_definition", "formula_definition"]
    assert all(item["required_relation_ids"] == group["required_relation_ids"] for item in children)
    assert all(item["eligibility"] == "candidate" for item in children)


def test_pure_figure_keeps_asset_only_without_provider_visual_placeholder_text():
    raw = _node([_node("VLM inferred hypothetical action", "image_body", image_path="images/figure.png")], "image")
    normal, _ = _source([raw], assets={"images/figure.png": str(uuid4())})
    result, _ = _build(normal)
    child = next(item for item in result["chunks"] if item["level"] == "child")
    assert (child["eligibility"], child["content"], child["token_count"]) == ("asset_only", "", None)
    assert result["chunk_blocks"][0]["segments"][0]["kind"] == "region"
    assert not result["support_groups"][0]["complete"]


def test_profile_rejects_unregistered_splitter_options():
    normal, _ = _source([_node("合成文字")])
    ir = recover_structure(normal)
    with pytest.raises(ParseContractError, match="CHUNK_PROFILE_UNSUPPORTED"):
        build_chunks(ir, chunk_set_id=str(uuid4()), profile=_profile(splitter_version="customer-code"),
                     tokenizer=_SyntheticCodepointCounter())


def test_plain_paragraphs_respect_heading_scope_and_unresolved_inline_applicability():
    normal, _ = _source([
        _node("第一节", "doc_title", level=1), _node("第一段正文。"), _node("第二段正文。"),
        _node("型号：SYNTHETIC-A"), _node("型号说明之后的正文。"),
        _node("附录 A", "doc_title", level=1), _node("独立附录内容。"),
    ])
    result, _ = _build(normal)
    candidates = [item["content"] for item in result["chunks"] if item["eligibility"] == "candidate"]
    assert candidates == ["第一节\n第一段正文。\n第二段正文。", "附录 A\n独立附录内容。"]
    assert any(issue["code"] == "CHUNK_APPLICABILITY_SCOPE_UNRESOLVED" for issue in result["quality"]["issues"])
    assert result["quality"]["checks"]["projection_and_source_mapping"] == "passed"


_LOCAL_TOKENIZER = (Path(__file__).resolve().parents[2] / "ai_models/modelscope_cache/models/models/BAAI--bge-m3"
                    / "snapshots/master/tokenizer.json")


@pytest.mark.skipif(not _LOCAL_TOKENIZER.is_file(), reason="repository machine's offline tokenizer is absent")
def test_pinned_local_bge_tokenizer_counts_bytes_independently_and_never_truncates():
    tokenizer = load_tokenizer(_LOCAL_TOKENIZER)
    text = "𠮷" * 4097
    normal, _ = _source([_node(text)])
    ir = recover_structure(normal)
    result = build_chunks(ir, chunk_set_id=str(uuid4()), profile=_profile(), tokenizer=tokenizer)
    child = next(item for item in result["chunks"] if item["level"] == "child")
    assert result["splitter_version"] == SPLITTER_VERSION and PROFILE["tokenizer_sha256"] == TOKENIZER_SHA256
    assert child["eligibility"] == "oversized" and child["content"] == text
    assert child["token_count"] == len(tokenizer.encode(text, add_special_tokens=True).ids) == 4
    assert child["utf8_byte_count"] == 16388 and result["quality"]["status"] == "needs_review"
