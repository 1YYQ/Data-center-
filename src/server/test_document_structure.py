"""Structure recovery checks with source-bound, synthetic Middle inputs."""

from collections import Counter
from copy import deepcopy
from uuid import uuid4

import pytest

from .parsing.ir import normalize_ir
from .parsing.local_pipeline import ParseContractError, fingerprint
from .parsing.structure import RECOVERY_VERSION, recover_structure
from .test_document_ir import _input, _page, _raw_page, _resolve


_BBOX = [.1, .1, .9, .9]


def _node(text, kind="text", **fields):
    return {"type": kind, "bbox": _BBOX, "content": text, **fields}


def _source(*pages, assets=None):
    roots = [_raw_page(index, *({"index": index_in_page, **node} for index_in_page, node in enumerate(page)))
             for index, page in enumerate(pages)]
    args = _input([_page(index + 1) for index in range(len(pages))], roots, asset_refs=assets)
    return normalize_ir(**args), args


def _table(title, html, **fields):
    return _node([_node(html, "table_body"), _node(title, "table_caption")], "table", **fields)


def _figure(title, path="images/figure.png"):
    return _node([_node(title, "image_caption"), _node("", "image_body", image_path=path)], "image")


def _relation_part(relation, endpoint, by_id):
    pointer = relation[f"{endpoint}_part"]
    block = by_id[relation[f"{endpoint}_block_id"]]
    return _resolve(block["structured_content"], pointer) if pointer else block


def test_structure_preserves_source_and_recovers_five_relationship_families():
    normal, source = _source([
        _node("操作步骤", "doc_title", level=1),
        _node("步骤1：按下主键，见图2。"),
        _node("步骤1 前置条件：确认设备已停止。"),
        _node("步骤1 警告：保持护罩关闭。"),
        _node("步骤2：关闭设备。"),
        _node("压力：−7.1–−6.5 kPa"),
        _node("x = 2*y", "equation", equation_label="(1)"),
        _node("公式(1)中，x 表示输出。"),
        _node("公式(1)中，y 表示输入。"),
        _node("公式(1)适用条件：常温环境。"),
        _table("表1 设置", '<table><thead><tr><th rowspan="2" scope="col">动作</th><th colspan="2">设置</th></tr>'
               '<tr><th>低</th><th>高</th></tr></thead><tbody><tr><td>按下<img src="images/key.png"></td>'
               '<td>3</td><td>5</td></tr></tbody></table>'),
        _figure("图2 主键位置"),
        _node("先读取仪表显示，"),
    ], [_node("随后记录结果。", continues_prev=True)], assets={"images/key.png": str(uuid4()),
                                                               "images/figure.png": str(uuid4())})
    before = deepcopy(normal)
    artifact_id = str(uuid4())
    structured = recover_structure(normal, normalized_artifact_id=artifact_id)
    assert structured == recover_structure(normal, normalized_artifact_id=artifact_id)
    assert normal == before and normal["relations"] == []
    assert structured["phase"] == "structured" and structured["recovery_version"] == RECOVERY_VERSION
    assert structured["normalized_input"] == {"artifact_id": artifact_id, "content_hash": fingerprint(normal)}
    assert not structured["issues"]
    by_id = {block["id"]: block for block in structured["blocks"]}
    leaf_orders = []
    for original in normal["blocks"]:
        block = by_id[original["id"]]
        for key in ("raw_ref", "text_content", "locator", "kind", "structure_role"):
            assert block[key] == original[key]
        assert block["structured_content"]["text_map"] == original["structured_content"]["text_map"]
        assert block["normalized_content_hash"] == original["content_hash"]
        if original["structure_role"] == "leaf":
            leaf_orders.append(block["reading_order"])
    assert leaf_orders == list(range(len(leaf_orders)))
    assert Counter(relation["relation_type"] for relation in structured["relations"]) == {
        "continues": 1, "warns": 1, "requires": 2, "unit_of": 1, "defines": 2,
        "header_of": 5, "refers_to": 2, "caption_of": 2,
    }
    for relation in structured["relations"]:
        assert "state" not in relation and relation["parse_run_id"] == normal["parse_run_id"]
        assert relation["basis"]["recovery_version"] == RECOVERY_VERSION
        for key in ("from", "to"):
            assert _relation_part(relation, key, by_id)
        for raw_ref in relation["basis"]["evidence"]:
            assert raw_ref["artifact_id"] == source["raw_artifact_id"]
            assert _resolve(source["middle"], raw_ref["json_pointer"]) is not None
    table_body = next(block for block in by_id.values() if block["kind"] == "table")
    low_setting = table_body["structured_content"]["rows"][2][1]
    assert [(ref["row"], ref["column"]) for ref in low_setting["header_refs"]] == [(0, 1), (1, 1)]
    containers = {block["kind"]: block for block in by_id.values() if block["source_kind"] == "derived_structure"}
    assert containers["section"]["parent_block_id"] == containers["document"]["id"]
    assert containers["section"]["structured_content"]["child_refs"][1] == containers["procedure"]["id"]
    assert all(block["text_content"] == "" and block["locator"]["regions"] == [] for block in containers.values())
    formula = next(block for block in by_id.values() if block["kind"] == "formula")
    assert [item["symbol"] for item in formula["structured_content"]["formula_symbols"]] == ["x", "y"]


