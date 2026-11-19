"""Manual graphic-key edits bind to the original table and keep its grid."""

from copy import deepcopy

from ..governance import block_binding
from ..models import ContentBlock
from ..schemas import GraphicTableInput
from ..services import SafeError
from .local_pdf import normalized_bbox
from .table_recovery import render_table_text


def table_graphic_edits(source: ContentBlock, edit: GraphicTableInput) -> tuple[str, dict]:
    if block_binding(source) != edit.source_content_hash:
        raise SafeError("CORRECTION_SOURCE_CHANGED", "原解析块已变化，请重新读取后校正", 409)
    structure = deepcopy(dict(source.structured_content or {}))
    rows = structure.get("rows")
    bounds = normalized_bbox(source.locator.get("bbox_normalized"))
    if (source.kind != "table" or source.locator.get("extraction") != "mineru_basic_ocr"
            or not rows or not bounds):
        raise SafeError("INVALID_GRAPHIC_CORRECTION", "图形键校正需要带原页定位的扫描表格", 422)

    pending = {(item["row"], item["column"]) for item in structure.get("pending_graphic_keys", [])}
    previous = {(ri, ci) for ri, row in enumerate(rows) for ci, cell in enumerate(row) if cell.get("graphic_keys")}
    allowed = pending | previous
    seen = set()
    reworded = set()
    for item in edit.cells:
        position = item.row, item.column
        if position not in allowed or position in seen:
            raise SafeError("INVALID_GRAPHIC_CORRECTION", "请指定单个待核验或已校正的图形键单元格", 422)
        keys = []
        for key in item.keys:
            box = normalized_bbox(key.bbox_normalized)
            if not box or not (bounds[0] <= box[0] < box[2] <= bounds[2] and bounds[1] <= box[1] < box[3] <= bounds[3]):
                raise SafeError("INVALID_GRAPHIC_CORRECTION", "按键裁区须位于同一原表内", 422)
            keys.append(key.model_dump(mode="json"))
        cell = rows[item.row][item.column]
        if cell["text"] not in item.text:
            reworded.add(position)
        cell.update(text=item.text, graphic_keys=keys)
        seen.add(position)

    remaining = pending - seen
    if remaining:
        structure["pending_graphic_keys"] = [{"row": r, "column": c} for r, c in sorted(remaining)]
    else:
        structure.pop("pending_graphic_keys", None)
    for key in ("recovered_icons", "recovered_warnings"):
        if key in structure:
            structure[key] = [ref for ref in structure[key] if (ref["row"], ref["column"]) not in reworded]
    structure["graphic_key_correction"] = {"parent_run_id": source.parse_run_id, "source_block_id": source.id,
                                           "source_content_hash": edit.source_content_hash}
    return render_table_text(structure), structure
