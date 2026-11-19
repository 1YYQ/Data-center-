"""Normalize the archived MinerU 4.2.3 Middle 2.0 PDF protocol into Document IR."""

from __future__ import annotations

from copy import deepcopy
from html import unescape
from html.parser import HTMLParser
from math import isclose, isfinite
import re
from uuid import NAMESPACE_URL, uuid5

from .local_pipeline import PROFILE, ParseContractError, _member_name, fingerprint, middle_page_blocks
from .local_pdf import normalized_bbox


RENDERER = "dc.pdf-source-text/1"
RAW_MEMBER = "middle_json.json"
_BLOCK_NAMESPACE = uuid5(NAMESPACE_URL, "dc.document-ir/1.0/physical-block")
_CONTAINERS = {"image": "image_group", "table": "table_group", "chart": "chart_group",
               "code": "code_group", "list": "list", "index": "index"}
_LEAVES = {
    "text": "paragraph", "paragraph": "paragraph", "ref_text": "ref_text", "doc_title": "title",
    "paragraph_title": "title", "header": "header", "footer": "footer", "page_number": "page_number",
    "aside_text": "aside_text", "page_footnote": "footnote", "table_body": "table",
    "image_body": "image", "chart_body": "chart", "equation": "formula", "code_body": "code",
    "algorithm_body": "algorithm", "warning": "warning", "step": "step",
    **{f"{kind}_{part}": part for kind in ("table", "image", "chart", "code") for part in ("caption", "footnote")},
}
_ASSET_FIELDS = ("image_path", "img_path", "src", "image_url", "image_base64")
_INLINE_STYLES = {"bold", "italic", "underline", "emphasis", "strikethrough", "superscript", "subscript"}
_HTML_LIMIT = 1_000_000
_ROW_LIMIT = 2_048
_COLUMN_LIMIT = 512
_SLOT_LIMIT = 65_536


def _pointer(base: str, field: str) -> str:
    return f"{base}/{field.replace('~', '~0').replace('/', '~1')}"


def _raw_ref(artifact_id: str, pointer: str) -> dict:
    return {"artifact_id": artifact_id, "raw_member_name": RAW_MEMBER, "json_pointer": pointer}


def _fields(node: dict, exclude: set[str], raw_ref: dict) -> dict:
    """Preserve extension fields, keeping markup and binary payloads in the original archive."""
    def safe_value(value, pointer):
        if isinstance(value, str) and ("<" in value or len(value) > _HTML_LIMIT):
            return {"raw_ref": {**raw_ref, "json_pointer": pointer}}
        if isinstance(value, dict):
            return {key: safe_value(item, _pointer(pointer, key)) for key, item in value.items()}
        if isinstance(value, list):
            return [safe_value(item, f"{pointer}/{index}") for index, item in enumerate(value)]
        return deepcopy(value)

    return {key: safe_value(value, _pointer(raw_ref["json_pointer"], key))
            for key, value in node.items() if key not in exclude}


class _Text:
    def __init__(self) -> None:
        self.chunks: list[str] = []
        self.length = 0
        self.text_map: list[dict] = []
        self.assembly: list[dict] = []

    def add(self, value: str, *, part: dict | None = None, marker: str | None = None) -> None:
        if not value:
            return
        span = {"block_start": self.length, "block_end": self.length + len(value)}
        if part is not None:
            self.text_map.append({**span, **part})
        else:
            self.assembly.append({**span, "text": value, "kind": marker})
        self.chunks.append(value)
        self.length = span["block_end"]

    def finish(self, structured: dict) -> str:
        structured.update(renderer=RENDERER, text_map=self.text_map, assembly=self.assembly)
        return "".join(self.chunks)


def _json_piece(content: str, raw_ref: dict, kind: str = "text") -> dict:
    return {"kind": kind, "raw_ref": raw_ref, "source_start": 0, "source_end": len(content)}


