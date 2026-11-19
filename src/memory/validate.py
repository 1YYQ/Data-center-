"""Validate the repository's Markdown-based Harness Memory.

The validator intentionally checks only deterministic structure. It does not
decide whether a memory statement is semantically correct or business-valid.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


MEMORY_FILES = ("README.md", "context.md", "decisions.md", "state.md")
DECISION_STATUSES = {"当前", "被取代", "待复评"}
STATE_SECTIONS = {"已实现", "已验证", "未验证", "阻塞"}
LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)]+)\)")
DATE_PATTERN = re.compile(r"日期：\d{4}-\d{2}-\d{2}")


class ValidationError:
    def __init__(self, message: str, path: Path | None = None) -> None:
        self.message = message
        self.path = path

    def __str__(self) -> str:
        return f"{self.path}: {self.message}" if self.path else self.message


def _local_links(path: Path, text: str) -> list[tuple[str, Path]]:
    links: list[tuple[str, Path]] = []
    for target in LINK_PATTERN.findall(text):
        target = target.strip().split("#", 1)[0]
        if not target or "://" in target or target.startswith("mailto:"):
            continue
        links.append((target, (path.parent / target).resolve()))
    return links


def _section(text: str, heading: str) -> str | None:
    match = re.search(rf"^## {re.escape(heading)}\s*$", text, re.MULTILINE)
    if not match:
        return None
    rest = text[match.end() :]
    next_heading = re.search(r"^## ", rest, re.MULTILINE)
    return rest[: next_heading.start()] if next_heading else rest


def validate(root: Path) -> list[ValidationError]:
    errors: list[ValidationError] = []
    memory_root = root / "doc" / "memory"

    contents: dict[str, str] = {}
    for name in MEMORY_FILES:
        path = memory_root / name
        if not path.is_file():
            errors.append(ValidationError("必需文件不存在", path))
            continue
        contents[name] = path.read_text(encoding="utf-8")

        for target, resolved in _local_links(path, contents[name]):
            if not resolved.is_file():
                errors.append(ValidationError(f"本地链接目标不存在：{target}", path))

    readme = contents.get("README.md", "")
    for required in ("## 会话启动", "## 文件约定", "## 任务结束时沉淀"):
        if required not in readme:
            errors.append(ValidationError(f"缺少入口章节：{required}", memory_root / "README.md"))

    context = contents.get("context.md", "")
    if context and "**来源**：" not in context:
        errors.append(ValidationError("至少一个长期背景条目必须包含来源", memory_root / "context.md"))

    decisions_path = memory_root / "decisions.md"
    decisions = contents.get("decisions.md", "")
    decision_headings = re.findall(r"^## (D-\d+：.+)$", decisions, re.MULTILINE)
    if not decision_headings:
        errors.append(ValidationError("至少需要一个带编号的决策条目", decisions_path))
    for heading in decision_headings:
        body = _section(decisions, heading)
        if body is None:
            errors.append(ValidationError(f"无法读取决策条目：{heading}", decisions_path))
            continue
        status = re.search(r"^- 状态：([^\n]+)", body, re.MULTILINE)
        if not status or status.group(1).strip() not in DECISION_STATUSES:
            errors.append(ValidationError(f"决策状态非法：{heading}", decisions_path))
        for label in ("决策", "原因与取舍", "拒绝的替代方案", "依据", "失效或复评条件"):
            if f"- {label}：" not in body:
                errors.append(ValidationError(f"决策缺少字段 {label}：{heading}", decisions_path))

    state_path = memory_root / "state.md"
    state = contents.get("state.md", "")
    for section_name in STATE_SECTIONS:
        section = _section(state, section_name)
        if section is None:
            errors.append(ValidationError(f"缺少状态章节：{section_name}", state_path))
            continue
        if "依据：" not in section or "有效范围：" not in section or not DATE_PATTERN.search(section):
            errors.append(ValidationError(f"状态章节缺少依据、有效范围或日期：{section_name}", state_path))

    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate Harness Memory Markdown files")
    parser.add_argument("--root", type=Path, help="repository root; defaults to the current directory")
    args = parser.parse_args(argv)
    root = (args.root or Path.cwd()).resolve()
    errors = validate(root)
    if errors:
        print(f"Memory validation failed: {len(errors)} error(s)")
        for error in errors:
            print(f"- {error}")
        return 1
    print("Memory validation passed: 4 files, required fields, statuses, and local links checked")
    return 0


if __name__ == "__main__":
    sys.exit(main())
