"""Run the provisioned MinerU ONNX recognizer on private table raster crops."""

import json
import sys
from pathlib import Path


def _cross_key_window(engine, image, text, bounds):
    if text.count("面板键") != 1:
        return None
    x0, y0, x1, y1 = bounds
    height, width = image.shape[:2]
    step = (x1 - x0) / (len(text) + 1)
    if step < 2:
        return None
    at = text.index("面板键")
    top = max(0, int(y0 - (y1 - y0) * .05))
    bottom = min(height, int(y1 + (y1 - y0) * .11))
    windows = []
    for start, length in ((-1, 5.5), (0, 5.5), (0, 6.5)):
        left = max(0, int(x0 + (at + start) * step))
        right = min(width, int(x0 + (at + start + length) * step))
        if bottom > top and right > left:
            windows.append((left, right, image[top:bottom, left:right].copy()))
    if not windows:
        return None
    reads = engine.ocr([window[2] for window in windows], det=False)[0] or []
    matches = [(float(score), left, right) for (left, right, _), (label, score) in zip(windows, reads)
               if score >= .92 and "面板×键" in "".join(label.split())]
    if not matches:
        return None
    _, left, right = max(matches)
    return [left / width, top / height, right / width, bottom / height]


def main(directory: Path) -> None:
    # This command runs inside the configured MinerU environment.
    from docvortex.image import decode_image
    from mineru.model.ocr.pp_ocr_v6_onnx import PPOCRV6_DICT_PATH, PPOCRv6ONNX
    from mineru.model.registry import MINERU_4_MODELS_ONNX

    engine = PPOCRv6ONNX(det_model_path=str(MINERU_4_MODELS_ONNX.ocr_det.ensure()),
                        rec_model_path=str(MINERU_4_MODELS_ONNX.ocr_rec.ensure()),
                        dict_path=str(PPOCRV6_DICT_PATH), device="cpu")
    results = {}
    for crop in sorted(directory.glob("table-*.png")):
        image = decode_image(crop.read_bytes(), color=True, apply_orientation=True)
        height, width = image.shape[:2]
        boxes, recognized = engine(image)
        lines = []
        for box, (text, score) in zip(boxes if boxes is not None else [], recognized or []):
            x0, y0 = box.min(0)
            x1, y1 = box.max(0)
            line = {"text": text, "score": float(score), "box": [
                float(x0 / width), float(y0 / height), float(x1 / width), float(y1 / height)]}
            if score >= .90 and (key_box := _cross_key_window(engine, image, text, (x0, y0, x1, y1))):
                line["cross_key_box"] = key_box
            lines.append(line)
        results[crop.stem] = lines
    (directory / "crop-ocr.json").write_text(json.dumps(results, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main(Path(sys.argv[1]))