def _inline(content, raw_ref: dict, out: _Text, parts: list, report, depth: int = 0) -> None:
    if depth > 8:
        report("IR_INLINE_UNSUPPORTED", raw_ref)
        return
    if isinstance(content, str):
        part = _json_piece(content, raw_ref)
        if content:
            parts.append(part)
            out.add(content, part=part)
        return
    if not isinstance(content, list):
        if content is not None:
            report("IR_INLINE_UNSUPPORTED", raw_ref)
        return
    for index, span in enumerate(content):
        ref = {**raw_ref, "json_pointer": f"{raw_ref['json_pointer']}/{index}"}
        kind = span.get("type") if isinstance(span, dict) else None
        if kind == "hyperlink" and isinstance(span.get("content"), list):
            _inline(span["content"], {**ref, "json_pointer": f"{ref['json_pointer']}/content"}, out, parts, report, depth + 1)
        elif isinstance(kind, str) and kind in {"text", "equation_inline", "code_inline"} and isinstance(span.get("content"), str):
            value = span["content"]
            if value:
                ref = {**ref, "json_pointer": f"{ref['json_pointer']}/content"}
                part = _json_piece(value, ref, kind)
                styles = span.get("styles")
                if kind == "text" and styles is not None:
                    if isinstance(styles, list) and all(isinstance(style, str) and style in _INLINE_STYLES for style in styles):
                        part["styles"] = list(styles)
                    else:
                        report("IR_INLINE_UNSUPPORTED", {**ref, "json_pointer": f"{ref['json_pointer'].removesuffix('/content')}/styles"})
                parts.append(part)
                out.add(value, part=part)
        else:
            report("IR_INLINE_UNSUPPORTED", ref)


class _MarkupError(ValueError):
    pass


class _Markup(HTMLParser):
    """Extract text and source offsets, with a strict subset of HTML table structure."""

    def __init__(self, source: str, table: bool) -> None:
        super().__init__(convert_charrefs=False)
        if len(source) > _HTML_LIMIT:
            raise _MarkupError("limit")
        self.source = source
        self.line_starts = [0, *(match.end() for match in re.finditer("\n", source))]
        self.table_mode = table
        self.table_state = 0
        self.row_group = None
        self.group_number = 0
        self.rows: list[list[dict]] = []
        self.row_groups: list[int | None] = []
        self.in_row = False
        self.cell: dict | None = None
        self.caption: dict | None = None
        self.in_caption = False
        self.pieces: list[dict] = []
        self.assets: list[tuple[str | None, int, int, int | None]] = []
        self.feed(source)
        self.close()
        if table and (self.table_state != 2 or self.in_row or self.cell or self.in_caption or not self.rows):
            raise _MarkupError("structure")

    def _offset(self) -> int:
        line, column = self.getpos()
        return self.line_starts[line - 1] + column

    def _tag_end(self) -> int:
        return self.source.index(">", self._offset()) + 1

    def handle_starttag(self, tag, attrs) -> None:
        attributes = dict(attrs)
        offset = self._offset()
        if len(attributes) != len(attrs):
            raise _MarkupError("duplicate attributes")
        if tag in {"script", "style", "iframe", "object", "template", "svg", "noscript"}:
            raise _MarkupError("unsupported markup")
        if tag == "img":
            cell_start = self.cell["html_start"] if self.cell else None
            self.assets.append((attributes.get("src"), offset, offset + len(self.get_starttag_text()), cell_start))
        if tag == "br":
            self._append("\n", offset, 0, marker="html_line_break")
        if not self.table_mode:
            return
        if tag == "table":
            if self.table_state:
                raise _MarkupError("nested or multiple tables")
            self.table_state = 1
        elif tag in {"thead", "tbody", "tfoot"}:
            if self.table_state != 1 or self.row_group or self.in_row or self.in_caption:
                raise _MarkupError("row group")
            self.row_group = tag
            self.group_number += 1
        elif tag == "tr":
            if self.table_state != 1 or self.in_row or self.in_caption or len(self.rows) >= _ROW_LIMIT:
                raise _MarkupError("row")
            self.rows.append([])
            self.row_groups.append(self.group_number if self.row_group else None)
            self.in_row = True
        elif tag in {"th", "td"}:
            if not self.in_row or self.cell or len(self.rows[-1]) >= _COLUMN_LIMIT:
                raise _MarkupError("cell")
            fields = {"header": tag == "th", "row_group": self.row_group, "html_start": offset, "pieces": []}
            if fields["header"] and attributes.get("scope") in {"row", "col", "rowgroup", "colgroup"}:
                fields["header_scope"] = attributes["scope"]
            for name in ("rowspan", "colspan"):
                value = attributes.get(name, "1")
                if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,4}", value) or not int(value):
                    raise _MarkupError("span")
                fields[name] = int(value)
            self.cell = fields
            self.rows[-1].append(fields)
        elif tag == "caption":
            if self.table_state != 1 or self.in_row or self.caption or self.row_group:
                raise _MarkupError("caption")
            self.caption = {"html_start": offset, "pieces": []}
            self.in_caption = True

    def handle_startendtag(self, tag, attrs) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in {"br", "img", "col", "wbr"}:
            self.handle_endtag(tag)

    def handle_endtag(self, tag) -> None:
        if not self.table_mode:
            return
        if tag in {"th", "td"}:
            if not self.cell or self.cell["header"] != (tag == "th"):
                raise _MarkupError("cell end")
            self.cell["html_end"] = self._tag_end()
            self.cell = None
        elif tag == "tr":
            if not self.in_row or self.cell:
                raise _MarkupError("row end")
            self.in_row = False
        elif tag in {"thead", "tbody", "tfoot"}:
            if tag != self.row_group or self.in_row:
                raise _MarkupError("row group end")
            self.row_group = None
        elif tag == "caption":
            if not self.in_caption:
                raise _MarkupError("caption end")
            self.caption["html_end"] = self._tag_end()
            self.in_caption = False
        elif tag == "table":
            if self.table_state != 1 or self.in_row or self.row_group or self.in_caption:
                raise _MarkupError("table end")
            self.table_state = 2

    def handle_data(self, data) -> None:
        start = self._offset()
        if self.source[start:start + len(data)] != data:
            raise _MarkupError("text position")
        self._append(data, start, len(data))

    def handle_entityref(self, name) -> None:
        self._entity(len(name) + 1)

    def handle_charref(self, name) -> None:
        self._entity(len(name) + 2)

    def _entity(self, width: int) -> None:
        start = self._offset()
        if self.source[start + width:start + width + 1] == ";":
            width += 1
        self._append(unescape(self.source[start:start + width]), start, width, encoding="html_entity")

    def _append(self, value, start, width, *, encoding=None, marker=None) -> None:
        owner = self.cell or (self.caption if self.in_caption else None)
        if not owner and self.table_mode:
            if value.strip():
                raise _MarkupError("unscoped text")
            return
        pieces = self.pieces if owner is None else owner["pieces"]
        pieces.append({"value": value, "source_start": start, "source_end": start + width,
                       **({"encoding": encoding} if encoding else {}), **({"marker": marker} if marker else {})})