def test_ambiguous_or_cross_scope_content_is_preserved_without_false_binding():
    normal, _ = _source([
        _node("操作步骤 A", "doc_title", level=1),
        _node("步骤1：开启设备。"), _node("步骤1：打开辅助开关。"),
        _node("Warning for step 1: Hold the shield."), _node("警告：保持安全距离。"),
        _node("压力：17 quux"), _node("a = 1", "equation", equation_label="(4)"),
        _node("a = 2", "equation", equation_label="(4)"), _node("公式(4)中，a 表示长度。"),
        _node("子章", "paragraph_title", level=2), _node("公式(4)适用条件：只在常温下使用。"),
        _node("步骤2：打开辅助开关，见图2。"),
    ], [
        _node("操作步骤 B", "doc_title", level=1),
        _figure("图2 辅助开关"),
        _table("表2 无表头", "<table><tr><td>2</td></tr></table>"),
        _node("后续操作", continues_prev=True),
    ], assets={"images/figure.png": str(uuid4())})
    structured = recover_structure(normal)
    assert [relation["relation_type"] for relation in structured["relations"]] == ["caption_of", "caption_of"]
    assert {issue["code"] for issue in structured["issues"]} >= {
        "STRUCTURE_STEP_REQUIREMENT_SCOPE_UNRESOLVED", "STRUCTURE_PARAMETER_UNIT_UNRESOLVED",
        "STRUCTURE_FORMULA_TARGET_UNRESOLVED", "STRUCTURE_FORMULA_DEFINITION_UNRESOLVED",
        "STRUCTURE_FORMULA_CONDITION_UNRESOLVED", "STRUCTURE_FIGURE_TARGET_UNRESOLVED",
        "STRUCTURE_FIGURE_ACTION_UNRESOLVED", "STRUCTURE_TABLE_CELL_HEADER_UNRESOLVED",
        "STRUCTURE_CONTINUATION_UNRESOLVED",
    }


def test_table_continuation_uses_provider_flag_and_matching_explicit_labels():
    html = "<table><tr><th>标题</th></tr><tr><td>8</td></tr></table>"
    normal, _ = _source([_table("表1 读数", html)], [_table("表1（续）", html, continues_prev=True)],
                        [_table("表2（续）", html, continues_prev=True)])
    structured = recover_structure(normal)
    relations = [relation for relation in structured["relations"] if relation["relation_type"] == "continues"]
    assert len(relations) == 1
    by_id = {block["id"]: block for block in structured["blocks"]}
    lead = by_id[relations[0]["from_block_id"]]
    tail = by_id[relations[0]["to_block_id"]]
    parent = by_id[lead["parent_block_id"]]
    assert parent["kind"] == "table_continuation" and tail["parent_block_id"] == parent["id"]
    assert (lead["file_page"], tail["file_page"]) == (1, 2)
    assert any(issue["code"] == "STRUCTURE_TABLE_CONTINUATION_UNRESOLVED" and issue["file_page"] == 3
               for issue in structured["issues"])


def test_unreliable_order_does_not_create_heading_or_step_placement():
    normal, _ = _source([_node("操作步骤", "doc_title", level=1), _node("步骤1：打开设备。", index=None)])
    structured = recover_structure(normal)
    assert [block["kind"] for block in structured["blocks"]] == ["title", "paragraph", "document"]
    assert all(block["reading_order"] is None for block in structured["blocks"])
    assert {issue["code"] for issue in structured["issues"]} == {
        "STRUCTURE_READING_ORDER_UNRESOLVED", "STRUCTURE_HEADING_UNRESOLVED", "STRUCTURE_STEP_NUMBER_UNRESOLVED",
    }


@pytest.mark.parametrize("mutation", ["cross-run", "cycle", "dangling", "overlap"])
def test_invalid_source_topology_or_grid_cannot_create_structured_artifact(mutation):
    normal, _ = _source([_table("表1", "<table><tr><th>甲</th><th>乙</th></tr><tr><td>1</td><td>2</td></tr></table>")])
    parent, body, _ = normal["blocks"]
    if mutation == "cross-run":
        body["parse_run_id"] = str(uuid4())
    elif mutation == "cycle":
        parent["parent_block_id"] = parent["id"]
    elif mutation == "dangling":
        body["parent_block_id"] = str(uuid4())
    else:
        body["structured_content"]["rows"][0][0]["colspan"] = 2
    with pytest.raises(ParseContractError, match="IR_STRUCTURE_INVALID"):
        recover_structure(normal)
