"""Fixed local PDF profile, complete-page preflight and strict save-bundle reader.

No business confirmation is inferred from this technical contract. Provider HTML
and external resource references are data; nothing here evaluates or downloads it.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path, PurePosixPath
import re
import stat
import time
from zipfile import BadZipFile, ZipFile


PROFILE = {"revision": "mineru-4.2.3-standard-v1", "provider": "mineru-kit", "engine_version": "4.2.3",
           "tier": "standard", "parse_mode": "auto", "small_backend": "onnx", "vlm_engine": "llama-cpp",
           "raw_schema": "docvortex.middle", "raw_schema_version": "2.0", "adapter_version": "dc-pdf/1"}
MAX_BUNDLE_BYTES = 128 * 1024 * 1024
MAX_UNPACKED_BYTES = 512 * 1024 * 1024
MAX_JSON_BYTES = 32 * 1024 * 1024


class ParseContractError(RuntimeError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def fingerprint(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _display_geometry(page, rect, *, clip=True, stroke_width=0):
    import pymupdf

    box = pymupdf.Rect(rect)
    if not all(math.isfinite(value) for value in box):
        return {"bbox": None, "geometry_status": "page_only"}
    # Horizontal and vertical centerlines can have zero area while their
    # visible, positive-width strokes occupy a real source region.
    if (box.width == 0) != (box.height == 0) and stroke_width > 0:
        half = stroke_width / 2
        box += (-half, -half, half, half)
    box = box * page.rotation_matrix
    if clip:
        box &= page.rect
    if box.is_empty or not page.rect.contains(box):
        return {"bbox": None, "geometry_status": "page_only"}
    return {"bbox": [box.x0 / page.rect.width, box.y0 / page.rect.height,
                     box.x1 / page.rect.width, box.y1 / page.rect.height], "geometry_status": "exact"}


def _native_text_lines(page):
    import pymupdf

    document = page.get_text("dict", flags=pymupdf.TEXTFLAGS_DICT & ~pymupdf.TEXT_PRESERVE_IMAGES)
    for block in document["blocks"]:
        for line in block.get("lines", []):
            text = "".join(span["text"] for span in line["spans"]).strip()
            if text and (geometry := _display_geometry(page, line["bbox"], clip=False))["bbox"]:
                yield {"text": text, "bbox": geometry["bbox"]}


def _vector_geometry(page, drawing):
    width = drawing.get("width")
    stroke = (type(width) in {float, int} and math.isfinite(width) and 0 < width
              and drawing.get("type") in {"s", "fs"} and bool(drawing.get("stroke_opacity")))
    return _display_geometry(page, drawing["rect"], stroke_width=width if stroke else 0)


def source_preflight(path: Path, expected_hash: str, expected_size: int, heartbeat=None) -> dict:
    try:
        import pymupdf
        if path.stat().st_size != expected_size or hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
            raise ParseContractError("PROCESSING_SOURCE_CHANGED")
        pages = []
        with pymupdf.open(path) as pdf:
            if pdf.needs_pass or not pdf.is_pdf or not 0 < len(pdf) <= 600:
                raise ParseContractError("PDF_PREFLIGHT_FAILED")
            # PageLabels are authored PDF metadata. Their presence does not
            # certify that the same label is printed on the page.
            try:
                first_label_page = min((rule["startpage"] for rule in pdf.get_page_labels()), default=len(pdf))
            except (KeyError, TypeError, ValueError, RuntimeError):
                first_label_page = len(pdf)
            last_heartbeat = time.monotonic()
            for index, page in enumerate(pdf):
                if heartbeat and time.monotonic() - last_heartbeat >= 5:
                    heartbeat()
                    last_heartbeat = time.monotonic()
                text = page.get_text("text").strip()
                images, drawings = len(page.get_images(full=True)), page.get_drawings()
                native = any(char.isalnum() for char in text)
                visual = bool(images or drawings)
                page_class = "mixed" if native and visual else "native" if native else "scan" if images else "unknown"
                blank = not text and not visual
                try:
                    label = page.get_label().strip() if index >= first_label_page else ""
                except (IndexError, KeyError, TypeError, ValueError, RuntimeError):
                    label = ""
                label_basis = {"kind": "pdf_page_labels", "label": label} if label and label.isprintable() and len(label) <= 128 else None
                image_candidates = [{"id": f"native-image-{index + 1}-{number}", "source_index": number,
                    **_display_geometry(page, image["bbox"])} for number, image in enumerate(page.get_image_info(xrefs=True))]
                vector_candidates = [{"id": f"native-vector-{index + 1}-{number}", "source_index": number,
                    **_vector_geometry(page, drawing)} for number, drawing in enumerate(drawings)]
                unit_type, raw_unit = pdf.xref_get_key(page.xref, "UserUnit")
                user_unit = 1.0 if unit_type == "null" else float(raw_unit)
                if not math.isfinite(user_unit) or user_unit <= 0:
                    raise ParseContractError("PDF_PREFLIGHT_FAILED")
                pages.append({"file_page": index + 1, "media_box_pt": list(page.mediabox),
                    "crop_box_pt": list(page.cropbox), "rotation": page.rotation, "user_unit": user_unit,
                    "width_pt": page.rect.width, "height_pt": page.rect.height, "printed_page": None,
                    "printed_page_basis": label_basis, "page_class": page_class,
                    "state": "blank" if blank else "pending", "native_text": text,
                    "coverage": {"native_text_present": native, "native_text_lines": list(_native_text_lines(page)) if native else [],
                        "image_candidates": image_candidates,
                        "vector_candidates": vector_candidates,
                        "image_candidate_count": images, "vector_candidate_count": len(drawings),
                        "blank_basis": "no_text_images_or_drawings" if blank else None},
                    "issue_refs": [], "transforms": {"display_space": "rotated_crop_box", "provider_space": "display_normalized"}})
        return {"schema": "dc.pdf-preflight", "schema_version": "1", "source_sha256": expected_hash,
                "producer": {"name": "PyMuPDF", "version": pymupdf.VersionBind}, "page_count": len(pages), "pages": pages}
    except ParseContractError:
        raise
    except (ImportError, OSError, RuntimeError, ValueError):
        raise ParseContractError("PDF_PREFLIGHT_FAILED") from None


def _member_name(name: str) -> str:
    path = PurePosixPath(name)
    if (not name or "\\" in name or "\x00" in name or path.is_absolute() or any(x in {".", ".."} for x in name.split("/"))
            or ":" in name or name.startswith("/") or any(ord(char) < 32 for char in name)):
        raise ParseContractError("PARSE_ARCHIVE_UNSAFE")
    return path.as_posix()


def read_bundle(path: Path | bytes, preflight: dict, profile: dict) -> tuple[dict, dict[str, bytes]]:
    """Read a full saved package without extracting or following any URLs."""
    try:
        from io import BytesIO
        size = len(path) if isinstance(path, bytes) else path.stat().st_size
        if not 0 < size <= MAX_BUNDLE_BYTES:
            raise ParseContractError("PARSE_ARCHIVE_LIMIT")
        with ZipFile(BytesIO(path) if isinstance(path, bytes) else path) as archive:
            infos = archive.infolist()
            if not infos or len(infos) > 10_000 or sum(info.file_size for info in infos) > MAX_UNPACKED_BYTES:
                raise ParseContractError("PARSE_ARCHIVE_LIMIT")
            members = {}
            names = set()
            for info in infos:
                if info.is_dir():
                    continue
                name = _member_name(info.filename)
                if name.casefold() in names or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                    raise ParseContractError("PARSE_ARCHIVE_UNSAFE")
                names.add(name.casefold())
                members[name] = archive.read(info)
            if not {"middle_json.json", "markdown.md", "structured_content.json"} <= members.keys() or len(members["middle_json.json"]) > MAX_JSON_BYTES:
                raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
            middle = json.loads(members["middle_json.json"].decode("utf-8"))
        validate_middle(middle, preflight, profile)
        structured = json.loads(members["structured_content.json"].decode("utf-8"))
        if not isinstance(structured, dict) or not isinstance(structured.get("pages"), list):
            raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
        projected = structured["pages"]
        if (len(projected) != len(middle["pages"]) or any(not isinstance(page, dict)
                or type(page.get("page_idx")) is not int or not isinstance(page.get("blocks"), list) for page in projected)
                or [page["page_idx"] for page in projected] != [page["page_idx"] for page in middle["pages"]]
                or any(len(a["blocks"]) != len(b.get("blocks", [])) for a, b in zip(projected, middle["pages"]))):
            raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
        _check_resources(middle, members)
        return middle, members
    except ParseContractError:
        raise
    except (BadZipFile, OSError, ValueError, TypeError, KeyError, RecursionError, RuntimeError):
        raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED") from None


def middle_page_blocks(page: dict, source_page: dict) -> list:
    """DocVortex omits empty defaults. Only independent blank-page evidence permits that omission."""
    if "blocks" not in page and isinstance(source_page, dict) and source_page.get("state") == "blank":
        return []
    blocks = page.get("blocks")
    if not isinstance(blocks, list):
        raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
    return blocks


def validate_middle(middle: dict, preflight: dict, profile: dict) -> None:
    if not isinstance(middle, dict) or middle.get("schema") != "docvortex.middle" or middle.get("schema_version") != "2.0":
        raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
    metadata, extensions = middle.get("metadata"), middle.get("extensions")
    if not isinstance(metadata, dict) or not isinstance(extensions, dict):
        raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
    producer, actual = metadata.get("producer"), extensions.get("mineru")
    if not isinstance(producer, dict) or not isinstance(actual, dict):
        raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
    if (str(producer.get("name", "")).casefold() != "mineru" or producer.get("version") != profile["engine_version"]
            or actual.get("tier") != profile["tier"] or actual.get("parse_mode") not in {"txt", "ocr"}
            or middle.get("is_full_document") is not True):
        raise ParseContractError("PARSE_PROFILE_MISMATCH")
    pages = middle.get("pages")
    if not isinstance(pages, list):
        raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
    seen = set()
    for page in pages:
        index = page.get("page_idx") if isinstance(page, dict) else None
        if type(index) is not int or index in seen or not 0 <= index < preflight["page_count"]:
            raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
        source_page = preflight["pages"][index]
        blocks = middle_page_blocks(page, source_page)
        seen.add(index)
        if not blocks and source_page["state"] != "blank":
            raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
        for block in blocks:
            if not isinstance(block, dict) or not isinstance(block.get("type"), str):
                raise ParseContractError("PARSE_CONTRACT_UNSUPPORTED")
            bbox = block.get("bbox")
            if bbox is not None and (not isinstance(bbox, list) or len(bbox) != 4
                    or any(type(x) not in {int, float} or not math.isfinite(x) or not 0 <= x <= 1 for x in bbox)
                    or bbox[0] >= bbox[2] or bbox[1] >= bbox[3]):
                raise ParseContractError("SOURCE_LOCATOR_MISSING")
    if seen != set(range(preflight["page_count"])):
        raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")
    if [page["page_idx"] for page in pages] != sorted(seen):
        raise ParseContractError("PARSE_PAGE_COVERAGE_INCOMPLETE")


def _check_resources(value, members: dict[str, bytes]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in {"src", "img_path", "image_path"} and isinstance(child, str):
                if ":" in child or child.startswith("//") or _member_name(child) not in members:
                    raise ParseContractError("PARSE_RESOURCE_MISSING")
            _check_resources(child, members)
    elif isinstance(value, list):
        for child in value:
            _check_resources(child, members)
    elif isinstance(value, str):
        for reference in re.findall(r'<img\b[^>]*\bsrc=[\"\']([^\"\']+)', value, re.I):
            if ":" in reference or reference.startswith("//") or _member_name(reference) not in members:
                raise ParseContractError("PARSE_RESOURCE_MISSING")
