"""Fixed, non-sensitive two-page synthetic manual and contract parse bundle."""
from __future__ import annotations

import io
import json
from zipfile import ZipFile, ZipInfo

MODEL = "SYNTHETIC-1"
QUERY = "SYNTHETIC-1 startup and pressure"
LINES = [
    ("paragraph_title", "SYNTHETIC startup procedure"),
    ("paragraph", "Step 1: Check the synthetic readiness indicator."),
    ("paragraph", "Step 2: Start the synthetic training console."),
    ("paragraph", "Warning for step 2: Keep the synthetic shield closed."),
    ("paragraph_title", "SYNTHETIC operating limits"),
    ("paragraph", "Synthetic operating pressure: 5 Pa."),
]


def create_pdf() -> bytes:
    import pymupdf
    with pymupdf.open() as pdf:
        page = pdf.new_page(width=595, height=842)
        for index, (_, literal) in enumerate(LINES):
            page.insert_text((67, 84 + 39 * index), literal)
        pdf.new_page(width=595, height=842)
        return pdf.tobytes()


def contract_parse_bundle() -> bytes:
    blocks = []
    for index, (kind, literal) in enumerate(LINES):
        y = 84 + 39 * index
        blocks.append({"type": kind, **({"level": 1} if kind == "paragraph_title" else {}),
            "index": index, "bbox": [.10, (y - 16) / 842, .91, (y + 8) / 842],
            "content": [{"type": "text", "content": literal}]})
    pages = [{"page_idx": 0, "blocks": blocks}, {"page_idx": 1, "blocks": []}]
    payload = {"schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": True,
        # This exact wire shape is required by the existing parser contract;
        # pilot metadata/config and evidence explicitly identify it as a
        # synthetic contract bundle, not a MinerU execution.
        "metadata": {"file_suffix": "pdf", "producer": {"name": "MinerU", "version": "4.2.3"}},
        "extensions": {"mineru": {"tier": "standard", "parse_mode": "txt"},
            "docvortex_layout": {"version": 1, "pages": [
                {"page_idx": n, "width_pt": 595, "height_pt": 842} for n in range(2)]}},
        "pages": pages}
    body = io.BytesIO()
    with ZipFile(body, "w") as archive:
        rows = (("middle_json.json", json.dumps(payload, ensure_ascii=False)),
            ("structured_content.json", json.dumps({"pages": pages}, ensure_ascii=False)),
            ("markdown.md", "\n".join(text for _, text in LINES)))
        for name, content in rows:
            archive.writestr(ZipInfo(name, date_time=(2026, 11, 19, 0, 0, 0)), content)
    return body.getvalue()
