"""执行 T-001 并把可复查证据保存到新的独立运行目录，不覆盖既有证据。"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import unittest
from uuid import uuid4

import calculate
from test_calculate import assert_matches_oracle, exact_oracle, rational_decimal, rational_display

BASE = Path(__file__).resolve().parent
SOURCE_NAMES = ("CASE.md", "input.json", "expected.json", "calculate.py", "test_calculate.py", "run_verification.py")
GROUPS = {
    "A": "正常三组与独立算式", "B": "80.00 cm / 全变量 cm 换算", "C": "交换 x1、x2",
    "D": "第二组缺少 x2，阻断该组及平均值", "E": "L=0、L<0、d=0、d>=L",
    "F": "单位缺失/不支持及非法数值", "G": "原始对象与文件不变", "H": "记录、字段、版本及复核状态",
    "I": "舍入边界及禁止提前舍入", "J": "变动输入、坐标平移及内核无文件读取", "K": "三类故障注入均应被识别",
}


def write_json(path, data):
    if path.exists():
        raise FileExistsError(f"不覆盖既有证据：{path}")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def execute(command, log_path):
    start = datetime.now(timezone.utc).isoformat()
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONDONTWRITEBYTECODE="1")
    completed = subprocess.run(command, cwd=BASE, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace", check=False)
    evidence = {"command": command, "command_windows": subprocess.list2cmdline(command), "cwd": str(BASE),
                "started_at_utc": start, "ended_at_utc": datetime.now(timezone.utc).isoformat(),
                "exit_code": completed.returncode, "stdout": completed.stdout, "stderr": completed.stderr}
    log_path.write_text(evidence["command_windows"] + "\n\nSTDOUT:\n" + completed.stdout + "\nSTDERR:\n" + completed.stderr, encoding="utf-8")
    return evidence


def main():
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_dir = BASE / "evidence" / f"run-{stamp}-{uuid4().hex[:8]}"
    run_dir.mkdir(parents=True, exist_ok=False)
    before = {name: digest(BASE / name) for name in SOURCE_NAMES}
    environment = {"timestamp_utc": datetime.now(timezone.utc).isoformat(),
                   "timestamp_shanghai": datetime.now(timezone(timedelta(hours=8))).isoformat(),
                   "python_executable": sys.executable, "python_version": sys.version,
                   "implementation": platform.python_implementation(), "platform": platform.platform(),
                   "cwd": str(BASE), "standard_library_only": True,
                   "subprocess_environment_overrides": {"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"},
                   "entry_command": [sys.executable, "-B", "run_verification.py"]}
    write_json(run_dir / "environment.json", environment)
    output_path, tests_path = run_dir / "result.json", run_dir / "tests.json"
    commands = [execute([sys.executable, "-B", "calculate.py", "--input", "input.json", "--output", str(output_path)], run_dir / "calculation-output.txt"),
                execute([sys.executable, "-B", "test_calculate.py", "--results", str(tests_path)], run_dir / "test-output.txt")]
    tests = json.loads(tests_path.read_text(encoding="utf-8")) if tests_path.exists() else {"tests_run": 0, "failures": 0, "errors": 1, "skipped": 0, "successful": False, "items": []}
    payload = json.loads((BASE / "input.json").read_text(encoding="utf-8"))
    result = json.loads(output_path.read_text(encoding="utf-8")) if output_path.exists() else {}
    oracle_rows, oracle_mean = exact_oracle(payload)
    independent = {"method": "Fraction: a=(L-d)/2, b=(L+d)/2, f=a*b/(a+b); 显示舍入采用整数整除与余数",
                   "nature": "模拟输入的代数核对，不是测量准确度核验", "status": "NOT_RUN",
                   "measurements": {mid: {"d_exact_fraction_mm": str(row["d"]), "f_exact_fraction_mm": str(row["f"]),
                                           "f_decimal_50": str(rational_decimal(row["f"])), "display_f_mm": rational_display(row["f"])} for mid, row in oracle_rows.items()},
                   "mean_exact_fraction_mm": str(oracle_mean), "mean_decimal_50": str(rational_decimal(oracle_mean)),
                   "display_mean_mm": rational_display(oracle_mean)}
    try:
        assert_matches_oracle(unittest.TestCase(), payload, result)
        independent["status"] = "PASS"
    except (AssertionError, KeyError, TypeError, ValueError) as exc:
        independent.update(status="FAIL", reason=str(exc))
    write_json(run_dir / "independent-check.json", independent)

    scenarios = []
    for name in ("B-L-as-cm", "C-swapped-x1-x2", "D-missing-second-x2"):
        case = deepcopy(payload)
        if name.startswith("B"):
            for row in case["measurements"]:
                row["inputs"]["L"] = {"value": "80.00", "unit": "cm"}
        elif name.startswith("C"):
            variables = case["measurements"][1]["inputs"]
            variables["x1"], variables["x2"] = variables["x2"], variables["x1"]
        else:
            del case["measurements"][1]["inputs"]["x2"]
        scenarios.append({"scenario": name, "input": case, "result": calculate.calculate_case(case)})
    write_json(run_dir / "scenario-results.json", scenarios)
    after = {name: digest(BASE / name) for name in SOURCE_NAMES}
    unchanged = before == after
    group_status = {}
    for key in GROUPS:
        matched = [item for item in tests["items"] if f".test_{key}_" in item["test"]]
        group_status[key] = "NOT_RUN" if not matched else ("PASS" if all(item["status"] == "PASS" for item in matched) else "FAIL")
    passed = (all(command["exit_code"] == 0 for command in commands) and tests["successful"] and independent["status"] == "PASS"
              and unchanged and all(value == "PASS" for value in group_status.values()))
    write_json(run_dir / "run.json", {"status": "PASS" if passed else "FAIL", "environment": environment, "commands": commands,
                                      "source_sha256_before": before, "source_sha256_after": after,
                                      "source_files_unchanged": unchanged, "check_groups": group_status})
    findings = """1. **精度与显示须分别保留。** 反例中逐组显示为 1.00、1.00、1.01 mm，正确平均显示 1.01 mm；先舍入再平均会变成 1.00 mm。单看最终位数不能证明计算路径正确。
