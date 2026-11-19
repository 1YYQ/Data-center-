"""Apply a source-bound manual overlay to a newly normalized candidate."""
from __future__ import annotations

from copy import deepcopy
from html import unescape
import re

from .ir import RENDERER
from .local_pdf import normalized_bbox
from .local_pipeline import ParseContractError, fingerprint


_EDIT_FIELDS = {"block_id", "ordinal", "text", "kind", "locator", "reason", "basis"}
_EDITABLE = {"paragraph", "title", "step", "warning", "header", "footer", "footnote", "caption",
             "ref_text", "aside_text", "page_number"}
_PROMOTIONS = {"paragraph": {"step", "warning"}, "ref_text": {"step", "warning"}, "aside_text": {"warning"}}
_IDENTITY = ("document_version_id", "source_file_id", "source_sha256", "profile", "actual_profile", "raw_contract", "page_count")
_PAGE_FRAME = ("file_page", "media_box_pt", "crop_box_pt", "rotation", "user_unit", "width_pt", "height_pt", "transforms")
_REVIEW_FIELDS = {"approved", "review", "review_status", "review_reason", "reviewed_at", "reviewed_by", "reviewer_id"}
_DISPLAY_SPACE = "rotated_crop_box_display_normalized"
_UNSUPPORTED_TEXT = re.compile(
    r"<\s*(?:[!?]|/?\s*[a-z][\w:-]*\b[^<>]*>)|\b[a-z][\w+.-]*://|(?<!\w)//\S+|"
    r"\b(?:www\.|mailto:|javascript:|vbscript:|data:|file:)|\[[^\r\n\]]*\]\(|```|~~~",
    re.IGNORECASE,
)


def _plain_text(value: object, limit: int) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > limit
            or any((ord(char) < 32 and char not in "\t\n\r") or 0x7F <= ord(char) <= 0x9F
                   or 0xD800 <= ord(char) <= 0xDFFF for char in value)):
        raise ParseContractError("IR_CORRECTION_INVALID")
    if _UNSUPPORTED_TEXT.search(unescape(unescape(value))):
        raise ParseContractError("IR_CORRECTION_MARKUP_UNSUPPORTED")
    return value


def _page_frames(ir: dict) -> dict:
    count = ir.get("page_count")
    if (type(count) is not int or count < 1 or not isinstance(ir.get("pages"), list) or len(ir["pages"]) != count):
        raise ParseContractError("IR_CORRECTION_SOURCE_MISMATCH")
    frames = {}
    for index, page in enumerate(ir["pages"], 1):
        number = page.get("file_page") if isinstance(page, dict) else None
        if type(number) is not int or number != index or page.get("parse_run_id") != ir["parse_run_id"]:
            raise ParseContractError("IR_CORRECTION_SOURCE_MISMATCH")
        frames[number] = tuple(page.get(key) for key in _PAGE_FRAME)
    return frames


def _bound_to_original(block: dict, ir: dict) -> tuple[int, str, str] | None:
    ref, root, locator = block.get("raw_ref"), ir.get("raw_ref"), block.get("locator")
    source_page = block.get("file_page")
    if (not isinstance(ref, dict) or not isinstance(root, dict) or not isinstance(locator, dict)
            or type(source_page) is not int or not source_page > 0 or locator.get("parse_run_id") != ir["parse_run_id"]
            or locator.get("block_id") != block.get("id") or locator.get("raw_ref") != ref
            or not isinstance(root.get("artifact_id"), str) or ref.get("artifact_id") != root["artifact_id"]
            or not isinstance(root.get("raw_member_name"), str) or ref.get("raw_member_name") != root["raw_member_name"]):
        return None
    pointer = ref.get("json_pointer")
    if not isinstance(pointer, str) or not re.fullmatch(r"/pages/\d+/blocks/\d+(?:/content/\d+)*", pointer):
        return None
    return source_page, root["raw_member_name"], pointer


