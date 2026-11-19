"""Private answer candidates and server-owned formal rendering.

Candidate plans never become employee-facing prose. Their exact model receipt,
source projection and validation report are retained separately from the answer.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .answer_context import build_answer_context
from .answer_plan import AnswerPlan, validate_and_render_answer_plan
from .models import FileObject, TaskArtifact
from .runtime_models import AgentSessionBinding, ModelCallAdmission, ModelInputProjection, RuntimeProfile
from .services import SafeError


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def delivery_schema():
    """Inline this small acyclic schema: nested tool parameters have no root $defs."""
    schema = AnswerPlan.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(value):
        if isinstance(value, list):
            return [resolve(item) for item in value]
        if isinstance(value, dict):
            if "$ref" in value:
                return resolve(deepcopy(definitions[value["$ref"].rsplit("/", 1)[-1]]))
            return {key: resolve(item) for key, item in value.items()}
        return value

    return {"type": "object", "properties": {"answer_plan": resolve(schema)}, "additionalProperties": False}


def model_answer_context(task, evidence):
    """No repeated source text in the model envelope; members reference exact sources."""
    context = build_answer_context(task, evidence)
    view = deepcopy(context["model_data"])
    # The identical strict schema is already declared by dc_artifacts_deliver.
    view.pop("answer_plan_schema", None)
    for group in view["support_groups"]:
        for member in group["members"]:
            member.pop("text", None)
    return context, view


def _unique(members):
    result = dict(members)
    if len(result) != len(members):
        raise ValueError("duplicate candidate member")
    return result


def _response_tools(body):
    """Replay OpenAI chat tool calls, including their exact streamed arguments."""
    if not isinstance(body, str) or len(body.encode("utf-8")) > 4 * 1024 * 1024:
        return []
    calls = {}
    try:
        events = [json.loads(body, object_pairs_hook=_unique)] if body.lstrip().startswith("{") else [
            json.loads(line[5:].strip(), object_pairs_hook=_unique) for line in body.splitlines()
            if line.startswith("data:") and line[5:].strip() and line[5:].strip() != "[DONE]"]
        for event in events:
            for choice in event.get("choices", []):
                message = choice.get("message", choice.get("delta", {}))
                tool_calls = message.get("tool_calls")
                if tool_calls is None:
                    continue
                if not isinstance(tool_calls, list):
                    return []
                for index, tool in enumerate(tool_calls):
                    key = (choice.get("index", 0), tool.get("index", index))
                    current = calls.setdefault(key, {"id": "", "name": "", "arguments": ""})
                    function = tool.get("function") or {}
                    delta = {"id": tool.get("id"), "name": function.get("name"), "arguments": function.get("arguments")}
                    for field, value in delta.items():
                        if value is not None:
                            if not isinstance(value, str):
                                return []
                            current[field] += value
        return [{**call, "args": json.loads(call["arguments"], object_pairs_hook=_unique)} for call in calls.values()]
    except (ValueError, TypeError, AttributeError, RecursionError):
        return []


def _candidate_origin(db, run, pi_call_id, plan, evidence):
    from .runtime_gate import _bailian_stream_call, _snapshot, canonical_json, digest
    from .service_admission import BAILIAN_WIRE

    expected = {item.id: _snapshot(item) for item in evidence}
    binding = db.get(AgentSessionBinding, run.session_binding_id)
    profile = db.get(RuntimeProfile, (binding.runtime_profile_id, binding.runtime_profile_revision)) if binding else None
    generation = profile.configuration.get("generation", {}) if profile else {}
    bailian = generation.get("wire_protocol") == BAILIAN_WIRE
    receipts = db.scalars(select(ModelCallAdmission).where(ModelCallAdmission.run_id == run.id,
        ModelCallAdmission.task_id == run.task_id, ModelCallAdmission.state == "received",
        ModelCallAdmission.response_status == 200).order_by(ModelCallAdmission.received_at.desc(), ModelCallAdmission.id)).all()
    for call in receipts:
        projection = db.get(ModelInputProjection, call.projection_id, populate_existing=True)
        source_rows = projection.dependencies.get("sources", []) if projection else []
        actual = {source.get("evidence", {}).get("id"): source.get("evidence") for source in source_rows}
        if (not projection or projection.run_id != run.id or actual != expected
                or projection.operation != "answer" or projection.business_purpose != "answer"
                or call.lease_epoch != run.lease_epoch or call.context_digest != projection.context_digest):
            continue
        expected_call = {"id": pi_call_id, "name": "dc_artifacts_deliver", "args": {"answer_plan": plan}}
        if bailian:
            if (projection.profile_configuration_hash != profile.configuration_hash
                    or projection.dependencies.get("bailian_tool") != expected_call["name"]
                    or digest(canonical_json(projection.dependencies)) != projection.dependency_digest
                    or not call.response_body
                    or _bailian_stream_call(call.response_body.encode("utf-8"), generation["model"], expected_call["name"]) != expected_call):
                continue
        elif not any(all(tool[key] == value for key, value in expected_call.items()) for tool in _response_tools(call.response_body)):
            continue
        return {"model_call_id": call.id, "projection_id": projection.id, "context_digest": call.context_digest,
                "payload_digest": call.payload_digest, "response_sha256": hashlib.sha256(call.response_body.encode()).hexdigest(),
                "pi_tool_call_id": pi_call_id, "profile_id": binding.runtime_profile_id if binding else None,
                "profile_revision": binding.runtime_profile_revision if binding else None, "profile_configuration_hash": profile.configuration_hash if profile else None,
                "adapter": call.adapter, "service_use": projection.dependencies.get("service_use_pin")}
    return None


def _private_record(db, settings, task, call, envelope, status):
    # Use the same verified, immutable local object installation as PDF outputs;
    # this object belongs to the query, never to a ParseRun or a public file route.
    from .parsing.artifacts import ArtifactError, _install_once, _private_directory, _storage_path, _verify
    if settings is None:
        raise SafeError("ANSWER_STORAGE_REQUIRED", "正式答案需要显式配置的私有产物存储", 409)
    body = _canonical(envelope).encode("utf-8")
    checksum = hashlib.sha256(body).hexdigest()
    key = f"internal/answer-candidates/{task.id}/{task.execution_revision}/{call.id}.json"
    try:
        path = _storage_path(settings, key)
        _private_directory(path.parent)
        path = _storage_path(settings, key)
        _install_once(path, body)
        _verify(path, len(body), checksum)
    except (ArtifactError, OSError):
        raise SafeError("ANSWER_STORAGE_UNAVAILABLE", "答案候选尚未安全归档，暂不交付", 409) from None
    identity = str(uuid5(NAMESPACE_URL, f"dc.answer-candidate/{call.id}"))
    file = db.get(FileObject, identity)
    if file and (file.object_key != key or file.sha256 != checksum or file.byte_size != len(body) or file.status != "available"):
        raise SafeError("ANSWER_CANDIDATE_CHANGED", "答案候选原记录已改变", 409)
    if not file:
        file = FileObject(id=identity, storage_name="local", object_key=key, sha256=checksum, byte_size=len(body),
                          mime_type="application/json", status="available", created_by=task.owner_user_id)
        db.add(file)
        db.flush()
    artifact = TaskArtifact(task_id=task.id, kind="answer_candidate", validation_status=status, file_object_id=file.id)
    db.add(artifact)
    db.flush()
    return {"artifact_id": artifact.id, "file_id": file.id, "sha256": checksum}


def delivery_coverage(available, rendered, *, origin_verified):
    """Project the coverage supported by the validated plan, keeping request labels."""
    validated = {item["item_ref"]: item["matched_group_refs"] for item in rendered["report"]["coverage"]} if origin_verified else {}
    items, unresolved = [], []
    for item in available["items"]:
        ref = item["item_ref"]
        matched = validated.get(ref, [])
        if matched:
            state = "covered"
        else:
            state = "missing" if ref in validated or not item["matched_group_refs"] else "needs_review"
            unresolved.append(ref)
        items.append({**item, "state": state, "matched_group_refs": matched})
    return {**available, "items": items, "missing_item_refs": unresolved, "missing_count": len(unresolved)}


def validate_candidate(db, settings, run, task, tool_call, pi_call_id, plan, evidence):
    """Caller holds delivery source/task locks and repeats live checks before commit."""
    context = build_answer_context(task, evidence)
    rendered = validate_and_render_answer_plan(plan, evidence=context["evidence"], support_groups=context["support_groups"],
                                               query_contract=context["query_contract"])
    origin = _candidate_origin(db, run, pi_call_id, plan, evidence)
    if not origin:
        rendered.update(status="needs_review", text=None, sections=[], claims=[], sources=[])
        rendered["report"].update(status="needs_review", claim_checks=[])
        rendered["report"]["reasons"].append({"code": "candidate_model_receipt_unverified"})
    envelope = {"schema_version": "answer-candidate/1", "task_id": task.id, "execution_revision": task.execution_revision,
                "run_id": run.id, "tool_call_id": tool_call.id, "candidate": plan, "origin": origin,
                "evidence_ids": [item.id for item in evidence], "coverage": context["coverage"], "validation": rendered["report"]}
    record = _private_record(db, settings, task, tool_call, envelope, rendered["status"])
    return rendered, delivery_coverage(context["coverage"], rendered, origin_verified=bool(origin)), record
