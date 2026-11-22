"""Explicit local outbox consumer and fixed Node SDK host.

No GET route invokes this local consumer. All runtime egress remains behind the
private gateway with an explicitly admitted, exact synthetic loopback fixture.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from collections import deque
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import queue
import re
import shutil
import stat
import subprocess
import threading
import time
from uuid import UUID
from urllib.parse import urlparse

from sqlalchemy import MetaData, Table, func, inspect, or_, select, update
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .db import build_engine
from .models import DocumentVersion, FileObject, QueryTask, TaskArtifact, TaskAttempt, TaskEvidence, TaskOutbox, TaskStep, User, utc_now
from .execution_models import ContextFact, Conversation, QueryAnswerSnapshot, RuntimeJournal, ToolCallBinding, ToolInvocation
from .execution import ENABLED_TOOLS, _current_evidence, canonical, fingerprint, invoke_tool, request_model
from .runtime_models import (AgentRun, AgentSessionBinding, ModelCallAdmission, ModelInputProjection, RuntimeProfile, BRIDGE_PROFILE,
    JOURNAL_COMPATIBILITY_SCHEMA, JOURNAL_SOURCE_SCHEMA, MAX_RUNTIME_JOURNAL_BYTES, TaskBudget)
from .runtime_gate import _snapshot, close_run, fence_run, open_run, renew_run, validate_run
from .services import SafeError, audit, can_query_kb, has_permission, task_write_guard

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT / "src/agent_runtime"
STOPPED_TOOLS = {"accepted", "waiting_input", "reconciliation_required", "succeeded", "blocked", "failed", "cancelled"}
RUN_EVENTS = ("opened", "running", "settled")


def _child_environment():
    return {k: v for k, v in os.environ.items()
            if k.upper() in {"PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATHEXT"}}


def _file_hash(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _installed_tree(root):
    files = {}
    directories = [root]
    while directories:
        directory = directories.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                # npm's CLI shims are not part of the launched Node module tree.
                if directory == root and entry.name == ".bin":
                    continue
                details = entry.stat(follow_symlinks=False)
                if (stat.S_ISLNK(details.st_mode)
                        or getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
                    raise SafeError("RUNTIME_ARTIFACT_CHANGED", "运行依赖必须位于锁定目录", 409)
                path = Path(entry.path)
                if stat.S_ISDIR(details.st_mode):
                    directories.append(path)
                elif stat.S_ISREG(details.st_mode):
                    files[path.relative_to(root).as_posix()] = _file_hash(path)
                else:
                    raise SafeError("RUNTIME_ARTIFACT_CHANGED", "运行依赖文件无法核对", 409)
    return hashlib.sha256(canonical(files).encode()).hexdigest()


def runtime_artifacts():
    built_files = sorted((RUNTIME / "dist").glob("*.js"))
    required = {"runtime-host.js", "controlled-provider.js", "pi00-session.js", "auxiliary-host.js"}
    if not required <= {path.name for path in built_files}:
        raise SafeError("RUNTIME_BUILD_REQUIRED", "请先构建固定Node宿主", 409)
    node = shutil.which("node")
    if not node:
        raise SafeError("RUNTIME_NODE_REQUIRED", "缺少Node运行环境", 409)
    try:
        executable = Path(node).resolve(strict=True)
        process_info = json.loads(subprocess.check_output(
            [str(executable), "-p", "JSON.stringify([process.version,process.platform,process.arch,process.execPath])"],
            text=True, timeout=5, stderr=subprocess.DEVNULL, env=_child_environment()))
        version, platform, arch, process_path = process_info
        if not re.fullmatch(r"v\d+\.\d+\.\d+", version) or tuple(map(int, version[1:].split("."))) < (22, 19, 0):
            raise SafeError("RUNTIME_NODE_REQUIRED", "Node版本低于SDK要求", 409)
        if Path(process_path).resolve(strict=True) != executable:
            raise SafeError("RUNTIME_ARTIFACT_CHANGED", "Node执行路径与验证路径不同", 409)
        package_json = RUNTIME / "package.json"
        lock_path = RUNTIME / "package-lock.json"
        package = json.loads(package_json.read_text(encoding="utf-8"))
        lock = json.loads(lock_path.read_text(encoding="utf-8"))
        entries = lock["packages"]
        if lock["lockfileVersion"] != 3 or entries[""]["dependencies"] != package["dependencies"]:
            raise SafeError("RUNTIME_SDK_CHANGED", "SDK清单与锁文件不匹配", 409)
        for name in ("@earendil-works/pi-ai", "@earendil-works/pi-coding-agent"):
            if package["dependencies"].get(name) != "1.5.2" or entries["node_modules/" + name]["version"] != "1.5.2":
                raise SafeError("RUNTIME_SDK_CHANGED", "SDK不符合已锁定版本", 409)
        for name, locked in entries.items():
            if not name:
                continue
            target = (RUNTIME / name).resolve()
            if not target.is_relative_to(RUNTIME / "node_modules") or locked.get("link"):
                raise SafeError("RUNTIME_SDK_CHANGED", "SDK锁文件含未批准路径", 409)
            installed = target / "package.json"
            if not installed.is_file() and (locked.get("optional") or locked.get("dev")):
                continue
            if json.loads(installed.read_text(encoding="utf-8"))["version"] != locked["version"]:
                raise SafeError("RUNTIME_SDK_CHANGED", "已安装依赖不符合锁文件", 409)
        built = {path.relative_to(RUNTIME).as_posix(): _file_hash(path) for path in built_files}
        manifest = {"node_version": version, "platform": platform, "arch": arch,
                    "node_sha256": _file_hash(executable), "package_sha256": _file_hash(package_json),
                    "lock_sha256": _file_hash(lock_path), "installed_tree_sha256": _installed_tree(RUNTIME / "node_modules"),
                    "built_files": built}
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError) as error:
        raise SafeError("RUNTIME_ARTIFACT_CHANGED", "无法核对本地运行制品", 409) from error
    return {"artifact_manifest_hash": hashlib.sha256(canonical(manifest).encode()).hexdigest(),
            "adapter_fingerprint": built["dist/controlled-provider.js"], "node": str(executable), "manifest": manifest}


def _mark_blocked(db, task, code, message, state="blocked"):
    try:
        task_write_guard(db, task, allowed_statuses=("pending", "running", "blocked", "reconciliation_required", "succeeded"))
    except SafeError as changed:
        if changed.code != "TASK_STATE_CHANGED":
            raise
        db.refresh(task)
        return False
    if state == "reconciliation_required":
        public_message = "原查询的服务请求结果待核对，请联系管理员查看原任务。"
    elif code in {"ACCESS_LIMITED", "EVIDENCE_INSUFFICIENT", "VERSION_CONFLICT", "ANSWER_PLAN_REQUIRED", "INDEX_NOT_READY"}:
        public_message = message
    else:
        public_message = "查询服务暂不可用，请联系管理员确认恢复后重试原查询。"
    task.status, task.error_code, task.next_action = state, code, public_message
    task.revision += 1
    coverage = task.result_summary.get("coverage") if code in {"EVIDENCE_INSUFFICIENT", "ANSWER_PLAN_REQUIRED"} else None
    task.result_summary = {"answer": None, "result_status": "needs_review" if code == "ANSWER_PLAN_REQUIRED" else "insufficient_evidence", "requires_review": True}
    if coverage:
        task.result_summary["coverage"] = coverage
    return True


def _utc(value):
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _journal_latest(db, binding_id):
    return db.scalar(select(RuntimeJournal).where(RuntimeJournal.binding_id == binding_id)
                     .order_by(RuntimeJournal.generation.desc(), RuntimeJournal.created_at.desc(), RuntimeJournal.id.desc()))


def _journal_compatibility(profile, artifacts):
    if (not profile or not isinstance(artifacts, dict) or not isinstance(profile.configuration, dict)
            or not all(key in profile.configuration for key in ("bridge", "settings", "resources"))
            or profile.artifact_manifest_hash != artifacts.get("artifact_manifest_hash")
            or profile.adapter_fingerprint != artifacts.get("adapter_fingerprint")
            or artifacts.get("manifest") != profile.configuration.get("artifact_manifest")
            or fingerprint(profile.configuration) != profile.configuration_hash
            or fingerprint(ENABLED_TOOLS) != profile.tool_set_hash):
        return None
    return {"schema": JOURNAL_COMPATIBILITY_SCHEMA, "profile_id": profile.id, "profile_revision": profile.revision,
            "configuration_hash": profile.configuration_hash, "artifact_manifest_hash": profile.artifact_manifest_hash,
            "sdk_version": profile.sdk_version, "sdk_source_revision": profile.sdk_source_revision,
            "adapter_fingerprint": profile.adapter_fingerprint, "tool_set_hash": profile.tool_set_hash,
            **{key + "_hash": fingerprint(profile.configuration[key]) for key in ("bridge", "settings", "resources")}}


def _journal_window():
    configured = os.getenv("DATACENTER_JOURNAL_RECOVERY_SECONDS")
    if configured is None:
        return None
    if not re.fullmatch(r"[1-9][0-9]{0,8}", configured):
        raise SafeError("RUNTIME_JOURNAL_POLICY_INVALID", "会话恢复窗口配置无效", 409)
    return timedelta(seconds=int(configured))


def _journal_path(config, key, prefix, *, exists=True):
    if (not isinstance(key, str) or not key.startswith(prefix + "/") or ":" in key or "\\" in key
            or any(component in {"", ".", ".."} for component in key.split("/"))
            or PurePosixPath(key).suffix != ".jsonl"):
        raise OSError("invalid private journal path")
    root = config.storage_dir.resolve()
    path = root / key
    _journal_storage_components(root, path, exists=exists)
    return path


def _journal_storage_components(root, path, *, exists):
    if not path.is_relative_to(root) or path.resolve() != path:
        raise OSError("noncanonical private journal path")
    for candidate in (path, *path.parents):
        if candidate == root:
            break
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            if exists:
                raise
            continue
        if stat.S_ISLNK(info.st_mode) or (getattr(info, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)):
            raise OSError("journal reparse path")


def _read_journal_bytes(path):
    with path.open("rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_RUNTIME_JOURNAL_BYTES or info.st_nlink != 1:
            raise OSError("invalid private journal object")
        payload = stream.read(MAX_RUNTIME_JOURNAL_BYTES + 1)
        if len(payload) != info.st_size or not os.path.samestat(info, path.lstat()):
            raise OSError("private journal object changed")
    return payload


def _check_journal_identity(body, pi_id):
    if str(UUID(pi_id)) != pi_id or not body.endswith(b"\n"):
        return False
    for index, line in enumerate(body.splitlines()):
        entry = json.loads(line.decode("utf-8"))
        if not isinstance(entry, dict) or not isinstance(entry.get("type"), str):
            return False
        if index == 0:
            if entry["type"] != "session" or entry.get("id") != pi_id:
                return False
        elif entry["type"] == "session":
            return False
    return True


def _journal_context(db, task):
    conversation = db.get(Conversation, task.conversation_id, populate_existing=True) if task.conversation_id else None
    if task.conversation_id and (conversation is None or conversation.owner_user_id != task.owner_user_id or conversation.status != "active"):
        raise ValueError("journal conversation binding changed")
    facts = db.scalars(select(ContextFact).where(ContextFact.conversation_id == conversation.id).order_by(ContextFact.id)
                      .execution_options(populate_existing=True)).all() if conversation else []
    return {"task_id": task.id, "owner_id": task.owner_user_id, "execution_revision": task.execution_revision,
            "source_message_id": task.source_message_id, "conversation_id": task.conversation_id,
            "effective_input_hash": fingerprint(task.effective_input), "accepted_scope_hash": fingerprint(task.accepted_scope),
            "conversation_revision": conversation.revision if conversation else None, "facts_hash": fingerprint([_snapshot(fact) for fact in facts])}


def _journal_fragment(evidence):
    context = getattr(evidence, "source_context", None)
    fragment = context.get("fragment") if isinstance(context, dict) else None
    if not isinstance(fragment, dict):
        # Existing TaskEvidence may store the canonical fragment directly.
        fragment = context if isinstance(context, dict) and "dependencies" in context else None
    if not isinstance(fragment, dict) or not isinstance(fragment.get("dependencies"), dict):
        return None
    kind = getattr(evidence, "evidence_kind", None)
    binding = getattr(evidence, "source_binding_hash", None)
    if (kind not in ("text", "region") or not isinstance(binding, str) or not re.fullmatch(r"[0-9a-f]{64}", binding)
            or fragment.get("source_binding_hash", binding) != binding
            or fragment.get("document_version_id") != evidence.document_version_id or fragment.get("block_id") != evidence.block_id
            or fragment.get("chunk_id") != getattr(evidence, "chunk_id", None) or fragment.get("chunk_set_id") != getattr(evidence, "chunk_set_id", None)
            or fragment.get("evidence_kind", kind) != kind or not evidence.chunk_id or not evidence.chunk_set_id):
        return None
    return {**fragment, "evidence_kind": kind, "source_binding_hash": binding, "quote": evidence.quote_snapshot,
            "text": evidence.quote_snapshot if kind == "text" else None, "locator": evidence.locator,
            "source_spans": evidence.source_spans, "asset_refs": evidence.asset_refs}


def _journal_sources(db, actor, task, identifiers, *, retain_partial=False):
    """Replay a journal boundary's evidence together and return current identities."""
    from .chunk_retrieval import validate_child_fragments
    from .governance import publication_dependency_snapshot

    versions, candidates = {}, {}
    for identifier in identifiers:
        if not isinstance(identifier, str):
            continue
        evidence = db.get(TaskEvidence, identifier, populate_existing=True)
        if not evidence or evidence.task_id != task.id or evidence.execution_revision != task.execution_revision:
            continue
        fragment = _journal_fragment(evidence)
        if fragment is None:
            continue
        version_id = evidence.document_version_id
        if version_id not in versions:
            versions[version_id] = db.get(DocumentVersion, version_id, populate_existing=True)
        version = versions[version_id]
        if version and version.selected_chunk_set_id == evidence.chunk_set_id:
            candidates[identifier] = evidence, fragment

    complete = len(candidates) == len(identifiers)
    if not candidates or (not complete and not retain_partial):
        return {}, complete

    payload = task.effective_input
    scope = {"model": payload.get("model"), "kb_ids": payload.get("knowledge_base_ids", []),
             "context": {key: payload.get(key) for key in ("level", "category", "need_class")}}
    fragments = [fragment for _, fragment in candidates.values()]
    if not validate_child_fragments(db, actor, fragments, **scope):
        if not retain_partial or len(candidates) == 1:
            return {}, False
        # Keep provable references in the quarantined manifest. A failed batch
        # cannot become a complete journal through these individual reads.
        complete = False
        candidates = {identifier: (evidence, fragment) for identifier, (evidence, fragment) in candidates.items()
                      if validate_child_fragments(db, actor, [fragment], **scope)}

    sources, publication_hashes = {}, {}
    for identifier, (evidence, fragment) in candidates.items():
        version_id = evidence.document_version_id
        if version_id not in publication_hashes:
            publication_hashes[version_id] = fingerprint(publication_dependency_snapshot(db, versions[version_id]))
        dep = fragment["dependencies"]
        sources[identifier] = {"evidence_id": evidence.id, "evidence_row_hash": _snapshot(evidence)["row_digest"],
            "source_binding_hash": fragment["source_binding_hash"], "fragment_hash": fingerprint(fragment),
            "version_id": version_id, "parse_run_id": fragment["parse_run_id"], "ir_hash": fragment["ir_hash"],
            "block_id": evidence.block_id, "chunk_id": evidence.chunk_id, "chunk_set_id": evidence.chunk_set_id,
            "publication_hash": publication_hashes[version_id],
            **{name: dep[name] for name in ("source_qc_ref", "collection_qc_ref", "review_set_id", "publication_event_id", "generation_id",
                                           "generation_manifest_hash", "embedding_profile_id", "group_ref", "group_hash", "mapping_hash")}}
    return sources, complete


