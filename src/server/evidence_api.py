"""Private, task-scoped source images and raster evidence assets."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib

from fastapi import Depends, FastAPI, Request, Response
from sqlalchemy.orm import Session

from .db import get_db
from .models import ContentBlock, DocumentVersion, FileObject, ParseArtifact, ParseRun, QueryTask, TaskEvidence
from .parsing.artifacts import ArtifactError, read_artifact
from .parsing.local_pdf import normalized_bbox, render_source_region
from .query_evidence import task_readable_evidence
from .services import SafeError, current_session, file_path


_IMAGE_HEADERS = {"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                  "Content-Security-Policy": "default-src 'none'; sandbox", "Cross-Origin-Resource-Policy": "same-origin"}
_ASSET_KINDS = {"image", "page_preview", "region_crop"}
_RASTER_MIME = {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"}


def _missing():
    return SafeError("NOT_FOUND", "资源不存在", 404, headers=_IMAGE_HEADERS)


def _access_changed():
    return SafeError("ACCESS_LIMITED", "任务依赖的资料访问条件已变化", 403, headers=_IMAGE_HEADERS)


def visible_task_evidence(db: Session, actor, task_id: str) -> list[TaskEvidence]:
    task = db.get(QueryTask, task_id, populate_existing=True)
    if not task:
        raise _missing()
    items = task_readable_evidence(db, actor, task)
    if items is None:
        raise _access_changed() if task.owner_user_id == actor.id else _missing()
    return items


def read_task_evidence(db: Session, actor, task_id: str, evidence_id: str) -> tuple[QueryTask, TaskEvidence]:
    """Keep binary access within the same current task and exact answer share."""
    task = db.get(QueryTask, task_id, populate_existing=True)
    item = db.get(TaskEvidence, evidence_id, populate_existing=True)
    if not task or not item or item.task_id != task.id or item.execution_revision != task.execution_revision:
        raise _missing()
    # Replay exact child sources and require membership in the receiver's answer.
    items = task_readable_evidence(db, actor, task, evidence_id=evidence_id)
    if items is None:
        raise _access_changed() if task.owner_user_id == actor.id else _missing()
    return task, next(row for row in items if row.id == evidence_id)


@dataclass(frozen=True)
class _Source:
    item: TaskEvidence
    run_id: str
    file: FileObject
    identity: tuple


def _source(db: Session, actor, task_id: str, evidence_id: str) -> _Source:
    task, item = read_task_evidence(db, actor, task_id, evidence_id)
    version = db.get(DocumentVersion, item.document_version_id, populate_existing=True)
    block = db.get(ContentBlock, item.block_id, populate_existing=True)
    run = db.get(ParseRun, block.parse_run_id, populate_existing=True) if block else None
    source = db.get(FileObject, version.source_file_id, populate_existing=True) if version else None
    if (not isinstance(item.locator, dict) or not version or not block or not run or run.document_version_id != version.id
            or not source or source.status != "available" or source.storage_name != "local"
            or source.mime_type not in {"application/pdf", "application/octet-stream"}):
        raise _missing()
    locator = item.locator
    expected = {"document_version_id": version.id, "parse_run_id": run.id, "block_id": block.id,
                "source_file_id": source.id, "source_sha256": source.sha256}
    if any(key in locator and locator[key] != value for key, value in expected.items()):
        raise _missing()
    if item.chunk_id or item.chunk_set_id or item.source_binding_hash:
        if (not (item.chunk_id and item.chunk_set_id and item.source_binding_hash)
                or any(locator.get(key) != value for key, value in expected.items())):
            raise _missing()
    identity = (task.execution_revision, item.id, block.id, run.id, version.id, source.id, source.sha256,
                source.byte_size, source.object_key, item.chunk_id, item.chunk_set_id, item.source_binding_hash,
                item.evidence_kind, deepcopy(locator), deepcopy(item.asset_refs))
    return _Source(item, run.id, source, identity)


def _source_bytes(source: FileObject, settings) -> bytes:
    if not 0 < source.byte_size <= settings.max_upload_bytes:
        raise ValueError("source byte size outside configured bounds")
    with file_path(source, settings).open("rb") as stream:
        body = stream.read(source.byte_size + 1)
    if len(body) != source.byte_size or hashlib.sha256(body).hexdigest() != source.sha256 or not body.startswith(b"%PDF-"):
        raise ValueError("source document changed")
    return body


def _region(locator: dict, index: int) -> tuple[int, tuple]:
    # A region fragment carries the exact replayed region. Its enclosing block
    # regions must not replace it, even when both reference the same source page.
    if locator.get("region") is not None:
        choices = [locator["region"]]
    elif isinstance(locator.get("regions"), list):
        choices = locator["regions"]
    else:
        choices = [locator]
    if 0 <= index < len(choices) and isinstance(chosen := choices[index], dict):
        page = chosen.get("file_page")
        try:
            bbox = normalized_bbox(chosen.get("bbox_normalized"))
        except OverflowError:
            bbox = None
        if (chosen.get("geometry_status") in ("exact", "table_region_only") and bbox
                and type(page) is int and page > 0):
            return page, bbox
    raise SafeError("EVIDENCE_REGION_UNAVAILABLE", "暂无可用裁图，请查看原文定位", 404, headers=_IMAGE_HEADERS)


def _raster_matches(body: bytes, mime: str) -> bool:
    if mime == "image/png":
        return len(body) >= 33 and body.startswith(b"\x89PNG\r\n\x1a\n") and body[8:16] == b"\x00\x00\x00\x0dIHDR"
    if mime == "image/jpeg":
        return len(body) >= 8 and body.startswith(b"\xff\xd8\xff") and body.endswith(b"\xff\xd9")
    if mime == "image/webp":
        return (len(body) >= 20 and body[:4] == b"RIFF" and body[8:12] == b"WEBP"
                and body[12:16] in (b"VP8 ", b"VP8L", b"VP8X") and int.from_bytes(body[4:8], "little") + 8 == len(body))
    if mime == "image/gif":
        return len(body) >= 14 and body[:6] in (b"GIF87a", b"GIF89a") and body.endswith(b";")
    if mime == "image/bmp":
        return len(body) >= 26 and body[:2] == b"BM" and int.from_bytes(body[2:6], "little") == len(body)
    if mime == "image/tiff":
        return len(body) >= 8 and body[:4] in (b"II*\x00", b"MM\x00*")
    return False


def _asset(db: Session, source: _Source, artifact_id: str, settings) -> tuple[ParseArtifact, FileObject]:
    refs = source.item.asset_refs
    if not isinstance(refs, list) or artifact_id not in refs:
        raise _missing()
    artifact = db.get(ParseArtifact, artifact_id, populate_existing=True)
    if not artifact or artifact.parse_run_id != source.run_id or artifact.kind not in _ASSET_KINDS:
        raise _missing()
    file = db.get(FileObject, artifact.file_id, populate_existing=True)
    if (not file or file.status != "available" or file.storage_name != "local" or file.mime_type not in _RASTER_MIME
            or not 0 < file.byte_size <= settings.max_upload_bytes):
        raise _missing()
    return artifact, file


def _asset_identity(artifact, file) -> tuple:
    return (file.id, file.sha256, file.byte_size, file.mime_type, file.object_key, artifact.kind,
            artifact.logical_name, artifact.derived_from_artifact_id, deepcopy(artifact.source_locator))


def _refresh_source(db, request, task_id, evidence_id, previous):
    actor, _ = current_session(request, db, request.app.state.settings, persist_heartbeat=False)
    current = _source(db, actor, task_id, evidence_id)
    if current.identity != previous.identity:
        raise _access_changed()
    return current


def register_evidence_routes(app: FastAPI) -> None:
    @app.get("/tasks/{task_id}/evidence/{evidence_id}/assets/{artifact_id}")
    def read_evidence_asset(task_id: str, evidence_id: str, artifact_id: str, request: Request,
                            db: Session = Depends(get_db)) -> Response:
        settings = request.app.state.settings
        actor, _ = current_session(request, db, settings, persist_heartbeat=False)
        source = _source(db, actor, task_id, evidence_id)
        artifact, file = _asset(db, source, artifact_id, settings)
        asset_identity = _asset_identity(artifact, file)
        _, expected_hash, expected_size, mime, *_ = asset_identity
        try:
            _source_bytes(source.file, settings)
            raw = read_artifact(db, artifact, settings)
            if len(raw) != expected_size or hashlib.sha256(raw).hexdigest() != expected_hash or not _raster_matches(raw, mime):
                raise ValueError("raster identity changed")
        except (ArtifactError, OSError, SafeError, ValueError):
            raise SafeError("EVIDENCE_ASSET_UNAVAILABLE", "图像证据当前不可用，请核对资料状态", 409, headers=_IMAGE_HEADERS) from None
        current = _refresh_source(db, request, task_id, evidence_id, source)
        current_artifact, actual = _asset(db, current, artifact_id, settings)
        if _asset_identity(current_artifact, actual) != asset_identity:
            raise _access_changed()
        return Response(raw, media_type=mime, headers=_IMAGE_HEADERS)

    @app.get("/tasks/{task_id}/evidence/{evidence_id}/source-image")
    def read_evidence_source_image(task_id: str, evidence_id: str, request: Request, region: int = 0,
                                   db: Session = Depends(get_db)) -> Response:
        settings = request.app.state.settings
        actor, _ = current_session(request, db, settings, persist_heartbeat=False)
        source = _source(db, actor, task_id, evidence_id)
        page, bbox = _region(source.item.locator, region)
        try:
            import pymupdf

            body = _source_bytes(source.file, settings)
            with pymupdf.open(stream=body, filetype="pdf") as pdf:
                if pdf.needs_pass or not 1 <= page <= len(pdf):
                    raise ValueError("invalid source page")
                cropped = render_source_region(pdf[page - 1], bbox)
        except (ImportError, IndexError, OSError, RuntimeError, SafeError, ValueError):
            raise SafeError("EVIDENCE_REGION_UNAVAILABLE", "暂无可用裁图，请查看原文定位", 404, headers=_IMAGE_HEADERS) from None
        _refresh_source(db, request, task_id, evidence_id, source)
        return Response(cropped, media_type="image/png", headers=_IMAGE_HEADERS)
