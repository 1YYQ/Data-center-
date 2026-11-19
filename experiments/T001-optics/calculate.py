"""T-001 模拟计算；仅使用标准库，计算内核不进行文件读取。"""

import argparse
from copy import deepcopy
from decimal import Context, Decimal, DecimalException, ROUND_HALF_EVEN, ROUND_HALF_UP, localcontext
import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
ENGINE_VERSION = "T001-1.0"
PRECISION = 50
PROCEDURE = {"id": "SIM-OPT-001", "version": "V1.0"}
TARGETS = {"M-001": "R-001", "M-002": "R-002", "M-003": "R-003"}
FACTORS = {"mm": Decimal("1"), "cm": Decimal("10")}


class ContractError(ValueError):
    """无法安全映射到本实验三条记录的请求。"""


def display_mm(value):
    """只在最终显示处量化；不接受 float。"""
    with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)):
        return format(value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP), ".2f")


def error(field, code, reason):
    return {"field": field, "code": code, "reason": reason}


def normalize(inputs):
    normalized, errors = {}, []
    if not isinstance(inputs, dict):
        return {}, [error("inputs", "INVALID_STRUCTURE", "inputs 必须为变量对象")]
    for name in ("L", "x1", "x2"):
        raw = inputs.get(name)
        if raw is None:
            errors.append(error(name, "MISSING_INPUT", "缺少必要变量"))
            continue
        if not isinstance(raw, dict):
            errors.append(error(name, "INVALID_STRUCTURE", "变量必须包含 value 和 unit"))
            continue
        value, unit = raw.get("value"), raw.get("unit")
        before = len(errors)
        for part, item in (("value", value), ("unit", unit)):
            if item is None or (isinstance(item, str) and not item.strip()):
                errors.append(error(f"{name}.{part}", "MISSING_INPUT", f"缺少 {part}，不得猜测或填零"))
        if len(errors) != before:
            continue
        if not isinstance(value, str):
            errors.append(error(f"{name}.value", "INVALID_NUMBER", "数值必须为十进制字符串"))
            continue
        if not isinstance(unit, str) or unit not in FACTORS:
            errors.append(error(f"{name}.unit", "UNSUPPORTED_UNIT", "仅支持 mm、cm"))
            continue
        try:
            number = Decimal(value)
            if not number.is_finite():
                errors.append(error(f"{name}.value", "NON_FINITE", "数值必须有限"))
                continue
            converted = number * FACTORS[unit]
            if not converted.is_finite():
                raise ArithmeticError("换算结果必须有限")
            normalized[name] = {
                "value": str(converted), "unit": "mm",
                "source_value": value, "source_unit": unit,
                "factor": str(FACTORS[unit]),
                "conversion": f"{value} {unit} * {FACTORS[unit]} = {converted} mm",
            }
        except (DecimalException, ArithmeticError, ValueError) as exc:
            errors.append(error(f"{name}.value", "INVALID_NUMBER", f"十进制解析或换算失败：{type(exc).__name__}"))
    return normalized, errors


def field_result(record_id, name, formula_id, value, source_variables, display=False):
    result = {
        "formula_id": formula_id, "calculated": str(value), "unit": "mm",
        "source_variables": source_variables,
        "target": {"record_id": record_id, "field": name},
    }
    if display:
        result["display"] = display_mm(value)
    return result


def calculate_measurement(measurement):
    mid = measurement["measurement_id"]
    record_id = TARGETS[mid]
    result = {
        "measurement_id": mid, "target_record_id": record_id,
        "procedure": deepcopy(PROCEDURE), "source_locator": "CASE.md / §3",
        "raw_inputs": deepcopy(measurement.get("inputs", {})),
        "normalized_inputs": {}, "fields": {}, "status": "SUCCESS", "errors": [],
        "manual_review": {"status": "PENDING", "reviewer": None},
    }
    with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)):
        normalized, errors = normalize(result["raw_inputs"])
        result["normalized_inputs"] = normalized
        result["errors"] = errors
        if not errors:
            try:
                length, first, second = (Decimal(normalized[name]["value"]) for name in ("L", "x1", "x2"))
                distance = abs(second - first)
                if length <= 0:
                    errors.append(error("L", "INVALID_GEOMETRY", "要求 L > 0"))
                if distance <= 0 or distance >= length:
                    errors.append(error("d", "INVALID_GEOMETRY", "要求 0 < d < L"))
                if not errors:
                    focal = (length * length - distance * distance) / (Decimal("4") * length)
                    fields = {
                        "d_mm": field_result(record_id, "d_mm", "F-01", distance, ["x1", "x2"]),
                        "f_mm": field_result(record_id, "f_mm", "F-02", focal, ["L", "d"], display=True),
                    }
                    result["fields"] = fields
            except (DecimalException, ArithmeticError) as exc:
                errors.append(error("calculation", "NUMERIC_ERROR", f"十进制运算失败：{type(exc).__name__}"))
        if errors:
            result["status"] = "MISSING_INPUT" if all(e["code"] == "MISSING_INPUT" for e in errors) else "INVALID_INPUT"
    return result