def _journal_snapshot(db, config, row):
    file = db.get(FileObject, row.snapshot_file_id, populate_existing=True)
    binding = db.get(AgentSessionBinding, row.binding_id, populate_existing=True)
    key = f"internal/runtime-journals/{row.run_id}.jsonl"
    if (not file or file.object_key != key or file.storage_name != "local" or file.status != "available"
            or file.sha256 != row.content_sha256 or file.mime_type != "application/x-ndjson"
            or not binding or file.created_by != binding.owner_user_id):
        raise OSError("private journal snapshot missing")
    body = _read_journal_bytes(_journal_path(config, key, "internal/runtime-journals"))
    if (len(body) != file.byte_size or hashlib.sha256(body).hexdigest() != row.content_sha256
            or not _check_journal_identity(body, row.pi_session_id)):
        raise OSError("private journal snapshot changed")
    return body


def private_journal_cleanup_preflight(db, config, journal_id, *, policy=None, now=None):
    """Inventory private body references without granting or performing deletion."""
    journal = db.get(RuntimeJournal, journal_id, populate_existing=True)
    if not journal:
        raise SafeError("RUNTIME_JOURNAL_NOT_FOUND", "运行日志不存在", 404)
    run = db.get(AgentRun, journal.run_id, populate_existing=True)
    binding = db.get(AgentSessionBinding, journal.binding_id, populate_existing=True)
    dependencies = journal.source_bindings
    inherited_calls = []
    if (isinstance(dependencies, dict) and journal.source_binding_hash == fingerprint(dependencies)
            and isinstance(dependencies.get("model_calls"), dict)):
        inherited_calls = list(dependencies["model_calls"])
    source_call = or_(ModelCallAdmission.run_id == journal.run_id, ModelCallAdmission.id.in_(inherited_calls))
    model_calls = db.scalars(select(ModelCallAdmission.id).where(source_call,
                            ModelCallAdmission.response_body.is_not(None)).order_by(ModelCallAdmission.id)).all()
    auxiliary_kinds = ("answer_candidate", "answer_validation", "summary", "compaction", "runtime_manifest")
    auxiliary = db.scalars(select(TaskArtifact.id).where(TaskArtifact.task_id == run.task_id,
                           TaskArtifact.kind.in_(auxiliary_kinds)).order_by(TaskArtifact.id)).all() if run else []
    working_refs = db.scalars(select(RuntimeJournal.id).where(RuntimeJournal.working_key == journal.working_key)
                              .order_by(RuntimeJournal.id)).all()
    connection = db.connection()
    schema = inspect(connection)
    file_links = []
    for name in sorted(schema.get_table_names()):
        columns = sorted({fk["constrained_columns"][0] for fk in schema.get_foreign_keys(name)
                          if fk["referred_table"] == "file_objects" and fk["referred_columns"] == ["id"]
                          and len(fk["constrained_columns"]) == 1})
        if not columns:
            continue
        table = Table(name, MetaData(), autoload_with=connection, resolve_fks=False)
        for column in columns:
            count = db.scalar(select(func.count()).select_from(table).where(table.c[column] == journal.snapshot_file_id))
            if count:
                file_links.append({"table": name, "column": column, "count": count})
    timestamp = _utc(now or utc_now())
    policy_set = (isinstance(policy, dict) and isinstance(policy.get("policy_ref"), str) and bool(policy["policy_ref"].strip())
                  and isinstance(policy.get("retain_until"), datetime) and policy["retain_until"].tzinfo is not None
                  and type(policy.get("legal_hold")) is bool)
    if not policy_set:
        state = "policy_required"
    elif (policy["legal_hold"] or timestamp < policy["retain_until"] or journal.state == "active"
          or journal.recovery_expires_at and timestamp < _utc(journal.recovery_expires_at)
          or binding and binding.lease_until and timestamp < _utc(binding.lease_until)):
        state = "retained"
    elif file_links or working_refs or model_calls or auxiliary:
        state = "referenced"
    else:
        state = "manual_disposition_required"
    try:
        snapshot_body = _journal_snapshot(db, config, journal)
        snapshot_integrity = "verified"
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        snapshot_body = None
        snapshot_integrity = "reconciliation_required"
    try:
        working = _journal_path(config, journal.working_key, f"runtime/{journal.binding_id}/journal")
        if _read_journal_bytes(working) == snapshot_body:
            working_integrity = "verified"
        else:
            working_integrity = "shared_or_advanced" if len(working_refs) > 1 else "reconciliation_required"
    except (OSError, RuntimeError):
        working_integrity = "reconciliation_required"
    return {"journal_id": journal.id, "state": state, "snapshot_file_id": journal.snapshot_file_id,
            "snapshot_integrity": snapshot_integrity, "working_integrity": working_integrity,
            "snapshot_references": file_links, "working_references": working_refs,
            "model_response_call_refs": model_calls, "auxiliary_artifact_refs": auxiliary, "deletion_authorized": False}


