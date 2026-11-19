"""Bind answer coverage to the accepted question and replayed task evidence."""
from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from dataclasses import replace
import hashlib
import json
import re
import unicodedata

from .answer_plan import AnswerEvidence, AnswerPlan, QueryContract, RENDERER_KEYS, SupportGroup, SupportMember


REQUEST_VERSION = "answer-request/1"
_FACETS = {
    "startup": ("operation", ("开机", "启动", "上电", "power on", "turn on", "start up", "startup", "start")),
    "shutdown": ("operation", ("关机", "停机", "下电", "power off", "turn off", "shut down", "shutdown", "stop")),
    "operation": ("operation", ("操作", "使用方法", "operate", "operating", "operation")),
    "replace": ("maintenance", ("更换", "换掉", "replace", "replacement")),
    "clean": ("maintenance", ("清洁", "清洗", "cleaning", "clean")),
    "inspection": ("maintenance", ("巡检", "点检", "inspection", "inspect")),
    "maintenance": ("maintenance", ("维护", "保养", "维修", "maintenance", "servicing", "service")),
    "precautions": ("precautions", ("注意事项", "注意什么", "需要注意", "警告", "风险", "禁忌", "安全事项", "安全",
                                    "precautions", "precaution", "cautions", "caution", "warnings", "warning", "safety")),
}
_WARNING = ("禁止", "不得", "严禁", "警告", "注意", "do not", "must not", "danger", "warning", "caution")
_CLAUSE = re.compile(r"[，,；;、。！？!?\n]+|以及|并且|同时|还有|和|与|及|\b(?:and|also)\b")
_SOURCE_CLAUSE = re.compile(r"[；;。！？!?\n]+|(?<!\d)[.,]|[.,](?!\d)")
_TERMS = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[._/-][a-z0-9]+)*")
_ZH_PREFIX = re.compile(r"^(?:请问一下|请告诉我|请说明|请列出|帮我查一下|帮我看一下|请问|我想知道|麻烦|请|这个设备的|这台设备的|设备的|"
                        r"我应该|怎么|怎样|如何|是否|的|[\s:：])+")
_ZH_SUFFIX = re.compile(r"(?:应该是多少|应是多少|应为多少|是多少|是什么|是怎样的|有什么|有哪些|有多久|怎么样|怎么做|如何做|"
                        r"多久|多少|几度|几个|吗|呢|呀|么|请说明|的|[\s:：])+$")
_EN_QUESTION = frozenset("a an the how what which when is are was were does do did can could would should to of for this that it its "
                         "i we you me my please tell show list describe explain know want like device much many about".split())
_PROCEDURE_TERMS = ("怎么", "怎样", "如何", "步骤", "流程", "方法", "how to", "steps", "procedure", "instructions")
_VALUE_TERMS = ("多少", "多久", "几度", "几个", "数值是多少", "how much", "how many", "how long")
_NUMBER = r"[+−-]?\d+(?:[.,]\d+)?"
_QUANTITY = re.compile(rf"(?<![a-z0-9]){_NUMBER}(?:\s*[-–—~至到]\s*{_NUMBER})?\s*(?P<unit>°[cf]?|[%℃℉]|pa\b|kpa\b|mpa\b|bar\b|psi\b|k\b|"
                       r"rpm\b|r/min\b|hz\b|khz\b|kv\b|v\b|a\b|w\b|kw\b|nm\b|n·m\b|kg\b|g\b|"
                       r"mm\b|cm\b|m\b|hours?\b|days?\b|months?\b|minutes?\b|seconds?\b|"
                       r"小时|分钟|秒钟|秒|日|天|个月|年|个|次|级|转)")
_DIRECT_VALUE_LINK = re.compile(r"\s*(?:(?:为|是|设为|设定为|设置为|规定为|等于|保持在|范围(?:为|是)?|range is|is(?: set to)?|are|equals?|of|set to)\s*)?[:=]?\s*")
_UNCERTAIN_VALUE = ("未知", "不确定", "无法确定", "不明确", "待定", "待确认", "暂缺", "不是",
                    "unknown", "not known", "not determined", "to be confirmed", "tbd", "unavailable")
