"""T-001 的独立算式、变动输入、异常与故障注入测试。"""

import argparse
from copy import deepcopy
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from fractions import Fraction
import hashlib
import json
from pathlib import Path
import random
import sys
import unittest
from unittest.mock import patch

import calculate

BASE = Path(__file__).resolve().parent
RECORD_MAP = {"M-001": "R-001", "M-002": "R-002", "M-003": "R-003"}


def exact_oracle(payload):
    """精确分数等价式；不调用计算程序的公式或换算函数。"""
    rows = {}
    for row in payload["measurements"]:
        numbers = {}
        for name in ("L", "x1", "x2"):
            raw = row["inputs"][name]
            factor = {"mm": 1, "cm": 10}[raw["unit"]]
            numbers[name] = Fraction(raw["value"]) * factor
        distance = abs(numbers["x2"] - numbers["x1"])
        a = (numbers["L"] - distance) / 2
        b = (numbers["L"] + distance) / 2
        focal = a * b / (a + b)
        rows[row["measurement_id"]] = {"normalized": numbers, "d": distance, "f": focal}
    return rows, sum((row["f"] for row in rows.values()), Fraction(0)) / 3


def rational_display(value):
    """用整数整除和余数实现两位四舍五入，独立于 Decimal.quantize。"""
    sign = "-" if value < 0 else ""
    cents, remainder = divmod(abs(value.numerator) * 100, value.denominator)
    if remainder * 2 >= value.denominator:
        cents += 1
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def rational_decimal(value):
    with localcontext(Context(prec=50, rounding=ROUND_HALF_EVEN)):
        return Decimal(value.numerator) / Decimal(value.denominator)


def assert_matches_oracle(check, payload, result):
    expected, mean = exact_oracle(payload)
    check.assertEqual(result["status"], "SUCCESS")
    check.assertEqual(len(result["records"]), 3)
    check.assertEqual({r["measurement_id"] for r in result["records"]}, set(expected))
    for record in result["records"]:
        mid = record["measurement_id"]
        check.assertEqual(record["target_record_id"], RECORD_MAP[mid])
        for field, key, formula in (("d_mm", "d", "F-01"), ("f_mm", "f", "F-02")):
            actual = record["fields"][field]
            check.assertEqual(Decimal(actual["calculated"]), rational_decimal(expected[mid][key]))
            check.assertEqual(actual["target"], {"record_id": RECORD_MAP[mid], "field": field})
            check.assertEqual(actual["formula_id"], formula)
            check.assertEqual(actual["unit"], "mm")
        check.assertEqual(record["fields"]["f_mm"]["display"], rational_display(expected[mid]["f"]))
        for name, value in expected[mid]["normalized"].items():
            check.assertEqual(Fraction(record["normalized_inputs"][name]["value"]), value)
    aggregate = result["summary"]["fields"]["mean_f_mm"]
    check.assertEqual(Decimal(aggregate["calculated"]), rational_decimal(mean))
    check.assertEqual(aggregate["display"], rational_display(mean))
    check.assertEqual(aggregate["target"], {"record_id": "SUMMARY-001", "field": "mean_f_mm"})
    check.assertEqual(aggregate["formula_id"], "F-03")


def make_half_distance_case(lengths):
    payload = json.loads((BASE / "input.json").read_text(encoding="utf-8"))
    for row, length in zip(payload["measurements"], lengths):
        with localcontext(Context(prec=50)):
            half = Decimal(length) / Decimal("2")
        row["inputs"] = {"L": {"value": length, "unit": "mm"},
                         "x1": {"value": "0", "unit": "mm"},
                         "x2": {"value": str(half), "unit": "mm"}}
    return payload


