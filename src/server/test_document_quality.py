"""Source QC verifies frozen technical evidence without inspecting approval overlays."""

from copy import deepcopy
from uuid import uuid4

import pytest

from .parsing.ir import normalize_ir
from .parsing.local_pipeline import fingerprint
from .parsing.quality import CHECKER_REVISION, evaluate_source_qc
from .parsing.structure import recover_structure
from .test_document_ir import _input, _page, _raw_page
from .test_document_structure import _figure, _node, _source, _table


_INPUTS = {"normalized_sha256": "1" * 64, "structured_sha256": "2" * 64, "raw_manifest_sha256": "3" * 64}


def _recover(ir):
    return recover_structure(ir, normalized_artifact_id=str(uuid4()))


def _qc(ir):
    return evaluate_source_qc(ir, **_INPUTS)


def _codes(report):
    return {item["code"] for item in report["issues"]}


def _rehash(block):
    block["content_hash"] = fingerprint({key: value for key, value in block.items() if key != "content_hash"})


def test_source_qc_is_frozen_and_independent_of_business_and_collection_state():
    normal, _ = _source([_node("合成来源，逐字核对 😀。")])
    structured = _recover(normal)
    before = deepcopy(structured)
    report = _qc(structured)
    assert structured == before
    assert report["status"] == "passed"
    assert report["schema"] == "dc.source-qc" and report["schema_version"] == "1"
    assert report["checker_revision"] == CHECKER_REVISION
    assert report["inputs"] == {"normalized_ir_sha256": "1" * 64, "structured_ir_sha256": "2" * 64,
                                "raw_manifest_sha256": "3" * 64}
    assert report["required_leaf_refs"] == [structured["blocks"][0]["id"]]
    assert list(report["container_coverage"].values()) == [report["required_leaf_refs"]]
    assert report["critical_refs"] == {} and report["required_relation_refs"] == []
    assert report["required_human_checks"] and not _codes(report)
    structured["blocks"][0].update(review_status="approved", reviewed_by="synthetic-business-review", revision=7)
    structured["chunk_sets"] = [{"status": "failed"}]
    structured["review_sets"] = [{"state": "approved"}]
    assert _qc(structured) == report


def test_critical_tags_include_paragraphs_and_require_actual_relationship_parts():
    normal, _ = _source([
        _node("操作步骤", "doc_title", level=1), _node("步骤1：按下主键，见图2。"),
        _node("步骤1 前置条件：确认设备已停止。"), _node("步骤1 警告：关闭护罩。"),
        _node("压力：−7.1 kPa"), _node("x = 2", "equation", equation_label="(1)"),
        _node("公式(1)中，x 表示输出。"), _node("公式(1)适用条件：常温。"),
        _table("表1", "<table><tr><th>说明</th></tr><tr><td>合成内容</td></tr></table>"),
        _figure("图2 主键位置"),
    ], assets={"images/figure.png": str(uuid4())})
    structured = _recover(normal)
    report = _qc(structured)
    assert report["status"] == "passed", report["issues"]
    assert {tag for tags in report["critical_refs"].values() for tag in tags} == {
        "formula", "parameter", "unit", "table", "operation_condition", "operation_step", "warning"}
    assert {tuple(tags) for tags in report["critical_refs"].values()} >= {("parameter", "unit"), ("warning",)}
    assert set(report["required_relation_refs"]) == {item["id"] for item in structured["relations"]}
    assert {item["relation_type"] for item in structured["relations"]} == {
        "defines", "requires", "unit_of", "header_of", "warns", "caption_of", "refers_to"}
    structured["relations"][0]["state"] = "confirmed"
    assert _qc(structured) == report
    structured["relations"] = [relation for relation in structured["relations"] if relation["relation_type"] not in {"warns", "unit_of"}]
    lacking = _qc(structured)
    assert lacking["status"] == "needs_review"
    assert {"QC_WARNING_SCOPE_UNRESOLVED", "QC_PARAMETER_UNIT_UNRESOLVED"} <= _codes(lacking)