def _markup_text(pieces: list, raw_ref: dict, out: _Text, role: str, cell_ref=None) -> tuple[str, list]:
    parts = []
    for piece in pieces:
        if marker := piece.get("marker"):
            out.add(piece["value"], marker=marker)
            continue
        part = {key: item for key, item in piece.items() if key != "value"}
        part.update(kind=role, raw_ref=raw_ref)
        if cell_ref is not None:
            part["cell_ref"] = cell_ref
        if piece["value"]:
            parts.append(part)
            out.add(piece["value"], part=part)
    return "".join(piece["value"] for piece in pieces), parts


def _table(markup: _Markup, ref: dict, region: dict, out: _Text, report) -> dict:
    count = len(markup.rows)
    occupied: set[tuple[int, int]] = set()
    grid: list[list[tuple[int, dict]]] = []
    for row, source_row in enumerate(markup.rows):
        target_row = []
        column = 0
        for cell in source_row:
            while (row, column) in occupied:
                column += 1
            bottom, right = row + cell["rowspan"], column + cell["colspan"]
            if bottom > count or right > _COLUMN_LIMIT or len(occupied) + cell["rowspan"] * cell["colspan"] > _SLOT_LIMIT:
                raise _MarkupError("grid bounds")
            if any(markup.row_groups[y] != markup.row_groups[row] for y in range(row, bottom)):
                raise _MarkupError("rowspan crosses row groups")
            slots = {(y, x) for y in range(row, bottom) for x in range(column, right)}
            if occupied & slots:
                raise _MarkupError("overlapping cells")
            occupied.update(slots)
            target_row.append((column, cell))
            column = right
        grid.append(target_row)
    column_count = 1 + max((column for _, column in occupied), default=-1)
    if column_count == 0:
        raise _MarkupError("empty grid")
    if len(occupied) != count * column_count:
        report("IR_TABLE_GRID_INCOMPLETE", ref)
    result: dict = {"rows": [], "row_count": count, "column_count": column_count, "grid_origin": 0,
                    "html_raw_ref": ref}
    if markup.caption:
        value, parts = _markup_text(markup.caption["pieces"], ref, out, "table_caption")
        result["caption"] = {"text": value, "parts": parts, "raw_ref": ref,
                             "html_start": markup.caption["html_start"], "html_end": markup.caption["html_end"]}
        if out.length:
            out.add("\n", marker="table_caption_separator")
    cell_region = dict(region)
    if cell_region["geometry_status"] == "exact":
        cell_region["geometry_status"] = "table_region_only"
    cell_refs = {}
    for row, source_row in enumerate(grid):
        if row:
            out.add("\n", marker="table_row_separator")
        rendered_row = []
        for number, (column, cell) in enumerate(source_row):
            if number:
                out.add(" | ", marker="table_cell_separator")
            cell_ref = {"row": row, "column": column}
            cell_refs[cell["html_start"]] = cell_ref
            value, parts = _markup_text(cell["pieces"], ref, out, "table_cell", cell_ref)
            cell_locator = {"regions": [cell_region], "raw_ref": ref, "cell_ref": cell_ref}
            rendered_row.append({**cell_ref, **{key: item for key, item in cell.items() if key != "pieces"},
                                 "text": value, "value_state": "extracted" if value.strip() else "unreadable",
                                 "header_refs": [], "parts": parts, "locator": cell_locator})
        result["rows"].append(rendered_row)
    result["_markup_assets"] = _markup_assets(markup, ref, cell_refs)
    return result


