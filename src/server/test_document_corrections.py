"""Source-bound manual overlays of real synthetic Normalized Document IR."""
from copy import deepcopy
import json
from uuid import uuid4

import pytest

from .parsing.corrections import apply_manual_corrections, inherit_manual_corrections
from .parsing.ir import normalize_ir
from .parsing.local_pipeline import ParseContractError
from .parsing.structure import recover_structure
from .test_document_ir import _input, _page, _raw_page, _resolve


def _source_args():
    box = [.1, .2, .7, .5]
    table = {"type": "table", "bbox": box, "content": [
        {"type": "table_body", "bbox": box, "content": "<table><tr><td>原值</td></tr></table>"},
    ]}
    first = _raw_page(0, {"type": "text", "bbox": box, "content": "合成文字"}, table)
    second = _raw_page(1, {"type": "warning", "bbox": box, "content": "警告：先断电"})
    return _input([_page(1), _page(2)], [first, second])


@pytest.fixture
def correction_source():
    setup = _source_args()
    parent = recover_structure(normalize_ir(**setup), normalized_artifact_id=str(uuid4()))
    child = normalize_ir(**{**setup, "run_id": str(uuid4()), "raw_artifact_id": str(uuid4())})
    return parent, child, str(uuid4())


def _edit(block, **extra):
    return {"block_id": block["id"], "text": "合成修正", "reason": "原段换行识别有误",
            "basis": {"file_page": block["file_page"], "description": "复核原页同一段正文"}, **extra}


def test_reviewed_column_headers_keep_cells_and_bind_original_and_claim():
    box = [.1, .2, .7, .5]
    table = {"type": "table", "bbox": box, "content": [{"type": "table_body", "bbox": box,
        "content": "<table><tr><td>Lens</td><td>Reading</td></tr><tr><td>1</td><td>6 mm</td></tr></table>"}]}
    setup = _input([_page(1)], [_raw_page(0, table)])
    parent = normalize_ir(**setup)
    child = normalize_ir(**{**setup, "run_id": str(uuid4()), "raw_artifact_id": str(uuid4())})
    body = next(block for block in parent["blocks"] if block["kind"] == "table")
    claim = {"block_id": body["id"], "reason": "Compare printed header", "basis": {"file_page": 1, "description": "First ruled row"},
             "column_headers": [{"column": 0, "text": "Lens"}, {"column": 1, "text": "Reading"}]}
    request = str(uuid4())
    corrected = apply_manual_corrections(child, parent_ir=parent, correction_artifact_id=request, payload={"blocks": [claim]})
    structured = recover_structure(corrected)
    changed = next(block for block in structured["blocks"] if block["kind"] == "table")
    assert changed["text_content"] == body["text_content"]
    assert changed["structured_content"]["manual_correction"]["requires_source_review"]
    links = [r for r in structured["relations"] if r["relation_type"] == "header_of"]
    assert {(r["from_part"], r["to_part"]) for r in links} == {("/rows/0/0", "/rows/1/0"), ("/rows/0/1", "/rows/1/1")}
    assert all(any(ref["artifact_id"] == request for ref in r["basis"]["evidence"]) for r in links)
    claim["column_headers"][1]["text"] = "Different text"
    with pytest.raises(ParseContractError, match="IR_CORRECTION_HEADER_MISMATCH"):
        apply_manual_corrections(child, parent_ir=parent, correction_artifact_id=request, payload={"blocks": [claim]})


def test_manual_text_proves_unicode_against_frozen_request_and_keeps_old_ir(correction_source):
    parent, child, request_artifact = correction_source
    parent["blocks"][0]["review_status"] = "approved"
    old_parent, old_child = deepcopy(parent), deepcopy(child)
    text = "  原页：扳手🔧、e\u0301，扭矩 < 5 N·m\n完成  "
    payload = {"blocks": [_edit(parent["blocks"][0], text=text, kind="warning")]}
    frozen = json.loads(json.dumps(payload, ensure_ascii=False))
    corrected = apply_manual_corrections(child, parent_ir=parent, correction_artifact_id=request_artifact, payload=frozen)
    assert corrected == apply_manual_corrections(child, parent_ir=parent, correction_artifact_id=request_artifact, payload=frozen)
    assert parent == old_parent and child == old_child
    assert corrected["blocks"][1:] == child["blocks"][1:]
    block, original = corrected["blocks"][0], child["blocks"][0]
    assert block["id"] == original["id"] != parent["blocks"][0]["id"]
    assert block["source_kind"] == "manual" and block["source_block_ref"] == parent["blocks"][0]["id"]
    assert block["kind"] == "warning" and block["text_content"] == text
    assert block["locator"] == original["locator"] and block["raw_ref"] == original["raw_ref"]
    assert block["content_hash"] != original["content_hash"] and "review_status" not in block
    structured = block["structured_content"]
    assert structured["manual_correction"]["requires_source_review"] is True
    assert _resolve(frozen, structured["manual_correction"]["request_ref"]["json_pointer"]) == frozen["blocks"][0]
    [span] = structured["text_map"]
    assert span["raw_ref"] == {"artifact_id": request_artifact, "json_pointer": "/blocks/0/text"}
    assert _resolve(frozen, span["raw_ref"]["json_pointer"])[span["source_start"]:span["source_end"]] == text[span["block_start"]:span["block_end"]]
    assert corrected["artifact_refs"][-1] == {"artifact_id": request_artifact, "kind": "structured", "role": "manual_correction"}