def _journal_dependencies(db, actor, task, run, inherited=None):
    sources, projections, model_calls, tools = {}, {}, {}, {}
    if inherited:
        for key, target in (("sources", sources), ("projections", projections), ("model_calls", model_calls), ("tools", tools)):
            target.update(inherited[key])
    version_ids = {record["version_id"] for record in sources.values()}
    references = {}
    complete = True

    def remember(evidence_id, expected=None):
        nonlocal complete
        if not isinstance(evidence_id, str):
            complete = False
            return
        expectations = references.setdefault(evidence_id, [])
        if expected is not None:
            expectations.append(expected)

    calls = db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.run_id == run.id)
                       .order_by(ModelCallAdmission.id).execution_options(populate_existing=True)).all()
    for call in calls:
        if call.dispatched_at is None and call.state != "received":
            continue
        projection = db.get(ModelInputProjection, call.projection_id, populate_existing=True)
        if (not projection or call.task_id != task.id or projection.run_id != run.id
                or projection.operation != "answer" or projection.business_purpose != "answer"
                or not isinstance(projection.dependencies, dict) or not isinstance(projection.dependencies.get("sources"), list)
                or fingerprint(projection.dependencies) != projection.dependency_digest):
            complete = False
            continue
        model_calls[call.id] = _snapshot(call)["row_digest"]
        projections[projection.id] = _snapshot(projection)["row_digest"]
        for row in projection.dependencies["sources"]:
            if not isinstance(row, dict) or not isinstance(row.get("evidence"), dict):
                complete = False
                continue
            evidence_id = row["evidence"].get("id")
            if isinstance(version := row.get("version"), dict) and isinstance(version.get("id"), str):
                version_ids.add(version["id"])
            remember(evidence_id, row)

    receipts = db.scalars(select(ToolInvocation).join(ToolCallBinding, ToolCallBinding.invocation_id == ToolInvocation.id)
                          .where(ToolCallBinding.run_id == run.id).order_by(ToolInvocation.id)
                          .execution_options(populate_existing=True)).unique().all()
    for receipt in receipts:
        if receipt.task_id != task.id or receipt.actor_user_id != actor.id or receipt.execution_revision != task.execution_revision:
            complete = False
            continue
        tools[receipt.id] = _snapshot(receipt)["row_digest"]
        for evidence_id in receipt.evidence_ids:
            remember(evidence_id)
        if receipt.tool_name == "dc_evidence_search" and "evidence" in receipt.output:
            returned = receipt.output["evidence"]
            if (not isinstance(returned, list) or any(not isinstance(item, dict) for item in returned)
                    or {item.get("id") for item in returned} != set(receipt.evidence_ids)):
                complete = False
        for metadata in receipt.output.get("sources", []):
            if not isinstance(metadata, dict):
                complete = False
                continue
            if isinstance(metadata.get("version_id"), str):
                version_ids.add(metadata["version_id"])
            # Version-only tool receipts cannot establish the consumed source.
            remember(metadata.get("evidence_id"), metadata)

    new_ids = [evidence_id for evidence_id in references if evidence_id not in sources]
    current, sources_complete = _journal_sources(db, actor, task, new_ids, retain_partial=True)
    sources.update(current)
    complete = complete and sources_complete
    for evidence_id, expectations in references.items():
        source = sources.get(evidence_id)
        if source is None:
            complete = False
            continue
        version_ids.add(source["version_id"])
        for expected in expectations:
            old = expected.get("evidence")
            publication = expected.get("publication")
            binding = expected.get("source_binding_hash")
            projected_fragment = expected.get("fragment")
            if not isinstance(projected_fragment, dict):
                ctx = expected.get("fragment_source_context")
                if isinstance(ctx, dict):
                    projected_fragment = ctx.get("fragment", ctx)
            projected_dependency = projected_fragment.get("dependencies") if isinstance(projected_fragment, dict) else None
            if (not isinstance(old, dict) or old.get("row_digest") != source["evidence_row_hash"]
                    or not isinstance(publication, dict) or publication.get("digest") != source["publication_hash"]
                    or binding != source["source_binding_hash"] or not isinstance(projected_dependency, dict)
                    or projected_fragment.get("chunk_id") != source["chunk_id"]
                    or projected_dependency.get("generation_manifest_hash") != source["generation_manifest_hash"]):
                complete = False
    return {"schema": JOURNAL_SOURCE_SCHEMA, "state": "complete" if complete else "incomplete",
            "context": _journal_context(db, task), "sources": sources,
            "projections": projections, "model_calls": model_calls, "tools": tools}, sorted(version_ids)


