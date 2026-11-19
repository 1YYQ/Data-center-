"""/verify 入口：python verify.py --task <task-id>."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))

from verify.engine import VerifyError, format_text, verify_task  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify a task from its .verify/tasks contract")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--task", help="任务 ID，匹配 .verify/tasks/<id>.json")
    group.add_argument("--config", help="任务契约 JSON 路径")
    parser.add_argument("--tasks-dir", default=".verify/tasks")
    parser.add_argument("--output", help="写入 JSON 证据报告的仓库内路径")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    parser.add_argument("--check-only", action="store_true", help="不执行命令，只检查契约可读性")
    args = parser.parse_args()
    config = args.config or str(Path(args.tasks_dir) / f"{args.task}.json")
    try:
        report = verify_task(ROOT, config, output=args.output, run_commands=not args.check_only)
    except VerifyError as exc:
        print(f"BLOCKED: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_text(report))
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