def test_same_page_coordinate_claim_retains_raw_geometry_and_raw_text_map(correction_source):
    parent, child, request_artifact = correction_source
    first = parent["blocks"][0]
    edit = {"ordinal": str(first["ordinal"]), "reason": "边缘漏框", "basis": {"file_page": 1, "description": "核对原页正文四边"},
            "locator": {"file_page": 1, "bbox_normalized": [.11, .2, .63, .51]}}
    payload = {"blocks": [edit]}
    block = apply_manual_corrections(child, parent_ir=parent, correction_artifact_id=request_artifact, payload=payload)["blocks"][0]
    raw = child["blocks"][0]
    [region] = block["locator"]["regions"]
    assert block["source_kind"] == "manual" and block["raw_ref"] == raw["raw_ref"]
    assert region["file_page"] == raw["file_page"] and region["geometry_basis"] == "manual_claim"
    assert _resolve(payload, region["geometry_evidence_ref"]["json_pointer"]) == region["bbox_normalized"]
    assert block["structured_content"]["manual_correction"]["original_regions"] == raw["locator"]["regions"]
    assert block["structured_content"]["text_map"] == raw["structured_content"]["text_map"]


@pytest.mark.parametrize(("failure", "code"), [
    ("foreign_block", "IR_CORRECTION_TARGET_INVALID"),
    ("duplicate", "IR_CORRECTION_DUPLICATE"),
    ("foreign_version", "IR_CORRECTION_SOURCE_MISMATCH"),
    ("basis_page", "IR_CORRECTION_PAGE_MISMATCH"),
    ("locator_page", "IR_CORRECTION_PAGE_MISMATCH"),
    ("unknown_region", "IR_CORRECTION_REGION_INVALID"),
    ("markup", "IR_CORRECTION_MARKUP_UNSUPPORTED"),
    ("external_link", "IR_CORRECTION_MARKUP_UNSUPPORTED"),
    ("arbitrary_source", "IR_CORRECTION_INVALID"),
    ("missing_basis", "IR_CORRECTION_BASIS_REQUIRED"),
    ("container", "IR_CORRECTION_TARGET_UNSUPPORTED"),
    ("table_text", "IR_CORRECTION_TABLE_UNSUPPORTED"),
    ("graphic_table", "IR_CORRECTION_TABLE_UNSUPPORTED"),
    ("downgrade_warning", "IR_CORRECTION_KIND_UNSUPPORTED"),
])
def test_manual_rejects_unbound_or_unsafe_changes(correction_source, failure, code):
    parent, child, request_artifact = correction_source
    source = parent["blocks"][0]
    edit = _edit(source)
    payload = {"blocks": [edit]}
    if failure == "foreign_block":
        edit["block_id"] = child["blocks"][0]["id"]
    elif failure == "duplicate":
        duplicate = _edit(source, ordinal=source["ordinal"])
        duplicate.pop("block_id")
        payload["blocks"].append(duplicate)
    elif failure == "foreign_version":
        parent["document_version_id"] = str(uuid4())
    elif failure == "basis_page":
        edit["basis"]["file_page"] = 2
    elif failure == "locator_page":
        edit["locator"] = {"file_page": 2, "bbox_normalized": [.1, .2, .3, .4]}
    elif failure == "unknown_region":
        edit["locator"] = {"file_page": 1, "bbox_normalized": [.1, .2, .3, 10**400]}
    elif failure == "markup":
        edit["text"] = "&lt;script&gt;SYNTHETIC&lt;/script&gt;"
    elif failure == "external_link":
        edit["text"] = "原页详情 https://example.invalid/"
    elif failure == "arbitrary_source":
        edit["source_block_ref"] = source["id"]
    elif failure == "missing_basis":
        edit.pop("basis")
    elif failure == "container":
        payload["blocks"] = [_edit(next(block for block in parent["blocks"] if block["kind"] == "table_group"))]
    elif failure == "table_text":
        payload["blocks"] = [_edit(next(block for block in parent["blocks"] if block["kind"] == "table"))]
    elif failure == "graphic_table":
        payload["graphic_tables"] = [{"block_id": source["id"]}]
    else:
        payload["blocks"] = [_edit(next(block for block in parent["blocks"] if block["kind"] == "warning"), kind="paragraph")]
    with pytest.raises(ParseContractError, match=f"^{code}$"):
        apply_manual_corrections(child, parent_ir=parent, correction_artifact_id=request_artifact, payload=payload)