def _journal_dependencies_current(db, dependencies, task, actor):
    if (not isinstance(dependencies, dict) or dependencies.get("schema") != JOURNAL_SOURCE_SCHEMA
            or dependencies.get("state") != "complete" or dependencies.get("context") != _journal_context(db, task)
            or any(not isinstance(dependencies.get(key), dict) for key in ("sources", "projections", "model_calls", "tools"))):
        return False
    if any(not isinstance(source, dict) for source in dependencies["sources"].values()):
        return False
    current, complete = _journal_sources(db, actor, task, dependencies["sources"])
    if not complete or dependencies["sources"] != current:
        return False
    for key, type_ in (("projections", ModelInputProjection), ("model_calls", ModelCallAdmission), ("tools", ToolInvocation)):
        for identifier, expected in dependencies[key].items():
            row = db.get(type_, identifier, populate_existing=True)
            if not row or _snapshot(row)["row_digest"] != expected:
                return False
            if key == "tools":
                if row.task_id != task.id or row.actor_user_id != actor.id or row.execution_revision != task.execution_revision:
                    return False
            else:
                owner = db.get(AgentRun, row.run_id, populate_existing=True)
                if (not owner or owner.task_id != task.id or owner.owner_user_id != actor.id
                        or owner.execution_revision != task.execution_revision):
                    return False
    return True