def _blocks(ir: dict) -> tuple[dict, dict]:
    if not isinstance(ir.get("blocks"), list):
        raise ParseContractError("IR_CORRECTION_SOURCE_MISMATCH")
    by_id, by_ordinal = {}, {}
    for block in ir["blocks"]:
        if (not isinstance(block, dict) or not isinstance(block.get("id"), str) or not block["id"] or block["id"] in by_id
                or block.get("parse_run_id") != ir["parse_run_id"] or type(block.get("ordinal")) is not int
                or block["ordinal"] < 0 or block["ordinal"] in by_ordinal):
            raise ParseContractError("IR_CORRECTION_SOURCE_MISMATCH")
        by_id[block["id"]] = block
        by_ordinal[block["ordinal"]] = block
    return by_id, by_ordinal


def _target(edit: dict, by_id: dict, by_ordinal: dict) -> dict:
    block_id, ordinal = edit.get("block_id"), edit.get("ordinal")
    if isinstance(ordinal, str) and re.fullmatch(r"(?:0|[1-9][0-9]{0,9})", ordinal):
        ordinal = int(ordinal)
    if ("block_id" in edit and (not isinstance(block_id, str) or block_id not in by_id)
            or "ordinal" in edit and (type(ordinal) is not int or ordinal not in by_ordinal)):
        raise ParseContractError("IR_CORRECTION_TARGET_INVALID")
    if block_id is not None and ordinal is not None and by_id[block_id]["ordinal"] != ordinal:
        raise ParseContractError("IR_CORRECTION_TARGET_INVALID")
    if block_id is not None:
        return by_id[block_id]
    if ordinal is not None:
        return by_ordinal[ordinal]
    raise ParseContractError("IR_CORRECTION_TARGET_INVALID")


def _same_page_claim(edit: dict, page: int) -> None:
    _plain_text(edit.get("reason"), 500)
    basis = edit.get("basis")
    if not isinstance(basis, dict) or set(basis) != {"file_page", "description"}:
        raise ParseContractError("IR_CORRECTION_BASIS_REQUIRED")
    if type(basis["file_page"]) is not int or basis["file_page"] != page:
        raise ParseContractError("IR_CORRECTION_PAGE_MISMATCH")
    _plain_text(basis["description"], 1000)


def _claim_region(edit: dict, block: dict, request_ref: dict) -> dict | None:
    locator, page = edit["locator"], block["file_page"]
    if not isinstance(locator, dict) or set(locator) != {"file_page", "bbox_normalized"}:
        raise ParseContractError("IR_CORRECTION_REGION_INVALID")
    if type(locator["file_page"]) is not int or locator["file_page"] != page:
        raise ParseContractError("IR_CORRECTION_PAGE_MISMATCH")
    box = locator["bbox_normalized"]
    if (not isinstance(box, list) or any(type(value) not in {float, int} or not 0 <= value <= 1 for value in box)
            or normalized_bbox(box) is None or box == [0, 0, 1, 1]):
        raise ParseContractError("IR_CORRECTION_REGION_INVALID")
    original = block["locator"].get("regions")
    if (not isinstance(original, list) or len(original) != 1 or not isinstance(original[0], dict)
            or original[0].get("file_page") != page):
        raise ParseContractError("IR_CORRECTION_REGION_INVALID")
    if original[0].get("geometry_status") == "exact" and original[0].get("bbox_normalized") == box:
        return None
    return {"file_page": page, "printed_page": original[0].get("printed_page"), "bbox_normalized": deepcopy(box),
            "geometry_status": "exact", "coordinate_space": _DISPLAY_SPACE, "geometry_basis": "manual_claim",
            "geometry_evidence_ref": {**request_ref, "json_pointer": request_ref["json_pointer"] + "/locator/bbox_normalized"}}


def _same_source(normalized_child: dict, parent_ir: dict) -> None:
    if (not isinstance(normalized_child, dict) or not isinstance(parent_ir, dict)
            or any(ir.get("schema") != "dc.document-ir" or ir.get("schema_version") != "1.0"
                   or not isinstance(ir.get("parse_run_id"), str) or not ir["parse_run_id"]
                   for ir in (normalized_child, parent_ir))
            or normalized_child.get("phase") != "normalized" or parent_ir.get("phase") not in {"normalized", "structured"}
            or normalized_child["parse_run_id"] == parent_ir["parse_run_id"]
            or any(key not in normalized_child or normalized_child[key] != parent_ir.get(key) for key in _IDENTITY)
            or any(not isinstance(normalized_child.get(key), str) or not normalized_child[key]
                   for key in ("document_version_id", "source_file_id", "source_sha256"))
            or not re.fullmatch(r"[0-9a-f]{64}", normalized_child["source_sha256"])
            or _page_frames(normalized_child) != _page_frames(parent_ir)):
        raise ParseContractError("IR_CORRECTION_SOURCE_MISMATCH")


