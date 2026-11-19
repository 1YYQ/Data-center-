import pytest

from .parsing import local_mineru
from .parsing.local_pdf import Region, parse_local_pdf
from .parsing.table_recovery import mark_graphic_keys, supplement_icons, supplement_warnings


@pytest.mark.parametrize("rotation", [0, 90])
def test_mixed_page_keeps_raster_and_vector_regions_without_ocr(tmp_path, monkeypatch, rotation):
    pymupdf = pytest.importorskip("pymupdf")
    source = tmp_path / "mixed.pdf"
    image = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 80, 40), False)
    image.clear_with(120)
    with pymupdf.open() as document:
        page = document.new_page(width=600, height=800)
        page.insert_text((40, 50), "Native text must not hide adjacent graphics.")
        page.insert_image(pymupdf.Rect(80, 150, 240, 230), pixmap=image)
        page.draw_rect(pymupdf.Rect(90, 350, 260, 420))
        page.set_rotation(rotation)
        document.save(source)

    def unexpected_ocr(*args):
        raise AssertionError("native mixed-page inventory must work without OCR")

    monkeypatch.setattr(local_mineru, "read_scan_pages", unexpected_ocr)
    blocks = parse_local_pdf(source, None, None)
    graphics = [block for block in blocks if block[3].get("coverage_candidate")]
    assert {block[3]["source_type"] for block in graphics} == {"embedded_image", "vector_graphics"}
    assert any("Native text" in block[0] for block in blocks)
    for _, locator, kind, _ in graphics:
        assert kind == "image" and locator["file_page"] == 1
        assert locator["parse_issue"] == "visual_semantics_unverified"
        assert locator["extraction"] == "pdf_visual_inventory" and locator["needs_review"]
        assert all(0 <= value <= 1 for value in locator["bbox_normalized"])


def test_symbol_only_pdf_text_does_not_bypass_ocr(tmp_path, monkeypatch):
    pymupdf = pytest.importorskip("pymupdf")
    source = tmp_path / "scan.pdf"
    with pymupdf.open() as document:
        page = document.new_page()
        for y in range(60, 300, 15):
            page.insert_text((72, y), "\u2022")
        assert page.get_text().strip() and not any(char.isalnum() for char in page.get_text())
        document.save(source)

    def recognize(path, pages, executable, home):
        assert path == source and pages == [1]
        return {1: [Region("扫描识别出的正文", "paragraph", (0.1, 0.2, 0.5, 0.3))]}, None

    monkeypatch.setattr(local_mineru, "read_scan_pages", recognize)
    text, locator, kind, _ = parse_local_pdf(source, None, None)[0]
    assert text == "扫描识别出的正文" and kind == "paragraph"
    assert locator["extraction"] == "mineru_basic_ocr"
    assert locator["file_page"] == 1 and locator["printed_page"] is None
    assert locator["needs_review"] and locator["bbox_normalized"] == (0.1, 0.2, 0.5, 0.3)


def test_mineru_table_cells_preserve_spans_and_keep_unclear_rows_for_review():
    table = local_mineru._region({
        "type": "table", "bbox": [0.1, 0.2, 0.8, 0.5],
        "content": [
            {"type": "table_caption", "content": [{"type": "text", "content": "Table caption"}]},
            {"type": "table_body", "content": "<table><tr><th colspan='2'>Header</th></tr>"
             "<tr><td>Part</td><td>5</td></tr><tr><td></td><td></td></tr></table>"},
            {"type": "table_footnote", "content": [{"type": "text", "content": "Table footnote"}]},
        ],
    })
    assert table.kind == "table" and table.problem == "table_structure_uncertain"
    assert table.structure["rows"][0] == [{"text": "Header", "header": True, "rowspan": 1, "colspan": 2}]
    assert table.structure["rows"][1][1]["text"] == "5"
    assert table.structure["captions"] == ["Table caption"]
    assert table.structure["footnotes"] == ["Table footnote"]


@pytest.mark.parametrize("duplicate", [False, True])
def test_scan_warning_recovery_requires_a_unique_later_cell_suffix(duplicate):
    suffix = "勿在测试进行中拔出测试附件，结束后再尝试重启。"
    cells = [[{"text": "演练说明"}, {"text": suffix}]]
    if duplicate:
        cells.append([{"text": "另一条说明"}, {"text": suffix}])
    table = Region(suffix, "table", (0.1, 0.2, 0.9, 0.7), {"rows": cells})
    supplement_warnings(table, [
        {"text": "⚠注意：这里是模拟提示，请", "box": [0.4, 0.1, 0.96, 0.15], "score": 0.99},
        {"text": "演练说明", "box": [0.01, 0.18, 0.2, 0.23], "score": 0.99},
        {"text": "勿在测试进行中拔出测试附件", "box": [0.35, 0.18, 0.94, 0.22], "score": 0.99},
        {"text": "，结束后再尝试重启。", "box": [0.35, 0.25, 0.77, 0.29], "score": 0.99},
    ])
    if duplicate:
        assert table.text == suffix and "recovered_warnings" not in table.structure
    else:
        warning = "注意：这里是模拟提示，请" + suffix
        assert cells[0][1]["text"] == warning
        assert warning in table.text
        assert table.structure["recovered_warnings"] == [{"row": 0, "column": 1,
            "bbox_normalized": [0.38, 0.25, 0.868, 0.345]}]


