"""Non-sensitive E inputs and an offline MinerU 4.2.3 evidence capture."""
from hashlib import sha256
import json
from pathlib import Path
import sys
import time
from zipfile import ZipFile

from .parsing.local_mineru import run_standard_bundle
from .parsing.local_pipeline import PROFILE


ROOT = Path(__file__).resolve().parents[2]
QUALITY_ROOT = ROOT / "outputs/pilot-af/2026-11-19/quality"
OLD_EVIDENCE = ROOT / "outputs/pdf-implementation/2026-11-19/standard-offline"
MINERU_HOME = OLD_EVIDENCE / "mineru-home"
MINERU = ROOT / "ai_models/mineru-venv/Scripts/mineru-kit.exe"
NATIVE_ZIP_SHA = "b12de9b818fd9c3a82673f67854c96591e2334fa1e34c6bd4feec642f8a24484"


def digest(path):
    return sha256(path.read_bytes()).hexdigest()


def write_json(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _line(page, x, y, text, size=12, *, bold=False, gray=0):
    page.insert_text((x, y), text, fontsize=size, fontname="hebo" if bold else "helv", color=(gray,) * 3)


def _training_panel():
    import pymupdf

    with pymupdf.open() as diagram:
        page = diagram.new_page(width=290, height=102)
        page.draw_rect(pymupdf.Rect(8, 9, 282, 93), width=4, color=(.12, .23, .4), fill=(.83, .89, .97))
        page.draw_rect(pymupdf.Rect(30, 30, 74, 72), width=3, color=(.12, .23, .4), fill=(.88, .32, .19))
        page.draw_circle(pymupdf.Point(141, 51), 18, width=3, color=(.12, .23, .4), fill=(.31, .66, .45))
        page.draw_rect(pymupdf.Rect(198, 30, 255, 72), width=3, color=(.12, .23, .4), fill=(.96, .84, .35))
        return page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).tobytes("png")


def _mixed(path):
    import pymupdf

    with pymupdf.open() as doc:
        page = doc.new_page(width=595, height=842)
        _line(page, 55, 62, "Synthetic Training Console", 23, bold=True)
        _line(page, 55, 87, "Maintenance Reference | Training revision E-MIXED")
        _line(page, 55, 126, "Maintenance checks", 16, bold=True)
        _line(page, 55, 153, "Step 1: Check the simulated ready indicator.")
        _line(page, 55, 180, "Warning for step 1: Keep the training shield closed.", bold=True)
        _line(page, 55, 225, "Table 1. Training inspection limits", 13, bold=True)
        xs, ys = (55, 215, 381, 540), (238, 278, 318, 358)
        page.draw_rect(pymupdf.Rect(xs[0], ys[0], xs[-1], ys[1]), color=None, fill=(.9, .94, .98))
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]), color=(.15, .21, .31), width=1)
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y), color=(.15, .21, .31), width=1)
        rows = (("Check", "Training value", "Condition"), ("Pressure", "5 Pa", "Demo mode"), ("Hold time", "8 s", "Indicator green"))
        for index, row in enumerate(rows):
            for x, value in zip(xs, row):
                _line(page, x + 12, ys[index] + 26, value, bold=index == 0)
        _line(page, 55, 381, "Table 1 note: Apply these limits only in demo mode.", 11)
        page.insert_image(pymupdf.Rect(105, 428, 489, 562), stream=_training_panel())
        _line(page, 121, 586, "Figure 1. Synthetic indicator panel", 11)
        _line(page, 278, 800, "M-12", 10)
        doc.set_page_labels([{"startpage": 0, "prefix": "M-", "style": "D", "firstpagenum": 12}])
        doc.save(path, deflate=True)


def _scan(path, *, revision="E-SCAN", number=37, note="Note: Apply this card only during a simulated exercise."):
    import pymupdf

    with pymupdf.open() as original, pymupdf.open() as scan:
        page = original.new_page(width=595, height=842)
        _line(page, 58, 65, "Synthetic Training Console", 23, bold=True)
        _line(page, 58, 87, f"Scanned Maintenance Card | Training revision {revision}")
        _line(page, 58, 137, "Service checks", 17, bold=True)
        _line(page, 58, 173, "Step 1: Check the simulated ready indicator.")
        _line(page, 58, 211, "Warning for step 1: Keep the training shield closed.", bold=True)
        _line(page, 58, 253, "Pressure: 5 Pa. Use the training setting only.")
        _line(page, 58, 290, "Correction check: The reference code is OI1Q0.", 11, gray=.47)
        _line(page, 58, 330, note, 11)
        _line(page, 275, 790, f"S-{number}", 10)
        # The page contains raster pixels only. Grayscale small glyphs exercise
        # source comparison without changing the bytes emitted by the OCR engine.
        pixmap = page.get_pixmap(dpi=118, colorspace=pymupdf.csGRAY, alpha=False)
        target = scan.new_page(width=595, height=842)
        target.insert_image(target.rect, stream=pixmap.tobytes("png"))
        scan.set_page_labels([{"startpage": 0, "prefix": "S-", "style": "D", "firstpagenum": number}])
        scan.save(path, deflate=True)


def capture(name):
    attempts = {"mixed": _mixed, "scan": _scan, "scan-positive": lambda path: _scan(
        path, revision="E-SCAN-POSITIVE", number=38, note="SYNTHETIC exercise only.")}
    if name not in attempts:
        raise ValueError("Expected mixed, scan or scan-positive")
    root = QUALITY_ROOT / "native" / name
    root.mkdir(parents=True, exist_ok=False)
    source = root / f"SYNTHETIC-E-{name.upper()}.pdf"
    attempts[name](source)
    start = time.monotonic()

    def heartbeat():
        if time.monotonic() - start > 180:
            raise TimeoutError("Synthetic offline E parse exceeded 180 seconds")

    receipt = {"source_sha256": digest(source), "profile": PROFILE,
               "offline_model_receipt_sha256": digest(OLD_EVIDENCE / "offline-model-verification.json"),
               "runner_sha256": digest(ROOT / "src/server/parsing/standard_runner.py"), "state": "failed"}
    try:
        bundle = run_standard_bundle(source, root, MINERU, MINERU_HOME, heartbeat)
        with ZipFile(bundle) as raw:
            assert raw.testzip() is None
            middle = json.loads(raw.read("middle_json.json"))
            receipt.update(state="passed", bundle_sha256=digest(bundle), producer=middle["metadata"]["producer"],
                           mineru=middle["extensions"]["mineru"], page_count=len(middle["pages"]),
                           member_names=raw.namelist())
    finally:
        receipt["elapsed_seconds"] = round(time.monotonic() - start, 2)
        write_json(root / "capture.json", receipt)


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Expected one of mixed, scan or scan-positive")
    capture(sys.argv[1])
