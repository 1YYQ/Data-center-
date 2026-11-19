"""MinerU Precision Extract API v4 with legacy flat Content List V1 output."""
from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
import json
import math
import re
import stat
from typing import Any
from unicodedata import normalize
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZIP_DEFLATED, ZIP_STORED, ZipFile, ZipInfo
import zlib

from ..provider_transport import ProviderError, ProviderTransport

_FILE = "synthetic.pdf"
_DATA_ID = re.compile(r"[A-Za-z0-9_.-]{1,128}")
_BATCH_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}")
_PENDING = frozenset({"waiting-file", "pending", "running", "converting"})
_MAX_API_BYTES = 256 * 1024
_MAX_ARCHIVE_BYTES = 16 * 1024 * 1024
_MAX_DECOMPRESSED_BYTES = 64 * 1024 * 1024
_MAX_CONTENT_BYTES = 8 * 1024 * 1024
_MAX_MEMBERS = 1024
_MAX_COMPRESSION_RATIO = 200
_MAX_BLOCKS = 10000

_TEXT_TYPES = (
    "text", "title", "equation", "header", "footer", "page_number",
    "page_footnote", "aside_text", "ref_text", "phonetic",
)
_CONTENT_FIELDS = {
    **dict.fromkeys(_TEXT_TYPES, ("text",)),
    "table": ("table_caption", "table_body", "table_footnote"),
    "image": ("image_caption", "content", "image_footnote"),
    "chart": ("chart_caption", "content", "chart_footnote"),
    "code": ("code_caption", "code_body", "code_footnote"),
    "list": ("list_items",),
}
_METADATA_FIELDS = frozenset({"type", "page_idx", "bbox", "text_level", "text_format", "sub_type", "img_path"})
_VISUAL_TYPES = frozenset({"table", "image", "chart", "equation"})


class MinerUAdapter:
    def __init__(self, transport: ProviderTransport):
        self._transport = transport

    def submit(self, pdf: bytes, data_id: str, *, checkpoint: Callable[[str], None]) -> str:
        """Create a one-file batch, durably checkpoint its ID, then upload once."""
        if not isinstance(pdf, bytes) or not pdf.startswith(b"%PDF-") or not callable(checkpoint):
            raise ProviderError("MINERU_INVALID_INPUT")
        _require_identifier(data_id, _DATA_ID, "MINERU_INVALID_INPUT")
        request = {
            "files": [{"name": _FILE, "data_id": data_id}],
            "model_version": "vlm",
            "enable_formula": True,
            "enable_table": True,
        }
        data = _response_data(self._transport.request(
            "POST", "/api/v4/file-urls/batch", body=json.dumps(request).encode("utf-8"),
        ))
        batch_id = _require_identifier(data.get("batch_id"), _BATCH_ID, "MINERU_INVALID_RESPONSE")
        # Even a malformed upload URL can accompany a valid, recoverable batch ID.
        checkpoint(batch_id)
        urls = data.get("file_urls")
        if not isinstance(urls, list) or len(urls) != 1:
            raise ProviderError("MINERU_INVALID_RESPONSE")
        self._transport.request("PUT", _transfer_url(urls[0]), body=pdf, purpose="upload", content_type=None)
        return batch_id

    def poll(self, batch_id: str, data_id: str) -> list[tuple[str, dict[str, Any], str]] | None:
        """Query an existing batch once. Return None while it can still progress."""
        _require_identifier(batch_id, _BATCH_ID, "MINERU_INVALID_INPUT")
        _require_identifier(data_id, _DATA_ID, "MINERU_INVALID_INPUT")
        data = _response_data(self._transport.request("GET", f"/api/v4/extract-results/batch/{batch_id}"))
        results = data.get("extract_result")
        if data.get("batch_id") != batch_id or not isinstance(results, list) or len(results) != 1:
            raise ProviderError("MINERU_INVALID_RESPONSE")
        result = results[0]
        if not isinstance(result, dict) or result.get("data_id") != data_id or result.get("file_name") != _FILE:
            raise ProviderError("MINERU_INVALID_RESPONSE")
        state = result.get("state")
        if not isinstance(state, str):
            raise ProviderError("MINERU_INVALID_RESPONSE")
        if state in _PENDING:
            return None
        if state == "failed":
            raise ProviderError("MINERU_EXTRACTION_FAILED")
        if state != "done":
            raise ProviderError("MINERU_INVALID_RESPONSE")
        archive = self._transport.request(
            "GET", _transfer_url(result.get("full_zip_url")), purpose="download", content_type=None,
        )
        return self.parse_archive(archive)

    @staticmethod
    def parse_archive(archive: bytes) -> list[tuple[str, dict[str, Any], str]]:
        return _normalize_content_list(_read_content_list(archive))