def _restore_journal(db, config, binding, actor, *, profile=None, artifacts=None):
    previous = _journal_latest(db, binding.id)
    if not previous:
        return None, 1
    if previous.state != "active":
        return None, previous.generation + 1
    try:
        task = db.get(QueryTask, binding.task_id, populate_existing=True)
        current = db.get(User, actor.id, populate_existing=True) if actor else None
        source_run = db.get(AgentRun, previous.run_id, populate_existing=True)
        compatibility = _journal_compatibility(profile, artifacts)
        now, window = utc_now(), _journal_window()
        configured_expiry = _utc(previous.created_at) + window if window is not None else None
        valid = (task and source_run and current and current.id == binding.owner_user_id == task.owner_user_id
                 and current.status == "active" and has_permission(current, "task.read") and has_permission(current, "knowledge.query")
                 and task.kind == "query" and task.status in {"pending", "running"}
                 and source_run.session_binding_id == binding.id and source_run.task_id == task.id and source_run.owner_user_id == current.id
                 and source_run.execution_revision == task.execution_revision and source_run.state in {"settled", "interrupted"}
                 and previous.pi_session_id == binding.pi_session_id
                 and (not previous.recovery_expires_at or now < _utc(previous.recovery_expires_at))
                 and (not configured_expiry or now < configured_expiry) and compatibility is not None
                 and (profile.id, profile.revision) == (binding.runtime_profile_id, binding.runtime_profile_revision)
                 and compatibility == previous.runtime_compatibility and previous.source_bindings is not None
                 and previous.source_binding_hash == fingerprint(previous.source_bindings)
                 and all(can_query_kb(db, current, kid) for kid in task.effective_input.get("knowledge_base_ids", []))
                 and not db.scalar(select(ModelCallAdmission.id).where(ModelCallAdmission.task_id == task.id,
                     ModelCallAdmission.state.in_(("unknown", "dispatching", "streaming"))).limit(1))
                 and _journal_dependencies_current(db, previous.source_bindings, task, current))
        if valid:
            path = _journal_path(config, previous.working_key, f"runtime/{binding.id}/journal")
            if _read_journal_bytes(path) == _journal_snapshot(db, config, previous):
                return str(path), previous.generation
    except (ArithmeticError, AttributeError, KeyError, OSError, RuntimeError, SafeError, TypeError, ValueError):
        pass  # Quarantine uncertain history and rebuild from the current task records.
    previous.state = "quarantined"
    audit(db, binding.owner_user_id, "runtime.journal_quarantined", "runtime_journal", previous.id, reason="dependency_or_file_changed")
    return None, previous.generation + 1


def _save_journal(db, config, run, reply, generation, *, profile, artifacts, inherited=None):
    now = utc_now()
    updated = db.execute(update(AgentSessionBinding).where(AgentSessionBinding.id == run.session_binding_id,
        AgentSessionBinding.state == "in_use", AgentSessionBinding.lease_owner == run.id,
        AgentSessionBinding.lease_epoch == run.lease_epoch, AgentSessionBinding.lease_until > now)
        .values(lease_epoch=run.lease_epoch).execution_options(synchronize_session=False))
    step = db.execute(update(TaskStep).where(TaskStep.id == run.step_id, TaskStep.status == "running",
        TaskStep.lease_owner == run.id, TaskStep.lease_epoch == run.step_lease_epoch, TaskStep.lease_until > now)
        .values(lease_epoch=run.step_lease_epoch).execution_options(synchronize_session=False))
    if updated.rowcount != 1 or step.rowcount != 1:
        raise SafeError("RUNTIME_LEASE_EXPIRED", "运行租约已失效，拒绝旧执行者提交", 409)
    root = config.storage_dir.resolve()
    pi_id = reply.get("pi_session_id", "")
    working = reply.get("session_file", "")
    key = f"internal/runtime-journals/{run.id}.jsonl"
    try:
        working_key = Path(working).relative_to(root).as_posix()
        path = _journal_path(config, working_key, f"runtime/{run.session_binding_id}/journal")
        body = _read_journal_bytes(path)
        if not _check_journal_identity(body, pi_id) or (inherited and inherited.pi_session_id != pi_id):
            raise OSError("private journal session identity changed")
        content_hash = hashlib.sha256(body).hexdigest()
        snapshot = _journal_path(config, key, "internal/runtime-journals", exists=False)
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot = _journal_path(config, key, "internal/runtime-journals", exists=False)
        try:
            fd = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0), 0o600)
            with os.fdopen(fd, "wb") as target:
                target.write(body)
                target.flush()
                os.fsync(target.fileno())
        except FileExistsError:
            if _read_journal_bytes(snapshot) != body:
                raise OSError("private journal previous bytes differ") from None
    except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
        raise SafeError("RUNTIME_JOURNAL_INVALID", "运行日志不符合绑定范围或无法对账", 409) from None
    file = db.scalar(select(FileObject).where(FileObject.object_key == key).execution_options(populate_existing=True))
    if file and (file.sha256 != content_hash or file.byte_size != len(body) or file.status != "available" or file.created_by != run.owner_user_id):
        raise SafeError("RUNTIME_JOURNAL_INVALID", "运行日志原快照登记不一致", 409)
    if file is None:
        file = FileObject(storage_name="local", object_key=key, sha256=content_hash, byte_size=len(body),
                          mime_type="application/x-ndjson", status="available", created_by=run.owner_user_id)
        db.add(file)
        db.flush()
    task = db.get(QueryTask, run.task_id, populate_existing=True)
    actor = db.get(User, run.owner_user_id, populate_existing=True)
    compatibility = _journal_compatibility(profile, artifacts)
    expires = None
    try:
        inherited_bindings = inherited.source_bindings if inherited else None
        manifest, versions = _journal_dependencies(db, actor, task, run, inherited_bindings)
        if inherited and not _journal_dependencies_current(db, inherited_bindings, task, actor):
            manifest["state"] = "incomplete"
        if inherited and not body.startswith(_journal_snapshot(db, config, inherited)):
            manifest["state"] = "incomplete"
        window = _journal_window()
        expires = now + window if window is not None else None
        if inherited and inherited.recovery_expires_at:
            inherited_expiry = _utc(inherited.recovery_expires_at)
            expires = min(expires, inherited_expiry) if expires is not None else inherited_expiry
    except (ArithmeticError, AttributeError, KeyError, OSError, RuntimeError, SafeError, TypeError, ValueError):
        manifest, versions = {"schema": JOURNAL_SOURCE_SCHEMA, "state": "incomplete"}, []
    state = "active" if manifest["state"] == "complete" and compatibility else "quarantined"
    existing = db.scalar(select(RuntimeJournal).where(RuntimeJournal.run_id == run.id).execution_options(populate_existing=True))
    if existing:
        if existing.content_sha256 != content_hash or existing.runtime_compatibility != compatibility or existing.source_bindings != manifest:
            raise SafeError("RUNTIME_JOURNAL_INVALID", "重试与原封存日志不一致", 409)
        return
    db.add(RuntimeJournal(run_id=run.id, binding_id=run.session_binding_id, generation=generation, pi_session_id=pi_id,
        working_key=working_key, snapshot_file_id=file.id, content_sha256=content_hash, version_ids=versions,
        source_bindings=manifest, source_binding_hash=fingerprint(manifest), runtime_compatibility=compatibility,
        recovery_expires_at=expires, state=state))
    db.get(AgentSessionBinding, run.session_binding_id).pi_session_id = pi_id


