"""Source-bound normalization checks using synthetic Middle JSON and preflight."""

from copy import deepcopy
from html import unescape
import json
from uuid import UUID, uuid4

import pytest

from .parsing.ir import RENDERER, normalize_ir
from .parsing.local_pipeline import PROFILE, ParseContractError


def _page(number, *, state="pending", provider_space="display_normalized", **kwargs):
    return {"file_page": number, "media_box_pt": [-30, 0, 270, 400], "crop_box_pt": [20, 10, 220, 310],
            "rotation": 90, "width_pt": 300, "height_pt": 200, "user_unit": 1,
            "printed_page": None, "printed_page_basis": None, "page_class": "unknown", "state": state,
            "coverage": {"image_candidate_count": 0}, "issue_refs": [],
            "transforms": {"display_space": "rotated_crop_box", "provider_space": provider_space, **kwargs}}


def _input(pages, raw_pages, *, full=True, requested=None, asset_refs=None):
    sha = "a" * 64
    preflight = {"page_count": len(pages), "pages": pages, "source_sha256": sha}
    if requested is not None:
        preflight["requested_file_pages"] = requested
    middle = {"schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": full,
              "metadata": {"file_suffix": "pdf", "producer": {"name": "MinerU", "version": "4.2.3"}},
              "extensions": {"mineru": {"tier": "standard", "parse_mode": "txt"},
                  "docvortex_layout": {"version": 1, "pages": [
                      {"page_idx": page["page_idx"], "width_pt": 300, "height_pt": 200} for page in raw_pages]}},
              "pages": raw_pages}
    return dict(run_id=str(uuid4()), version_id=str(uuid4()), source_file_id=str(uuid4()), source_sha256=sha,
                profile=PROFILE, preflight=preflight, middle=middle, raw_artifact_id=str(uuid4()), asset_refs=asset_refs)


def _raw_page(index, *blocks):
    return {"page_idx": index, "blocks": list(blocks)}


def _resolve(value, pointer):
    for segment in pointer.split("/")[1:]:
        segment = segment.replace("~1", "/").replace("~0", "~")
        value = value[int(segment)] if isinstance(value, list) else value[segment]
    return value


def _check_text(block, args):
    structured = block["structured_content"]
    assert structured["renderer"] == RENDERER
    entries = sorted([*structured["text_map"], *structured["assembly"]], key=lambda part: part["block_start"])
    offset = 0
    for part in entries:
        assert part["block_start"] == offset
        offset = part["block_end"]
        if "raw_ref" in part:
            raw_ref = part["raw_ref"]
            assert raw_ref["artifact_id"] == args["raw_artifact_id"]
            assert raw_ref["raw_member_name"] == "middle_json.json"
            source = _resolve(args["middle"], raw_ref["json_pointer"])
            text = source[part["source_start"]:part["source_end"]]
            if part.get("encoding") == "html_entity":
                text = unescape(text)
        else:
            text = part["text"]
        assert text == block["text_content"][part["block_start"]:offset]
    assert offset == len(block["text_content"])


def test_page_inventory_and_crop_to_rotated_display_are_source_bound():
    pages = [_page(1), _page(2, state="blank"),
             _page(3, provider_space="unrotated_crop_pixels", provider_to_display_normalized=[0, .005, -.005, 0, .8, .1]),
             _page(4, state="failed"), _page(5)]
    blocks = [{"type": "text", "bbox": [20, 40, 60, 80], "content": [{"type": "text", "content": "甲😀"},
               {"type": "hyperlink", "url": "https://example.invalid", "content": [{"type": "text", "content": "乙"}]}]},
              {"type": "paragraph_title", "level": 2, "bbox": [.1, .1, .2, .2], "bbox_space": "unverified", "content": "原段名"}]
    args = _input(pages, [_raw_page(1), _raw_page(2, *blocks)], full=False, requested=[2, 3, 4])
    before = deepcopy(args)
    result = normalize_ir(**args)
    assert args == before
    assert (result["schema"], result["schema_version"], result["phase"]) == ("dc.document-ir", "1.0", "normalized")
    assert result["source_sha256"] == args["source_sha256"]
    assert result["relations"] == []
    assert [(page["file_page"], page["state"]) for page in result["pages"]] == [
        (1, "not_requested"), (2, "blank"), (3, "needs_review"), (4, "failed"), (5, "not_requested")]
    assert result["pages"][2]["crop_box_pt"] == [20, 10, 220, 310]
    assert result["pages"][2]["rotation"] == 90
    assert result["pages"][2]["printed_page"] is None
    first, second = result["blocks"]
    assert first["text_content"] == "甲😀乙"
    assert first["file_page"] == 3 and first["reading_order"] is None
    assert first["raw_ref"]["json_pointer"] == "/pages/1/blocks/0"
    assert _resolve(args["middle"], first["raw_ref"]["json_pointer"]) == blocks[0]
    region = first["locator"]["regions"][0]
    assert region["geometry_status"] == "exact"
    assert region["bbox_normalized"] == pytest.approx([.4, .2, .6, .4])
    assert region["raw_geometry"]["bbox"] == [20, 40, 60, 80]
    assert region["raw_geometry"]["coordinate_space"] == "unrotated_crop_pixels"
    assert region["transform_ref"] == "/transforms/provider_to_display_normalized"
    assert "polygon" not in region
    assert second["heading_level"] == 2
    assert second["locator"]["regions"][0]["geometry_status"] == "page_only"
    assert "bbox_normalized" not in second["locator"]["regions"][0]
    _check_text(first, args)
    assert {issue["code"] for issue in result["issues"]} == {"SOURCE_LOCATOR_MISSING", "IR_PAGE_OUTPUT_MISSING"}