def _markup_assets(markup: _Markup, ref: dict, cell_refs=None) -> list:
    return [(member, {**ref, "html_start": start, "html_end": end}, cell_refs.get(cell_start) if cell_refs else None)
            for member, start, end, cell_start in markup.assets]


def _finite_vector(value, size):
    if isinstance(value, (list, tuple)) and len(value) == size and all(
        type(number) in {int, float} and isfinite(number) for number in value
    ):
        return value
    return None


def _has_page_frame(page: dict) -> bool:
    media = _finite_vector(page.get("media_box_pt"), 4)
    crop = _finite_vector(page.get("crop_box_pt"), 4)
    dimensions = _finite_vector([page.get("width_pt"), page.get("height_pt"), page.get("user_unit")], 3)
    rotation = page.get("rotation")
    return bool(media and media[0] < media[2] and media[1] < media[3]
                and crop and crop[0] < crop[2] and crop[1] < crop[3] and dimensions and min(dimensions) > 0
                and type(rotation) is int and rotation in {0, 90, 180, 270})


def _layout_pages(extensions: dict, count: int) -> dict:
    layout = extensions.get("docvortex_layout")
    if not isinstance(layout, dict) or type(layout.get("version")) is not int or layout["version"] != 1:
        return {}
    pages = layout.get("pages")
    if not isinstance(pages, list):
        return {}
    by_page = {}
    for index, node in enumerate(pages):
        page_idx = node.get("page_idx") if isinstance(node, dict) else None
        if type(page_idx) is not int or not 0 <= page_idx < count or page_idx + 1 in by_page:
            return {}
        by_page[page_idx + 1] = (index, node)
    return by_page


def _layout_matches(page: dict, provider_layout: dict | None) -> bool:
    if provider_layout is None:
        return False
    provider_size = _finite_vector([provider_layout.get("width_pt"), provider_layout.get("height_pt")], 2)
    if not provider_size or min(provider_size) <= 0:
        return False
    # The pinned DocVortex extension records PDFium's visible box dimensions in
    # raw PDF units. PyMuPDF page.rect reports points scaled by /UserUnit.
    visible_size = [page[axis] / page["user_unit"] for axis in ("width_pt", "height_pt")]
    return all(isclose(provider, visible, rel_tol=1e-6, abs_tol=1e-4)
               for provider, visible in zip(provider_size, visible_size))