def _node_run(db, config, run, token, gateway_url, artifacts):
    binding = db.get(AgentSessionBinding, run.session_binding_id)
    profile = db.get(RuntimeProfile, (binding.runtime_profile_id, binding.runtime_profile_revision))
    actor = db.get(User, run.owner_user_id)
    session_file, generation = _restore_journal(db, config, binding, actor, profile=profile, artifacts=artifacts)
    inherited = _journal_latest(db, binding.id) if session_file else None
    db.commit()
    storage = config.storage_dir.resolve()
    root = storage / "runtime" / binding.id
    try:
        _journal_storage_components(storage, root, exists=False)
        root.mkdir(parents=True, exist_ok=True)
        _journal_storage_components(storage, root, exists=True)
    except (OSError, RuntimeError):
        raise SafeError("RUNTIME_JOURNAL_INVALID", "运行日志目录超出隔离绑定范围", 409) from None
    process = subprocess.Popen([artifacts["node"], str(RUNTIME / "dist/runtime-host.js")], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, cwd=root, env=_child_environment(),
        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    size_limit = BRIDGE_PROFILE["max_message_bytes"]
    pending_limit = BRIDGE_PROFILE["max_pending_messages"]
    lines = queue.Queue(maxsize=pending_limit)
    protocol_error = threading.Event()
    on_exit = threading.Event()

    def read_lines():
        try:
            while line := process.stdout.readline(size_limit + 1):
                if len(line) > size_limit or not line.endswith(b"\n"):
                    protocol_error.set()
                    break
                lines.put_nowait(line)
        except (queue.Full, OSError):
            protocol_error.set()
        finally:
            on_exit.set()

    threading.Thread(target=read_lines, daemon=True).start()

    def send(item):
        data = (canonical(item) + "\n").encode()
        if len(data) > size_limit:
            raise SafeError("RUNTIME_HOST_PROTOCOL", "运行消息超出限制", 409)
        process.stdin.write(data)
        process.stdin.flush()

    def receive(wait):
        if protocol_error.is_set():
            raise SafeError("RUNTIME_HOST_PROTOCOL", "运行消息超出限制", 409)
        try:
            line = lines.get(timeout=wait)
        except queue.Empty:
            if on_exit.is_set():
                raise SafeError("RUNTIME_HOST_INTERRUPTED", "运行进程已中断，先核对原回执", 409) from None
            return None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError()
            return message
        except (ValueError, UnicodeError):
            raise SafeError("RUNTIME_HOST_PROTOCOL", "运行消息无效", 409) from None

    engine, run_id, task_id = db.get_bind(), run.id, run.task_id

    def tool_reply(message):
        params = message["params"]
        with Session(engine, expire_on_commit=False, autoflush=False, info={"settings": config}) as tool_db:
            try:
                result = invoke_tool(tool_db, run_id, token, params["pi_tool_call_id"], params["name"], params["args"], settings=config)
                tool_db.commit()
                if params["name"] == "dc_evidence_search" and result["evidence_ids"] and config.auxiliary_operations:
                    from .auxiliary import run_auxiliary
                    factory = sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, info={"settings": config})
                    # An optional idle operation must leave two bounded model
                    # calls for the actual employee answer. Each purpose still
                    # needs its own frozen service pin, proof and service budget.
                    for operation in config.auxiliary_operations:
                        with factory() as idle_db:
                            budget = idle_db.get(TaskBudget, task_id, populate_existing=True)
                            enough = bool(budget and budget.reserved_calls + 3 <= budget.max_calls
                                and budget.reserved_tokens + 3 * profile.configuration["tokens_per_call"] <= budget.max_reserved_tokens)
                        view = run_auxiliary(factory, config, run_id, token, gateway_url, artifacts["node"], artifacts["manifest"], operation=operation) if enough else None
                        if not view:
                            with factory() as limited_db:
                                audit(limited_db, run.owner_user_id, "runtime.auxiliary_unavailable", "task", task_id, purpose=operation)
                                limited_db.commit()
                return {"id": message["id"], "result": result}
            except SafeError as error:
                tool_db.rollback()
                return {"id": message["id"], "error": {"code": error.code}}

    todo, future = deque(), None
    executor = ThreadPoolExecutor(max_workers=1)
    deadline, check_at, heartbeat = time.monotonic() + 90, time.monotonic(), time.monotonic() + 10
    last_event, last_call, opened, waiting = 0, 0, False, False
    expected = {"protocol": BRIDGE_PROFILE["protocol"], "node_version": artifacts["manifest"]["node_version"],
                "sdk_version": profile.sdk_version, "configuration_hash": profile.configuration_hash,
                "tool_set_hash": profile.tool_set_hash}
    try:
        if session_file:
            _, _, current_actor, current_profile = validate_run(db, run_id, token)
            session_file, generation = _restore_journal(db, config, binding, current_actor, profile=current_profile, artifacts=artifacts)
            inherited = _journal_latest(db, binding.id) if session_file else None
            db.commit()
        send({"id": "open", "method": "session.open", "params": {
            "bridge": profile.configuration["bridge"], "settings": profile.configuration["settings"],
            "resources": profile.configuration["resources"], "artifact_manifest": artifacts["manifest"],
            "generation": profile.configuration["generation"],
            "fixed_generation": profile.configuration["generation_revision_id"] is not None,
            "configuration_hash": profile.configuration_hash, "tool_set_hash": profile.tool_set_hash,
            "gate_url": gateway_url, "run_id": run_id, "token": token,
            "session_root": str(root), "session_file": session_file, "tools": ENABLED_TOOLS}})
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= check_at:
                task = db.get(QueryTask, task_id, populate_existing=True)
                if task.status == "cancelled":
                    raise SafeError("RUNTIME_CANCELLED", "任务已取消", 409)
                if now >= heartbeat and task.status in {"pending", "running"}:
                    renew_run(db, run_id, token)
                    heartbeat = now + 10
                else:
                    db.rollback()  # Release read snapshots during the child run.
                check_at = now + 0.25
            if future is not None and future.done():
                reply = future.result()
                waiting = reply.get("result", {}).get("state") in STOPPED_TOOLS
                send(reply)
                future = None
            while future is None and todo:
                request = todo.popleft()
                if waiting:
                    send({"id": request["id"], "error": {"code": "RUNTIME_WAITING"}})
                else:
                    future = executor.submit(tool_reply, request)
            message = receive(0.1)
            if message is None:
                continue
            if not opened and message in ({"id": "open", "result": expected},
                                           {"id": "open", "error": {"code": "RUNTIME_PROFILE_INCOMPATIBLE"}}):
                if "error" in message:
                    raise SafeError("RUNTIME_PROFILE_INCOMPATIBLE", "当前宿主不符合运行配置", 409)
                validate_run(db, run_id, token)
                db.rollback()
                opened = True
                send({"id": "run", "method": "run.prompt"})
                continue
            method = message.get("method")
            if opened and method == "run.event" and set(message) == {"method", "params"}:
                params = message["params"]
                if (isinstance(params, dict) and set(params) == {"kind", "sequence", "status"}
                        and params["kind"] == "run_status" and type(params["sequence"]) is int
                        and params["sequence"] == last_event + 1 and last_event < len(RUN_EVENTS)
                        and params["status"] == RUN_EVENTS[last_event]):
                    last_event += 1
                    continue
            if last_event == 2 and method == "tool.invoke" and set(message) == {"id", "method", "params"}:
                params = message["params"]
                if (len(todo) < pending_limit and message["id"] == f"tool-{last_call + 1}"
                        and isinstance(params, dict) and set(params) == {"pi_tool_call_id", "name", "args"}
                        and isinstance(params["pi_tool_call_id"], str) and 1 <= len(params["pi_tool_call_id"]) <= 200
                        and isinstance(params["name"], str) and isinstance(params["args"], dict)):
                    last_call += 1
                    todo.append(message)
                    continue
            if opened and message.get("id") == "run" and set(message) in ({"id", "error"}, {"id", "result"}):
                if "error" in message:
                    raise SafeError("RUNTIME_HOST_FAILED", "运行宿主失败，请核对任务回执", 409)
                reply = message["result"]
                if (last_event == len(RUN_EVENTS) and not todo and future is None and isinstance(reply, dict)
                        and reply.get("state") in ("settled", "model_error") and type(reply.get("waiting")) is bool
                        and isinstance(reply.get("session_file"), str) and isinstance(reply.get("pi_session_id"), str)):
                    task = db.get(QueryTask, task_id, populate_existing=True)
                    task_write_guard(db, task, allowed_statuses=("running", "waiting_input", "succeeded", "blocked", "reconciliation_required"))
                    if task.status == "running":
                        validate_run(db, run_id, token)
                    _save_journal(db, config, run, reply, generation, profile=profile, artifacts=artifacts, inherited=inherited)
                    db.commit()
                    return reply
            raise SafeError("RUNTIME_HOST_PROTOCOL", "运行消息或能力不符合锁定协议", 409)
        raise SafeError("RUNTIME_HOST_TIMEOUT", "运行已超时，先核对原请求结果", 409)
    except (OSError, SafeError) as error:
        db.rollback()
        fence_run(db, run_id, token)
        if process.poll() is None and not protocol_error.is_set():
            try:
                send({"id": "abort", "method": "run.abort"})
                abort_until = time.monotonic() + 1
                while time.monotonic() < abort_until:
                    if receive(0.1) == {"id": "abort", "result": {"state": "aborted"}}:
                        break
            except (OSError, SafeError):
                pass  # Exact child is still terminated below; receipts remain durable.
        if isinstance(error, SafeError):
            raise
        raise SafeError("RUNTIME_HOST_INTERRUPTED", "运行进程已中断，先核对原回执", 409) from error
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        if process.poll() is None:
            process.kill()  # This exact child is owned by this local attempt.
        process.wait(timeout=5)
        process.stdin.close()
        process.stdout.close()


