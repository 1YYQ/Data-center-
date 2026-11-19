"""Private, immutable parser output storage. Callers own the transaction and access checks."""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..models import DocumentVersion, FileObject, ParseArtifact, ParseRun


_STATUSES = {"available", "quarantined"}
_ATTEMPT_METADATA = {"preflight.json", "preflight.pending", "receipt.json", "receipt.pending", "runtime-config.json",
                     "runner-exit.json", "runner-exit.pending"}
_MIME_TYPES = {
    ".json": "application/json",
    ".zip": "application/zip",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".tiff": "image/tiff",
    ".tif": "image/tiff",
    ".gif": "image/gif",
}


class ArtifactError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class PreparedWorkspace:
    """Metadata for immutable objects already installed and verified on disk."""

    _receipts: tuple[bytes, ...]
    _manifest: bytes

    @property
    def manifest(self) -> dict:
        """A detached metadata view; changing it cannot change prepared bytes."""
        return json.loads(self._manifest)


@dataclass(frozen=True)
class PreparedArtifactAlias:
    _metadata: bytes

    @property
    def reference(self) -> dict:
        metadata = json.loads(self._metadata)
        artifact, file = metadata["artifact"], metadata["file"]
        return {"artifact_id": artifact["id"], "file_id": file["id"], "logical_name": artifact["logical_name"],
                "kind": artifact["kind"], "sha256": file["sha256"], "byte_size": file["byte_size"]}


