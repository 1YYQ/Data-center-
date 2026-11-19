"""Check project navigation, requirement coverage and task dependencies offline.

This checks document structure, never business approval or runtime correctness.
The only write operation is an explicitly requested traceability projection.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urlsplit

from src.memory.validate import validate as validate_memory


TASK = r"T\d{3}(?:-(?:PDF|PI)-\d{2})?"
FEATURE_CONFIG = ".specify/feature.json"
STATUS = "doc/planning/STATUS.md"


def without_fences(text: str) -> str:
    return re.sub(r"^(`{3,}|~{3,}).*?^\1[^\n]*$", "", text, flags=re.M | re.S)


def anchors(text: str) -> set[str]:
    counts: Counter[str] = Counter()
    result: set[str] = set()
    for title in re.findall(r"^#{1,6}\s+(.+?)\s*#*\s*$", without_fences(text), re.M):
        title = re.sub(r"\[([^]]+)\]\([^)]+\)", r"\1", title)
        slug = re.sub(r"[^\w\- ]", "", title.lower()).replace(" ", "-")
        count = counts[slug]
        counts[slug] += 1
        result.add(f"{slug}-{count}" if count else slug)
    result.update(re.findall(r'<(?:a|span)\s+id=["\']([^"\']+)', text))
    return result


def document_paths(root: Path) -> list[Path]:
    paths = [root / "README.md", root / "AGENTS.md"]
    for folder in ("doc", "specs", ".specify", ".verify"):
        paths.extend(p for p in (root / folder).rglob("*.md") if "example" not in p.parts)
    paths.extend((root / "src").rglob("README.md"))
    return sorted(p for p in set(paths) if "node_modules" not in p.parts and "dist" not in p.parts)


def check_links(root: Path, paths: list[Path]) -> tuple[list[str], list[str], int]:
    errors, warnings = [], []
    checked = 0
    for path in paths:
        if not path.is_file():
            errors.append(f"missing document: {path.relative_to(root).as_posix()}")
            continue
        text = without_fences(path.read_text(encoding="utf-8"))
        for target in re.findall(r"\[[^\]\n]*\]\(([^)\n]+)\)", text):
            target = target.strip().strip("<>")
            if urlsplit(target).scheme or target.startswith("//"):
                continue
            file_part, _, fragment = unquote(target).partition("#")
            resolved = (path.parent / file_part).resolve() if file_part else path
            origin = path.relative_to(root).as_posix()
            if not resolved.is_relative_to(root):
                errors.append(f"{origin}: link outside repository: {target}")
                continue
            relative = resolved.relative_to(root).as_posix()
            if relative.startswith(("outputs/", "data/", "ai_models/", "doc/example/")):
                warnings.append(f"{origin}: local evidence not certified by this check: {target}")
                continue
            checked += 1
            if not resolved.exists():
                errors.append(f"{origin}: missing target: {target}")
            elif fragment and resolved.is_file() and resolved.suffix == ".md":
                if fragment not in anchors(resolved.read_text(encoding="utf-8")):
                    errors.append(f"{origin}: missing heading: {target}")
    return errors, warnings, checked


def task_graph(text: str) -> tuple[dict[str, list[str]], list[str]]:
    declarations = re.findall(rf"^- \[[ xX]\]\s+(?:\*\*)?({TASK})(?=[ ｜\s])", text, re.M)
    errors = [f"duplicate task: {task}" for task, n in Counter(declarations).items() if n != 1]
    graph: dict[str, list[str]] = {}
    dependency_section = text.split("## Dependencies", 1)[-1].split("\n## ", 1)[0]
    for line in dependency_section.splitlines():
        match = re.match(r"\| (T\d{3}) \| ([^|]*) \|", line)
        if match:
            graph[match[1]] = re.findall(TASK, match[2])
    for match in re.finditer(rf"^- \[[ xX]\]\s+\*\*({TASK})[｜ ].*?(?=^- \[[ xX]\]|^#|\Z)", text, re.M | re.S):
        task, body = match[1], match[0]
        if "-PDF-" not in task and "-PI-" not in task:
            continue
        deps = re.search(r"^  - 直接依赖：([^\n]+)", body, re.M)
        if not deps:
            errors.append(f"{task}: missing explicit dependency field")
        else:
            graph[task] = re.findall(TASK, deps[1])
    for task in declarations:
        if task not in graph:
            errors.append(f"{task}: missing dependency definition")
    for task, deps in graph.items():
        if task not in declarations:
            errors.append(f"dependency row without task: {task}")
        for dep in deps:
            if dep not in declarations:
                errors.append(f"{task}: unknown dependency: {dep}")
    errors.extend(check_graph(graph))
    return graph, errors


def check_graph(graph: dict[str, list[str]]) -> list[str]:
    errors, visited, stack = [], set(), []

    def visit(task: str) -> None:
        if task in stack:
            errors.append("dependency cycle: " + " -> ".join(stack[stack.index(task):] + [task]))
            return
        if task in visited:
            return
        stack.append(task)
        for dep in graph.get(task, []):
            visit(dep)
        stack.pop()
        visited.add(task)

    for task in graph:
        visit(task)
    return errors


def relative_link(feature: Path, root: Path, path: str, label: str, heading: str = "") -> str:
    target = Path(os.path.relpath(root / path, feature)).as_posix()
    if heading:
        target += "#" + next(iter(anchors("# " + heading)))
    return f"[{label}]({target})"


def render_traceability(root: Path, feature: Path, data: dict) -> str:
    scenarios = {row["id"]: row for row in data["scenarios"]}
    api, database = root / "doc/api/接口设计.md", root / "doc/database/数据库设计.md"

    def section_link(path: Path, number: str, prefix: str) -> str:
        heading = next(title for title in re.findall(r"^#{1,6} (.+)$", path.read_text(encoding="utf-8"), re.M)
                       if re.match(re.escape(number) + r"(?:[. ]|$)", title))
        return relative_link(feature, root, path.relative_to(root).as_posix(), prefix + number, heading)

    def scenario_links(ids: list[str]) -> str:
        return "、".join(relative_link(feature, root, scenarios[i]["path"], i, scenarios[i].get("heading", "")) for i in ids)

    def evidence_links(ids: list[str]) -> str:
        return "、".join(f"[{i}](#{i.lower()})" for i in ids)

    def cell(value: str | list[str]) -> str:
        return ("；".join(value) if isinstance(value, list) else value).replace("|", "\\|").replace("\n", " ")

    lines = ["# 首期需求—任务—验收—证据映射", "",
             "本文件由 `python -m src.tools.project_docs --write-traceability` 生成；唯一映射源为[traceability.json](traceability.json)。请修改JSON后重生成。任务状态与直接依赖只在[tasks](tasks.md#dependencies)，入场规则在[baseline](baseline.md)，项目进度在[STATUS](../../doc/planning/STATUS.md)。", "",
             "EV关联包含静态记录、局部运行及明确缺口，不表示需求或整任务通过。下列证据摘要是带日期的历史索引；原运行报告和当前源码身份必须另行核对。忽略目录缺失时只能判证据未取得，不能借本表补成通过。", "",
             "## 功能需求映射", "",
             "| 需求与契约依据 | 任务 / 成功标准 | 验收场景 | 证据关联 | 关键断言 | 剩余证据或能力 |",
             "| --- | --- | --- | --- | --- | --- |"]
    for row in data["requirements"]:
        refs = [section_link(api, n, "API") for n in row["api_sections"]]
        refs += [section_link(database, n, "DB") for n in row["database_sections"]]
        columns = [row["id"] + " " + row["label"] + "；" + "、".join(refs),
                   "、".join(row["task_ids"]) + " / " + "、".join(row["criteria_ids"]),
                   scenario_links(row["scenario_ids"]), evidence_links(row["evidence_ids"]),
                   row["assertions"], row["remaining"]]
        lines.append("| " + " | ".join(cell(c) for c in columns) + " |")
    lines += ["", "## 成功标准与判定方法", "", "| 标准 | 任务 | 场景 / 证据 | 判定与缺口 |", "| --- | --- | --- | --- |"]
    for row in data["success_criteria"]:
        lines.append("| " + " | ".join(cell(c) for c in [row["id"] + " " + row["label"],
                     "、".join(row["task_ids"]), scenario_links(row["scenario_ids"]) + " / " + evidence_links(row["evidence_ids"]),
                     row["pass_condition"] + "；" + row["remaining"]]) + " |")
    lines += ["", "## 任务反向覆盖", "", "| 任务 | 需求 | 成功标准 |", "| --- | --- | --- |"]
    for task in (f"T{i:03}" for i in range(1, 35)):
        reqs = [r["id"] for r in data["requirements"] if task in r["task_ids"]]
        criteria = [r["id"] for r in data["success_criteria"] if task in r["task_ids"]]
        lines.append(f"| {task} | {'、'.join(reqs)} | {'、'.join(criteria) or '通过关联FR核对'} |")
    lines += ["", "PDF/Pi子任务沿用父任务FR/SC；身份、父任务、依赖与验收保留在tasks正文，检查器校验全部子任务及跨专项依赖，不另维护第二张状态表。", "", "## 证据目录与限制", "",
              f"历史盘点：{data['snapshot_date']}。详细运行身份见原[证据盘点](../../{data['authority']['evidence_inventory']})；以下为非敏感索引，不能替代本机原始证据或当前候选复验。"]
    for group in data["evidence_catalog"]:
        lines += ["", "### " + group["id"], "", group["title"] + "；证据类别 `" + group["evidence_level"] + "`。", "",
                  "支持范围：" + cell(group["supported_claims"]), "", "限制/缺口：" + cell(group["limits"]), "",
                  "来源：" + "、".join(relative_link(feature, root, ref["path"], Path(ref["path"]).name + "（" + ref["role"] + "）") for ref in group["source_refs"])]
        nodes = group.get("junit_testcases", [])
        if nodes:
            lines += ["", "历史节点示例：" + "；".join("`" + n + "`" for n in nodes[:3]) + "。"]
    return "\n".join(lines) + "\n"


def check_traceability(root: Path, feature: Path, data: dict, tasks: str) -> list[str]:
    errors = []
    for name in ("requirements", "success_criteria", "scenarios", "evidence_catalog"):
        duplicates = [key for key, n in Counter(row["id"] for row in data[name]).items() if n != 1]
        if duplicates:
            errors.append(f"{name}: duplicate IDs: {', '.join(duplicates)}")
    spec = (feature / "spec.md").read_text(encoding="utf-8")
    project_spec = (root / "doc/requirements/SPEC.md").read_text(encoding="utf-8")
    if re.search(r"^\| (?:FR|SC)-\d{3} \|", project_spec, re.M):
        errors.append("duplicate FR/SC definition table in project SPEC; feature spec owns these tables")
    all_tasks = set(re.findall(rf"^- \[[ xX]\]\s+(?:\*\*)?({TASK})(?=[ ｜\s])", tasks, re.M))
    requirements = {r["id"] for r in data["requirements"]}
    criteria = {r["id"] for r in data["success_criteria"]}
    scenarios = {r["id"] for r in data["scenarios"]}
    evidence = {r["id"] for r in data["evidence_catalog"]}
    for key, expected in (("FR", requirements), ("SC", criteria)):
        declared = re.findall(rf"^\| ({key}-\d{{3}}) \|", spec, re.M)
        if set(declared) != expected or len(declared) != len(expected):
            errors.append(f"{key} spec/mapping coverage mismatch")
    covered = set()
    for row in data["requirements"] + data["success_criteria"]:
        for field, valid in (("task_ids", all_tasks), ("criteria_ids", criteria), ("scenario_ids", scenarios), ("evidence_ids", evidence)):
            if set(row.get(field, [])) - valid:
                errors.append(f"{row['id']}: invalid {field}")
        covered.update(row["task_ids"])
        if not row.get("remaining"):
            errors.append(f"{row['id']}: missing evidence/remaining boundary")
    if not {f"T{i:03}" for i in range(1, 35)} <= covered:
        errors.append("base task coverage incomplete")
    for authority, path in data["authority"].items():
        if not path.startswith("outputs/") and not (root / path).is_file():
            errors.append(f"missing authority {authority}: {path}")
    for row in data["scenarios"]:
        if not (root / row["path"]).is_file():
            errors.append(f"missing scenario source: {row['id']}")
    return errors


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--write-traceability", action="store_true", help="regenerate the Markdown projection only")
    parser.add_argument("--output", type=Path, help="optional local JSON result")
    args = parser.parse_args(argv)
    root = args.root.resolve()
    errors, warnings, count, graph = [], [], 0, {}
    try:
        feature_dir = json.loads((root / FEATURE_CONFIG).read_text(encoding="utf-8"))["feature_directory"]
        feature = (root / feature_dir).resolve()
        if not feature.is_relative_to(root / "specs"):
            raise ValueError("feature_directory must be within specs/")
        tasks = (feature / "tasks.md").read_text(encoding="utf-8")
        data = json.loads((feature / "traceability.json").read_text(encoding="utf-8"))
        graph, task_errors = task_graph(tasks)
        errors.extend(task_errors)
        errors.extend(check_traceability(root, feature, data, tasks))
        projection = render_traceability(root, feature, data)
        if args.write_traceability and not errors:
            (feature / "traceability.md").write_text(projection, encoding="utf-8", newline="\n")
        elif (feature / "traceability.md").read_text(encoding="utf-8") != projection:
            errors.append("traceability.md is stale; regenerate from traceability.json")
        for entry in (root / "README.md", root / "doc/README.md", root / "AGENTS.md", feature / "README.md"):
            if "STATUS.md" not in entry.read_text(encoding="utf-8"):
                errors.append(f"{entry.relative_to(root)}: missing canonical status pointer")
        if not (root / STATUS).is_file():
            errors.append("missing canonical project status")
        link_errors, warnings, count = check_links(root, document_paths(root))
        errors.extend(link_errors)
        errors.extend(str(error) for error in validate_memory(root))
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        errors.append(f"invalid document source: {type(exc).__name__}: {exc}")
    report = {"status": "FAIL" if errors else "PASS", "scope": "offline document structure only; no runtime or business acceptance",
              "checked_local_links": count, "task_nodes": len(graph), "errors": errors,
              "uncertified_local_evidence_links": len(warnings), "warnings": warnings}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Project docs {report['status']}: {count} links, {len(graph)} task nodes, {len(errors)} errors; {len(warnings)} local evidence links require separate verification")
    for error in errors:
        print("- " + error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