def calculate_case(payload):
    """纯函数：不读取任何文件，不覆盖调用方输入。"""
    if not isinstance(payload, dict):
        raise ContractError("顶层必须是对象")
    if payload.get("case_id") != "T-001" or payload.get("material_kind") != "SIMULATED":
        raise ContractError("仅允许 T-001 模拟材料")
    if payload.get("procedure") != PROCEDURE:
        raise ContractError("规程标识或版本不匹配 SIM-OPT-001 V1.0")
    measurements = payload.get("measurements")
    if not isinstance(measurements, list) or len(measurements) != 3:
        raise ContractError("必须有三组唯一的测量记录")
    ids = [m.get("measurement_id") if isinstance(m, dict) else None for m in measurements]
    if any(not isinstance(mid, str) for mid in ids) or sorted(ids) != sorted(TARGETS):
        raise ContractError("测量编号必须恰好为 M-001、M-002、M-003，不得重复或缺失")
    records = [calculate_measurement(m) for m in measurements]
    indexed = {r["measurement_id"]: r for r in records}
    summary = {
        "target_record_id": "SUMMARY-001", "procedure": deepcopy(PROCEDURE),
        "source_locator": "CASE.md / §3 / F-03", "formula_id": "F-03",
        "source_measurements": list(TARGETS), "source_values_mm": {},
        "status": "INCOMPLETE_DATA", "reason": "数据不完整", "fields": {},
        "manual_review": {"status": "PENDING", "reviewer": None},
    }
    if all(r["status"] == "SUCCESS" for r in records):
        summary["source_values_mm"] = {mid: indexed[mid]["fields"]["f_mm"]["calculated"] for mid in TARGETS}
        try:
            with localcontext(Context(prec=PRECISION, rounding=ROUND_HALF_EVEN)):
                mean = sum((Decimal(value) for value in summary["source_values_mm"].values()), Decimal("0")) / Decimal("3")
                mean_field = field_result("SUMMARY-001", "mean_f_mm", "F-03", mean, list(TARGETS), display=True)
                summary.update(status="SUCCESS", reason=None, fields={"mean_f_mm": mean_field})
        except DecimalException as exc:
            summary["reason"] = f"数据不完整：汇总十进制运算失败（{type(exc).__name__}）"
    return {
        "case_id": "T-001", "material_kind": "SIMULATED",
        "engine_version": ENGINE_VERSION,
        "numeric_policy": {"decimal_precision": PRECISION, "intermediate_rounding": "ROUND_HALF_EVEN", "display_rounding": "ROUND_HALF_UP", "display_places": 2},
        "procedure": deepcopy(PROCEDURE),
        "preconditions": {"status": "ASSUMED_FOR_SIMULATION", "physically_verified": False,
                          "items": ["薄凸透镜、近轴成像", "存在两个不同的清晰成像位置", "同组使用相同坐标系和读数基准"]},
        "status": summary["status"], "records": records, "summary": summary,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source, target = Path(args.input).resolve(), Path(args.output).resolve()
    if not source.is_relative_to(BASE) or not target.is_relative_to(BASE) or source == target or target.exists():
        parser.error("输入输出须在实验目录内；输出须为新文件且不能覆盖输入")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
        result = calculate_case(payload)
        code = 0 if result["status"] == "SUCCESS" else 2
    except (ContractError, OSError, ValueError) as exc:
        result, code = {"case_id": "T-001", "status": "INVALID_INPUT", "reason": str(exc)}, 2
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"{result['status']}: {target}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