_VALUE_UNITS = (
    (("气压", "压力", "压强", "油压", "风压", "pressure"), {"pa", "kpa", "mpa", "bar", "psi"}),
    (("温度", "temperature"), {"°c", "°f", "℃", "℉", "k"}),
)
_SPECIFIC_PROCEDURES = ("startup", "shutdown", "replace", "clean", "inspection")
_UNSUPPORTED = ("比较", "对比", "换算", "推算", "估算", "为什么", "推测", "推断", "compare", "predict", "calculate", "estimate", "why")


def _normal(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _pattern(term):
    return re.escape(term) if any("\u3400" <= char <= "\u9fff" for char in term) else (
        r"(?<![a-z0-9_-])" + re.escape(term) + r"(?![a-z0-9_-])")


def _mentions(text, choices):
    return any(re.search(_pattern(term), text) for term in choices)


def _remove(text, choices):
    if not choices:
        return text
    return re.sub("|".join(_pattern(term) for term in sorted(choices, key=len, reverse=True)), " ", text)


def _terms(text):
    value = _ZH_SUFFIX.sub("", _ZH_PREFIX.sub("", text.strip())).strip()
    return list(dict.fromkeys(token for token in _TERMS.findall(value)
                              if token not in _EN_QUESTION and token not in {"的", "要", "设备", "本机"}))


def _item(facet, clause, terms):
    heading = _FACETS[facet][0] if facet in _FACETS else facet
    value = _mentions(clause, _VALUE_TERMS)
    return {"label": clause, "kind": heading, "facet": facet, "terms": terms,
            "requires_procedure": not value and heading in {"operation", "maintenance"},
            "requires_value": value}


def _requests(clause):
    if _mentions(clause, _UNSUPPORTED):
        return [_item("unresolved", clause, [])]
    if _mentions(clause, ("开关机", "启停")):
        rest = _remove(clause, ("开关机", "启停", *_PROCEDURE_TERMS))
        return [_item(facet, clause, _terms(rest)) for facet in ("startup", "shutdown")]
    found = [facet for facet, (_, choices) in _FACETS.items() if _mentions(clause, choices)]
    if "precautions" in found and not _mentions(clause, _PROCEDURE_TERMS):
        # “维护时注意什么” requests precautions with a maintenance qualifier.
        return [_item("precautions", clause, _terms(_remove(clause, _FACETS["precautions"][1])))]
    for general, specific in (("operation", {"startup", "shutdown"}), ("maintenance", {"replace", "clean", "inspection"})):
        if general in found and specific.intersection(found):
            found.remove(general)
    if found:
        common = [term for facet in found for term in _FACETS[facet][1]]
        # Remove only the server-recognized intent and interrogative wording.
        topic = _terms(_remove(clause, (*common, *_PROCEDURE_TERMS, "要求", "事项")))
        return [_item(facet, clause, topic) for facet in found]
    terms = _terms(clause)
    return [_item("fact" if terms else "unresolved", clause, terms)]


def freeze_request_items(query, model) -> dict:
    """Freeze deterministic coverage rules over the original accepted text."""
    original = query if isinstance(query, str) else ""
    model = model if isinstance(model, str) and model.strip() else None
    text = _normal(original)
    if model:
        text = _remove(text, (_normal(model),))
    clauses = [clause.strip() for clause in _CLAUSE.split(text) if _TERMS.search(_ZH_PREFIX.sub("", clause.strip()))]
    requests = [item for clause in clauses for item in _requests(clause)] or [_item("unresolved", "原始请求", [])]
    return {"schema_version": REQUEST_VERSION, "query_sha256": hashlib.sha256(original.encode("utf-8")).hexdigest(),
            "model": model, "items": [{"item_ref": f"request-{number}", **item} for number, item in enumerate(requests, 1)]}


def _task_requests(task):
    original = task.input_snapshot.get("query")
    scope = task.accepted_scope
    if "request_items" not in scope:
        return freeze_request_items(original, task.input_snapshot.get("model"))["items"], "legacy_derived"
    stored = scope["request_items"]
    if (not isinstance(original, str) or not isinstance(stored, Mapping) or stored.get("schema_version") != REQUEST_VERSION
            or stored.get("query_sha256") != hashlib.sha256(original.encode("utf-8")).hexdigest()
            or stored.get("model") != scope.get("model")):
        return [{"item_ref": "request-1", **_item("unresolved", "原始请求", [])}], "invalid_frozen"
    items = stored.get("items")
    if not isinstance(items, list) or not items or any(not isinstance(item, dict)
            or item.get("facet") not in {*_FACETS, "fact", "unresolved"} or item.get("item_ref") != f"request-{i}"
            or not isinstance(item.get("label"), str) or not item["label"] or not isinstance(item.get("terms"), list)
            or any(not isinstance(term, str) or not term for term in item["terms"])
            or item.get("kind") != (_FACETS[item["facet"]][0] if item["facet"] in _FACETS else item["facet"])
            or type(item.get("requires_procedure")) is not bool or type(item.get("requires_value")) is not bool
            for i, item in enumerate(items, 1)):
        return [{"item_ref": "request-1", **_item("unresolved", "原始请求", [])}], "invalid_frozen"
    return items, "frozen"


def _evidence_and_groups(task, rows, model):
    evidence, registry = {}, {}
    group_rows = defaultdict(list)
    original_ids = Counter()
    for row in rows:
        if row.task_id != task.id or row.execution_revision != task.execution_revision or not row.source_binding_hash:
            continue
        source = row.source_context
        fragment = source.get("fragment")
        if not isinstance(fragment, Mapping) or not isinstance(fragment.get("id"), str):
            continue
        original = fragment["id"]
        original_ids[original] += 1
        try:
            if any(fragment[key] != getattr(row, key) for key in ("document_version_id", "block_id", "chunk_set_id", "chunk_id", "evidence_kind")):
                continue
            label = fragment.get("version_label")
            evidence[row.id] = AnswerEvidence(
                document_id=fragment["document_id"], document_version_id=row.document_version_id,
                document_title=fragment["document_title"], version_label=label if isinstance(label, str) and label.strip() else None,
                parse_run_id=fragment["parse_run_id"], ir_hash=fragment["ir_hash"], chunk_set_id=row.chunk_set_id,
                chunk_id=row.chunk_id, block_id=row.block_id, source_binding_hash=row.source_binding_hash,
                file_page=row.locator["file_page"], printed_page=fragment.get("printed_page"), section=fragment.get("section"),
                text=row.quote_snapshot if row.evidence_kind == "text" else None, block_start=fragment.get("block_start"),
                block_end=fragment.get("block_end"), region_ref=fragment.get("region_ref"), asset_ref=fragment.get("asset_ref"),
            )
            registry[original] = (row.id, fragment)
            group = source.get("group")
            if isinstance(group, Mapping):
                group_rows[group["id"]].append((original, group))
        except (KeyError, AttributeError, TypeError):
            continue
    groups = {}
    for group_ref, entries in group_rows.items():
        if len(entries) != 1:
            continue
        original, group = entries[0]
        try:
            members = group["members"]
            if (not members or original != members[0]["evidence_ref"] or group["complete"] is not True
                    or group["kind"] not in RENDERER_KEYS or not group["scope_key"] or group["predecessor_refs"] is None):
                continue
            mapped = []
            for member in members:
                identifier = member["evidence_ref"]
                row_id, fragment = registry[identifier]
                if (original_ids[identifier] != 1 or fragment["dependencies"]["group_ref"] != group_ref
                        or member["source_binding_hash"] != evidence[row_id].source_binding_hash
                        or member["role"] != fragment["role"]):
                    raise ValueError("Group member is outside task evidence")
                step = member.get("step_number") if group["kind"] == "procedure_group" and member["role"] == "body" else None
                mapped.append(SupportMember(row_id, member["role"], member["source_binding_hash"], step))
            if (len({item.evidence_ref for item in mapped}) != len(mapped)
                    or len({evidence[item.evidence_ref].origin for item in mapped}) != 1):
                continue
            groups[group_ref] = SupportGroup(group["kind"], model, group["scope_key"], (), tuple(mapped),
                                             tuple(group["predecessor_refs"]), True)
        except (IndexError, KeyError, TypeError, ValueError):
            continue
    return evidence, groups


def _can_render(group, evidence):
    bodies = [member for member in group.members if member.role == "body"]
    steps = [member.step_number for member in bodies]
    return (bool(bodies) and all(evidence[member.evidence_ref].text and evidence[member.evidence_ref].text.strip()
                                for member in group.members)
            and (group.kind != "procedure_group" or (all(type(number) is int and number > 0 for number in steps)
                                                        and steps == sorted(steps))))


def _complete_chain(groups, ref, evidence):
    active, done = set(), set()
    pending = [(ref, False)]
    while pending:
        current, visited = pending.pop()
        if current in done:
            continue
        group = groups.get(current)
        if not group or not _can_render(group, evidence) or group.predecessor_refs is None:
            return set()
        if visited:
            active.remove(current)
            done.add(current)
        else:
            if current in active:
                return set()
            active.add(current)
            pending.append((current, True))
            pending.extend((before, False) for before in reversed(group.predecessor_refs))
    return done


def _direct_value(text, terms):
    """Require a subject and its own quantity, even within a multi-field sentence."""
    if not terms:
        return False
    dimensions = [units for topics, units in _VALUE_UNITS if any(_mentions(term, topics) for term in terms)]
    for clause in _SOURCE_CLAUSE.split(text):
        if _mentions(clause, _UNCERTAIN_VALUE):
            continue
        for quantity in _QUANTITY.finditer(clause):
            if any(quantity["unit"] not in units for units in dimensions):
                continue
            before = clause[:quantity.start()]
            endpoints = [max((match.end() for match in re.finditer(_pattern(term), before)), default=0) for term in terms]
            if all(endpoints) and _DIRECT_VALUE_LINK.fullmatch(before[max(endpoints):]):
                return True
    return False


def _named_procedures(text):
    facets = {facet for facet in _SPECIFIC_PROCEDURES if _mentions(text, _FACETS[facet][1])}
    if _mentions(text, ("开关机", "启停")):
        facets.update(("startup", "shutdown"))
    return facets


def _matches(item, group, evidence):
    if item["kind"] == "unresolved" or item["requires_procedure"] and group.kind != "procedure_group":
        return False
    body = [_normal(evidence[member.evidence_ref].text) for member in group.members if member.role == "body"]
    titles = [_normal(evidence[member.evidence_ref].text) for member in group.members if member.role in {"header", "context"}]
    subjects = body + titles + [_normal(evidence[member.evidence_ref].text) for member in group.members
                               if member.role in {"definition", "condition"}]
    warnings = [_normal(evidence[member.evidence_ref].text) for member in group.members if member.role == "warning"]
    def has_terms(texts):
        return all(any(_mentions(part, (term,)) for part in texts) for term in item["terms"])

    if item["requires_value"] and not any(_direct_value(text, item["terms"]) for text in body):
        return False
    facet = item["facet"]
    if facet == "precautions":
        return (bool(warnings) or any(_mentions(part, _WARNING) for part in body)) and has_terms([*subjects, *warnings])
    if facet == "fact":
        return bool(item["terms"]) and (any(has_terms([part]) for part in body)
                                         or has_terms(subjects) and (len(body) == 1 or group.kind == "procedure_group"))
    aliases = _FACETS[facet][1]
    if facet == "operation":
        aliases += _FACETS["startup"][1] + _FACETS["shutdown"][1]
    if facet == "maintenance":
        aliases += _FACETS["replace"][1] + _FACETS["clean"][1] + _FACETS["inspection"][1]
    actions = body + titles
    if group.kind == "procedure_group":
        actions = [part for part in body if not _mentions(part, _WARNING)]
        if facet in _SPECIFIC_PROCEDURES and len(_named_procedures(" ".join(titles + actions))) > 1:
            actions = [part for text in actions for part in _SOURCE_CLAUSE.split(text) if _named_procedures(part) == {facet}]
        else:
            actions += titles
    return any(_mentions(part, aliases) for part in actions) and has_terms(subjects)


def build_answer_context(task, evidence_rows) -> dict:
    """Map only the current execution's already revalidated, exact child evidence."""
    items, contract_state = _task_requests(task)
    conditions = {key: task.effective_input.get(key) for key in ("model", "level", "category", "need_class")}
    model = conditions["model"] if isinstance(conditions["model"], str) else ""
    accepted_model = task.accepted_scope.get("model")
    if ((accepted_model and accepted_model != model) or any(
            key in task.accepted_scope and task.accepted_scope[key] != conditions[key] for key in ("level", "category", "need_class"))):
        model = ""
    scope = hashlib.sha256(json.dumps(conditions, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest() if model else None
    evidence, groups = _evidence_and_groups(task, evidence_rows, model)
    required, headings = {}, defaultdict(set)
    coverage = []
    for item in items:
        matching = item
        if model and not accepted_model and not task.input_snapshot.get("model"):
            # A later task-bound model confirmation can resolve its literal
            # identifier without rewriting the frozen request or its label.
            matching = {**item, "terms": [term for term in item["terms"] if term != _normal(model)]}
        alternative = []
        for group_ref, group in sorted(groups.items()):
            closure = _complete_chain(groups, group_ref, evidence) if model else set()
            if closure and _matches(matching, group, evidence):
                alternative.append(group_ref)
                heading = "operation" if item["kind"] == "fact" else item["kind"]
                for ref in closure:
                    headings[ref].add(heading)
        required[item["item_ref"]] = tuple(alternative)
        coverage.append({"item_ref": item["item_ref"], "label": item["label"], "kind": item["kind"],
                         "state": "covered" if alternative else "missing", "matched_group_refs": alternative})
    eligible = {ref: replace(groups[ref], allowed_headings=tuple(sorted(headings[ref]))) for ref in sorted(headings)}
    missing = [item["item_ref"] for item in coverage if not item["matched_group_refs"]]
    coverage_view = {"contract_state": contract_state, "matching_policy": "deterministic-source-terms/1",
                     "items": coverage, "missing_item_refs": missing, "missing_count": len(missing)}
    projected = [{"id": ref, "kind": group.kind, "structure_scope": group.scope_key,
                  "allowed_headings": list(group.allowed_headings), "renderer_key": RENDERER_KEYS[group.kind],
                  "predecessor_refs": list(group.predecessor_refs),
                  "members": [{"evidence_ref": member.evidence_ref, "role": member.role, "step_number": member.step_number,
                               "text": evidence[member.evidence_ref].text, "file_page": evidence[member.evidence_ref].file_page}
                              for member in group.members]} for ref, group in eligible.items()]
    return {"evidence": evidence, "support_groups": eligible,
            "query_contract": QueryContract(model, scope, required, {ref: group.scope_key for ref, group in eligible.items()}),
            "coverage": coverage_view, "model_data": {"answer_plan_schema": AnswerPlan.model_json_schema(),
                "coverage": coverage_view, "support_groups": projected, "instruction": (
                    "只输出符合 answer-plan/1 的 JSON。选择完整支持组和每组全部证据引用；"
                    "按已核验前置关系排序。正文与来源由服务端生成。没有匹配组的请求项仍缺依据。")}}