def test_icon_recovery_needs_exact_surrounding_text_and_a_confirmed_crop():
    original = "按操作面板键取消模拟步骤。"
    table = Region(original, "table", (0.1, 0.2, 0.9, 0.7), {"rows": [[{"text": original}]]})
    attempt = {"text": original, "score": 0.99, "box": [0.1, 0.1, 0.8, 0.2]}
    supplement_icons(table, [attempt])
    attempt["cross_key_box"] = [0.3, 0.1, 0.5, 0.2]
    supplement_icons(table, [{**attempt, "text": "按操作面板键确认模拟步骤。"}])
    assert table.text == original
    supplement_icons(table, [attempt])
    assert table.text == "按操作面板×键取消模拟步骤。"
    assert table.structure["recovered_icons"] == [{"row": 0, "column": 0, "symbol": "×",
                                                   "bbox_normalized": [0.34, 0.25, 0.5, 0.3]}]


def test_graphic_key_legend_keeps_operations_and_suppresses_uncertain_ocr():
    rows = [[{"text": cell} for cell in row] for row in [
        ("示意图", "按键", "按键介绍"),
        ("U", "返回键，返回上一级。"),
        ("目", "双面复印键，进入双面复印模式。"),
        ("", "开始键，开始相应操作。"),
        ("OK", "确定键，确认当前操作。"),
    ]]
    table = Region("", "table", (.1, .2, .8, .9), {"rows": rows})
    mark_graphic_keys(table)
    assert table.structure["pending_graphic_keys"] == [{"row": row, "column": 0} for row in (1, 2, 3)]
    assert "返回键" in table.text and "目" not in table.text and "U |" not in table.text
    assert "OK | 确定键" in table.text


def test_graphic_keys_in_operation_rows_flag_only_missing_references():
    rows = [[{"text": cell} for cell in row] for row in [
        ("操作", "关键操作步骤"),
        ("开机", "拨动机身后侧 键开机。"),
        ("关机", "注意：此操作需要按原文核验。"),
        ("常规复印", "点击键进行复印。"),
        ("取消当前任务", "按×键。"),
        ("休眠唤醒", "点击操作面板上任意按键将机器唤醒。"),
    ]]
    table = Region("", "table", (.1, .2, .8, .9), {"rows": rows})
    mark_graphic_keys(table)
    assert table.structure["pending_graphic_keys"] == [{"row": row, "column": 1} for row in (1, 2, 3)]
    assert "注意：此操作需要按原文核验。" in table.text


def test_manual_graphic_cell_correction_preserves_source_and_remaining_cells():
    from .governance import block_binding
    from .models import ContentBlock
    from .schemas import GraphicTableInput
    from .services import SafeError
    from .parsing.table_corrections import table_graphic_edits

    source = ContentBlock(id="source", parse_run_id="parent", kind="table", text_content="初始",
        locator={"file_page": 6, "extraction": "mineru_basic_ocr", "bbox_normalized": [.1, .1, .9, .9]},
        structured_content={"rows": [[{"text": "原文", "rowspan": 2}, {"text": "旧 OCR"}],
                                     [{"text": "另一格"}]],
                            "footnotes": ["保留脚注"],
                            "pending_graphic_keys": [{"row": 0, "column": 1}, {"row": 1, "column": 0}]})
    request = {"block_id": "source", "source_content_hash": block_binding(source),
               "cells": [{"row": 0, "column": 1, "text": "依据原图校正", "keys": [
                   {"label": "模拟按键", "operation": "模拟用途", "bbox_normalized": [.3, .31, .35, .36]}]}]}
    text, corrected = table_graphic_edits(source, GraphicTableInput(**request))
    assert corrected["pending_graphic_keys"] == [{"row": 1, "column": 0}]
    assert corrected["rows"][0][0]["rowspan"] == 2 and "保留脚注" in text
    assert corrected["rows"][0][1]["graphic_keys"][0]["bbox_normalized"] == [.3, .31, .35, .36]
    assert corrected["graphic_key_correction"]["parent_run_id"] == "parent"
    assert source.structured_content["rows"][0][1]["text"] == "旧 OCR"

    with pytest.raises(SafeError) as stale:
        table_graphic_edits(source, GraphicTableInput(**{**request, "source_content_hash": "0" * 64}))
    assert stale.value.status_code == 409
    request["cells"][0]["keys"][0]["bbox_normalized"] = [.02, .02, .04, .04]
    with pytest.raises(SafeError) as outside:
        table_graphic_edits(source, GraphicTableInput(**request))
    assert outside.value.status_code == 422