def test_table_grid_text_sources_associations_and_image_member():
    asset_id = str(uuid4())
    html = ('<table><caption>表&#x1f605;</caption><thead><tr><th rowspan="2">  A&amp; 😀 </th>'
            '<th colspan="2">B<br>C</th></tr><tr><th>第二</th><th>第三</th></tr></thead>'
            '<tbody><tr><td><img src="images/glyph.png"></td><td>  </td><td>45&nbsp;N</td></tr></tbody></table>')
    box = [.1, .2, .8, .9]
    node = {"type": "table", "bbox": box, "content": [
        {"type": "table_caption", "bbox": [.1, .1, .3, .2], "content": [{"type": "text", "content": "源题注"}]},
        {"type": "table_body", "bbox": box, "content": html},
        {"type": "table_footnote", "bbox": [.1, .9, .4, 1], "content": [{"type": "text", "content": "源脚注"}]},
    ]}
    args = _input([_page(1)], [_raw_page(0, node)], asset_refs={"images/glyph.png": asset_id})
    ir = normalize_ir(**args)
    assert ir["issues"] == []
    parent, caption, table, footnote = ir["blocks"]
    assert [block["ordinal"] for block in ir["blocks"]] == [0, 1, 2, 3]
    assert parent["structure_role"] == "container" and parent["locator"]["regions"] == []
    assert parent["text_content"] == ""
    assert parent["structured_content"]["caption_refs"] == [caption["id"]]
    assert parent["structured_content"]["footnote_refs"] == [footnote["id"]]
    assert table["parent_block_id"] == parent["id"]
    assert table["text_content"] == "表😅\n  A& 😀  | B\nC\n第二 | 第三\n |    | 45\xa0N"
    structured = table["structured_content"]
    assert structured["asset_refs"] == [asset_id]
    assert structured["asset_parts"][0]["raw_ref"]["json_pointer"] == "/pages/0/blocks/0/content/1/content"
    assert structured["asset_parts"][0]["cell_ref"] == {"row": 2, "column": 0}
    assert ir["artifact_refs"][1] == {"artifact_id": asset_id, "kind": "image", "raw_member_name": "images/glyph.png"}
    assert "<table>" not in json.dumps(ir, ensure_ascii=False)
    assert (structured["grid_origin"], structured["row_count"], structured["column_count"]) == (0, 3, 3)
    rows = structured["rows"]
    assert [(cell["column"], cell["rowspan"], cell["colspan"]) for cell in rows[0]] == [(0, 2, 1), (1, 1, 2)]
    assert [cell["column"] for cell in rows[1]] == [1, 2]
    assert all(cell["header"] for row in rows[:2] for cell in row)
    assert rows[2][1]["text"] == "  " and rows[2][1]["value_state"] == "unreadable"
    position = rows[2][2]["locator"]
    assert position["cell_ref"] == {"row": 2, "column": 2}
    assert position["regions"][0]["geometry_status"] == "table_region_only"
    assert position["regions"][0]["bbox_normalized"] == box
    _check_text(table, args)


