"""Offline MinerU Basic adapter for PDF pages that require OCR."""

from __future__ import annotations

import json
import os
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from tempfile import TemporaryDirectory
import time

from .local_pdf import Region, normalized_bbox, ocr_region, render_source_region
from .table_recovery import mark_graphic_keys, supplement_icons, supplement_warnings


def run_standard_bundle(pdf: Path, workspace: Path, executable: Path | None, home: Path | None, heartbeat) -> Path:
    """Run the reviewed SDK script, retaining every actual output on failure."""
    from .local_pipeline import ParseContractError

    if not executable or not executable.is_file() or not home or not home.is_dir():
        raise ParseContractError("LOCAL_STANDARD_NOT_READY")
    python = executable.resolve().with_name("python.exe" if os.name == "nt" else "python")
    if not python.is_file():
        raise ParseContractError("LOCAL_STANDARD_NOT_READY")
    system_keys = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES",
                   "PROGRAMFILES(X86)", "USERPROFILE", "LOCALAPPDATA", "APPDATA"}
    env = {key: value for key, value in os.environ.items() if key.upper() in system_keys}
    env.update(MINERU_HOME=str(home.resolve()), MINERU_MODEL_SOURCE="local", MINERU_MODEL_SMALL_BACKEND="onnx",
        MINERU_MODEL_VLM_ENGINE="llama-cpp", MINERU_TELEMETRY_DISABLED="1", HF_HUB_OFFLINE="1",
        HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9", ALL_PROXY="http://127.0.0.1:9",
        NO_PROXY="127.0.0.1,localhost,::1", TEMP=str(workspace), TMP=str(workspace), TMPDIR=str(workspace))
    # Never inherit a business config from MINERU_HOME, including a remote VLM
    # URL or LLM postprocessors. This file contains no credentials or user data.
    controlled_config = workspace / "runtime-config.json"
    controlled_config.write_text(json.dumps({"model": {"source": "local", "small_backend": "onnx",
        "vlm": {"engine": "llama-cpp", "server_url": "", "api_key": "", "model": ""}},
        "llm_aided": {"features": {"title_leveling": False, "cross_page_table_cell_merge": False}}}), encoding="utf-8")
    env["MINERU_CONFIG"] = str(controlled_config.resolve())
    command = [str(python), "-I", str(Path(__file__).with_name("standard_runner.py").resolve()),
               str(pdf.resolve()), str(workspace.resolve())]
    process = None
    started = last_heartbeat = time.monotonic()
    try:
        with (workspace / "stdout.log").open("xb") as stdout, (workspace / "stderr.log").open("xb") as stderr:
            process = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr)
            while process.poll() is None:
                now = time.monotonic()
                if now - started > 1800:
                    raise ParseContractError("LOCAL_PARSE_TIMEOUT")
                if now - last_heartbeat >= 5:
                    heartbeat()
                    last_heartbeat = now
                time.sleep(0.2)
            if process.returncode != 0:
                raise ParseContractError("LOCAL_PARSE_FAILED")
        return workspace / "result.zip"
    except OSError:
        raise ParseContractError("LOCAL_PARSE_UNAVAILABLE") from None
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        if process is not None and process.poll() is not None:
            # Recovery may restart only after an actual observed process exit.
            from ..parse_jobs import _write_receipt
            _write_receipt(workspace / "runner-exit.json", {"returncode": process.returncode})


class _TableCells(HTMLParser):
    """Read cell text and spans without returning any provider markup to clients."""

    def __init__(self):
        super().__init__()
        self.rows = []
        self.row = None
        self.cell = None
        self.tables = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.tables += 1
            if self.tables > 1:
                raise ValueError("one table per block")
        if tag == "tr" and self.tables:
            self.row = []
        if tag in {"th", "td"} and self.row is not None:
            attrs = dict(attrs)
            spans = {key: int(attrs.get(key, "1")) for key in ("rowspan", "colspan")}
            if not all(1 <= count <= 250 for count in spans.values()):
                raise ValueError("invalid cell span")
            self.cell = {"text": "", "header": tag == "th", **spans}
        if tag == "br" and self.cell is not None:
            self.cell["text"] += "\n"

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["text"] += data

    def handle_endtag(self, tag):
        if tag in {"th", "td"} and self.cell is not None:
            self.cell["text"] = self.cell["text"].strip()
            self.row.append(self.cell)
            self.cell = None
        if tag == "tr" and self.row:
            self.rows.append(self.row)
            self.row = None


def _content(value, separator="") -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return separator.join(_content(item) for item in value)
    if isinstance(value, dict):
        return _content(value.get("content"))
    return ""


