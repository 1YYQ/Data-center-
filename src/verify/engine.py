"""Small, project-adaptive verification engine.

The engine owns the verification protocol, not project test commands. Commands
and evidence rules live in a task contract under ``.verify/tasks``.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


class VerifyError(ValueError):
    """Invalid task contract or unsafe path."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _repo_path(repo: Path, value: str | os.PathLike[str]) -> Path:
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else repo / candidate).resolve()
    try:
        resolved.relative_to(repo.resolve())
    except ValueError as exc:
        raise VerifyError(f"路径超出仓库范围：{value}") from exc
    return resolved


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise VerifyError(f"无法读取 JSON：{path}：{exc}") from exc


def _get_path(value: Any, dotted: str) -> Any:
    current = value
    parts = dotted.split(".")
    while parts:
        if isinstance(current, dict) and ".".join(parts) in current:
            return current[".".join(parts)]
        part = parts.pop(0)
        if isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            raise KeyError(dotted)
    return current


def _replace(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        return value.format_map(variables)
    if isinstance(value, list):
        return [_replace(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: _replace(item, variables) for key, item in value.items()}
    return value


def _git_changes(repo: Path) -> list[str]:
    names: set[str] = set()
    for command in (["git", "diff", "--name-only"], ["git", "diff", "--cached", "--name-only"]):
        try:
            completed = subprocess.run(command, cwd=repo, capture_output=True, text=True, check=False)
        except OSError:
            continue
        if completed.returncode == 0:
            names.update(line.strip() for line in completed.stdout.splitlines() if line.strip())
    try:
        status = subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True, check=False)
        if status.returncode == 0:
            for line in status.stdout.splitlines():
                if len(line) > 3:
                    names.add(line[3:].strip().strip('"'))
    except OSError:
        pass
    return sorted(names)


def _scope(repo: Path, config: dict[str, Any]) -> dict[str, Any]:
    paths = [str(item).replace("\\", "/").rstrip("/") for item in config.get("scope", {}).get("paths", [])]
    changed = _git_changes(repo)
    relevant = [name for name in changed if any(name == path or name.startswith(path + "/") for path in paths)]
    return {"declared_paths": paths, "changed_files": changed, "relevant_files": relevant,
            "status": "RELEVANT" if relevant else "NO_MATCHING_CHANGE"}


def _sources(repo: Path, config: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    source_groups = config.get("sources", {})
    if not isinstance(source_groups, dict):
        raise VerifyError("sources 必须是对象")
    for role, values in source_groups.items():
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list):
            raise VerifyError(f"sources.{role} 必须是字符串列表")
        for value in values:
            path = _repo_path(repo, value)
            item = {"role": role, "path": str(path.relative_to(repo)).replace("\\", "/"), "exists": path.exists()}
            if path.is_file():
                item["sha256"] = _sha256(path)
                item["bytes"] = path.stat().st_size
            sources.append(item)
    return sources


def _evidence_paths(repo: Path, values: list[str], variables: dict[str, str]) -> list[dict[str, Any]]:
    result = []
    for raw in values:
        path = _repo_path(repo, _replace(raw, variables))
        item = {"path": str(path.relative_to(repo)).replace("\\", "/"), "exists": path.is_file()}
        if path.is_file():
            item["sha256"] = _sha256(path)
            item["bytes"] = path.stat().st_size
        result.append(item)
    return result


def _check_reused(repo: Path, check: dict[str, Any], variables: dict[str, str]) -> dict[str, Any]:
    evidence = _evidence_paths(repo, check.get("evidence", []), variables)
    missing = [item["path"] for item in evidence if not item["exists"] or item.get("bytes", 0) == 0]
    manifest_path = _repo_path(repo, _replace(check["manifest"], variables))
    if not manifest_path.is_file():
        return {"id": check["id"], "status": "INCONCLUSIVE", "reason": f"缺少证据清单：{manifest_path}", "evidence": evidence}
    try:
        manifest = _read_json(manifest_path)
    except VerifyError as exc:
        return {"id": check["id"], "status": "INCONCLUSIVE", "reason": str(exc), "evidence": evidence}
    reasons = list(missing)
    status_mismatch = False
    try:
        status = _get_path(manifest, check.get("status_path", "status"))
        if status != check.get("expected_status", "PASS"):
            status_mismatch = True
            reasons.append(f"清单状态为 {status!r}")
    except KeyError:
        reasons.append(f"清单缺少状态字段：{check.get('status_path', 'status')}")
    for item in check.get("source_hashes", []):
        path = _repo_path(repo, _replace(item["path"], variables))
        try:
            expected = _get_path(manifest, item.get("manifest_path", "source_sha256_before." + path.name))
        except KeyError:
            reasons.append(f"清单缺少源码哈希：{path}")
            continue
        if not path.is_file() or _sha256(path) != expected:
            reasons.append(f"源码哈希不匹配：{path}")
    return {"id": check["id"], "status": "FAIL" if status_mismatch else ("INCONCLUSIVE" if reasons else "PASS"),
            "reason": "; ".join(reasons) if reasons else "复用证据状态与源码哈希匹配",
            "evidence": evidence + [{"path": str(manifest_path.relative_to(repo)).replace("\\", "/"), "exists": True, "sha256": _sha256(manifest_path)}]}


def _run_check(repo: Path, check: dict[str, Any], variables: dict[str, str]) -> dict[str, Any]:
    if check.get("type") == "evidence":
        return _check_reused(repo, check, variables)
    check_id = check["id"]
    command = _replace(check.get("command"), variables)
    evidence = _evidence_paths(repo, check.get("evidence", []), variables)
    if not isinstance(command, list) or not command or not all(isinstance(item, str) for item in command):
        return {"id": check_id, "status": "INCONCLUSIVE", "reason": "command 必须是非空字符串列表", "evidence": evidence}
    cwd = _repo_path(repo, _replace(check.get("cwd", "."), variables))
    if not cwd.is_dir():
        return {"id": check_id, "status": "BLOCKED", "reason": f"工作目录不存在：{cwd}", "command": command, "evidence": evidence}
    env = os.environ.copy()
    env.update({"PYTHONUTF8": "1", "PYTHONDONTWRITEBYTECODE": "1"})
    try:
        completed = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                   timeout=int(check.get("timeout_seconds", 300)), check=False)
    except FileNotFoundError as exc:
        return {"id": check_id, "status": "BLOCKED", "reason": f"命令不可用：{exc}", "command": command, "evidence": evidence}
    except PermissionError as exc:
        return {"id": check_id, "status": "BLOCKED", "reason": f"命令无权限：{exc}", "command": command, "evidence": evidence}
    except subprocess.TimeoutExpired as exc:
        return {"id": check_id, "status": "BLOCKED", "reason": f"超过超时限制：{exc.timeout}s", "command": command, "evidence": evidence}
    except OSError as exc:
        return {"id": check_id, "status": "BLOCKED", "reason": f"无法启动命令：{exc}", "command": command, "evidence": evidence}
    result: dict[str, Any] = {"id": check_id, "status": "PASS" if completed.returncode == check.get("expected_exit", 0) else "FAIL",
                              "command": command, "cwd": str(cwd.relative_to(repo)).replace("\\", "/"), "exit_code": completed.returncode,
                              "expected_exit": check.get("expected_exit", 0), "stdout": completed.stdout, "stderr": completed.stderr, "evidence": evidence}
    if result["status"] == "PASS" and any(not item["exists"] or item.get("bytes", 0) == 0 for item in evidence):
        result["status"] = "INCONCLUSIVE"
        result["reason"] = "命令通过但声明的证据文件缺失或为空"
    for assertion in check.get("assertions", []):
        path = _repo_path(repo, _replace(assertion["file"], variables))
        try:
            actual = _get_path(_read_json(path), assertion["path"])
        except (VerifyError, KeyError, OSError) as exc:
            result["status"] = "INCONCLUSIVE"
            result.setdefault("assertion_errors", []).append(str(exc))
            continue
        if "equals" in assertion and actual != assertion["equals"]:
            result["status"] = "FAIL"
            result.setdefault("assertion_errors", []).append(f"{assertion['path']}={actual!r}，期望 {assertion['equals']!r}")
        if assertion.get("truthy") is True and not actual:
            result["status"] = "FAIL"
            result.setdefault("assertion_errors", []).append(f"{assertion['path']} 应为真值")
    return result