def _child_nodes(ir: dict) -> dict:
    by_id, _ = _blocks(ir)
    nodes = {}
    for block in by_id.values():
        for key in _REVIEW_FIELDS:
            block.pop(key, None)
        node = _bound_to_original(block, ir)
        if node is not None:
            if node in nodes:
                raise ParseContractError("IR_CORRECTION_SOURCE_MISMATCH")
            nodes[node] = block
    return nodes


def _artifact_rows(ir: dict) -> dict:
    rows = ir.get("artifact_refs")
    if not isinstance(rows, list):
        raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
    result = {}
    for row in rows:
        identifier = row.get("artifact_id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in result or not isinstance(row.get("kind"), str):
            raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
        result[identifier] = row
    return result


def _alias_id(mapping: dict, value: object) -> str:
    alias = mapping.get(value) if isinstance(value, str) else None
    if not isinstance(alias, str) or not alias or alias == value:
        raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
    return alias


def _remap_evidence(value, mapping):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if key == "artifact_id":
                result[key] = _alias_id(mapping, child)
            elif key == "asset_refs":
                if not isinstance(child, list):
                    raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
                result[key] = [_alias_id(mapping, artifact) for artifact in child]
            else:
                result[key] = _remap_evidence(child, mapping)
        return result
    if isinstance(value, list):
        return [_remap_evidence(child, mapping) for child in value]
    return value


def _with_inherited_block(block, parent, index, parent_run_id, mapping, normalized_alias):
    structured = deepcopy(parent.get("structured_content"))
    manual = structured.get("manual_correction") if isinstance(structured, dict) else None
    request = manual.get("request_ref") if isinstance(manual, dict) else None
    locator = parent.get("locator")
    regions = locator.get("regions") if isinstance(locator, dict) else None
    if (not isinstance(request, dict) or not isinstance(request.get("artifact_id"), str)
            or not isinstance(request.get("json_pointer"), str) or not isinstance(regions, list)
            or manual.get("requires_source_review") is not True or not isinstance(parent.get("text_content"), str)
            or not isinstance(parent.get("kind"), str) or parent["kind"] not in _EDITABLE):
        raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
    # The previous generation's direct-parent pointer remains in that frozen
    # IR. This generation links to its own alias of the direct parent instead.
    manual.pop("inherited_from", None)
    structured = _remap_evidence(structured, mapping)
    structured["manual_correction"].update(parent_run_id=parent_run_id, parent_block_id=parent["id"],
        inherited_from={"artifact_id": normalized_alias, "json_pointer": f"/blocks/{index}"})
    result = deepcopy(block)
    result["locator"]["regions"] = _remap_evidence(regions, mapping)
    result.update(text_content=parent["text_content"], kind=parent["kind"], source_kind="manual",
                  source_block_ref=parent["id"], structured_content=structured)
    result["content_hash"] = fingerprint({key: value for key, value in result.items() if key != "content_hash"})
    return result


def inherit_manual_corrections(normalized_child: dict, *, parent_ir: dict, artifact_map: dict,
                               parent_normalized_alias_id: str | None = None) -> dict:
    """Carry previous edits only through verified, same-run aliases of their evidence."""
    _same_source(normalized_child, parent_ir)
    _blocks(parent_ir)
    candidate = deepcopy(normalized_child)
    nodes = _child_nodes(candidate)
    manual_blocks = [(index, block) for index, block in enumerate(parent_ir["blocks"]) if block.get("source_kind") == "manual"]
    if not manual_blocks:
        return candidate
    if (parent_ir.get("phase") != "normalized" or not isinstance(artifact_map, dict)
            or not isinstance(parent_normalized_alias_id, str) or not parent_normalized_alias_id
            or not any(alias == parent_normalized_alias_id for alias in artifact_map.values())):
        raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")

    parent_refs, child_refs = _artifact_rows(parent_ir), _artifact_rows(candidate)
    if parent_normalized_alias_id in child_refs:
        raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
    new_refs = []
    used = {parent_normalized_alias_id}
    for original, row in parent_refs.items():
        if row.get("role") == "parent_normalized_ir":
            continue
        alias = _alias_id(artifact_map, original)
        if alias in used:
            raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
        used.add(alias)
        if row.get("role") == "manual_correction" and row.get("kind") == "structured":
            if alias in child_refs:
                raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
            new_refs.append({"artifact_id": alias, "kind": "structured", "role": "manual_correction",
                             "derived_from_artifact_id": original})
        elif alias not in child_refs or any(row.get(key) != child_refs[alias].get(key)
                for key in ("kind", "raw_member_name", "file_id", "sha256")):
            raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
    allowed_map = {original: alias for original, alias in artifact_map.items()
                   if original in parent_refs and parent_refs[original].get("role") != "parent_normalized_ir"}
    parent_raw, child_raw = parent_ir.get("raw_ref"), candidate.get("raw_ref")
    if (not isinstance(parent_raw, dict) or not isinstance(child_raw, dict)
            or _alias_id(allowed_map, parent_raw.get("artifact_id")) != child_raw.get("artifact_id")):
        raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
    for index, parent in manual_blocks:
        block = nodes.get(_bound_to_original(parent, parent_ir))
        if (not block or block.get("source_kind") != "parser" or block.get("structure_role") != "leaf"
                or parent.get("structure_role") != "leaf" or parent.get("provider_type") != block.get("provider_type")):
            raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
        inherited = _with_inherited_block(block, parent, index, parent_ir["parse_run_id"], allowed_map, parent_normalized_alias_id)
        block.update(inherited)
    candidate["artifact_refs"].extend([*new_refs, {"artifact_id": parent_normalized_alias_id, "kind": "document_ir",
                                                  "role": "parent_normalized_ir"}])
    return candidate


def _require_inherited_manual(candidate: dict, parent_ir: dict, nodes: dict) -> None:
    previous = [(index, block) for index, block in enumerate(parent_ir["blocks"]) if block.get("source_kind") == "manual"]
    if not previous:
        return
    parent_refs, child_refs = _artifact_rows(parent_ir), _artifact_rows(candidate)
    parents = [row["artifact_id"] for row in child_refs.values() if row.get("role") == "parent_normalized_ir"]
    if len(parents) != 1:
        raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
    mapping = {}
    for child in child_refs.values():
        if child.get("role") == "parent_normalized_ir":
            continue
        if child["kind"] in {"raw_archive", "preflight"}:
            sources = [key for key, row in parent_refs.items() if row["kind"] == child["kind"]]
            original = sources[0] if len(sources) == 1 else None
        else:
            original = child.get("derived_from_artifact_id")
        if isinstance(original, str) and original in parent_refs:
            if original in mapping:
                raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")
            mapping[original] = child["artifact_id"]
    for index, parent in previous:
        block = nodes.get(_bound_to_original(parent, parent_ir))
        if (not block or block.get("source_kind") != "manual"
                or _with_inherited_block(block, parent, index, parent_ir["parse_run_id"], mapping, parents[0]) != block):
            raise ParseContractError("IR_CORRECTION_INHERITANCE_INVALID")


def apply_manual_corrections(normalized_child: dict, *, parent_ir: dict, correction_artifact_id: str, payload: dict) -> dict:
    """Create a candidate in the child run and keep original locator and text evidence distinct.

    The caller supplies the exact JSON object already frozen as the correction
    artifact. Character offsets index decoded Unicode code points.
    """
    _same_source(normalized_child, parent_ir)
    if (not isinstance(payload, dict) or set(payload) - {"blocks", "graphic_tables"}
            or not isinstance(payload.get("blocks", []), list) or len(payload.get("blocks", [])) > 200
            or not isinstance(payload.get("graphic_tables", []), list)):
        raise ParseContractError("IR_CORRECTION_INVALID")
    if payload.get("graphic_tables"):
        raise ParseContractError("IR_CORRECTION_TABLE_UNSUPPORTED")

    candidate = deepcopy(normalized_child)
    by_parent_id, by_parent_ordinal = _blocks(parent_ir)
    child_nodes = _child_nodes(candidate)

    edits = payload.get("blocks", [])
    _require_inherited_manual(candidate, parent_ir, child_nodes)
    if not edits:
        return candidate
    refs = candidate.get("artifact_refs")
    if (not isinstance(correction_artifact_id, str) or not correction_artifact_id or not isinstance(refs, list)
            or any(not isinstance(ref, dict) or ref.get("artifact_id") == correction_artifact_id for ref in refs)):
        raise ParseContractError("IR_CORRECTION_ARTIFACT_INVALID")
    edited = set()
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) - _EDIT_FIELDS or not {"text", "kind", "locator"}.intersection(edit):
            raise ParseContractError("IR_CORRECTION_INVALID")
        parent = _target(edit, by_parent_id, by_parent_ordinal)
        if parent["id"] in edited:
            raise ParseContractError("IR_CORRECTION_DUPLICATE")
        original_kind = parent.get("kind")
        if original_kind == "table":
            raise ParseContractError("IR_CORRECTION_TABLE_UNSUPPORTED")
        if (parent.get("structure_role") != "leaf" or not isinstance(original_kind, str) or original_kind not in _EDITABLE
                or parent.get("source_kind") not in {"parser", "manual"}):
            raise ParseContractError("IR_CORRECTION_TARGET_UNSUPPORTED")
        node = _bound_to_original(parent, parent_ir)
        block = child_nodes.get(node)
        if (block is None or block.get("source_kind") != parent["source_kind"] or block.get("structure_role") != "leaf"
                or block.get("kind") != parent["kind"] or block.get("provider_type") != parent.get("provider_type")
                or block.get("text_content") != parent.get("text_content") or not isinstance(block.get("structured_content"), dict)):
            raise ParseContractError("IR_CORRECTION_SOURCE_MISMATCH")
        _same_page_claim(edit, node[0])
        kind = edit.get("kind", block["kind"])
        if not isinstance(kind, str) or (kind != block["kind"] and kind not in _PROMOTIONS.get(block["kind"], ())):
            raise ParseContractError("IR_CORRECTION_KIND_UNSUPPORTED")
        text = _plain_text(edit["text"], 12_000) if "text" in edit else block["text_content"]
        request_ref = {"artifact_id": correction_artifact_id, "json_pointer": f"/blocks/{index}"}
        structured = block["structured_content"]
        manual = {**structured.get("manual_correction", {}), "request_ref": request_ref, "parent_run_id": parent_ir["parse_run_id"],
                  "parent_block_id": parent["id"], "requires_source_review": True}
        manual.setdefault("original_text_for_inventory", block["text_content"])
        original_regions = block["locator"].get("regions")
        changed_region = _claim_region(edit, block, request_ref) if "locator" in edit else None
        if text == block["text_content"] and kind == block["kind"] and changed_region is None:
            raise ParseContractError("IR_CORRECTION_NO_CHANGE")
        if changed_region is not None:
            manual.setdefault("original_regions", deepcopy(original_regions))
            block["locator"]["regions"] = [changed_region]
        if text != block["text_content"]:
            text_ref = {**request_ref, "json_pointer": request_ref["json_pointer"] + "/text"}
            manual["text_source_ref"] = text_ref
            part = {"kind": "text", "raw_ref": text_ref, "source_start": 0, "source_end": len(text)}
            structured.update(renderer=RENDERER, parts=[part], assembly=[],
                              text_map=[{**part, "block_start": 0, "block_end": len(text)}])
        structured["manual_correction"] = manual
        block.update(text_content=text, kind=kind, source_kind="manual", source_block_ref=parent["id"])
        block["content_hash"] = fingerprint({key: value for key, value in block.items() if key != "content_hash"})
        edited.add(parent["id"])

    refs.append({"artifact_id": correction_artifact_id, "kind": "structured", "role": "manual_correction"})
    return candidate