class OpticsTests(unittest.TestCase):
    def setUp(self):
        self.payload = json.loads((BASE / "input.json").read_text(encoding="utf-8"))

    def test_A_reference_answers(self):
        expected = json.loads((BASE / "expected.json").read_text(encoding="utf-8"))
        result = calculate.calculate_case(self.payload)
        for row in result["records"]:
            ref = expected["measurements"][row["measurement_id"]]
            self.assertEqual(Decimal(row["fields"]["d_mm"]["calculated"]), Decimal(ref["d"]))
            self.assertEqual(Decimal(row["fields"]["f_mm"]["calculated"]), Decimal(ref["f"]))
            self.assertEqual(row["fields"]["f_mm"]["display"], ref["display_f"])
        self.assertEqual(result["summary"]["fields"]["mean_f_mm"]["display"], expected["display_mean_f"])

    def test_A_independent_fraction_substitution(self):
        assert_matches_oracle(self, self.payload, calculate.calculate_case(self.payload))

    def test_B_length_in_cm_and_all_variables_in_cm(self):
        for variables in (("L",), ("L", "x1", "x2")):
            with self.subTest(variables=variables):
                case = deepcopy(self.payload)
                for row in case["measurements"]:
                    for name in variables:
                        original = row["inputs"][name]["value"]
                        value = "80.00" if name == "L" else str(Decimal(original) / Decimal("10"))
                        row["inputs"][name] = {"value": value, "unit": "cm"}
                result = calculate.calculate_case(case)
                assert_matches_oracle(self, case, result)
                baseline = calculate.calculate_case(self.payload)
                for actual, ref in zip(result["records"], baseline["records"]):
                    self.assertEqual(Decimal(actual["fields"]["f_mm"]["calculated"]), Decimal(ref["fields"]["f_mm"]["calculated"]))

    def test_C_swap_coordinates(self):
        case = deepcopy(self.payload)
        inputs = case["measurements"][1]["inputs"]
        inputs["x1"], inputs["x2"] = inputs["x2"], inputs["x1"]
        assert_matches_oracle(self, case, calculate.calculate_case(case))

    def test_D_missing_second_x2_blocks_group_and_mean(self):
        del self.payload["measurements"][1]["inputs"]["x2"]
        result = calculate.calculate_case(self.payload)
        self.assertEqual(result["records"][1]["status"], "MISSING_INPUT")
        self.assertEqual(result["records"][1]["fields"], {})
        self.assertNotIn("x2", result["records"][1]["raw_inputs"])
        self.assertEqual([r["status"] for r in result["records"]], ["SUCCESS", "MISSING_INPUT", "SUCCESS"])
        self.assertEqual(result["summary"]["status"], "INCOMPLETE_DATA")
        self.assertEqual(result["summary"]["reason"], "数据不完整")
        self.assertEqual(result["summary"]["fields"], {})

    def test_E_invalid_geometry(self):
        for length, first, second in (("0", "1", "2"), ("-1", "1", "2"),
                                      ("800", "117", "117"), ("800", "0", "800"), ("800", "0", "801")):
            with self.subTest(L=length, x1=first, x2=second):
                case = deepcopy(self.payload)
                for name, value in zip(("L", "x1", "x2"), (length, first, second)):
                    case["measurements"][0]["inputs"][name]["value"] = value
                result = calculate.calculate_case(case)
                self.assertEqual(result["records"][0]["status"], "INVALID_INPUT")
                self.assertIn("INVALID_GEOMETRY", [e["code"] for e in result["records"][0]["errors"]])
                self.assertEqual(result["records"][0]["fields"], {})
                self.assertEqual(result["summary"]["fields"], {})

    def test_F_missing_or_unsupported_units(self):
        for unit, status, code in ((None, "MISSING_INPUT", "MISSING_INPUT"), ("", "MISSING_INPUT", "MISSING_INPUT"),
                                   ("m", "INVALID_INPUT", "UNSUPPORTED_UNIT"), ("inch", "INVALID_INPUT", "UNSUPPORTED_UNIT")):
            with self.subTest(unit=unit):
                case = deepcopy(self.payload)
                raw = case["measurements"][0]["inputs"]["L"]
                if unit is None:
                    del raw["unit"]
                else:
                    raw["unit"] = unit
                result = calculate.calculate_case(case)
                row = result["records"][0]
                self.assertEqual(row["status"], status)
                self.assertIn(code, [e["code"] for e in row["errors"]])
                self.assertEqual(row["fields"], {})
                self.assertEqual(result["summary"]["fields"], {})

    def test_F_missing_malformed_or_nonfinite_values(self):
        for value in (None, "", "  ", "NaN", "sNaN", "Infinity", "-Infinity", "abc", 800, True):
            with self.subTest(value=value):
                case = deepcopy(self.payload)
                case["measurements"][0]["inputs"]["L"]["value"] = value
                result = calculate.calculate_case(case)
                row = result["records"][0]
                self.assertIn(row["status"], ("MISSING_INPUT", "INVALID_INPUT"))
                self.assertTrue(row["errors"])
                self.assertEqual(row["fields"], {})
                self.assertEqual(result["summary"]["fields"], {})

    def test_G_raw_input_object_and_file_unchanged(self):
        path = BASE / "input.json"
        before_bytes = hashlib.sha256(path.read_bytes()).hexdigest()
        for missing in (False, True):
            with self.subTest(missing=missing):
                case = deepcopy(self.payload)
                if missing:
                    del case["measurements"][1]["inputs"]["x2"]
                original = deepcopy(case)
                result = calculate.calculate_case(case)
                self.assertEqual(case, original)
                for actual, raw in zip(result["records"], original["measurements"]):
                    self.assertEqual(actual["raw_inputs"], raw["inputs"])
                result["records"][0]["raw_inputs"]["L"]["value"] = "changed copy"
                self.assertEqual(case, original)
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), before_bytes)

    def test_H_reordered_records_keep_identity_and_targets(self):
        self.payload["measurements"].reverse()
        result = calculate.calculate_case(self.payload)
        assert_matches_oracle(self, self.payload, result)
        for row in result["records"]:
            self.assertEqual(row["procedure"], {"id": "SIM-OPT-001", "version": "V1.0"})
            self.assertEqual(row["manual_review"], {"status": "PENDING", "reviewer": None})
        self.assertNotIn("合格", json.dumps(result, ensure_ascii=False))
        self.assertFalse(result["preconditions"]["physically_verified"])

    def test_H_duplicate_unknown_or_missing_record_rejected(self):
        for kind in ("duplicate", "unknown", "missing"):
            with self.subTest(kind=kind):
                case = deepcopy(self.payload)
                if kind == "duplicate":
                    case["measurements"][1]["measurement_id"] = "M-001"
                elif kind == "unknown":
                    case["measurements"][1]["measurement_id"] = "OTHER"
                else:
                    case["measurements"].pop()
                with self.assertRaises(calculate.ContractError):
                    calculate.calculate_case(case)

    def test_I_rounding_below_at_and_above_half(self):
        for value, expected in (("1.0049", "1.00"), ("1.005", "1.01"), ("1.0051", "1.01"),
                                ("100.105", "100.11"), ("-1.005", "-1.01"), ("0.005", "0.01")):
            with self.subTest(value=value):
                self.assertEqual(calculate.display_mm(Decimal(value)), expected)
                self.assertEqual(calculate.display_mm(Decimal(value)), rational_display(Fraction(value)))

    def test_I_actual_focal_and_mean_at_half(self):
        case = make_half_distance_case(["5.36", "5.36", "5.36"])
        result = calculate.calculate_case(case)
        assert_matches_oracle(self, case, result)
        self.assertEqual(result["records"][0]["fields"]["f_mm"]["calculated"], "1.005")
        self.assertEqual(result["summary"]["fields"]["mean_f_mm"]["display"], "1.01")

    def test_I_average_uses_unrounded_focal_values(self):
        case = make_half_distance_case(["5.344", "5.344", "5.392"])
        result = calculate.calculate_case(case)
        assert_matches_oracle(self, case, result)
        self.assertEqual([r["fields"]["f_mm"]["display"] for r in result["records"]], ["1.00", "1.00", "1.01"])
        self.assertEqual(result["summary"]["fields"]["mean_f_mm"]["display"], "1.01")

    def test_J_varied_inputs_against_exact_oracle(self):
        generator = random.Random(1001)
        for batch in range(12):
            with self.subTest(batch=batch):
                case = deepcopy(self.payload)
                for row in case["measurements"]:
                    length = generator.choice([512, 640, 800, 1024, 1280])
                    first = Decimal(generator.randint(-900, 900)) / Decimal("10")
                    distance = Decimal(generator.randint(1, length * 10 - 1)) / Decimal("10")
                    row["inputs"] = {name: {"value": str(value), "unit": "mm"} for name, value in
                                     (("L", length), ("x1", first), ("x2", first + distance))}
                assert_matches_oracle(self, case, calculate.calculate_case(case))

    def test_J_translated_coordinates_preserve_results(self):
        for row in self.payload["measurements"]:
            for name in ("x1", "x2"):
                raw = row["inputs"][name]
                raw["value"] = str(Decimal(raw["value"]) - Decimal("1000"))
        result = calculate.calculate_case(self.payload)
        assert_matches_oracle(self, self.payload, result)
        self.assertEqual(result["summary"]["fields"]["mean_f_mm"]["display"], "100.03")

    def test_J_kernel_does_not_read_any_file(self):
        with patch("builtins.open", side_effect=AssertionError("内核不应读文件")), \
             patch.object(Path, "open", side_effect=AssertionError("内核不应读文件")):
            result = calculate.calculate_case(self.payload)
        assert_matches_oracle(self, self.payload, result)

    def test_J_mismatched_procedure_rejected(self):
        self.payload["procedure"]["version"] = "V2.0"
        with self.assertRaises(calculate.ContractError):
            calculate.calculate_case(self.payload)

    def test_K_detect_hardcoded_results(self):
        frozen = calculate.calculate_case(self.payload)
        changed = deepcopy(self.payload)
        changed["measurements"][0]["inputs"]["x2"]["value"] = "680.1"
        with self.assertRaises(AssertionError):
            assert_matches_oracle(self, changed, frozen)

    def test_K_detect_premature_rounding(self):
        case = make_half_distance_case(["5.344", "5.344", "5.392"])
        wrong = calculate.calculate_case(case)
        mean_of_display = sum((Fraction(r["fields"]["f_mm"]["display"]) for r in wrong["records"]), Fraction(0)) / 3
        wrong["summary"]["fields"]["mean_f_mm"]["display"] = rational_display(mean_of_display)
        with self.assertRaises(AssertionError):
            assert_matches_oracle(self, case, wrong)

    def test_K_detect_misaligned_record_fields(self):
        wrong = calculate.calculate_case(self.payload)
        wrong["records"][0]["fields"], wrong["records"][1]["fields"] = wrong["records"][1]["fields"], wrong["records"][0]["fields"]
        with self.assertRaises(AssertionError):
            assert_matches_oracle(self, self.payload, wrong)


class RecordingResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.items = []

    def addSuccess(self, test):
        super().addSuccess(test)
        self.items.append({"test": test.id(), "status": "PASS"})

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self.items.append({"test": test.id(), "status": "FAIL", "reason": self._exc_info_to_string(err, test)})

    def addError(self, test, err):
        super().addError(test, err)
        self.items.append({"test": test.id(), "status": "ERROR", "reason": self._exc_info_to_string(err, test)})

    def addSubTest(self, test, subtest, err):
        super().addSubTest(test, subtest, err)
        item = {"test": subtest.id(), "status": "PASS" if err is None else "FAIL"}
        if err:
            item["reason"] = self._exc_info_to_string(err, test)
        self.items.append(item)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True)
    args = parser.parse_args()
    target = Path(args.results).resolve()
    if not target.is_relative_to(BASE) or target.exists():
        parser.error("测试结果须为实验目录中的新文件")
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OpticsTests)
    result = unittest.TextTestRunner(verbosity=2, resultclass=RecordingResult).run(suite)
    data = {"tests_run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors),
            "skipped": len(result.skipped), "successful": result.wasSuccessful(), "items": result.items}
    target.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