def test_invalid_unicode_mapping_and_absent_page_fail_technical_qc():
    normal, _ = _source([_node("来源 😀 字符")], [_node("第二页")])
    structured = _recover(normal)
    first = structured["blocks"][0]
    first["structured_content"]["text_map"][0]["block_end"] += 1  # UTF-16 offsets cannot be substituted.
    _rehash(first)
    structured["pages"].pop()
    report = _qc(structured)
    assert report["status"] == "failed"
    assert {"QC_TEXT_PROVENANCE_INVALID", "QC_PAGE_MISSING", "QC_BLOCK_PAGE_INVALID"} <= _codes(report)
    assert "QC_BLOCK_CONTENT_HASH_MISMATCH" not in _codes(report)


def test_blank_page_accounting_and_unmatched_native_inventory():
    pages = [_page(1), _page(2, state="blank"), _page(3)]
    pages[0].update(native_text="Independent synthetic volume 4500 calibrated setting.")
    pages[1]["coverage"]["blank_basis"] = "no_text_images_or_drawings"
    pages[2].update(page_class="scan")
    pages[2]["coverage"].update(image_candidate_count=1, image_candidates=[{
        "id": "native-image-3-0", "bbox": [0, 0, 1, 1], "geometry_status": "exact", "source_index": 0}])
    args = _input(pages, [_raw_page(0, _node("Poor recognition 45", index=0)), _raw_page(1),
                          _raw_page(2, _node("", "image_body", index=0, image_path="images/full.png"))],
                  asset_refs={"images/full.png": str(uuid4())})
    normal = normalize_ir(**args, preflight_artifact_id=str(uuid4()))
    report = _qc(_recover(normal))
    assert report["status"] == "needs_review"
    assert {"QC_NATIVE_TEXT_DISCREPANCY", "QC_SCAN_INTERNAL_COVERAGE_UNRESOLVED"} <= _codes(report)
    assert "QC_BLANK_PAGE_CONTENT_CONFLICT" not in _codes(report)
    assert len(report["observation_refs"]) == 1
    assert set(report["observation_refs"]).isdisjoint(set(report["required_leaf_refs"]) | report["critical_refs"].keys())


def test_attributed_native_region_still_requires_independent_person_disposition():
    page = _page(1)
    page["coverage"].update(vector_candidate_count=1, vector_candidates=[{
        "id": "native-vector-1-0", "bbox": [.2, .2, .3, .3], "geometry_status": "exact", "source_index": 0}])
    args = _input([page], [_raw_page(0, _node("前言文字", index=0))])
    normal = normalize_ir(**args, preflight_artifact_id=str(uuid4()))
    structured = _recover(normal)
    report = _qc(structured)
    assert report["status"] == "passed", report["issues"]
    assert list(report["observation_disposition"].values()) == ["pending_human"]
    observation = next(block for block in structured["blocks"] if block["structure_role"] == "observation")
    observation["raw_ref"]["artifact_id"] = str(uuid4())
    _rehash(observation)
    assert "QC_OBSERVATION_RAW_REF_INVALID" in _codes(_qc(structured))


@pytest.mark.parametrize("corruption, expected", [
    ("part", "QC_RELATION_PART_INVALID"), ("cross-run", "QC_RELATION_RECORD_INVALID"),
    ("parent-cycle", "QC_PARENT_CYCLE"), ("geometry", "QC_CRITICAL_REGION_UNRESOLVED"),
])
def test_relationship_and_region_failures_are_bound_to_current_artifact(corruption, expected):
    normal, _ = _source([_node("压力：−7.1 kPa")])
    structured = _recover(normal)
    leaf, root = structured["blocks"]
    relation = structured["relations"][0]
    if corruption == "part":
        relation["to_part"] = "/source_parts/999"
    elif corruption == "cross-run":
        relation["parse_run_id"] = str(uuid4())
    elif corruption == "parent-cycle":
        root["parent_block_id"] = root["id"]
        _rehash(root)
    else:
        region = leaf["locator"]["regions"][0]
        region.pop("bbox_normalized")
        region["geometry_status"] = "page_only"
        _rehash(leaf)
    report = _qc(structured)
    assert expected in _codes(report)
    assert report["status"] == ("needs_review" if corruption == "geometry" else "failed")