def _region(node: dict) -> Region | None:
    kind = node.get("type")
    bbox = normalized_bbox(node.get("bbox"))
    if kind == "table":
        children = node.get("content", [])
        captions = [_content(child).strip() for child in children if child.get("type") == "table_caption"]
        footnotes = [_content(child).strip() for child in children if child.get("type") == "table_footnote"]
        table = _TableCells()
        try:
            table.feed(next(_content(child) for child in children if child.get("type") == "table_body"))
            table.close()
            if not table.rows or not any(cell["text"] for row in table.rows for cell in row):
                raise ValueError("empty table")
        except (StopIteration, ValueError, TypeError):
            text = "\n".join(filter(None, [*captions, "识别到表格区域，单元格结构待对照原文核验。", *footnotes]))
            return ocr_region(text, "table", bbox,
                              problem="table_structure_uncertain")
        lines = [" | ".join(cell["text"] for cell in row) for row in table.rows]
        notes = {"rows": table.rows, "captions": captions, "footnotes": footnotes}
        issue = "table_structure_uncertain" if any(not any(cell["text"] for cell in row) for row in table.rows) else None
        return ocr_region("\n".join(filter(None, [*captions, *lines, *footnotes])), "table", bbox, notes, issue)

    text = _content(node, "\n" if kind in {"list", "index"} else "").strip()
    if kind in {"image", "chart"}:
        prompt = "图片或图表区域，请对照 PDF 原文核验。"
        return ocr_region(f"{prompt}\n{text}" if text else prompt, "image", bbox)
    if not text:
        return None
    label = {"page_footnote": "footnote", "equation": "formula", "page_number": "page_number",
             "footer": "footer", "doc_title": "title", "paragraph_title": "title"}.get(kind, "paragraph")
    return ocr_region(text, label, bbox)


def _page_range(pages: list[int]) -> str:
    ranges = []
    start = end = pages[0]
    for page in pages[1:]:
        if page == end + 1:
            end = page
        else:
            ranges.append(str(start) if start == end else f"{start}-{end}")
            start = end = page
    ranges.append(str(start) if start == end else f"{start}-{end}")
    return ",".join(ranges)


def _recheck_tables(pdf: Path, pages: dict[int, list[Region]], executable: Path, scratch: Path, env: dict) -> None:
    tables = [(number, region) for number, regions in pages.items() for region in regions
              if region.kind == "table" and region.bbox and region.structure.get("rows")]
    python = executable.resolve().with_name("python.exe" if os.name == "nt" else "python")
    if not tables or not python.is_file():
        return

    try:
        import pymupdf

        candidates = {}
        with pymupdf.open(pdf) as source:
            for index, (page, table) in enumerate(tables[:120]):
                key = f"table-{index:04d}"
                try:
                    pixels = render_source_region(source[page - 1], table.bbox, dpi=215)
                except (ValueError, RuntimeError):
                    continue
                (scratch / (key + ".png")).write_bytes(pixels)
                candidates[key] = table
        if not candidates:
            return
        runner = Path(__file__).with_name("mineru_crop_ocr.py").resolve()
        subprocess.run([str(python), "-I", str(runner), str(scratch)], env=env, stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=240, check=True)
        output = scratch / "crop-ocr.json"
        if output.stat().st_size > 16 * 1024 * 1024:
            return
        results = json.loads(output.read_text(encoding="utf-8"))
    except (ImportError, OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return
    if isinstance(results, dict):
        for key, table in candidates.items():
            lines = results.get(key)
            if isinstance(lines, list):
                supplement_icons(table, lines)
                supplement_warnings(table, lines)


def read_scan_pages(pdf: Path, pages: list[int], executable: Path | None, home: Path | None):
    if not executable or not executable.is_file() or not home or not home.is_dir():
        return {}, "ocr_not_configured"
    system_keys = {"PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT", "PROGRAMFILES",
                   "PROGRAMFILES(X86)", "USERPROFILE", "LOCALAPPDATA", "APPDATA"}
    env = {key: value for key, value in os.environ.items() if key.upper() in system_keys}
    env.update(MINERU_HOME=str(home.resolve()), MINERU_MODEL_SOURCE="local", MINERU_MODEL_SMALL_BACKEND="onnx",
               MINERU_TELEMETRY_DISABLED="1", MINERU_CPU_NUM_THREADS="4", MINERU_INTRA_OP_NUM_THREADS="4",
               HF_HUB_OFFLINE="1", HTTP_PROXY="http://127.0.0.1:9", HTTPS_PROXY="http://127.0.0.1:9",
               ALL_PROXY="http://127.0.0.1:9")
    try:
        with TemporaryDirectory(prefix=".local-ocr-", dir=pdf.resolve().parent) as scratch:
            output = Path(scratch) / "middle.json"
            env.update(TEMP=scratch, TMP=scratch, TMPDIR=scratch)
            subprocess.run([str(executable.resolve()), "parse", str(pdf.resolve()), "-o", str(output),
                            "--format", "middle_json", "--tier", "basic", "--ocr-mode", "ocr",
                            "--pages", _page_range(pages)],
                           env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=900, check=True)
            if output.stat().st_size > 64 * 1024 * 1024:
                return {}, "ocr_result_too_large"
            payload = json.loads(output.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema") != "docvortex.middle" or not isinstance(payload["pages"], list):
                return {}, "ocr_result_invalid"
            requested = set(pages)
            result = {}
            seen = set()
            for page in payload["pages"]:
                page_index = page["page_idx"]
                if type(page_index) is not int or page_index + 1 not in requested or page_index in seen:
                    return {}, "ocr_result_invalid"
                seen.add(page_index)
                regions = [region for node in page["blocks"] if (region := _region(node))]
                if any(region.kind not in {"page_number", "footer", "image"} for region in regions):
                    result[page_index + 1] = regions
            _recheck_tables(pdf, result, executable, Path(scratch), env)
            for regions in result.values():
                for region in regions:
                    if region.kind == "table" and region.bbox and region.structure.get("rows"):
                        mark_graphic_keys(region)
        return result, None
    except subprocess.TimeoutExpired:
        return {}, "ocr_timeout"
    except (subprocess.CalledProcessError, OSError):
        return {}, "ocr_unavailable"
    except (AttributeError, KeyError, TypeError, ValueError):
        return {}, "ocr_result_invalid"
