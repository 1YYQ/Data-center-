"""Supplement scan tables when crop OCR identifies a unique existing cell."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from .local_pdf import Region, normalized_bbox


_WARNING_PREFIX = re.compile(r"(?:重要须知|警告|警示|注意|小心|提示)\s*[:：]")
_MIN_CONFIDENCE = 0.90
_KEY_DESCRIPTION = re.compile(r"^([^，。:：]{1,12}键)(?=[，。,:：\s]|$)")
_MISSING_KEY = re.compile(r"(?:点击|按下|面板上的|或者)\s*键|(?:^|[。；，])\s*按\s*键(?=[，。；]|进入|进行|开始|扫描)")
_DIRECT_KEY_LABELS = {"OK": "确定键", "×": "取消键"}


@dataclass(frozen=True)
class Line:
    text: str
    box: tuple[float, float, float, float]


def _letters(value: str) -> str:
    return "".join(x for x in unicodedata.normalize("NFKC", value) if x.isalnum())


def _prefix(value: str, anchor: str) -> str:
    remaining = len(anchor)
    for index in range(len(value) - 1, -1, -1):
        remaining -= len(_letters(value[index]))
        if remaining == 0:
            return value[:index]
    return ""


def _continuation(previous: Line, candidate: Line) -> bool:
    ax, ay, bx, by = previous.box
    cx, cy, dx, dy = candidate.box
    common_width = min(bx, dx) - max(ax, cx)
    height = max(by - ay, dy - cy)
    return (0.35 * height < cy - ay <= 2.5 * height
            and common_width >= 0.65 * min(bx - ax, dx - cx))


def _source_box(lines: list[Line], crop):
    x0, y0, x1, y1 = crop
    return [round(x0 + (x1 - x0) * min(line.box[0] for line in lines), 5),
            round(y0 + (y1 - y0) * min(line.box[1] for line in lines), 5),
            round(x0 + (x1 - x0) * max(line.box[2] for line in lines), 5),
            round(y0 + (y1 - y0) * max(line.box[3] for line in lines), 5)]


def _refresh_table_text(table: Region) -> None:
    table.text = render_table_text(table.structure)


def render_table_text(structure: dict) -> str:
    captions = structure.get("captions", [])
    footnotes = structure.get("footnotes", [])
    rows = [" | ".join(cell["text"] for cell in row) for row in structure["rows"]]
    return "\n".join(filter(None, [*captions, *rows, *footnotes]))


def mark_graphic_keys(table: Region) -> None:
    """Keep unmatched pictograms in the source image and flag their cells."""
    pending = []
    layout = None
    for row_index, row in enumerate(table.structure.get("rows", [])):
        if len(row) < 2:
            continue
        label, detail = (cell["text"].strip() for cell in row[-2:])
        if label == "按键" and detail in {"按键介绍", "按键说明"}:
            layout = "legend"
            continue
        if label == "操作" and detail == "关键操作步骤":
            layout = "operations"
            continue
        if layout == "legend" and (description := _KEY_DESCRIPTION.match(detail)):
            if _DIRECT_KEY_LABELS.get(label) == description[1]:
                continue
            # Values such as U and 目 are unsupported interpretations of
            # pictograms. The adjacent operation and original crop are retained.
            row[-2]["text"] = ""
            pending.append({"row": row_index, "column": len(row) - 2})
        elif layout == "operations" and (
            _MISSING_KEY.search(detail) or
            (label in {"开机", "关机"} and "电源开关" not in detail and "电源键" not in detail)
        ):
            pending.append({"row": row_index, "column": len(row) - 1})
    if pending:
        table.structure["pending_graphic_keys"] = pending
        _refresh_table_text(table)


def supplement_icons(table: Region, result: list[dict]) -> None:
    rows = table.structure["rows"]
    recovered = []
    for item in result:
        box = normalized_bbox(item.get("cross_key_box"))
        if not box or item["score"] < _MIN_CONFIDENCE or item["text"].count("面板键") != 1:
            continue
        anchor = _letters(item["text"])
        found = [(i, j, cell) for i, row in enumerate(rows) for j, cell in enumerate(row)
                 if _letters(cell["text"]) == anchor]
        if len(found) != 1:
            continue
        row, column, cell = found[0]
        if cell["text"].count("面板键") != 1:
            continue
        cell["text"] = cell["text"].replace("面板键", "面板×键")
        recovered.append({"row": row, "column": column, "symbol": "×",
                          "bbox_normalized": _source_box([Line(item["text"], box)], table.bbox)})
    if recovered:
        table.structure["recovered_icons"] = recovered
        _refresh_table_text(table)


def supplement_warnings(table: Region, result: list[dict]) -> None:
    lines = [Line(item["text"].strip(), box) for item in result
             if item["score"] >= _MIN_CONFIDENCE and (box := normalized_bbox(item["box"]))]
    lines.sort(key=lambda line: (line.box[1], line.box[0]))
    rows = table.structure["rows"]
    cells = [(i, j, _letters(cell["text"])) for i, row in enumerate(rows) for j, cell in enumerate(row)
             if not _WARNING_PREFIX.search(cell["text"]) and len(_letters(cell["text"])) >= 12]
    matches = {}
    for i, line in enumerate(lines):
        mark = _WARNING_PREFIX.search(line.text)
        if not mark:
            continue
        warning = line.text[mark.start():]
        span = [line]
        for j in range(i + 1, min(i + 10, len(lines))):
            candidate = lines[j]
            if not _continuation(span[-1], candidate):
                continue
            if _WARNING_PREFIX.search(candidate.text):
                break
            warning += candidate.text
            span.append(candidate)
            plain = _letters(warning)
            anchored = [(ri, ci) for ri, ci, anchor in cells
                        if len(plain) > len(anchor) and plain.endswith(anchor)]
            if anchored:
                if len(anchored) == 1:
                    row, column = anchored[0]
                    original = rows[row][column]["text"]
                    prefix = _prefix(warning, _letters(original))
                    if prefix:
                        matches.setdefault((row, column), []).append((prefix + original, _source_box(span, table.bbox)))
                break
    recovered = []
    for (row, column), candidates in matches.items():
        if len(candidates) != 1:
            continue
        warning, box = candidates[0]
        rows[row][column]["text"] = warning
        recovered.append({"row": row, "column": column, "bbox_normalized": box})
    if recovered:
        table.structure["recovered_warnings"] = recovered
        _refresh_table_text(table)