def _region(node: dict, page: dict, provider_page: dict) -> dict:
    frames = page.get("transforms")
    if not isinstance(frames, dict):
        frames = {}
    space = frames.get("provider_space")
    declared_space = node.get("bbox_space", provider_page.get("bbox_space", space))
    bbox = node.get("bbox")
    layout = page.get("provider_layout")
    size = [layout.get("width_pt"), layout.get("height_pt")] if layout else provider_page.get("page_size")
    raw_geometry = {"bbox": deepcopy(bbox), "coordinate_space": declared_space,
                    "page_size": deepcopy(size)}
    if layout:
        raw_geometry["layout_raw_ref"] = layout["raw_ref"]
    result = {"file_page": page["file_page"], "printed_page": page.get("printed_page"),
              "geometry_status": "page_only", "raw_geometry": raw_geometry}
    bounds = _finite_vector(bbox, 4)
    if (not bounds or bounds[0] >= bounds[2] or bounds[1] >= bounds[3] or not _has_page_frame(page)
            or not space or space != declared_space or frames.get("display_space") != "rotated_crop_box"):
        return result
    if space == "display_normalized" and "provider_to_display_normalized" not in frames:
        if normalized_bbox(bounds) and _layout_matches(page, layout):
            result.update(geometry_status="exact", bbox_normalized=list(bounds),
                          transform_ref="/transforms/provider_space")
        return result
    matrix = _finite_vector(frames.get("provider_to_display_normalized"), 6)
    if not matrix:
        return result
    a, b, c, d, e, f = matrix  # x' = a*x + c*y + e, y' = b*x + d*y + f
    determinant = a * d - b * c
    if not isfinite(determinant) or determinant == 0:
        return result
    x0, y0, x1, y1 = bounds
    corners = [(a * x + c * y + e, b * x + d * y + f) for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
    if any(not isfinite(coord) or coord < -1e-9 or coord > 1 + 1e-9 for point in corners for coord in point):
        return result
    polygon = [[min(1.0, max(0.0, coord)) for coord in point] for point in corners]
    xs, ys = zip(*polygon)
    mapped = [min(xs), min(ys), max(xs), max(ys)]
    if normalized_bbox(mapped):
        result.update(geometry_status="exact", bbox_normalized=mapped,
                      transform_ref="/transforms/provider_to_display_normalized")
        if any(x != u and y != v for (x, y), (u, v) in zip(polygon, [*polygon[1:], polygon[0]])):
            result["polygon"] = polygon
    return result


class _Normalizer:
    def __init__(self, run_id: str, version_id: str, source_file_id: str, source_sha256: str,
                 raw_artifact_id: str, asset_refs: dict) -> None:
        self.run_id = run_id
        self.raw_artifact_id = raw_artifact_id
        self.locator_identity = {"schema_version": "dc.source-locator.v1", "parse_run_id": run_id,
                                 "document_version_id": version_id, "source_file_id": source_file_id,
                                 "source_sha256": source_sha256}
        self.source_assets = asset_refs
        self.assets: dict[str, dict] = {}
        self.blocks: list[dict] = []
        self.issues: list[dict] = []

    def ref(self, pointer: str) -> dict:
        return _raw_ref(self.raw_artifact_id, pointer)

    def issue(self, code: str, raw_ref: dict | None, page: dict, block_id: str | None = None) -> None:
        issue = {"id": f"issue-{len(self.issues) + 1}", "code": code, "file_page": page["file_page"]}
        if raw_ref is not None:
            issue["raw_ref"] = raw_ref
        if block_id is not None:
            issue["block_id"] = block_id
        self.issues.append(issue)
        page["issue_refs"].append(issue["id"])

    def asset(self, member, ref: dict, report) -> str | None:
        try:
            if not isinstance(member, str) or _member_name(member) != member:
                raise ParseContractError("PARSE_RESOURCE_MISSING")
        except ParseContractError:
            report("PARSE_RESOURCE_MISSING", ref)
            return None
        asset = self.source_assets.get(member)
        if isinstance(asset, str):
            asset = {"artifact_id": asset}
        if (not isinstance(asset, dict) or not isinstance(asset.get("artifact_id"), str) or not asset["artifact_id"]
                or asset.get("raw_member_name", member) != member or str(asset.get("parse_run_id", self.run_id)) != self.run_id
                or asset["artifact_id"] == self.raw_artifact_id):
            report("PARSE_RESOURCE_MISSING", ref)
            return None
        asset_id = asset["artifact_id"]
        if asset_id in self.assets and self.assets[asset_id]["raw_member_name"] != member:
            report("PARSE_RESOURCE_MISSING", ref)
            return None
        metadata = {key: deepcopy(asset[key]) for key in ("file_id", "mime_type", "sha256", "source_locator",
                    "derived_from_artifact_id") if key in asset}
        self.assets[asset_id] = {**metadata, "artifact_id": asset_id, "kind": asset.get("kind", "image"),
                                 "raw_member_name": member}
        return asset_id

    def block(self, node: dict, pointer: str, page: dict, provider_page: dict, parent=None, depth=0) -> dict:
        if depth > 64 or not isinstance(node, dict) or not isinstance(node.get("type"), str):
            raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
        raw_ref = self.ref(pointer)
        block_id = str(uuid5(_BLOCK_NAMESPACE, f"{self.run_id}/{pointer}"))

        def report(code, ref):
            self.issue(code, ref, page, block_id)

        provider_type = node["type"]
        content = node.get("content")
        container = provider_type in _CONTAINERS
        kind = _CONTAINERS[provider_type] if container else _LEAVES.get(provider_type, "unknown")
        region = _region(node, page, provider_page)
        locator = {**self.locator_identity, "block_id": block_id, "raw_ref": raw_ref,
                   "regions": [] if container else [region]}
        level = node.get("level")
        block = {"id": block_id, "parse_run_id": self.run_id, "ordinal": len(self.blocks),
                 "kind": kind, "provider_type": provider_type, "text_content": "", "file_page": page["file_page"],
                 "source_kind": "parser", "raw_ref": raw_ref, "locator": locator,
                 "parent_block_id": parent, "reading_order": None,
                 "heading_level": level if kind == "title" and type(level) is int and 1 <= level <= 6 else None,
                 "structure_role": "container" if container else "observation" if kind == "unknown" else "leaf"}
        self.blocks.append(block)
        excluded = {"type", *_ASSET_FIELDS}
        if not container:
            excluded.add("bbox")
        structured: dict = {"provider_type": provider_type}
        out = _Text()
        if container:
            children = []
            if isinstance(content, list):
                excluded.add("content")
                for index, child in enumerate(content):
                    children.append(self.block(child, f"{pointer}/content/{index}", page, provider_page, block_id, depth + 1))
            else:
                report("IR_CONTAINER_CONTENT_UNSUPPORTED", raw_ref)
            structured["child_refs"] = [child["id"] for child in children]
            for annot in ("caption", "footnote"):
                refs = [child["id"] for child in children if child["kind"] == annot]
                if refs:
                    structured[f"{annot}_refs"] = refs
            if provider_type in {"table", "image", "chart", "code"}:
                body_types = {f"{provider_type}_body"} | ({"algorithm_body"} if provider_type == "code" else set())
                if sum(child["provider_type"] in body_types for child in children) != 1:
                    report("IR_CONTAINER_BODY_UNSUPPORTED", raw_ref)
        else:
            if region["geometry_status"] != "exact":
                report("SOURCE_LOCATOR_MISSING", raw_ref)
            if kind == "unknown":
                report("IR_PROVIDER_TYPE_UNKNOWN", raw_ref)
            else:
                excluded.add("content")
                excluded.update(self.leaf(node, block, structured, region, out, report))
        asset_parts = self.block_assets(node, structured.pop("_markup_assets", []), raw_ref, report)
        if asset_parts:
            structured["asset_parts"] = asset_parts
            structured["asset_refs"] = list(dict.fromkeys(part["artifact_id"] for part in asset_parts))
        if kind in {"image", "chart"} and not asset_parts:
            report("IR_VISUAL_RESOURCE_UNAVAILABLE", raw_ref)
        block["text_content"] = out.finish(structured)
        block["structured_content"] = structured
        block["provider_fields"] = _fields(node, excluded, raw_ref)
        block["content_hash"] = fingerprint({key: block[key] for key in (
            "kind", "text_content", "structured_content", "locator", "raw_ref", "parent_block_id",
            "structure_role", "heading_level", "provider_fields")})
        return block

    def leaf(self, node, block, structured, region, out, report) -> set[str]:
        kind = block["kind"]
        pointer = block["raw_ref"]["json_pointer"]
        content = node.get("content")
        ref = self.ref(f"{pointer}/content")
        if kind == "table":
            html_field = "content" if isinstance(content, str) else "html"
            html = node.get(html_field)
            html_ref = self.ref(f"{pointer}/{html_field}")
            if isinstance(html, str) and html:
                structured["html_raw_ref"] = html_ref
                try:
                    markup = _Markup(html, table=True)
                    structured.update(_table(markup, html_ref, region, out, report))
                except (ValueError, OverflowError):
                    report("IR_TABLE_HTML_UNSUPPORTED", html_ref)
            else:
                report("IR_TABLE_HTML_MISSING", block["raw_ref"])
            return {"html"}
        if kind == "formula":
            return self.formula(node, pointer, structured, out, report)
        parts: list = []
        if kind in {"image", "chart"} and isinstance(content, str) and re.search(r"</?[A-Za-z][^>]*>", content):
            try:
                markup = _Markup(content, table=False)
                structured["_markup_assets"] = _markup_assets(markup, ref)
                _, parts = _markup_text(markup.pieces, ref, out, "visual_text")
            except (ValueError, OverflowError):
                report("IR_VISUAL_CONTENT_UNSUPPORTED", ref)
        elif kind not in {"image", "chart"} or not isinstance(content, str) or content.strip():
            _inline(content, ref, out, parts, report)
        structured["parts"] = parts
        return set()

    def formula(self, node, pointer, structured, out, report) -> set[str]:
        parts = []
        structured.update(definition_refs=[], condition_refs=[], asset_refs=[])
        fields = {"latex_raw": ("latex_raw", "latex", "content"),
                  "mathml_raw": ("mathml_raw", "mathml", "mml"),
                  "equation_label": ("equation_label", "formula_number")}
        for name, aliases in fields.items():
            for field in aliases:
                value = node.get(field)
                if isinstance(value, str) and value.strip():
                    part = _json_piece(value, self.ref(f"{pointer}/{field}"), name)
                    parts.append(part)
                    if name not in structured:
                        structured[name] = value
                        if name != "mathml_raw":
                            if out.length:
                                out.add("\n", marker="formula_label_separator")
                            out.add(value, part=part)
        structured["parts"] = parts
        if "latex_raw" not in structured and "mathml_raw" not in structured:
            report("IR_FORMULA_EXPRESSION_MISSING", self.ref(pointer))
        return {alias for aliases in fields.values() for alias in aliases}

    def block_assets(self, node, embedded: list, ref: dict, report) -> list:
        found = []
        paths = []
        for key in _ASSET_FIELDS:
            if not node.get(key):
                continue
            source_ref = {**ref, "json_pointer": _pointer(ref["json_pointer"], key)}
            if key in {"image_url", "image_base64"}:
                report("PARSE_RESOURCE_MISSING", source_ref)
            else:
                paths.append((node[key], source_ref, None))
        for member, source_ref, cell_ref in [*paths, *embedded]:
            if asset_id := self.asset(member, source_ref, report):
                part = {"artifact_id": asset_id, "raw_ref": source_ref}
                if cell_ref is not None:
                    part["cell_ref"] = cell_ref
                found.append(part)
        return found


def normalize_ir(*, run_id, version_id, source_file_id, source_sha256, profile, preflight, middle,
                 raw_artifact_id, asset_refs=None, preflight_artifact_id=None) -> dict:
    """Build the frozen normalized candidate from a saved bundle and an original-page manifest.

    Asset keys are exact archive member names. Explicit affine preflight transforms
    map provider coordinates to the normalized rotated CropBox display frame.
    """
    if not isinstance(middle, dict) or (middle.get("schema"), middle.get("schema_version")) != ("docvortex.middle", "2.0"):
        raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
    metadata, extensions = middle.get("metadata"), middle.get("extensions")
    producer = metadata.get("producer") if isinstance(metadata, dict) else None
    actual = extensions.get("mineru") if isinstance(extensions, dict) else None
    if (not isinstance(producer, dict) or str(producer.get("name", "")).casefold() != "mineru"
            or producer.get("version") != PROFILE["engine_version"] or not isinstance(actual, dict)
            or actual.get("tier") != PROFILE["tier"] or actual.get("parse_mode") not in {"txt", "ocr"}
            or metadata.get("file_suffix", "pdf") != "pdf" or type(middle.get("is_full_document")) is not bool):
        raise ParseContractError("PARSE_PROFILE_MISMATCH")
    if (not isinstance(profile, dict) or profile.get("engine_version", PROFILE["engine_version"]) != PROFILE["engine_version"]
            or profile.get("tier", PROFILE["tier"]) != PROFILE["tier"]):
        raise ParseContractError("PARSE_PROFILE_MISMATCH")
    if not isinstance(preflight, dict) or (asset_refs is not None and not isinstance(asset_refs, dict)):
        raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
    if preflight.get("source_sha256", source_sha256) != source_sha256:
        raise ParseContractError("PROCESSING_SOURCE_CHANGED")
    count, source_pages, provider_pages = preflight.get("page_count"), preflight.get("pages"), middle.get("pages")
    if (type(count) is not int or not 0 < count <= 600 or not isinstance(source_pages, list) or len(source_pages) != count
            or not isinstance(provider_pages, list)):
        raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
    provider_by_page = {}
    for index, node in enumerate(provider_pages):
        origin = node.get("page_idx") if isinstance(node, dict) else None
        if type(origin) is not int or not 0 <= origin < count or origin + 1 in provider_by_page:
            raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
        provider_by_page[origin + 1] = (f"/pages/{index}", node, middle_page_blocks(node, source_pages[origin]))
    requested = preflight.get("requested_file_pages")
    if requested is None:
        requested = list(range(1, count + 1)) if middle["is_full_document"] else list(provider_by_page)
    if (not isinstance(requested, list) or any(type(value) is not int or not 1 <= value <= count for value in requested)
            or len(set(requested)) != len(requested) or not provider_by_page.keys() <= set(requested)):
        raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
    normalizer = _Normalizer(str(run_id), str(version_id), str(source_file_id), source_sha256, str(raw_artifact_id), asset_refs or {})
    layout_by_page = _layout_pages(extensions, count)
    pages = []
    requested = set(requested)
    for number, original in enumerate(source_pages, 1):
        if not isinstance(original, dict) or type(original.get("file_page")) is not int or original["file_page"] != number:
            raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
        page = deepcopy(original)
        page["issue_refs"] = list(page.get("issue_refs") or [])
        page["parse_run_id"] = normalizer.run_id
        if number in provider_by_page:
            pointer, node, blocks = provider_by_page[number]
            page["raw_ref"] = normalizer.ref(pointer)
            page["provider_fields"] = _fields(node, {"page_idx", "blocks"}, page["raw_ref"])
            page["provider_page_idx"] = node["page_idx"]
            if number in layout_by_page:
                layout_index, layout = layout_by_page[number]
                layout_ref = normalizer.ref(f"/extensions/docvortex_layout/pages/{layout_index}")
                page["provider_layout"] = {**_fields(layout, {"page_idx"}, layout_ref), "raw_ref": layout_ref}
            if original.get("state") == "not_requested":
                raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
            for index, block in enumerate(blocks):
                normalizer.block(block, f"{pointer}/blocks/{index}", page, node)
            if not _has_page_frame(page):
                normalizer.issue("IR_PAGE_FRAME_UNKNOWN", page["raw_ref"], page)
            if original.get("state") == "failed":
                page["state"] = "failed"
            elif not blocks and original.get("state") == "blank":
                page["state"] = "blank"
            else:
                if not blocks or original.get("state") == "blank":
                    normalizer.issue("IR_PAGE_CONTENT_MISMATCH", page["raw_ref"], page)
                page["state"] = "needs_review" if page["issue_refs"] or original.get("state") == "needs_review" else "processed"
        elif original.get("state") == "failed":
            page["state"] = "failed"
            normalizer.issue("IR_PAGE_OUTPUT_MISSING", None, page)
        elif number not in requested or original.get("state") == "not_requested":
            page["state"] = "not_requested"
        else:
            page["state"] = "failed"
            normalizer.issue("IR_PAGE_OUTPUT_MISSING", None, page)
        pages.append(page)
    # An independent PDF inventory is retained as observations. It cannot
    # invent extracted text or claim the internals of a scan are explained.
    for page_index, page in enumerate(pages):
        for inventory_kind in ("image", "vector"):
            candidates = page.get("coverage", {}).get(inventory_kind + "_candidates", [])
            for index, candidate in enumerate(candidates):
                if not preflight_artifact_id:
                    normalizer.issue("IR_INVENTORY_SOURCE_MISSING", None, page)
                    continue
                ref = {"artifact_id": preflight_artifact_id,
                       "json_pointer": f"/pages/{page_index}/coverage/{inventory_kind}_candidates/{index}"}
                block_id = str(uuid5(_BLOCK_NAMESPACE, f"{run_id}/inventory/{inventory_kind}/{page_index}/{index}"))
                region = {"file_page": page["file_page"], "printed_page": page.get("printed_page"),
                    "bbox_normalized": deepcopy(candidate.get("bbox")), "geometry_status": candidate.get("geometry_status", "page_only"),
                    "coordinate_space": "rotated_crop_box_display_normalized"}
                block = {"id": block_id, "parse_run_id": str(run_id), "ordinal": len(normalizer.blocks),
                    "kind": "image" if inventory_kind == "image" else "unknown", "file_page": page["file_page"],
                    "source_kind": "native_inventory", "structure_role": "observation", "text_content": "",
                    "raw_ref": ref, "parent_block_id": None, "reading_order": None, "heading_level": None,
                    "locator": {**normalizer.locator_identity, "block_id": block_id, "raw_ref": ref, "regions": [region]},
                    "structured_content": {"inventory_kind": inventory_kind, "inventory_candidate": deepcopy(candidate),
                        "renderer": RENDERER, "text_map": [], "asset_refs": []}}
                block["content_hash"] = fingerprint({key: block[key] for key in (
                    "kind", "text_content", "structured_content", "locator", "source_kind", "raw_ref")})
                normalizer.blocks.append(block)
    root_ref = normalizer.ref("")
    return {"schema": "dc.document-ir", "schema_version": "1.0", "phase": "normalized",
            **{key: value for key, value in normalizer.locator_identity.items() if key != "schema_version"},
            "profile": deepcopy(profile), "actual_profile": {**deepcopy(actual), "producer": deepcopy(producer)},
            "raw_contract": {"schema": middle["schema"], "schema_version": middle["schema_version"],
                             "api_family": "mineru-kit-save"},
            "adapter_version": PROFILE["adapter_version"], "page_count": count, "raw_ref": root_ref,
            "provider_fields": _fields(middle, {"schema", "schema_version", "pages"}, root_ref),
            "pages": pages, "blocks": normalizer.blocks, "relations": [], "issues": normalizer.issues,
            "artifact_refs": [{"artifact_id": normalizer.raw_artifact_id, "kind": "raw_archive"},
                *([{"artifact_id": preflight_artifact_id, "kind": "preflight"}] if preflight_artifact_id else []), *normalizer.assets.values()]}