def test_formula_resources_unknown_types_and_stable_physical_ids():
    box = [.1, .2, .8, .9]
    asset_id = str(uuid4())
    nodes = [{"type": "equation", "bbox": box, "content": "  α=β^2 ", "mathml": "<math><mi>α</mi></math>",
              "equation_label": "(1)", "image_path": "images/expression.png"},
             {"type": "chart", "bbox": box, "content": [{"type": "chart_body", "bbox": box, "content": "",
              "image_path": "images/missing.png", "image_url": "https://example.invalid/chart.png"}]},
             {"type": "custom_critical", "bbox": box, "content": "数据", "future": {"field/tilde~": "<b>raw</b>"}}]
    args = _input([_page(1)], [_raw_page(0, *nodes)], asset_refs={"images/expression.png": asset_id})
    ir = normalize_ir(**args)
    assert ir == normalize_ir(**args)
    assert json.loads(json.dumps(ir)) == ir
    formula, _, chart, unknown = ir["blocks"]
    assert formula["kind"] == "formula" and formula["text_content"] == "  α=β^2 \n(1)"
    assert formula["structured_content"]["mathml_raw"] == "<math><mi>α</mi></math>"
    assert formula["structured_content"]["definition_refs"] == []
    assert formula["structured_content"]["asset_refs"] == [asset_id]
    _check_text(formula, args)
    assert chart["kind"] == "chart" and chart["text_content"] == ""
    assert "asset_refs" not in chart["structured_content"]
    assert unknown["kind"] == "unknown" and unknown["structure_role"] == "observation"
    assert unknown["provider_fields"]["content"] == "数据"
    assert unknown["provider_fields"]["future"]["field/tilde~"]["raw_ref"]["json_pointer"].endswith("/field~1tilde~0")
    assert [issue["code"] for issue in ir["issues"]].count("PARSE_RESOURCE_MISSING") == 2
    assert "IR_PROVIDER_TYPE_UNKNOWN" in {issue["code"] for issue in ir["issues"]}
    changed = normalize_ir(**{**args, "run_id": str(uuid4())})
    assert set(UUID(block["id"]) for block in ir["blocks"]).isdisjoint(UUID(block["id"]) for block in changed["blocks"])
    assert len({block["content_hash"] for block in ir["blocks"]}) == len(ir["blocks"])


@pytest.mark.parametrize("html", [
    "<table><tr><td rowspan='2'>outside</td></tr></table>",
    "<table><tr><td>A</td><td rowspan='2'>B</td></tr><tr><td colspan='2'>overlap</td></tr></table>",
    "<table><tr><td><script>alert('no')</script></td></tr></table>",
])
def test_unverified_table_has_no_invented_cells_or_executable_html(html):
    args = _input([_page(1)], [_raw_page(0, {"type": "table_body", "bbox": [.1, .1, .9, .9], "content": html})])
    ir = normalize_ir(**args)
    block = ir["blocks"][0]
    assert block["text_content"] == ""
    assert "rows" not in block["structured_content"]
    assert html not in json.dumps(ir)
    assert ir["issues"][0]["code"] == "IR_TABLE_HTML_UNSUPPORTED"
    assert _resolve(args["middle"], ir["issues"][0]["raw_ref"]["json_pointer"]) == html


def test_dimension_or_resource_provenance_cannot_be_guessed():
    args = _input([_page(1, provider_space=None)], [_raw_page(0, {"type": "image_body", "bbox": [.1, .1, .9, .9],
                  "image_path": "images/asset.png", "content": ""})],
                  asset_refs={"images/asset.png": {"artifact_id": str(uuid4()), "parse_run_id": str(uuid4())}})
    ir = normalize_ir(**args)
    block = ir["blocks"][0]
    assert block["locator"]["regions"][0]["geometry_status"] == "page_only"
    assert len(ir["artifact_refs"]) == 1
    assert block["text_content"] == ""
    assert {issue["code"] for issue in ir["issues"]} == {
        "SOURCE_LOCATOR_MISSING", "PARSE_RESOURCE_MISSING", "IR_VISUAL_RESOURCE_UNAVAILABLE"}


@pytest.mark.parametrize("layout_size", [None, (200, 300), (300, 200)])
def test_pinned_display_bbox_requires_provider_layout_on_same_page(layout_size):
    source_page = _page(1)
    source_page.update(width_pt=600, height_pt=400, user_unit=2)
    args = _input([source_page], [_raw_page(0, {"type": "text", "bbox": [.1, .1, .9, .9], "content": "源文字"})])
    layout = args["middle"]["extensions"]["docvortex_layout"]
    if layout_size is None:
        del args["middle"]["extensions"]["docvortex_layout"]
    else:
        layout["pages"][0].update(width_pt=layout_size[0], height_pt=layout_size[1])
    ir = normalize_ir(**args)
    region = ir["blocks"][0]["locator"]["regions"][0]
    if layout_size == (300, 200):
        assert region["geometry_status"] == "exact"
        assert region["bbox_normalized"] == [.1, .1, .9, .9]
        assert _resolve(args["middle"], region["raw_geometry"]["layout_raw_ref"]["json_pointer"]) == layout["pages"][0]
    else:
        assert region["geometry_status"] == "page_only"
        assert len(ir["issues"]) == 1 and ir["issues"][0]["code"] == "SOURCE_LOCATOR_MISSING"


def test_unknown_schema_and_duplicate_source_pages_are_rejected():
    args = _input([_page(1)], [_raw_page(0), _raw_page(0)])
    with pytest.raises(ParseContractError, match="PARSE_PAGE_COVERAGE_INCOMPLETE"):
        normalize_ir(**args)
    args["middle"]["schema_version"] = "3.0"
    with pytest.raises(ParseContractError, match="PARSE_CONTRACT_UNSUPPORTED"):
        normalize_ir(**args)