def test_empty_payload_keeps_parser_output_and_new_edits_do_not_discard_prior_manual_evidence(correction_source):
    parent, child, request_artifact = correction_source
    assert apply_manual_corrections(child, parent_ir=parent, correction_artifact_id=request_artifact, payload={}) == child
    parent["blocks"][0]["source_kind"] = "manual"
    warning = next(block for block in parent["blocks"] if block["kind"] == "warning")
    with pytest.raises(ParseContractError, match="^IR_CORRECTION_INHERITANCE_INVALID$"):
        apply_manual_corrections(child, parent_ir=parent, correction_artifact_id=request_artifact, payload={"blocks": [_edit(warning)]})


def test_repeated_edit_uses_new_request_and_preserves_prior_frozen_claim_through_local_alias(correction_source):
    source, original, first_request = correction_source
    previous = apply_manual_corrections(original, parent_ir=source, correction_artifact_id=first_request,
        payload={"blocks": [_edit(source["blocks"][0], text="第一次 👀", locator={"file_page": 1, "bbox_normalized": [.1, .2, .8, .6]})]})
    before = deepcopy(previous)
    candidate = normalize_ir(**{**_source_args(), "version_id": original["document_version_id"],
                                "source_file_id": original["source_file_id"]})
    first_alias, previous_ir_alias, second_request = str(uuid4()), str(uuid4()), str(uuid4())
    mapping = {previous["raw_ref"]["artifact_id"]: candidate["raw_ref"]["artifact_id"], first_request: first_alias,
               str(uuid4()): previous_ir_alias}
    inherited = inherit_manual_corrections(candidate, parent_ir=previous, artifact_map=mapping,
                                            parent_normalized_alias_id=previous_ir_alias)
    carried, first_block = inherited["blocks"][0], previous["blocks"][0]
    assert carried["id"] == candidate["blocks"][0]["id"]
    assert carried["text_content"] == "第一次 👀" and carried["source_block_ref"] == first_block["id"]
    assert carried["raw_ref"] == candidate["blocks"][0]["raw_ref"]
    assert carried["structured_content"]["manual_correction"]["inherited_from"] == {
        "artifact_id": previous_ir_alias, "json_pointer": "/blocks/0",
    }
    assert carried["locator"]["regions"][0]["geometry_evidence_ref"]["artifact_id"] == first_alias
    assert carried["structured_content"]["text_map"][0]["raw_ref"]["artifact_id"] == first_alias
    assert inherited["blocks"][1:] == candidate["blocks"][1:]

    payload = {"blocks": [_edit(first_block, text="再修正 🛠️")]}  # Apply to the already inherited manual leaf.
    updated = apply_manual_corrections(inherited, parent_ir=previous, correction_artifact_id=second_request, payload=payload)
    current = updated["blocks"][0]
    manual = current["structured_content"]["manual_correction"]
    assert current["source_block_ref"] == first_block["id"] and current["text_content"] == payload["blocks"][0]["text"]
    assert manual["text_source_ref"] == {"artifact_id": second_request, "json_pointer": "/blocks/0/text"}
    assert manual["inherited_from"]["artifact_id"] == previous_ir_alias
    assert {first_alias, second_request, previous_ir_alias} <= {row["artifact_id"] for row in updated["artifact_refs"]}
    assert previous == before and current["locator"] == carried["locator"]

    without_text = _edit(first_block, locator={"file_page": 1, "bbox_normalized": [.2, .2, .8, .6]})
    without_text.pop("text")
    region_only = apply_manual_corrections(inherited, parent_ir=previous, correction_artifact_id=second_request,
        payload={"blocks": [without_text]})["blocks"][0]
    assert region_only["structured_content"]["manual_correction"]["text_source_ref"]["artifact_id"] == first_alias
    assert region_only["locator"]["regions"][0]["geometry_evidence_ref"]["artifact_id"] == second_request

    del mapping[first_request]
    with pytest.raises(ParseContractError, match="^IR_CORRECTION_INHERITANCE_INVALID$"):
        inherit_manual_corrections(candidate, parent_ir=previous, artifact_map=mapping, parent_normalized_alias_id=previous_ir_alias)
    tampered = deepcopy(inherited)
    tampered["blocks"][0]["text_content"] = "来源被替换"
    with pytest.raises(ParseContractError, match="^IR_CORRECTION_INHERITANCE_INVALID$"):
        apply_manual_corrections(tampered, parent_ir=previous, correction_artifact_id=second_request, payload=payload)