def _combine(statuses: list[str]) -> str:
    for status in ("FAIL", "BLOCKED", "INCONCLUSIVE"):
        if status in statuses:
            return status
    return "PASS" if statuses and all(status == "PASS" for status in statuses) else "INCONCLUSIVE"


def verify_task(repo: str | os.PathLike[str], config_path: str | os.PathLike[str], *, output: str | os.PathLike[str] | None = None,
                run_commands: bool = True) -> dict[str, Any]:
    """Verify one task and return a JSON-serialisable evidence report."""
    root = Path(repo).resolve()
    config_file = _repo_path(root, config_path)
    config = _read_json(config_file)
    if not isinstance(config, dict) or not config.get("task", {}).get("id"):
        raise VerifyError("任务契约必须包含 task.id")
    task = config["task"]
    run_dir = _repo_path(root, config.get("run_dir", "outputs/verify") + "/" + task["id"])
    variables = {"repo": str(root), "python": sys.executable, "run_dir": str(run_dir)}
    source_items = _sources(root, config)
    report: dict[str, Any] = {"schema": "verify.v1", "task": task, "contract": str(config_file.relative_to(root)).replace("\\", "/"),
                              "started_at": _utc_now(), "sources": source_items, "scope": _scope(root, config),
                              "checks": [], "acceptance": [], "risks": config.get("risks", [])}
    checks = ([{"id": check.get("id", "unknown"), "status": "INCONCLUSIVE", "reason": "仅检查模式未执行命令"} for check in config.get("checks", []) if check.get("type") != "evidence"]
              if not run_commands else [_run_check(root, check, variables) for check in config.get("checks", [])])
    report["checks"] = checks
    by_id = {check["id"]: check for check in checks}
    for acceptance in config.get("acceptance", []):
        refs = acceptance.get("checks", [])
        statuses = [by_id[ref]["status"] for ref in refs if ref in by_id]
        missing = [ref for ref in refs if ref not in by_id]
        status = _combine(statuses) if not missing else "INCONCLUSIVE"
        evidence = _evidence_paths(root, acceptance.get("evidence", []), variables)
        if any(not item["exists"] for item in evidence) and status == "PASS":
            status = "INCONCLUSIVE"
        report["acceptance"].append({"id": acceptance["id"], "statement": acceptance["statement"], "status": status,
                                     "checks": refs, "missing_checks": missing, "evidence": evidence})
    missing_sources = [item["path"] for item in source_items if not item["exists"]]
    if missing_sources:
        report["source_error"] = "缺少规范或治理来源：" + ", ".join(missing_sources)
    statuses = [item["status"] for item in report["acceptance"]]
    if missing_sources:
        statuses.append("BLOCKED")
    report["verdict"] = _combine(statuses)
    report["finished_at"] = _utc_now()
    if output:
        target = _repo_path(root, output)
        target.parent.mkdir(parents=True, exist_ok=True)
        report["report_path"] = str(target.relative_to(root)).replace("\\", "/")
        target.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


def format_text(report: dict[str, Any]) -> str:
    lines = [f"{report['verdict']}: {report['task']['id']} (revision {report['task'].get('revision', 'unknown')})",
             f"变更范围：{report['scope']['status']}；相关文件 {len(report['scope']['relevant_files'])} 个"]
    lines.extend(f"  [{check['status']}] check/{check['id']}" + (f" — {check['reason']}" if check.get("reason") else "") for check in report["checks"])
    lines.extend(f"  [{item['status']}] {item['id']}: {item['statement']}" for item in report["acceptance"])
    if report.get("risks"):
        lines.append("未覆盖风险：" + "；".join(str(risk) for risk in report["risks"]))
    if report.get("report_path"):
        lines.append(f"证据报告：{report['report_path']}")
    return "\n".join(lines)
