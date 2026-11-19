"""Local PDF evidence with page coordinates and reviewable table cells."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from math import isfinite
from pathlib import Path
from typing import Any

_PAGE_NUMBER = re.compile(r"(?:第\s*)?[-–—]?\s*(\d{1,5})\s*[-–—]?(?:\s*页)?")
_WARNING = re.compile(r"^(?:重要须知|警告|警示|小心|注意)(?:\s*[:：]|\s*$)|高温")


@dataclass
class Region:
    text: str
    kind: str
    bbox: tuple[float, float, float, float] | None = None
    structure: dict[str, Any] = field(default_factory=dict)
    problem: str | None = None


def normalized_bbox(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4 or not all(
        type(x) in {int, float} and isfinite(x) and 0 <= x <= 1 for x in value
    ):
        return None
    x0, y0, x1, y1 = value
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def render_source_region(page, bbox, *, dpi=200):
    """Render only the stored source region at a bounded raster size."""
    import pymupdf

    x0, y0, x1, y1 = normalized_bbox(bbox) or (0, 0, 0, 0)
    clip = pymupdf.Rect(x0 * page.rect.width, y0 * page.rect.height,
                        x1 * page.rect.width, y1 * page.rect.height)
    if clip.width < 1 or clip.height < 1:
        raise ValueError("source region is empty")
    scale = min(dpi / 72, 2200 / max(clip.width, clip.height))
    return page.get_pixmap(matrix=pymupdf.Matrix(scale, scale), clip=clip, alpha=False).tobytes("png")


def _table_regions(page, paths) -> list[Region]:
    result = []
    for table in page.find_tables(strategy="lines_strict", paths=paths).tables:
        rows = table.extract()
        if len(rows) < 2 or table.col_count < 2 or sum(
            sum(bool(cell and cell.strip()) for cell in row) >= 2 for row in rows
        ) < 2:
            continue
        cells = [[{"text": cell or "", "header": row_no == 0 and not table.header.external}
                  for cell in row] for row_no, row in enumerate(rows)]
        # A missing cell bbox can denote a merge, so preserve the text for
        # review and leave interpretation of an ambiguous grid to the reviewer.
        uncertain = any(cell is None for row in table.rows for cell in row.cells)
        result.append(Region(
            "\n".join(" | ".join(cell or "" for cell in row) for row in rows),
            "table", tuple(table.bbox), {} if uncertain else {"rows": cells},
            "table_structure_uncertain" if uncertain else None,
        ))
    return result


def _inside(region, parent) -> bool:
    x = (region[0] + region[2]) / 2
    y = (region[1] + region[3]) / 2
    return parent[0] <= x <= parent[2] and parent[1] <= y <= parent[3]


def _native_regions(page) -> list[Region]:
    import pymupdf

    text_blocks = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)["blocks"]
    lines = [("".join(span["text"] for span in line["spans"]).strip(), tuple(line["bbox"]))
             for block in text_blocks for line in block.get("lines", [])]
    lines = [(text, box) for text, box in lines if text]
    paths = page.get_drawings() if lines else []
    tables = _table_regions(page, paths) if paths and lines else []
    regions: list[Region] = []
    warning_heading = None
    height = page.rect.height
    for text, box in lines:
        if any(_inside(box, table.bbox) for table in tables):
            continue
        rotated_box = pymupdf.Rect(box) * page.rotation_matrix
        relative_box = (rotated_box.x0 / page.rect.width, rotated_box.y0 / height,
                        rotated_box.x1 / page.rect.width, rotated_box.y1 / height)
        number = _PAGE_NUMBER.fullmatch(text)
        if number and relative_box[1] >= 0.89:
            regions.append(Region(number[1], "page_number", relative_box))
            continue
        kind = "warning" if _WARNING.search(text) else "paragraph"
        if warning_heading and box[1] <= warning_heading + 12:
            kind = "warning"
        warning_heading = box[3] if kind == "warning" else None
        if regions and regions[-1].kind == kind and regions[-1].bbox and 0 <= relative_box[1] - regions[-1].bbox[3] <= 0.012:
            previous = regions[-1]
            previous.text += "\n" + text
            previous.bbox = (min(previous.bbox[0], relative_box[0]), previous.bbox[1],
                             max(previous.bbox[2], relative_box[2]), relative_box[3])
        else:
            regions.append(Region(text, kind, relative_box))
    for table in tables:
        box = pymupdf.Rect(table.bbox) * page.rotation_matrix
        table.bbox = (box.x0 / page.rect.width, box.y0 / height, box.x1 / page.rect.width, box.y1 / height)
    return sorted([*regions, *tables], key=lambda region: (region.bbox[1], region.bbox[0]))


def _visual_regions(page) -> list[Region]:
    """Inventory source graphics independently of text and OCR success."""
    import pymupdf

    candidates = [(image["bbox"], "embedded_image") for image in page.get_image_info()]
    candidates.extend((box, "vector_graphics") for box in page.cluster_drawings(final_filter=False))
    result = []
    for bounds, source_type in candidates:
        box = pymupdf.Rect(bounds) * page.rotation_matrix
        if source_type == "vector_graphics":
            box = box + (-0.5, -0.5, 0.5, 0.5)
        box = box & page.rect
        if box.is_empty:
            continue
        normalized = (box.x0 / page.rect.width, box.y0 / page.rect.height,
                      box.x1 / page.rect.width, box.y1 / page.rect.height)
        result.append(Region("原页图形区域，内容、内部元素及关联待核验。", "image", normalized,
                             {"source_type": source_type, "coverage_candidate": True},
                             "visual_semantics_unverified"))
    return result


def _printed_pages(pages: dict[int, list[Region]]) -> dict[int, str]:
    candidates = {}
    for page, regions in pages.items():
        numbers = {item.text for item in regions if item.kind == "page_number"}
        if len(numbers) == 1:
            candidates[page] = numbers.pop()
    # Support numbering resets and unnumbered covers. A lone candidate is not
    # promoted to a printed label without corroboration from an adjacent page.
    return {page: number for page, number in candidates.items() if any(
        neighbor in candidates and int(candidates[neighbor]) - int(number) == neighbor - page
        for neighbor in (page - 1, page + 1)
    )}


def parse_local_pdf(path: Path, mineru_executable: Path | None, mineru_home: Path | None):
    """Return text, locator, kind and structure for each independently reviewable block."""
    pages: dict[int, list[Region]] = {}
    visuals: dict[int, list[Region]] = {}
    scans: list[int] = []
    try:
        import pymupdf
        with pymupdf.open(path) as document:
            for number, page in enumerate(document, 1):
                try:
                    visuals[number] = _visual_regions(page)
                except (RuntimeError, ValueError):
                    visuals[number] = [Region("原页图形清点失败，请核对整页。", "image", (0, 0, 1, 1),
                                              problem="visual_inventory_failed")]
                try:
                    regions = _native_regions(page)
                except (RuntimeError, ValueError):
                    regions = []
                pages[number] = regions
                meaningful_chars = sum(
                    char.isalnum() for region in regions if region.kind != "page_number" for char in region.text
                )
                # Bullet-only and symbol-only text layers may sit above raster body text.
                if not meaningful_chars or (meaningful_chars < 12 and page.get_image_info()):
                    scans.append(number)
    except (ImportError, RuntimeError, ValueError):
        return [("PDF 暂无可用解析结果，请核对原件及本地解析组件。",
                 {"file_page": None, "printed_page": None, "needs_review": True}, "unreviewed", {})]

    extracted = {}
    if scans:
        from .local_mineru import read_scan_pages

        extracted, failure = read_scan_pages(path, scans, mineru_executable, mineru_home)
        for number in scans:
            original_labels = [item for item in pages[number] if item.kind == "page_number"]
            if extracted.get(number):
                pages[number] = original_labels + extracted[number]
            else:
                pages[number].append(Region("扫描页待核验，请打开原文核对。", "scan",
                                            problem=failure or "ocr_no_text"))
    labels = _printed_pages(pages)
    blocks = []
    for number, regions in pages.items():
        regions = [*regions, *visuals.get(number, [])]
        for item in regions:
            if item.kind == "page_number" and number in labels:
                continue
            locator = {"file_page": number, "printed_page": labels.get(number), "needs_review": True,
                       "extraction": "mineru_basic_ocr" if number in extracted else "ocr_pending" if item.kind == "scan" else "pdf_text"}
            if item.bbox:
                locator["bbox_normalized"] = item.bbox
            if item.problem:
                locator["parse_issue"] = item.problem
            if item.structure.get("coverage_candidate") or item.problem == "visual_inventory_failed":
                locator["extraction"] = "pdf_visual_inventory"
            blocks.append((item.text, locator, "paragraph" if item.kind == "page_number" else item.kind, item.structure))
    return blocks or [("PDF 暂无可用页面，请核对原件。", {"file_page": None, "printed_page": None,
                       "needs_review": True}, "unreviewed", {})]


def ocr_region(text, kind, bbox=None, structure=None, problem=None) -> Region:
    number = _PAGE_NUMBER.fullmatch(text)
    if number and bbox and bbox[1] >= 0.89 and kind in {"page_number", "footer", "paragraph"}:
        return Region(number[1], "page_number", bbox)
    return Region(text, "warning" if kind == "paragraph" and _WARNING.search(text) else kind,
                  bbox, structure or {}, problem)
