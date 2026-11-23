"""Recover source-bound structure and candidate relationships from Normalized IR."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import re
from uuid import NAMESPACE_URL, uuid5

from .local_pipeline import ParseContractError, fingerprint


RECOVERY_VERSION = "dc.pdf-structure/1"
_NAMESPACE = uuid5(NAMESPACE_URL, "dc.document-ir/1.0/structure")
_AUXILIARY = {"header", "footer", "page_number", "aside_text", "page_footnote"}
_STEP = re.compile(r"^\s*(?:步骤\s*|Step\s+)([1-9][0-9]{0,3})(?:\s*[：:、.)）]|\s+(?=\S))\s*(?=\S)", re.I)
_LIST_STEP = re.compile(r"^\s*([1-9][0-9]{0,3})[.、)）]\s*(?=\D)")
_PROCEDURE_TITLE = re.compile(r"操作步骤|操作流程|维护步骤|\bprocedure\b|\boperating instructions\b", re.I)
_STEP_NOTE = re.compile(r"^\s*(?:步骤\s*(?P<cn>[1-9][0-9]{0,3})\s*(?P<cn_role>前置条件|警告)|"
                        r"(?P<en_role>precondition|warning)\s+for\s+step\s+(?P<en>[1-9][0-9]{0,3}))\s*[:：]\s*\S", re.I)
_UNSCOPED_NOTE = re.compile(r"^\s*(?:警告|前置条件|warning|precondition)\s*[:：]", re.I)
_GLOBAL_WARNING = re.compile(r"^\s*(?:全局安全警告|warning for all procedures)\s*[:：]\s*\S", re.I)
_IDENT = r"[A-Z]?[0-9]{1,5}(?:[.-][0-9]{1,4})*"
_LABEL_END = r"(?![A-Za-z0-9]|[.-][0-9])"
_FIGURE = re.compile(rf"^\s*(?:图\s*|Fig(?:ure)?\.?\s+)(?P<label>{_IDENT}){_LABEL_END}", re.I)
_FIGURE_REF = re.compile(rf"(?:见图\s*|\b(?:see|refer to)\s+fig(?:ure)?\.?\s+)(?P<label>{_IDENT}){_LABEL_END}", re.I)
_TABLE = re.compile(rf"^\s*(?:表\s*|Table\s+)(?P<label>{_IDENT}){_LABEL_END}", re.I)
_TABLE_CONT = re.compile(r"^\s*(?:[（(]\s*(?:续|continued)\s*[）)]|续表\b|continued\b)", re.I)
_ACTION = re.compile(r"按(?:下|住|动)|开启|打开|关闭|切换|\b(?:press|hold|open|close|switch)\b", re.I)
_SYMBOL = r"(?:[A-Za-zα-ωΑ-Ω]|\\(?:alpha|beta|gamma|delta|theta|lambda|mu|rho|sigma|phi|omega))(?:_(?:\{[A-Za-z0-9]{1,12}\}|[A-Za-z0-9]))?"
_TOKEN = re.compile(rf"{_SYMBOL}|\d+(?:\.\d+)?|[+\-*/=^(){{}}\[\]\s]")
_STYLED_SYMBOL = re.compile(r"\\mathsf\s*\{\s*(?P<letter>[A-Za-z])\s*\}(?:\s*_\s*\{(?P<sub>[A-Za-z0-9]{1,12})\})?")
_LAYOUT_TOKEN = re.compile(r"\\(?:frac|cdot|left|right)(?![A-Za-z])")
_TRAILING_TAG = re.compile(r"\\tag\s*\{(?:[IVX]{1,8}|[A-Z]?[0-9]{1,5})\}\s*$")
_FORMULA_SOURCE = re.compile(
    rf"^\s*(?:公式\s*|(?:equation|formula)\s+)[（(]?(?P<label>{_IDENT})[）)]?\s*"
    rf"(?:(?:中[，,]?\s*|[,，]\s*|where\s+)(?P<symbol>{_SYMBOL})\s*(?:表示|为|denotes\b|means\b)\s*\S|"
    rf"(?P<condition>适用条件\s*[:：]|(?:applies|valid)\s+(?:only\s+)?when\b)\s*\S)", re.I)
_NUMBER = r"[+\-−]?(?:\d+(?:\.\d+)?|\.\d+)(?:[eE][+\-−]?\d+)?"
_UNITS = r"°\s*[CF]|℃|℉|%|(?:[kMGTµμmcd]?(?:Pa|Hz|[mgsAVWLJ]))(?:[²³]|\^[23])?(?:\s*/\s*(?:s|min|h|[mL]))?|rpm|min|bar|ohm|Ω|K|N|h"
_PARAMETER = re.compile(
    rf"(?:^|(?<=[;；]))[ \t]*(?P<name>[^\W\d_][\w ()（）/-]{{0,40}}?)[ \t]*[:：=][ \t]*"
    rf"(?P<value>{_NUMBER}(?:[ \t]*(?:[~～–—]|至|to\b)[ \t]*{_NUMBER})?)"
    r"(?P<suffix>[^\r\n;；]*)(?=$|[;；])", re.M)
_UNIT = re.compile(rf"[ \t]*(?P<unit>{_UNITS})(?=[ \t]*(?:$|[。.;；,，]|[（(]))")
_PARAMETER_PREFIX = re.compile(r"^(?:步骤|Step\b|图(?:\s|\d)|表(?:\s|\d)|Fig(?:ure)?\b|Table\b|公式|Equation\b)", re.I)
_SOURCE_CLAUSE = re.compile(r"[^。！？；;\r\n]+(?:[。！？；;]|$)?")


def _id(*parts: str) -> str:
    return str(uuid5(_NAMESPACE, fingerprint(list(parts))))


def _field(block: dict, name: str) -> dict:
    ref = block["raw_ref"]
    return {**ref, "json_pointer": f"{ref['json_pointer']}/{name}"}


def _symbol_value(raw: str) -> str | None:
    if styled := _STYLED_SYMBOL.fullmatch(raw):
        letter, sub = styled.group("letter", "sub")
        return f"{letter}_{{{sub}}}" if sub else letter
    return raw if re.fullmatch(_SYMBOL, raw) else None


def _formula_ranges(expression: str) -> dict | None:
    """Recognize a bounded TeX subset and retain the exact source character spans."""
    if not expression or len(expression) > 2000:
        return None
    if tag := _TRAILING_TAG.search(expression):
        expression = expression[:tag.start()]
    occurrences = defaultdict(list)
    pos = 0
    while pos < len(expression):
        token = (_STYLED_SYMBOL.match(expression, pos) or _LAYOUT_TOKEN.match(expression, pos)
                 or _TOKEN.match(expression, pos))
        if token is None:
            return None
        if symbol := _symbol_value(token[0]):
            if token[0].isascii() and token[0].isalpha() and pos and expression[pos - 1].isascii() and expression[pos - 1].isalpha():
                return None
            occurrences[symbol].append({"source_start": pos, "source_end": token.end()})
        pos = token.end()
    return occurrences or None


def _part_value(block: dict, pointer: str):
    value = block["structured_content"]
    if pointer:
        if not pointer.startswith("/"):
            raise ParseContractError("IR_STRUCTURE_INVALID")
        try:
            for token in pointer[1:].split("/"):
                decoded = token.replace("~1", "/").replace("~0", "~")
                if isinstance(value, list):
                    if not re.fullmatch(r"0|[1-9][0-9]*", decoded):
                        raise ValueError
                    value = value[int(decoded)]
                else:
                    value = value[decoded]
        except (IndexError, KeyError, OverflowError, TypeError, ValueError):
            raise ParseContractError("IR_STRUCTURE_INVALID") from None
        if not isinstance(value, str | dict):
            raise ParseContractError("IR_STRUCTURE_INVALID")
    return value


def _validate_blocks(blocks: list, page_numbers: set, run_id: str) -> dict:
    by_id = {}
    ordinals = set()
    for block in blocks:
        if not isinstance(block, dict) or block.get("parse_run_id") != run_id:
            raise ParseContractError("IR_STRUCTURE_INVALID")
        identifier, ordinal = block.get("id"), block.get("ordinal")
        if (not isinstance(identifier, str) or not identifier or identifier in by_id
                or type(ordinal) is not int or ordinal < 0 or ordinal in ordinals
                or block.get("structure_role") not in {"container", "leaf", "observation"}
                or not isinstance(block.get("text_content"), str) or not isinstance(block.get("structured_content"), dict)
                or not isinstance(block.get("locator"), dict) or not isinstance(block.get("kind"), str)
                or (block.get("file_page") is not None and (type(block["file_page"]) is not int or block["file_page"] not in page_numbers))
                or (block.get("parent_block_id") is not None and not isinstance(block["parent_block_id"], str))
                or (block.get("reading_order") is not None and (type(block["reading_order"]) is not int or block["reading_order"] < 0))
                or (block["structure_role"] == "container" and block["text_content"])):
            raise ParseContractError("IR_STRUCTURE_INVALID")
        if block.get("source_kind") == "parser" and (not isinstance(block.get("raw_ref"), dict)
                or not isinstance(block["raw_ref"].get("json_pointer"), str) or block.get("file_page") is None):
            raise ParseContractError("IR_STRUCTURE_INVALID")
        by_id[identifier] = block
        ordinals.add(ordinal)
    visited = set()
    for identifier in by_id:
        chain = set()
        current = identifier
        while current is not None and current not in visited:
            if current in chain or current not in by_id:
                raise ParseContractError("IR_STRUCTURE_INVALID")
            chain.add(current)
            current = by_id[current].get("parent_block_id")
            if current is not None and (current not in by_id or by_id[current]["structure_role"] != "container"):
                raise ParseContractError("IR_STRUCTURE_INVALID")
        visited.update(chain)
    return by_id


class _Recovery:
    def __init__(self, document: dict) -> None:
        self.document = document
        self.run_id = document["parse_run_id"]
        self.blocks = document["blocks"]
        self.pages = {page["file_page"]: page for page in document["pages"]}
        self.by_id = _validate_blocks(self.blocks, self.pages.keys(), self.run_id)
        self.source = sorted(self.blocks, key=lambda block: block["ordinal"])
        self.source_children: dict = defaultdict(list)
        for block in self.source:
            self.source_children[block["parent_block_id"]].append(block)
        self.ordinal = max((block["ordinal"] for block in self.blocks), default=-1) + 1
        self.relations = document["relations"]
        self.relation_keys: set = set()
        self.section_of: dict[str, str] = {}
        self.section_titles: dict[str, dict] = {}
        self.qualified_pages: set[int] = set()
        self.order = 0
        self.root = self.container("document", self.run_id, None, {"rule": "document_identity", "raw_ref": document["raw_ref"]})

    def container(self, kind: str, anchor: str, parent: str | None, basis: dict, *, heading_level=None) -> dict:
        identifier = _id(self.run_id, kind, anchor)
        if identifier in self.by_id:
            raise ParseContractError("IR_STRUCTURE_INVALID")
        locator = {key: self.document[key] for key in ("document_version_id", "source_file_id", "source_sha256", "parse_run_id")}
        locator.update(schema_version="dc.source-locator.v1", block_id=identifier, regions=[])
        block = {"id": identifier, "parse_run_id": self.run_id, "ordinal": self.ordinal, "kind": kind,
                 "source_kind": "derived_structure", "structure_role": "container", "text_content": "",
                 "structured_content": {"basis": {**basis, "recovery_version": RECOVERY_VERSION}},
                 "locator": locator, "file_page": None, "raw_ref": None, "parent_block_id": parent,
                 "reading_order": None, "heading_level": heading_level}
        self.ordinal += 1
        self.by_id[identifier] = block
        self.blocks.append(block)
        return block

    def issue(self, code: str, block: dict | None = None, *, page: int | None = None, critical=True, **basis) -> None:
        if block is not None:
            page = block["file_page"]
        issue = {"id": f"structure-{len(self.document['issues']) + 1}", "code": code, "file_page": page,
                 "parse_run_id": self.run_id, "document_version_id": self.document["document_version_id"],
                 "rule_revision": RECOVERY_VERSION, "severity": "blocking" if critical else "review",
                 "state": "unresolved", "basis": basis}
        if block is not None:
            issue.update(block_id=block["id"], raw_ref=block.get("raw_ref"))
        self.document["issues"].append(issue)
        if page is not None:
            self.pages[page]["issue_refs"].append(issue["id"])

    def scope(self, block: dict, **fields) -> dict:
        return {"section_id": self.section_of.get(block["id"], self.root["id"]), **fields}

    def relation(self, kind, left, right, rule, *, from_part="", to_part="", scope=None, evidence=()) -> str:
        key = (left["id"], from_part, kind, right["id"], to_part)
        if key in self.relation_keys or left["parse_run_id"] != self.run_id or right["parse_run_id"] != self.run_id:
            raise ParseContractError("IR_STRUCTURE_INVALID")
        if kind == "continues" and left["ordinal"] >= right["ordinal"]:
            raise ParseContractError("IR_STRUCTURE_INVALID")
        _part_value(left, from_part)
        _part_value(right, to_part)
        identifier = _id(self.run_id, *key)
        self.relations.append({"id": identifier, "parse_run_id": self.run_id, "from_block_id": left["id"],
            "from_part": from_part, "relation_type": kind, "to_block_id": right["id"], "to_part": to_part,
            "basis": {"rule": rule, "recovery_version": RECOVERY_VERSION, "scope": scope or self.scope(left),
                      "evidence": list(evidence)}})
        self.relation_keys.add(key)
        return identifier

    def source_part(self, block: dict, role: str, bounds: tuple[int, int]) -> str | None:
        start, end = bounds
        if not 0 <= start < end <= len(block["text_content"]):
            return None
        structured = block["structured_content"]
        segments = []
        cursor = start
        for portion in structured.get("text_map", []):
            lo, hi = max(start, portion["block_start"]), min(end, portion["block_end"])
            if lo >= hi:
                continue
            if lo != cursor:
                return None
            raw_from, raw_to = portion["source_start"], portion["source_end"]
            if "encoding" in portion:
                if (lo, hi) != (portion["block_start"], portion["block_end"]):
                    return None
            else:
                raw_from += lo - portion["block_start"]
                raw_to -= portion["block_end"] - hi
            source = {"block_start": lo, "block_end": hi, "raw_ref": portion["raw_ref"],
                      "source_start": raw_from, "source_end": raw_to}
            if "encoding" in portion:
                source["encoding"] = portion["encoding"]
            segments.append(source)
            cursor = hi
        if cursor != end:
            return None
        parts = structured.setdefault("source_parts", [])
        entry = {"role": role, "block_start": start, "block_end": end,
                 "text": block["text_content"][start:end], "sources": segments}
        if entry in parts:
            index = parts.index(entry)
        else:
            index = len(parts)
            parts.append(entry)
        return f"/source_parts/{index}"

    def hierarchy(self) -> None:
        by_page = defaultdict(list)
        observations = [block for block in self.source_children[None] if block["structure_role"] == "observation"]
        for block in observations:
            block["parent_block_id"] = self.root["id"]
        roots = [block for block in self.source_children[None] if block["structure_role"] != "observation"]
        for block in roots:
            by_page[block["file_page"]].append(block)
        qualified = self.qualified_pages
        for page, blocks in by_page.items():
            indices = [block.get("provider_fields", {}).get("index") for block in blocks]
            if (page is not None and self.pages[page]["state"] not in {"failed", "not_requested"}
                    and all(type(index) is int and index >= 0 for index in indices)
                    and all(a < b for a, b in zip(indices, indices[1:]))):
                qualified.add(page)
            else:
                self.issue("STRUCTURE_READING_ORDER_UNRESOLVED", page=page, critical=False)
        stack = []
        previous_page = None
        for block in roots:
            page = block["file_page"]
            if page not in qualified or (previous_page is not None and page != previous_page
                    and any(self.pages[number]["state"] not in {"processed", "needs_review", "blank"}
                            for number in range(previous_page + 1, page))):
                stack.clear()
            previous_page = page
            if block["kind"] == "title":
                level = block.get("heading_level")
                if page not in qualified or type(level) is not int or not 1 <= level <= 6:
                    self.issue("STRUCTURE_HEADING_UNRESOLVED", block, critical=False)
                    stack.clear()
                else:
                    while stack and stack[-1]["heading_level"] >= level:
                        stack.pop()
                    parent = stack[-1]["id"] if stack else self.root["id"]
                    section = self.container("section", block["id"], parent, {"rule": "provider_heading_level",
                        "heading_block_id": block["id"], "raw_ref": _field(block, "level")}, heading_level=level)
                    self.section_titles[section["id"]] = block
                    stack.append(section)
            section_id = stack[-1]["id"] if stack else self.root["id"]
            block["parent_block_id"] = section_id
            self.assign_order(block, section_id, root=block if page in qualified else None)

    def assign_order(self, block: dict, section_id: str, root: dict | None) -> None:
        self.section_of[block["id"]] = section_id
        if root is not None and block["structure_role"] == "leaf":
            block["reading_order"] = self.order
            block["structured_content"]["reading_order_basis"] = {"rule": "provider_page_index_and_children",
                "root_index_ref": _field(root, "index"), "raw_ref": block["raw_ref"]}
            self.order += 1
        for child in self.source_children[block["id"]]:
            self.assign_order(child, section_id, root)

    def continuations(self) -> None:
        previous = None
        chains = {}
        for block in self.source_children[None]:
            provider = block.get("provider_type")
            if provider in _AUXILIARY:
                continue
            if block.get("provider_fields", {}).get("continues_prev") is not True:
                if provider == "table" and (label := self.table_label(block)) and label[2]:
                    self.issue("STRUCTURE_TABLE_CONTINUATION_UNRESOLVED", block)
                previous = block
                continue
            if (previous is None or previous.get("provider_type") != provider
                    or provider not in {"text", "paragraph", "ref_text", "list", "table"}
                    or block["file_page"] - previous["file_page"] not in {0, 1}
                    or self.section_of[previous["id"]] != self.section_of[block["id"]]
                    or block["file_page"] not in self.qualified_pages or previous["file_page"] not in self.qualified_pages
                    or (provider == "list" and any(node["provider_fields"].get("sub_type") != "ref_text" for node in (previous, block)))):
                self.issue("STRUCTURE_CONTINUATION_UNRESOLVED", block, critical=provider == "table")
                previous = block
                continue
            evidence = [_field(block, "continues_prev"), previous["raw_ref"]]
            if provider == "table":
                lead = self.table_label(previous)
                continuation = self.table_label(block)
                if not lead or not continuation or lead[0] != continuation[0] or not continuation[2]:
                    self.issue("STRUCTURE_TABLE_CONTINUATION_UNRESOLVED", block)
                    previous = block
                    continue
                evidence.extend([lead[1]["raw_ref"], continuation[1]["raw_ref"]])
            group = chains.get(previous["id"])
            if group is None:
                kind = "table_continuation" if provider == "table" else "paragraph_group"
                group = self.container(kind, previous["id"], previous["parent_block_id"],
                    {"rule": "provider_continuation", "leading_block_id": previous["id"], "evidence": evidence})
                previous["parent_block_id"] = group["id"]
            chains[block["id"]] = group
            block["parent_block_id"] = group["id"]
            self.relation("continues", previous, block, "provider_scoped_continues_prev",
                          scope=self.scope(block, group_id=group["id"]), evidence=evidence)
            previous = block

    def table_label(self, block: dict) -> tuple | None:
        labels = []
        for child in self.source_children[block["id"]]:
            if child.get("provider_type") != "table_caption":
                continue
            if match := _TABLE.match(child["text_content"]):
                labels.append((match["label"].casefold(), child, bool(_TABLE_CONT.match(child["text_content"][match.end():]))))
        return labels[0] if len(labels) == 1 else None

    def procedures(self) -> dict:
        steps = defaultdict(list)
        current = {}
        for block in self.source:
            if block["kind"] not in {"paragraph", "step"}:
                continue
            text = block["text_content"]
            if _STEP_NOTE.match(text):
                continue
            parent = self.by_id[block["parent_block_id"]]
            scope = self.scope(block)
            title = self.section_titles.get(scope["section_id"])
            match = _STEP.match(text)
            if match is None and parent["kind"] == "list" and title and _PROCEDURE_TITLE.search(title["text_content"]):
                match = _LIST_STEP.match(text)
            if match is None:
                if block["kind"] == "step":
                    self.issue("STRUCTURE_STEP_NUMBER_UNRESOLVED", block)
                continue
            part = self.source_part(block, "step_number", match.span(1))
            if part is None or block["reading_order"] is None:
                self.issue("STRUCTURE_STEP_NUMBER_UNRESOLVED", block)
                continue
            number = int(match[1])
            parent_id = parent["id"]
            prior = current.get(parent_id)
            if prior and number == prior[1] + 1:
                group = prior[0]
            else:
                if number != 1:
                    self.issue("STRUCTURE_STEP_SEQUENCE_UNRESOLVED", block, step_number=number)
                group = self.container("procedure", block["id"], parent_id, {"rule": "source_step_numbering",
                                       "first_step_id": block["id"], "first_step_part": part, "raw_ref": block["raw_ref"]})
            current[parent_id] = (group, number)
            block["parent_block_id"] = group["id"]
            block["structured_content"]["step"] = {"number": number, "part": part, "procedure_id": group["id"]}
            steps[(scope["section_id"], number)].append(block)
        return steps

    def step_requirements(self, steps: dict) -> None:
        for block in self.source:
            if block["kind"] not in {"paragraph", "warning"}:
                continue
            text = block["text_content"]
            scope = self.scope(block)
            if _GLOBAL_WARNING.match(text):
                part = self.source_part(block, "global_warning", (0, len(text)))
                if part:
                    self.relation("warns", block, self.root, "explicit_global_warning", from_part=part,
                                  scope={"document_root_id": self.root["id"]}, evidence=[block["raw_ref"]])
                    continue
            if match := _STEP_NOTE.match(text):
                number = int(match["cn"] or match["en"])
                targets = steps.get((scope["section_id"], number), [])
                warning = (match["cn_role"] or match["en_role"]).casefold() in {"警告", "warning"}
                source = self.source_part(block, "warning" if warning else "precondition", (0, len(text)))
                if len(targets) == 1 and source:
                    target = targets[0]
                    step = target["structured_content"]["step"]
                    evidence = [block["raw_ref"], target["raw_ref"]]
                    scope = self.scope(target, procedure_id=step["procedure_id"], step_number=number)
                    if warning:
                        self.relation("warns", block, target, "explicit_step_number_on_warning", from_part=source,
                                      to_part=step["part"], scope=scope, evidence=evidence)
                    else:
                        self.relation("requires", target, block, "explicit_step_number_on_precondition", from_part=step["part"],
                                      to_part=source, scope=scope, evidence=evidence)
                    continue
                self.issue("STRUCTURE_STEP_REQUIREMENT_SCOPE_UNRESOLVED", block, step_number=number)
            elif block["kind"] == "warning" or _UNSCOPED_NOTE.match(text):
                self.issue("STRUCTURE_STEP_REQUIREMENT_SCOPE_UNRESOLVED", block)

    def formula_relations(self) -> None:
        formulas = defaultdict(list)
        required_symbols = {}
        for block in self.source:
            if block["kind"] != "formula":
                continue
            structured = block["structured_content"]
            label = structured.get("equation_label", "").strip(" ()（）")
            expression = structured.get("latex_raw", "")
            if re.fullmatch(_IDENT, label) and block["reading_order"] is not None:
                formulas[(self.scope(block)["section_id"], label.casefold())].append(block)
            occurrences = _formula_ranges(expression)
            if occurrences is None:
                self.issue("STRUCTURE_FORMULA_SYMBOLS_UNRESOLVED", block)
                continue
            raw_formula = next((part["raw_ref"] for part in structured.get("parts", []) if part["kind"] == "latex_raw"), None)
            if raw_formula is None:
                self.issue("STRUCTURE_FORMULA_SYMBOLS_UNRESOLVED", block)
                continue
            structured["formula_symbols"] = [{"symbol": symbol, "raw_ref": raw_formula, "ranges": ranges}
                                             for symbol, ranges in occurrences.items()]
            required_symbols[block["id"]] = {symbol: f"/formula_symbols/{index}" for index, symbol in enumerate(occurrences)}
        definitions: dict = defaultdict(list)
        conditions: dict = defaultdict(list)
        for block in self.source:
            if block["kind"] not in {"paragraph", "footnote"}:
                continue
            text = block["text_content"]
            if not (match := _FORMULA_SOURCE.match(text)):
                continue
            targets = formulas.get((self.scope(block)["section_id"], match["label"].casefold()), [])
            if len(targets) != 1 or block["reading_order"] is None:
                self.issue("STRUCTURE_FORMULA_TARGET_UNRESOLVED", block)
                continue
            target = targets[0]
            symbol = match["symbol"]
            part = self.source_part(block, "variable_definition" if symbol else "formula_condition", (0, len(text)))
            if part is None or (symbol and symbol not in required_symbols.get(target["id"], set())):
                self.issue("STRUCTURE_FORMULA_TARGET_UNRESOLVED", block)
                continue
            if symbol:
                definitions[(target["id"], symbol)].append((block, part))
            else:
                conditions[target["id"]].append((block, part))
        for block in self.source:
            if block["kind"] != "formula":
                continue
            structured = block["structured_content"]
            expression = "/latex_raw" if "latex_raw" in structured else "/mathml_raw" if "mathml_raw" in structured else ""
            for symbol, symbol_part in required_symbols.get(block["id"], {}).items():
                candidates = definitions.get((block["id"], symbol), [])
                if len(candidates) != 1:
                    self.issue("STRUCTURE_FORMULA_DEFINITION_UNRESOLVED", block, symbol=symbol)
                    continue
                source, part = candidates[0]
                self.relation("defines", source, block, "explicit_equation_label_and_symbol", from_part=part, to_part=symbol_part,
                              scope=self.scope(block, symbol=symbol), evidence=[source["raw_ref"], block["raw_ref"]])
                if source["id"] not in structured["definition_refs"]:
                    structured["definition_refs"].append(source["id"])
            applicable = conditions.get(block["id"], [])
            if len(applicable) != 1:
                self.issue("STRUCTURE_FORMULA_CONDITION_UNRESOLVED", block)
            else:
                source, part = applicable[0]
                self.relation("requires", block, source, "explicit_equation_application_condition", from_part=expression,
                              to_part=part, scope=self.scope(block), evidence=[block["raw_ref"], source["raw_ref"]])
                structured["condition_refs"].append(source["id"])

    def parameter_units(self) -> None:
        for block in self.source:
            if block["kind"] not in {"paragraph", "step", "warning"}:
                continue
            for match in _PARAMETER.finditer(block["text_content"]):
                if _PARAMETER_PREFIX.match(match["name"]):
                    continue
                if unit_match := _UNIT.match(match["suffix"]):
                    name = self.source_part(block, "parameter", match.span("name"))
                    value = self.source_part(block, "parameter_value", match.span("value"))
                    unit_bounds = tuple(match.start("suffix") + position for position in unit_match.span("unit"))
                    unit = self.source_part(block, "parameter_unit", unit_bounds)
                    if name and value and unit:
                        self.relation("unit_of", block, block, "explicit_parameter_assignment", from_part=unit, to_part=value,
                                      scope=self.scope(block, parameter_part=name), evidence=[block["raw_ref"]])
                        continue
                self.issue("STRUCTURE_PARAMETER_UNIT_UNRESOLVED", block, block_range=list(match.span("value")))

    def tables(self) -> None:
        for block in self.source:
            if block["kind"] != "table":
                continue
            structured = block["structured_content"]
            rows = structured.get("rows")
            if not rows:
                self.issue("STRUCTURE_TABLE_HEADERS_UNRESOLVED", block)
                continue
            cells = _table_cells(rows, structured.get("column_count"))
            header_rows = set()
            for row, cells_in_row in enumerate(rows):
                if (not cells_in_row or not all(cell["header"] and cell.get("header_scope") not in {"row", "rowgroup"}
                        for cell in cells_in_row)):
                    break
                header_rows.add(row)
            header_columns = defaultdict(list)
            row_headers = defaultdict(list)
            for loc in cells.values():
                _, cell = loc
                if not cell["header"] or cell.get("value_state") != "extracted":
                    continue
                scope = cell.get("header_scope")
                if scope in {"col", None} and (cell["row"] in header_rows or cell.get("row_group") == "thead"):
                    for c in range(cell["column"], cell["column"] + cell["colspan"]):
                        header_columns[c].append(loc)
                elif scope == "row":
                    for r in range(cell["row"], cell["row"] + cell["rowspan"]):
                        row_headers[r].append(loc)
            table_relation_count = 0
            for position, (pointer, cell) in cells.items():
                if table_relation_count >= 20_000:
                    self.issue("STRUCTURE_TABLE_RELATION_LIMIT", block)
                    break
                if cell["header"]:
                    continue
                sources = {}
                row, col = position
                column_covered = set()
                row_covered = set()
                for column in range(col, col + cell["colspan"]):
                    for header_ptr, header in header_columns[column]:
                        if len(sources) > 64 or header["row"] >= row:
                            break
                        if header["row"] + header["rowspan"] <= row:
                            sources[header_ptr] = (header, "column")
                            column_covered.add(column)
                for index in range(row, row + cell["rowspan"]):
                    for header_ptr, header in row_headers[index]:
                        if len(sources) > 64 or header["column"] >= col:
                            break
                        if header["column"] + header["colspan"] <= col:
                            sources[header_ptr] = (header, "row")
                            row_covered.add(index)
                if len(sources) > 64 or (len(column_covered) != cell["colspan"] and len(row_covered) != cell["rowspan"]):
                    self.issue("STRUCTURE_TABLE_CELL_HEADER_UNRESOLVED", block, cell_ref={"row": row, "column": col})
                    if len(sources) > 64:
                        continue
                for header_ptr, (header, axis) in sources.items():
                    header_ref = {"row": header["row"], "column": header["column"]}
                    evidence = [_cell_evidence(header), _cell_evidence(cell)]
                    manual = structured.get("manual_correction", {}).get("column_headers_ref") if header["row"] == 0 else None
                    if manual is not None:
                        evidence.append({**manual, "json_pointer": f"{manual['json_pointer']}/{header['column']}"})
                    identifier = self.relation("header_of", block, block, "review_candidate_column_header" if manual else "html_header_axis_and_grid",
                        from_part=header_ptr, to_part=pointer, scope=self.scope(block, table_id=block["id"],
                            axis=axis, header_cell=header_ref, value_cell={"row": row, "column": col}),
                        evidence=evidence)
                    cell["header_refs"].append({**header_ref, "relation_id": identifier})
                    table_relation_count += 1
            assets_by_cell = defaultdict(list)
            for index, asset in enumerate(structured.get("asset_parts", [])):
                if cell_ref := asset.get("cell_ref"):
                    assets_by_cell[cell_ref["row"], cell_ref["column"]].append((index, asset))
            for (row, column), assets in assets_by_cell.items():
                cell_ref = {"row": row, "column": column}
                location = cells.get((row, column))
                if not location:
                    self.issue("STRUCTURE_TABLE_ASSET_CELL_UNRESOLVED", block, cell_ref=cell_ref)
                    continue
                pointer, cell = location
                if len(assets) != 1 or not _ACTION.search(cell["text"]):
                    self.issue("STRUCTURE_TABLE_ASSET_ACTION_UNRESOLVED", block, cell_ref=cell_ref)
                    continue
                index, asset = assets[0]
                self.relation("refers_to", block, block, "action_and_asset_in_same_html_cell",
                    from_part=pointer, to_part=f"/asset_parts/{index}",
                    scope=self.scope(block, table_id=block["id"], cell_ref=cell_ref),
                    evidence=[_cell_evidence(cell), asset["raw_ref"]])

    def figures(self) -> None:
        labels: dict = defaultdict(list)
        visual_ids = set()
        linked_action_ids = set()
        for group in self.source:
            provider = group.get("provider_type")
            if provider not in {"image", "chart", "table", "code"}:
                continue
            children = self.source_children[group["id"]]
            body_types = {f"{provider}_body"} | ({"algorithm_body"} if provider == "code" else set())
            bodies = [child for child in children if child.get("provider_type") in body_types]
            captions = [child for child in children if child.get("provider_type") == f"{provider}_caption"]
            if not captions and provider in {"image", "chart"}:
                corrected = [child for child in children if child["kind"] == "caption" and child["source_kind"] == "manual"
                             and child.get("provider_type") == f"{provider}_footnote" and _FIGURE.match(child["text_content"])]
                if len(corrected) == 1:
                    captions = corrected
            if len(bodies) != 1:
                continue  # The normalization issue preserves the untrusted container.
            body = bodies[0]
            for caption in captions:
                if not caption["text_content"].strip():
                    self.issue("STRUCTURE_CAPTION_CONTENT_UNRESOLVED", caption)
                    continue
                evidence = [group["raw_ref"], caption["raw_ref"], body["raw_ref"]]
                if caption.get("provider_type") == f"{provider}_footnote":
                    evidence.append(caption["structured_content"]["manual_correction"]["request_ref"])
                self.relation("caption_of", caption, body, "provider_visual_container_membership",
                    scope=self.scope(body, visual_group_id=group["id"]),
                    evidence=evidence)
            if provider not in {"image", "chart"}:
                continue
            visual_ids.add(body["id"])
            if not any(caption["text_content"].strip() for caption in captions):
                self.issue("STRUCTURE_FIGURE_CAPTION_UNRESOLVED", body)
            for caption in captions:
                if match := _FIGURE.match(caption["text_content"]):
                    scope = self.scope(body)
                    labels[(scope["section_id"], match["label"].casefold())].append((body, caption))
        for block in self.source:
            step = block["structured_content"].get("step")
            if not step:
                continue
            text = block["text_content"]
            for match in _FIGURE_REF.finditer(text):
                scope = self.scope(block)
                sources = labels.get((scope["section_id"], match["label"].casefold()), [])
                if len(sources) != 1:
                    self.issue("STRUCTURE_FIGURE_TARGET_UNRESOLVED", block)
                    continue
                body, caption = sources[0]
                part = self.source_part(block, "figure_reference", match.span())
                clause = re.split(r"[。！？;；\n]|[.!?]\s+", text[:match.start()])[-1]
                if not part or not _ACTION.search(clause):
                    self.issue("STRUCTURE_FIGURE_ACTION_UNRESOLVED", block)
                    continue
                self.relation("refers_to", block, body, "numbered_step_explicit_figure_reference", from_part=part,
                    scope=self.scope(block, procedure_id=step["procedure_id"], step_number=step["number"]),
                    evidence=[block["raw_ref"], caption["raw_ref"], body["raw_ref"]])
                linked_action_ids.add(body["id"])
        for block in self.source:
            if block["structure_role"] != "observation" and block["kind"] in {"image", "chart"} and block["id"] not in linked_action_ids:
                if block["id"] not in visual_ids:
                    self.issue("STRUCTURE_FIGURE_CAPTION_UNRESOLVED", block)
                self.issue("STRUCTURE_FIGURE_ACTION_UNRESOLVED", block)

    def finish(self) -> None:
        _validate_blocks(self.blocks, self.pages.keys(), self.run_id)
        children = defaultdict(list)
        for block in sorted(self.blocks, key=lambda value: value["ordinal"]):
            original = block.pop("content_hash", None)
            if original is not None:
                block["normalized_content_hash"] = original
            children[block["parent_block_id"]].append(block["id"])

        first_order: dict[str, int] = {}

        def first_source(identifier: str) -> int:
            if identifier not in first_order:
                block = self.by_id[identifier]
                first_child = min((first_source(child) for child in children[identifier]), default=block["ordinal"])
                first_order[identifier] = min(first_child, block["ordinal"])
            return first_order[identifier]

        for block in self.blocks:
            if block["structure_role"] == "container":
                block["structured_content"]["child_refs"] = sorted(children[block["id"]], key=first_source)
            block["content_hash"] = fingerprint(block)


def _cell_evidence(cell: dict) -> dict:
    return {**cell["locator"]["raw_ref"], "html_start": cell["html_start"], "html_end": cell["html_end"],
            "cell_ref": {"row": cell["row"], "column": cell["column"]}}


def _table_cells(rows, width) -> dict:
    if not isinstance(rows, list) or not 0 < len(rows) <= 2048 or type(width) is not int or not 0 < width <= 512:
        raise ParseContractError("IR_STRUCTURE_INVALID")
    result = {}
    used = set()
    for row, cells in enumerate(rows):
        if not isinstance(cells, list) or len(cells) > width:
            raise ParseContractError("IR_STRUCTURE_INVALID")
        for offset, cell in enumerate(cells):
            if not isinstance(cell, dict) or type(cell.get("row")) is not int or cell["row"] != row:
                raise ParseContractError("IR_STRUCTURE_INVALID")
            col, rows_spanned, cols_spanned = (cell.get(field) for field in ("column", "rowspan", "colspan"))
            if (any(type(value) is not int for value in (col, rows_spanned, cols_spanned))
                    or not 0 <= col < width or not 0 < rows_spanned <= len(rows) - row or not 0 < cols_spanned <= width - col
                    or len(used) + rows_spanned * cols_spanned > 65536 or type(cell.get("header")) is not bool
                    or not isinstance(cell.get("header_refs"), list) or cell["header_refs"]):
                raise ParseContractError("IR_STRUCTURE_INVALID")
            slots = {(r, c) for r in range(row, row + rows_spanned) for c in range(col, col + cols_spanned)}
            if used & slots:
                raise ParseContractError("IR_STRUCTURE_INVALID")
            used.update(slots)
            result[row, col] = (f"/rows/{row}/{offset}", cell)
    return result


def recover_structure(normalized: dict, *, normalized_artifact_id: str | None = None) -> dict:
    """Derive a structural candidate without changing the normalized artifact."""
    if (not isinstance(normalized, dict) or (normalized.get("schema"), normalized.get("schema_version"),
            normalized.get("phase")) != ("dc.document-ir", "1.0", "normalized") or normalized.get("relations") != []
            or any(not isinstance(normalized.get(field), list) for field in ("pages", "blocks", "issues"))
            or any(not isinstance(normalized.get(field), str) or not normalized[field]
                for field in ("parse_run_id", "document_version_id", "source_file_id", "source_sha256"))
            or not isinstance(normalized.get("raw_ref"), dict)):
        raise ParseContractError("IR_STRUCTURE_INVALID")
    for number, page in enumerate(normalized["pages"], 1):
        if (not isinstance(page, dict) or type(page.get("file_page")) is not int or page["file_page"] != number
                or page.get("parse_run_id") != normalized["parse_run_id"] or not isinstance(page.get("issue_refs"), list)):
            raise ParseContractError("IR_STRUCTURE_INVALID")
    candidate = deepcopy(normalized)
    candidate.update(phase="structured", recovery_version=RECOVERY_VERSION,
                     normalized_input={"content_hash": fingerprint(normalized)})
    if normalized_artifact_id is not None:
        candidate["normalized_input"]["artifact_id"] = str(normalized_artifact_id)
    recovery = _Recovery(candidate)
    recovery.hierarchy()
    recovery.continuations()
    steps = recovery.procedures()
    recovery.step_requirements(steps)
    recovery.formula_relations()
    recovery.parameter_units()
    recovery.tables()
    recovery.figures()
    recovery.finish()
    return candidate