def _valid_id(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except (TypeError, AttributeError, ValueError):
        return False


def _storage_path(settings: Settings, key: str) -> Path:
    try:
        root = settings.storage_dir.resolve()
        path = root / key
        if root not in path.parents or path.resolve() != path:
            raise ArtifactError("PARSE_ARTIFACT_PATH_INVALID")
        return path
    except (OSError, RuntimeError):
        raise ArtifactError("PARSE_ARTIFACT_PATH_INVALID") from None


def _sync_directory(path: Path) -> None:
    if os.name == "posix":
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _private_directory(path: Path) -> None:
    if path.is_dir():
        return
    _private_directory(path.parent)
    path.mkdir(mode=0o700, exist_ok=True)
    _sync_directory(path.parent)


def _workspace_key(run_id: str, epoch: int) -> str:
    if not _valid_id(run_id) or type(epoch) is not int or not 0 < epoch < 2**63:
        raise ArtifactError("PARSE_ARTIFACT_IDENTITY_INVALID")
    return f"parse-attempts/{run_id}/{epoch}"


def work_directory(settings: Settings, run_id: str, epoch: int) -> Path:
    key = _workspace_key(run_id, epoch)
    path = _storage_path(settings, key)
    try:
        _private_directory(path)
    except OSError:
        raise ArtifactError("PARSE_ARTIFACT_STORAGE_UNAVAILABLE") from None
    return _storage_path(settings, key)


def _object_key(run_id: str, logical_name: str) -> str:
    if (not _valid_id(run_id) or not isinstance(logical_name, str) or len(logical_name) > 500
            or re.search(r"[\\:\x00-\x1f\x7f]", logical_name)
            or any(part in {"", ".", ".."} for part in logical_name.split("/"))):
        raise ArtifactError("PARSE_ARTIFACT_IDENTITY_INVALID")
    try:
        name_hash = hashlib.sha256(logical_name.encode("utf-8")).hexdigest()
    except UnicodeError:
        raise ArtifactError("PARSE_ARTIFACT_IDENTITY_INVALID") from None
    return f"internal/parse-results/{run_id}/{name_hash}"


def _json_bytes(value: dict) -> bytes:
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    except (TypeError, ValueError):
        raise ArtifactError("PARSE_ARTIFACT_METADATA_INVALID") from None


def _install_once(path: Path, data: bytes) -> None:
    """Install a fully flushed file without ever replacing a visible name."""
    if path.exists():
        _sync_directory(path.parent)
        return
    staging = path.with_name(f".install-{uuid4().hex}")
    try:
        with staging.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # Windows rename fails if the destination already exists. On POSIX
            # a rename would replace it, so install by creating a hard link.
            if os.name == "nt":
                staging.rename(path)
            else:
                os.link(staging, path)
        except FileExistsError:
            pass
        _sync_directory(path.parent)
    finally:
        staging.unlink(missing_ok=True)


def _verify(path: Path, size: int, sha256: str) -> None:
    try:
        if path.is_file():
            with path.open("rb") as stream:
                if os.fstat(stream.fileno()).st_size == size and hashlib.file_digest(stream, "sha256").hexdigest() == sha256:
                    return
    except OSError:
        pass
    raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")


def _artifact_metadata(
    run_id: str,
    actor_id: str,
    raw: bytes,
    *,
    kind: str,
    logical_name: str,
    producer: dict | None = None,
    status: str = "available",
    source_locator: dict | None = None,
    derived_from_artifact_id: str | None = None,
) -> bytes:
    key = _object_key(run_id, logical_name)
    if (not _valid_id(actor_id) or not isinstance(status, str) or status not in _STATUSES or not isinstance(raw, bytes)
            or not isinstance(kind, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", kind)
            or (producer is not None and not isinstance(producer, dict))
            or (source_locator is not None and not isinstance(source_locator, dict))
            or (derived_from_artifact_id is not None and not _valid_id(derived_from_artifact_id))):
        raise ArtifactError("PARSE_ARTIFACT_METADATA_INVALID")
    file_id = str(uuid5(NAMESPACE_URL, f"dc:parse-file:{key}"))
    artifact_id = str(uuid5(NAMESPACE_URL, f"dc:parse-artifact:{key}"))
    digest = hashlib.sha256(raw).hexdigest()
    mime_type = _MIME_TYPES.get(Path(logical_name).suffix.lower(), "application/octet-stream")
    artifact_values = {
        "id": artifact_id, "parse_run_id": run_id, "file_id": file_id, "kind": kind,
        "logical_name": logical_name, "producer": producer or {}, "source_locator": source_locator,
        "derived_from_artifact_id": derived_from_artifact_id,
    }
    file_values = {
        "id": file_id, "storage_name": "local", "object_key": key, "sha256": digest,
        "byte_size": len(raw), "mime_type": mime_type, "status": status, "created_by": actor_id,
    }
    return _json_bytes({"schema": "dc.parse-artifact-install.v1", "artifact": artifact_values, "file": file_values})


def _matching_rows(db: Session, artifact_values: dict, file_values: dict) -> tuple[ParseArtifact | None, FileObject | None]:
    parent_id = artifact_values["derived_from_artifact_id"]
    if parent_id is not None:
        parent = db.get(ParseArtifact, parent_id)
        if parent is None or parent.parse_run_id != artifact_values["parse_run_id"]:
            raise ArtifactError("PARSE_ARTIFACT_METADATA_INVALID")
    file = db.scalar(select(FileObject).where(FileObject.object_key == file_values["object_key"]))
    existing = db.scalar(select(ParseArtifact).where(
        ParseArtifact.parse_run_id == artifact_values["parse_run_id"], ParseArtifact.logical_name == artifact_values["logical_name"],
    ))
    _check_matching_rows(artifact_values, file_values, existing, file)
    return existing, file


def _check_matching_rows(artifact_values: dict, file_values: dict,
                         existing: ParseArtifact | None, file: FileObject | None) -> None:
    if existing is not None and file is None:
        raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
    if ((existing is not None and any(getattr(existing, name) != value for name, value in artifact_values.items()))
            or (file is not None and any(getattr(file, name) != value for name, value in file_values.items()))):
        raise ArtifactError("PARSE_ARTIFACT_CONFLICT")


def _install_artifact(settings: Settings, raw: bytes, receipt: bytes, file_values: dict, *, already_bound: bool = False) -> None:
    key = file_values["object_key"]
    object_path = _storage_path(settings, key)
    receipt_path = _storage_path(settings, key + ".receipt.json")
    seal_path = _storage_path(settings, key + ".installed")
    stamp = hashlib.sha256(receipt).digest()
    try:
        if not (already_bound or seal_path.exists()):
            _private_directory(object_path.parent)
            if object_path.exists() and not receipt_path.is_file():
                raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
            _install_once(receipt_path, receipt)
        if not receipt_path.is_file():
            raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
        with receipt_path.open("rb") as installed:
            if installed.read(len(receipt) + 1) != receipt:
                raise ArtifactError("PARSE_ARTIFACT_CONFLICT")
        if not (already_bound or seal_path.exists()):
            _install_once(object_path, raw)
        _verify(object_path, file_values["byte_size"], file_values["sha256"])
        # A later disk-only prepare can distinguish missing installed bytes
        # from an interruption before the first successful installation.
        _install_once(seal_path, stamp)
        _verify(seal_path, len(stamp), hashlib.sha256(stamp).hexdigest())
    except OSError:
        raise ArtifactError("PARSE_ARTIFACT_STORAGE_UNAVAILABLE") from None


def _bind_artifact(
    db: Session, artifact_values: dict, file_values: dict, existing: ParseArtifact | None, file: FileObject | None,
) -> ParseArtifact:
    if existing is not None:
        return existing
    if file is None:
        db.add(FileObject(**file_values))
        db.flush()
    artifact = ParseArtifact(**artifact_values)
    db.add(artifact)
    db.flush()
    return artifact


def archive_bytes(
    db: Session,
    run_id: str,
    actor_id: str,
    settings: Settings,
    raw: bytes,
    *,
    kind: str,
    logical_name: str,
    producer: dict | None = None,
    status: str = "available",
    source_locator: dict | None = None,
    derived_from_artifact_id: str | None = None,
) -> ParseArtifact:
    receipt = _artifact_metadata(run_id, actor_id, raw, kind=kind, logical_name=logical_name, producer=producer,
                                 status=status, source_locator=source_locator, derived_from_artifact_id=derived_from_artifact_id)
    metadata = json.loads(receipt)
    artifact_values, file_values = metadata["artifact"], metadata["file"]
    existing, file = _matching_rows(db, artifact_values, file_values)
    _install_artifact(settings, raw, receipt, file_values, already_bound=file is not None)
    return _bind_artifact(db, artifact_values, file_values, existing, file)


def validate_raw_lineage(db: Session, run_id: str) -> list[str]:
    """Validate the bounded local source/profile chain for physical byte reuse."""
    from ..parse_jobs import ParseServiceJob
    from .local_pipeline import fingerprint

    run = db.get(ParseRun, run_id, populate_existing=True)
    version = db.get(DocumentVersion, run.document_version_id, populate_existing=True) if run else None
    file = db.get(FileObject, version.source_file_id, populate_existing=True) if version else None
    if not file or file.storage_name != "local" or file.status != "available" or file.mime_type != "application/pdf":
        raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")
    profile = (run.request_config, run.actual_profile, run.raw_contract, run.provider, run.engine_version, run.model_revision)
    expected = fingerprint({"version_id": version.id, "source_file_id": file.id, "source_sha256": file.sha256,
                            "request_config": run.request_config})
    lineage = []
    while run is not None:
        if len(lineage) >= 32 or run.id in lineage or run.document_version_id != version.id:
            raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")
        job = db.get(ParseServiceJob, run.id, populate_existing=True)
        actual = (run.request_config, run.actual_profile, run.raw_contract, run.provider, run.engine_version, run.model_revision)
        if not job or job.mode != "local" or job.source_file_id != file.id or job.source_sha256 != file.sha256:
            raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")
        if actual != profile or run.input_fingerprint != expected:
            raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")
        lineage.append(run.id)
        parent_id = run.parent_run_id
        if parent_id is None:
            return lineage
        run = db.get(ParseRun, parent_id, populate_existing=True)
    raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")


def _owner_key(db: Session, artifact: ParseArtifact, file: FileObject) -> str:
    key = _object_key(artifact.parse_run_id, artifact.logical_name)
    if file.object_key == key:
        if file.id != str(uuid5(NAMESPACE_URL, f"dc:parse-file:{key}")):
            raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
        return key
    lineage = validate_raw_lineage(db, artifact.parse_run_id)
    for parent_run_id in lineage[1:]:
        parent = db.get(ParseArtifact, artifact.derived_from_artifact_id, populate_existing=True) if artifact.derived_from_artifact_id else None
        if (not parent or parent.parse_run_id != parent_run_id or parent.file_id != file.id or parent.kind != artifact.kind
                or parent.producer != artifact.producer or parent.source_locator != artifact.source_locator):
            raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")
        key = _object_key(parent.parse_run_id, parent.logical_name)
        if file.object_key == key:
            if file.id != str(uuid5(NAMESPACE_URL, f"dc:parse-file:{key}")):
                raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
            return key
        artifact = parent
    raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")


def prepare_artifact_alias(
    db: Session, parent: ParseArtifact, settings: Settings, *, run_id: str, logical_name: str,
) -> PreparedArtifactAlias:
    """Verify the existing bytes and freeze metadata for a direct-parent alias."""
    key = _object_key(run_id, logical_name)
    read_artifact(db, parent, settings)
    file = db.get(FileObject, parent.file_id)
    fields = ("id", "object_key", "storage_name", "status", "mime_type", "sha256", "byte_size", "created_by")
    return PreparedArtifactAlias(_json_bytes({
        "file": {field: getattr(file, field) for field in fields},
        "artifact": {"id": str(uuid5(NAMESPACE_URL, f"dc:parse-artifact:{key}")), "parse_run_id": run_id,
                     "file_id": file.id, "kind": parent.kind, "logical_name": logical_name, "producer": parent.producer,
                     "source_locator": parent.source_locator, "derived_from_artifact_id": parent.id},
    }))


def bind_artifact_alias(db: Session, prepared: PreparedArtifactAlias) -> ParseArtifact:
    """Attach a child-run alias in the caller's authority-fenced transaction."""
    metadata = json.loads(prepared._metadata)
    values, file_values = metadata["artifact"], metadata["file"]
    file = db.get(FileObject, file_values["id"], populate_existing=True)
    if not file or any(getattr(file, name) != value for name, value in file_values.items()):
        raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")
    alias = ParseArtifact(**values)
    # The child cannot be the physical owner. Requiring a link through its
    # direct parent blocks references to arbitrary same-version parse runs.
    if _object_key(alias.parse_run_id, alias.logical_name) == file.object_key:
        raise ArtifactError("PARSE_ARTIFACT_LINEAGE_INVALID")
    _owner_key(db, alias, file)
    existing = db.scalar(select(ParseArtifact).where(ParseArtifact.parse_run_id == alias.parse_run_id,
                                                     ParseArtifact.logical_name == alias.logical_name))
    _check_matching_rows(values, file_values, existing, file)
    if existing is not None:
        return existing
    db.add(alias)
    db.flush()
    return alias


def read_artifact(db: Session, artifact: ParseArtifact, settings: Settings) -> bytes:
    """Read verified bytes for internal processing of an available artifact."""
    file = db.get(FileObject, artifact.file_id, populate_existing=True)
    if file is None:
        raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
    if file.storage_name != "local" or file.status != "available":
        raise ArtifactError("PARSE_ARTIFACT_UNAVAILABLE")
    if file.byte_size < 0:
        raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
    key = _owner_key(db, artifact, file)
    try:
        path = _storage_path(settings, key)
        if not path.is_file():
            raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
        with path.open("rb") as stream:
            raw = stream.read(file.byte_size + 1)
    except OSError:
        raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR") from None
    if len(raw) != file.byte_size or hashlib.sha256(raw).hexdigest() != file.sha256:
        raise ArtifactError("PARSE_ARTIFACT_INTEGRITY_ERROR")
    return raw


def _workspace_files(root: Path, directory: Path) -> list[tuple[str, Path]]:
    files = []
    for path in sorted(directory.iterdir()):
        if path.resolve() != path:
            raise ArtifactError("PARSE_ARTIFACT_PATH_INVALID")
        mode = path.lstat().st_mode
        if stat.S_ISDIR(mode):
            files.extend(_workspace_files(root, path))
        elif stat.S_ISREG(mode):
            files.append((path.relative_to(root).as_posix(), path))
        else:
            raise ArtifactError("PARSE_ARTIFACT_PATH_INVALID")
    return files


def _output_kind(relative_name: str) -> str:
    name = Path(relative_name).name.lower()
    if relative_name == "preflight.json":
        return "preflight"
    if name in {"middle.json", "middle_json.json"} or name.endswith("_middle.json"):
        return "middle_json"
    if name == "model.json" or name.endswith("_model.json"):
        return "model_json"
    if name in {"content_list.json", "structured_content.json"} or name.endswith("_content_list.json"):
        return "structured"
    suffix = Path(name).suffix
    if _MIME_TYPES.get(suffix, "").startswith("image/"):
        return "image"
    return {".zip": "raw_archive", ".md": "markdown"}.get(suffix, "raw_response")


def prepare_workspace(
    run_id: str,
    actor_id: str,
    settings: Settings,
    workspace: Path,
    *,
    epoch: int,
    complete: bool,
    safe_error: str | None = None,
) -> PreparedWorkspace:
    """Freeze and verify one attempt's actual output without accessing the DB."""
    expected = _storage_path(settings, _workspace_key(run_id, epoch))
    if safe_error is not None and (not isinstance(safe_error, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,99}", safe_error)):
        raise ArtifactError("PARSE_ARTIFACT_METADATA_INVALID")
    if type(complete) is not bool:
        raise ArtifactError("PARSE_ARTIFACT_METADATA_INVALID")
    prefix = f"attempt/{epoch}"
    records = []
    receipts = []
    has_output = False
    failed = not complete and safe_error is not None
    try:
        if workspace.resolve() != expected:
            raise ArtifactError("PARSE_ARTIFACT_PATH_INVALID")
        for name, path in sorted(_workspace_files(expected, expected)):
            raw = path.read_bytes()
            is_output = name not in _ATTEMPT_METADATA and Path(name).suffix.lower() != ".log"
            has_output = has_output or is_output
            receipt = _artifact_metadata(
                run_id, actor_id, raw, kind=_output_kind(name), logical_name=f"{prefix}/output/{name}",
                producer={"epoch": epoch, "role": "attempt_output"}, source_locator={"workspace_member": name},
                status="quarantined" if failed and is_output else "available",
            )
            metadata = json.loads(receipt)
            artifact, file = metadata["artifact"], metadata["file"]
            _install_artifact(settings, raw, receipt, file)
            receipts.append(receipt)
            records.append({
                "artifact_id": artifact["id"], "file_id": artifact["file_id"], "logical_name": artifact["logical_name"],
                "kind": artifact["kind"], "sha256": file["sha256"], "byte_size": file["byte_size"],
            })
    except (OSError, RuntimeError):
        raise ArtifactError("PARSE_ARTIFACT_STORAGE_UNAVAILABLE") from None

    actual_complete = has_output and complete and safe_error is None
    manifest = {
        "schema": "dc.parse-attempt-manifest", "schema_version": "1.0", "parse_run_id": run_id, "epoch": epoch,
        "complete": actual_complete,
        "output_state": ("complete" if actual_complete else "partial") if has_output else "none",
        "safe_error": safe_error or (None if has_output else "PARSE_NO_OUTPUT"),
        "artifacts": records,
    }
    body = _json_bytes(manifest)
    receipt = _artifact_metadata(
        run_id, actor_id, body, kind="structured", logical_name=f"{prefix}/manifest.json",
        producer={"epoch": epoch, "role": "attempt_manifest"},
    )
    metadata = json.loads(receipt)
    _install_artifact(settings, body, receipt, metadata["file"])
    receipts.append(receipt)
    manifest.update(manifest_artifact_id=metadata["artifact"]["id"], manifest_file_id=metadata["file"]["id"])
    return PreparedWorkspace(tuple(receipts), _json_bytes(manifest))


def prepare_artifact_set(run_id: str, actor_id: str, settings: Settings, items: list[dict]) -> PreparedWorkspace:
    """Prepare controlled stage artifacts using the same immutable installation.

    Only trusted pipeline code supplies these specifications. They are never
    accepted from document HTML, an HTTP payload or a model-generated path.
    """
    receipts, records, names = [], [], set()
    for item in items:
        logical_name = item["logical_name"]
        if logical_name in names:
            raise ArtifactError("PARSE_ARTIFACT_CONFLICT")
        names.add(logical_name)
        raw = item["raw"]
        receipt = _artifact_metadata(run_id, actor_id, raw, kind=item["kind"], logical_name=logical_name,
            producer=item.get("producer"), source_locator=item.get("source_locator"),
            derived_from_artifact_id=item.get("derived_from_artifact_id"), status=item.get("status", "available"))
        metadata = json.loads(receipt)
        _install_artifact(settings, raw, receipt, metadata["file"])
        receipts.append(receipt)
        records.append({"artifact_id": metadata["artifact"]["id"], "file_id": metadata["file"]["id"],
                        "logical_name": logical_name, "kind": item["kind"], "sha256": metadata["file"]["sha256"],
                        "byte_size": metadata["file"]["byte_size"], "mime_type": metadata["file"]["mime_type"]})
    return PreparedWorkspace(tuple(receipts), _json_bytes({"parse_run_id": run_id, "artifacts": records}))


def bind_workspace(db: Session, prepared: PreparedWorkspace) -> dict:
    """Bind checked metadata in the caller's transaction without storage I/O."""
    manifest = json.loads(prepared._manifest)
    files_to_bind, artifacts_to_bind = [], []
    decoded = [json.loads(receipt)["artifact"] for receipt in prepared._receipts]
    own_ids = {row["id"] for row in decoded}
    parent_ids = {row["derived_from_artifact_id"] for row in decoded if row["derived_from_artifact_id"] is not None}
    if any(row["derived_from_artifact_id"] == row["id"] for row in decoded):
        raise ArtifactError("PARSE_ARTIFACT_METADATA_INVALID")
    existing_parents = set(db.scalars(select(ParseArtifact.id).where(ParseArtifact.id.in_(parent_ids),
                                      ParseArtifact.parse_run_id == manifest["parse_run_id"]))) if parent_ids else set()
    if parent_ids - own_ids - existing_parents:
        raise ArtifactError("PARSE_ARTIFACT_METADATA_INVALID")
    # Bound SQL parameter counts on SQLite as well as network database trips.
    batch_size = 400
    for offset in range(0, len(prepared._receipts), batch_size):
        batch = [json.loads(receipt) for receipt in prepared._receipts[offset:offset + batch_size]]
        files = {file.object_key: file for file in db.scalars(select(FileObject).where(
            FileObject.object_key.in_([entry["file"]["object_key"] for entry in batch]),
        ))}
        artifacts = {artifact.logical_name: artifact for artifact in db.scalars(select(ParseArtifact).where(
            ParseArtifact.parse_run_id == manifest["parse_run_id"],
            ParseArtifact.logical_name.in_([entry["artifact"]["logical_name"] for entry in batch]),
        ))}
        for entry in batch:
            artifact_values, file_values = entry["artifact"], entry["file"]
            file = files.get(file_values["object_key"])
            existing = artifacts.get(artifact_values["logical_name"])
            _check_matching_rows(artifact_values, file_values, existing, file)
            if file is None:
                files_to_bind.append(FileObject(**file_values))
            if existing is None:
                artifacts_to_bind.append(ParseArtifact(**artifact_values))
    db.add_all(files_to_bind)
    db.flush()
    db.add_all(artifacts_to_bind)
    db.flush()
    return manifest


def archive_workspace(
    db: Session,
    run_id: str,
    actor_id: str,
    settings: Settings,
    workspace: Path,
    *,
    epoch: int,
    complete: bool,
    safe_error: str | None = None,
) -> dict:
    prepared = prepare_workspace(run_id, actor_id, settings, workspace, epoch=epoch, complete=complete, safe_error=safe_error)
    return bind_workspace(db, prepared)
