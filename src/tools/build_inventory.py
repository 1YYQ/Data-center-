"""Build a non-sensitive PDF inventory without copying document contents."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build(root: Path) -> dict:
    root = root.resolve()
    files = []
    for path in sorted(root.rglob("*.pdf")):
        digest = sha256(path)
        files.append({
            "file_id": str(uuid5(NAMESPACE_URL, f"{root.as_posix()}:{path.relative_to(root).as_posix()}:{digest}")),
            "name": path.name,
            "relative_path": path.relative_to(root).as_posix(),
            "sha256": digest,
            "byte_size": path.stat().st_size,
            "mime_type": "application/pdf",
            "title": None,
            "models": [],
            "revision_label": None,
            "applicability_status": "pending",
            "content_review_status": "unreviewed",
            "publish_status": "draft",
            "external_services": {"mineru": False, "embedding": False, "generation": False},
        })
    return {"schema_version": "device-inventory.v1", "source_root": str(root), "generated_at": datetime.now(timezone.utc).isoformat(), "files": files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = build(args.root)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {len(manifest['files'])} PDF records to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