2. **计算完成与测量有效、人工复核是不同状态。** 本实验仅检查数值必要条件，物理前提仍为模拟假设，人工复核人保持空值；真实使用时仍需明确确认依据及责任。
3. **记录身份和字段映射必须可核对。** 记录重排和错位注入用于检查按编号绑定；目前仅有模拟逻辑字段，尚无真实表格结构及覆盖已有值的业务规则。"""
    lines = ["# T-001 模拟场景验证报告", "", f"本次结论：**{'PASS' if passed else 'FAIL'}（仅限模拟计算与逻辑字段映射）**。",
             f"生成时间：{environment['timestamp_shanghai']}。人工构造材料：SIM-OPT-001 V1.0；执行版本：{calculate.ENGINE_VERSION}。", "",
             "## 实际执行", "", f"Python：{platform.python_version()}；解释器：`{sys.executable}`。仅使用标准库，未安装依赖。",
             "从实验目录复现：`python -B run_verification.py`。每次保存到新的 evidence/run-* 目录，保留既有运行。", "",
             "完整实际命令、时间、退出码及源码/输入哈希见 [run.json](run.json)；环境见 [environment.json](environment.json)。",
             "原始控制台输出：[calculation-output.txt](calculation-output.txt)、[test-output.txt](test-output.txt)。", "",
             f"测试方法数：{tests['tests_run']}；失败：{tests['failures']}；错误：{tests['errors']}；跳过：{tests['skipped']}。",
             f"源码及输入/预期文件运行前后 SHA256 一致：{unchanged}。", "", "| 检查 | 结果 |", "| --- | --- |"]
    lines += [f"| {key}：{description} | {group_status[key]} |" for key, description in GROUPS.items()]
    lines += ["", "三种故障注入通过表示对应的错误输出被断言拒绝，不表示程序曾输出这些错误，也不证明能识别所有可能缺陷。",
              "每项断言和子案例结果见 [tests.json](tests.json)；固定答案保存在 [expected.json](../../expected.json)。", "",
              "## 正常结果", "", "| 测量 | 目标字段 | d（mm） | f 计算值（mm） | f 显示值（mm） |", "| --- | --- | --- | --- | --- |"]
    for record in result.get("records", []):
        if record["status"] == "SUCCESS":
            fields = record["fields"]
            lines.append(f"| {record['measurement_id']} | {record['target_record_id']}.f_mm | {fields['d_mm']['calculated']} | {fields['f_mm']['calculated']} | {fields['f_mm']['display']} |")
    displayed_mean = result.get("summary", {}).get("fields", {}).get("mean_f_mm", {}).get("display", "未生成")
    lines += ["", f"三组平均焦距显示：**{displayed_mean} mm**。完整结构化结果见 [result.json](result.json)。",
              f"精确分数核对的平均值为 {oracle_mean} mm，50 位十进制表示为 {rational_decimal(oracle_mean)} mm；独立核验状态：{independent['status']}。",
              "独立代入记录见 [independent-check.json](independent-check.json)。这些值不是测量真值。",
              "换算、交换位置及缺失第二组 x2 的输入与结果另存于 [scenario-results.json](scenario-results.json)。缺项案例不得生成第二组焦距或三组平均。", "",
              "## 失败与未运行", ""]
    failures = [item for item in tests["items"] if item["status"] != "PASS"]
    if failures:
        lines += [f"- {item['test']}：{item['status']}，{item.get('reason', '')}" for item in failures]
    else:
        lines.append("本次测试记录中没有失败或错误；各命令实际退出码及异常原因以 run.json 为准。")
    for command in commands:
        if command["exit_code"] != 0:
            lines.append(f"命令失败（退出码 {command['exit_code']}）：`{command['command_windows']}`；原因见对应原始输出。")
    if independent["status"] != "PASS":
        lines.append(f"独立核验失败：{independent.get('reason', '未执行')}。")
    if not unchanged:
        lines.append("源码或输入哈希发生变化，需要核对 run.json；本次不判通过。")
    lines += ["", "最初两次执行因测试程序两处列表推导式缺少右方括号而在导入阶段失败，当时测试未运行。已修正并重新执行；原始失败记录见 [initial-run-failure.txt](../initial-run-failure.txt)。",
              "", "**NOT_RUN**：真实企业流程；真实仪器测量准确度及物理前提；PDF/Word 图片识别、OCR、RAG；真实 Excel 单元格填写与重算；员工效率评估。",
              "本次结果不能用于确认正式计算与填表进入首期，也不能作为业务验收完成的证明。", "", "## 对后续调查的三项发现", "", findings,
              "", "## 下一次真实材料验证仍缺少什么", "",
              "- 获准使用的真实手册、标识/有效版本、公式所在实际位置，以及变量、单位、适用条件、警告和业务舍入规则。",
              "- 获准的真实输入、操作者对物理前提的确认，以及业务人员独立核算的参考结果和允许误差；本实验没有测量不确定度评估。",
              "- 真实记录表及字段/单元格映射、必填与保护规则、已有值覆盖规则、人工复核责任和验收标准。", ""]
    (run_dir / "REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    write_json(run_dir / "artifact-sha256.json", {path.name: digest(path) for path in sorted(run_dir.iterdir()) if path.is_file()})
    print(f"{'PASS' if passed else 'FAIL'}: tests={tests['tests_run']}, failures={tests['failures']}, errors={tests['errors']}, skipped={tests['skipped']}")
    print(f"REPORT: {run_dir / 'REPORT.md'}")
    for command in commands:
        if command["exit_code"]:
            print(command["stdout"])
            print(command["stderr"])
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