def run_one(config: Settings, *, gateway_url: str, profile_id="local-pi", profile_revision=1,
            task_id: str | None = None, dispatch_id: str | None = None) -> dict:
    if dispatch_id and not task_id:
        raise ValueError("A dispatch identifier requires an exact task identifier")
    for identifier in (task_id, dispatch_id):
        if identifier is not None:
            try:
                if str(UUID(identifier)) != identifier:
                    raise ValueError
            except (ValueError, TypeError, AttributeError) as error:
                raise ValueError("Task and dispatch identifiers must be canonical UUIDs") from error
    address = urlparse(gateway_url)
    if address.scheme != "http" or address.hostname != "127.0.0.1" or not address.port or address.path not in ("", "/") or address.query or address.fragment or address.username:
        raise ValueError("An explicit private loopback API address is required")
    engine = build_engine(config.database_url)
    try:
        with Session(engine, expire_on_commit=False, autoflush=False, info={"settings": config}) as db:
            statement = select(TaskStep).join(QueryTask, QueryTask.id == TaskStep.task_id).where(
                QueryTask.kind == "query", TaskStep.step_key == "query",
                QueryTask.status.in_(["pending", "running"]), TaskStep.status.in_(["pending", "running"]),
                or_(TaskStep.lease_until.is_(None), TaskStep.lease_until <= utc_now()))
            if task_id:
                statement = statement.where(QueryTask.id == task_id)
            step = db.scalar(statement.order_by(QueryTask.created_at, TaskStep.id))
            if not step:
                return {"state": "idle"}
            task = db.get(QueryTask, step.task_id)
            if task_id and task.id != task_id:
                return {"state": "idle"}
            if dispatch_id:
                dispatch = db.get(TaskOutbox, dispatch_id)
                if not dispatch or dispatch.step_id != step.id or dispatch.status not in {"publishing", "published"}:
                    return {"state": "unknown_dispatch"}
            actor = db.get(User, task.owner_user_id)
            run = token = None
            try:
                if not task.accepted_scope or not actor or actor.status != "active" or not task.accepted_scope.get("knowledge_base_ids") or any(not can_query_kb(db, actor, kb) for kb in task.accepted_scope.get("knowledge_base_ids", [])):
                    if _mark_blocked(db, task, "ACCESS_LIMITED", "任务受理范围或当前权限不足"):
                        step.status = "blocked"
                    db.commit()
                    return {"task_id": task.id, "state": task.status}
                if not task.effective_input.get("model"):
                    request_model(db, task)
                    step.status = "waiting_input"
                    outbox = db.scalar(select(TaskOutbox).where(TaskOutbox.step_id == step.id).order_by(TaskOutbox.dispatch_no.desc()))
                    if outbox:
                        outbox.status, outbox.published_at, outbox.publish_attempts = "published", utc_now(), outbox.publish_attempts + 1
                    db.commit()
                    return {"task_id": task.id, "state": "waiting_input"}
                task_write_guard(db, task)
                task.next_action = "正在核对本地执行环境，首次处理可能较慢；无需重复提交。"
                task.revision += 1
                audit(db, actor.id, "runtime.preflight_started", "task", task.id)
                db.commit()
                preflight_started = time.monotonic()
                artifacts = runtime_artifacts()
                audit(db, actor.id, "runtime.preflight_verified", "task", task.id,
                      duration_ms=round((time.monotonic() - preflight_started) * 1000))
                profile = db.get(RuntimeProfile, (profile_id, profile_revision))
                if (not profile or profile.artifact_manifest_hash != artifacts["artifact_manifest_hash"]
                        or profile.adapter_fingerprint != artifacts["adapter_fingerprint"]
                        or profile.configuration.get("artifact_manifest") != artifacts["manifest"]):
                    raise SafeError("RUNTIME_PROFILE_REQUIRED", "尚未登记与当前宿主匹配的本地运行配置", 409)
                run, token = open_run(db, task.id, profile_id=profile_id, profile_revision=profile_revision)
                task_write_guard(db, task)
                task.status = "running"
                task.next_action = "正在按当前授权资料处理查询，请等待。"
                task.revision += 1
                outbox = db.scalar(select(TaskOutbox).where(TaskOutbox.step_id == run.step_id).order_by(TaskOutbox.dispatch_no.desc()))
                if outbox:
                    outbox.status, outbox.published_at, outbox.publish_attempts = "published", utc_now(), outbox.publish_attempts + 1
                db.commit()
                reply = _node_run(db, config, run, token, gateway_url, artifacts)
                db.refresh(task)
                if task.status == "running":
                    validate_run(db, run.id, token)
                    if reply["state"] == "model_error":
                        raise SafeError("RUNTIME_MODEL_FAILED", "模型请求未成功，先核对持久调用回执", 409)
                    # Required result checks are enforced by the host even if
                    # the model omits them or merely says it has finished.
                    invoke_tool(db, run.id, token, "host:evidence", "dc_evidence_search", {}, settings=config)
                    db.commit()
                    checked = invoke_tool(db, run.id, token, "host:validate", "dc_artifacts_validate", {}, settings=config)
                    db.commit()
                    if checked["data"].get("ready"):
                        if any(item.chunk_id for item in _current_evidence(db, task, actor)):
                            _mark_blocked(db, task, "ANSWER_PLAN_REQUIRED", "模型尚未提交可验证的完整组织方案，请核对原句或联系资料维护人员")
                            db.commit()
                        else:
                            invoke_tool(db, run.id, token, "host:present", "dc_artifacts_deliver", {}, settings=config)
                            db.commit()
                    else:
                        warnings = checked["data"].get("warnings", [])
                        if "version_conflict" in warnings:
                            _mark_blocked(db, task, "VERSION_CONFLICT", "该设备型号的适用手册版本存在冲突，已暂停正式回答。请联系知识管理员核对适用版本。")
                        elif {"index_not_ready", "index_stale", "index_unavailable"}.intersection(warnings):
                            _mark_blocked(db, task, "INDEX_NOT_READY", "资料尚未准备好查询。请联系知识管理员确认索引可用，再重试原查询。")
                        else:
                            _mark_blocked(db, task, "EVIDENCE_INSUFFICIENT", "当前没有完整且适用的证据，请确认资料或型号")
                        db.commit()
                if task.status == "succeeded":
                    _current_evidence(db, task, actor)  # Commit/return boundary.
                close_run(db, run.id, token)
            except SafeError as error:
                db.rollback()
                db.refresh(task)
                if error.code in {"RUNTIME_LEASE_CONFLICT", "RUNTIME_STEP_LEASE_CONFLICT"}:
                    return {"task_id": task.id, "state": "busy"}
                unresolved = db.scalar(select(ModelCallAdmission.id).where(ModelCallAdmission.task_id == task.id,
                    ModelCallAdmission.state.in_(["dispatching", "streaming", "unknown"])))
                preserved_result = False
                if task.status == "succeeded" and not unresolved and db.scalar(select(QueryAnswerSnapshot.id).where(
                    QueryAnswerSnapshot.task_id == task.id, QueryAnswerSnapshot.execution_revision == task.execution_revision)):
                    try:
                        _current_evidence(db, task, actor)
                        preserved_result = True
                    except SafeError as changed:
                        error = changed
                    if preserved_result and run:
                        db.get(TaskAttempt, run.attempt_id).diagnostic = {"result_committed": True, "runtime_error": error.code, "journal_reconciliation_required": True}
                marked = False
                if not preserved_result and task.status != "cancelled":
                    marked = _mark_blocked(db, task, error.code, error.message, "reconciliation_required" if unresolved else "blocked")
                if not run and marked:
                    step.status = "blocked"
                db.commit()
                if run and token:
                    final_state = "cancelled" if task.status == "cancelled" else "settled" if preserved_result else "interrupted"
                    close_run(db, run.id, token, state=final_state)
            return {"task_id": task.id, "state": task.status, "error_code": task.error_code}
    finally:
        engine.dispose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database-url", required=True)
    parser.add_argument("--storage-dir", type=Path, required=True)
    parser.add_argument("--gateway-url", required=True)
    parser.add_argument("--profile-id", default="local-pi")
    parser.add_argument("--profile-revision", type=int, default=1)
    parser.add_argument("--once", action="store_true", required=True)
    args = parser.parse_args()
    # Application startup/migration is separate; this command does not migrate
    # an implicit default database or create credentials/profiles.
    result = run_one(Settings(database_url=args.database_url, storage_dir=args.storage_dir), gateway_url=args.gateway_url,
                     profile_id=args.profile_id, profile_revision=args.profile_revision)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