def _require_identifier(value: object, pattern: re.Pattern[str], code: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ProviderError(code)
    return value


def _transfer_url(value: object) -> str:
    if not isinstance(value, str) or not (0 < len(value) <= 8192):
        raise ProviderError("MINERU_INVALID_RESPONSE")
    if any(ord(char) < 33 or ord(char) == 127 for char in value):
        raise ProviderError("MINERU_INVALID_RESPONSE")
    try:
        url = urlsplit(value)
    except ValueError:
        raise ProviderError("MINERU_INVALID_RESPONSE") from None
    if url.scheme not in {"http", "https"} or not url.netloc or url.fragment or url.username is not None:
        raise ProviderError("MINERU_INVALID_RESPONSE")
    # Network origin and credentials are enforced by the injected transport.
    return value


def _json(payload: bytes, code: str) -> Any:
    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def finite_float(value: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError
        return result

    try:
        return json.loads(payload.decode("utf-8"), object_pairs_hook=unique_object,
                          parse_float=finite_float, parse_constant=finite_float)
    except (ValueError, RecursionError):
        raise ProviderError(code) from None


def _response_data(payload: bytes) -> dict[str, Any]:
    if len(payload) > _MAX_API_BYTES:
        raise ProviderError("MINERU_INVALID_RESPONSE")
    response = _json(payload, "MINERU_INVALID_RESPONSE")
    if not isinstance(response, dict) or type(response.get("code")) is not int:
        raise ProviderError("MINERU_INVALID_RESPONSE")
    if response["code"] != 0:
        raise ProviderError("MINERU_API_REJECTED")
    data = response.get("data")
    if not isinstance(data, dict):
        raise ProviderError("MINERU_INVALID_RESPONSE")
    return data


def _member_identity(member: ZipInfo) -> str:
    name = member.orig_filename
    path = name[:-1] if name.endswith("/") else name
    if (any(ord(char) < 32 or char in "\\:" for char in path)
            or any(not part or part in {".", ".."} or part.endswith((".", " ")) for part in path.split("/"))):
        raise ProviderError("MINERU_INVALID_ARCHIVE")
    return normalize("NFC", path).casefold()


def _read_content_list(payload: bytes) -> Any:
    if len(payload) > _MAX_ARCHIVE_BYTES:
        raise ProviderError("MINERU_ARCHIVE_LIMIT")
    try:
        with ZipFile(BytesIO(payload)) as archive:
            members = archive.infolist()
            if len(members) > _MAX_MEMBERS:
                raise ProviderError("MINERU_ARCHIVE_LIMIT")
            identities: dict[str, bool] = {}
            candidates: list[ZipInfo] = []
            total_size = 0
            for member in members:
                identity = _member_identity(member)
                mode = stat.S_IFMT(member.external_attr >> 16)
                expected_mode = stat.S_IFDIR if member.is_dir() else stat.S_IFREG
                if (identity in identities or mode not in {0, expected_mode} or member.flag_bits & 0x41
                        or member.compress_type not in {ZIP_STORED, ZIP_DEFLATED}):
                    raise ProviderError("MINERU_INVALID_ARCHIVE")
                identities[identity] = member.is_dir()
                total_size += member.file_size
                if (total_size > _MAX_DECOMPRESSED_BYTES
                        or member.file_size > _MAX_COMPRESSION_RATIO * max(member.compress_size, 1)):
                    raise ProviderError("MINERU_ARCHIVE_LIMIT")
                name = member.filename.rsplit("/", 1)[-1]
                if name == "content_list.json" or name.endswith("_content_list.json"):
                    candidates.append(member)
            for identity in identities:
                parent = identity.rpartition("/")[0]
                while parent:
                    if identities.get(parent) is False:
                        raise ProviderError("MINERU_INVALID_ARCHIVE")
                    parent = parent.rpartition("/")[0]
            if len(candidates) != 1:
                raise ProviderError("MINERU_UNSUPPORTED_CONTENT_LIST")
            member = candidates[0]
            if member.file_size > _MAX_CONTENT_BYTES:
                raise ProviderError("MINERU_ARCHIVE_LIMIT")
            with archive.open(member) as content:
                data = content.read(_MAX_CONTENT_BYTES + 1)
            if len(data) != member.file_size:
                raise ProviderError("MINERU_INVALID_ARCHIVE")
    except (BadZipFile, EOFError, OSError, RuntimeError, ValueError, zlib.error) as exc:
        if isinstance(exc, ProviderError):
            raise
        raise ProviderError("MINERU_INVALID_ARCHIVE") from None
    return _json(data, "MINERU_INVALID_CONTENT_LIST")


def _content_text(value: object) -> str:
    if not isinstance(value, str) or "\x00" in value:
        raise ProviderError("MINERU_INVALID_CONTENT_LIST")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise ProviderError("MINERU_INVALID_CONTENT_LIST") from None
    return value


def _normalize_content_list(value: object) -> list[tuple[str, dict[str, Any], str]]:
    if not isinstance(value, list) or not value or len(value) > _MAX_BLOCKS:
        raise ProviderError("MINERU_INVALID_CONTENT_LIST")
    blocks = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict):
            raise ProviderError("MINERU_INVALID_CONTENT_LIST")
        page, provider_type = entry.get("page_idx"), entry.get("type")
        if type(page) is not int or page < 0 or not isinstance(provider_type, str):
            raise ProviderError("MINERU_INVALID_CONTENT_LIST")
        fields = _CONTENT_FIELDS.get(provider_type)
        if fields is None or any(key not in _METADATA_FIELDS and key not in fields for key in entry):
            raise ProviderError("MINERU_UNSUPPORTED_CONTENT_LIST")
        locator: dict[str, Any] = {
            "file_page": page + 1, "printed_page": None, "needs_review": True,
            "provider_type": provider_type, "provider_block_index": index,
        }
        if "bbox" in entry:
            bbox = entry["bbox"]
            if not isinstance(bbox, list) or len(bbox) != 4 or any(type(n) not in {int, float} for n in bbox):
                raise ProviderError("MINERU_INVALID_CONTENT_LIST")
            locator["provider_bbox"] = bbox
        if "text_level" in entry:
            level = entry["text_level"]
            if type(level) is not int or level < 0:
                raise ProviderError("MINERU_INVALID_CONTENT_LIST")
            locator["text_level"] = level
        for field in ("text_format", "sub_type", "img_path"):
            if field in entry:
                text = _content_text(entry[field])
                if field != "img_path":
                    locator[field] = text

        parts: list[str] = []
        for field in fields:
            if field not in entry:
                continue
            part = entry[field]
            if field.endswith(("_caption", "_footnote")) or field == "list_items":
                if not isinstance(part, list):
                    raise ProviderError("MINERU_INVALID_CONTENT_LIST")
                parts.extend(_content_text(item) for item in part)
            else:
                parts.append(_content_text(part))
        text = "\n".join(part for part in parts if part.strip())
        if not text.strip():
            if provider_type not in _VISUAL_TYPES:
                continue
            locator["visual_only"] = True
            text = "该区域无可用解析文本，请对照 PDF 原文核验。"
        kind = "paragraph" if provider_type == "text" else provider_type
        blocks.append((text, locator, kind))
    if not blocks:
        raise ProviderError("MINERU_INVALID_CONTENT_LIST")
    return blocks
