"""Source-level technical quality control for a frozen Structured PDF IR."""

from __future__ import annotations

from collections import Counter, defaultdict
from math import isfinite
import re
import unicodedata

from .ir import RAW_MEMBER, RENDERER
from .local_pdf import normalized_bbox
from .local_pipeline import ParseContractError, _member_name, fingerprint
from .structure import RECOVERY_VERSION, _part_value, _symbol_value


CHECKER_REVISION = "dc.pdf-source-qc/1"
_IDENTITY = ("parse_run_id", "document_version_id", "source_file_id", "source_sha256")
_CHECKS = ("integrity", "pages", "sources", "text_mapping", "structure", "relations", "critical_content", "inventory")
_HASH = re.compile(r"[a-f0-9]{64}")
_POINTER = re.compile(r"(?:/(?:[^~/]|~[01])*)*")
_REVIEW_FIELDS = {"content_hash", "review_status", "review", "reviewed_at", "reviewer_id", "reviewed_by", "revision"}
_ROLES = {"leaf", "container", "observation"}
_RELATION_TYPES = {"caption_of", "defines", "unit_of", "header_of", "warns", "requires", "continues", "refers_to", "covered_by"}
_RELATION_KEYS = ("id", "parse_run_id", "from_block_id", "from_part", "relation_type", "to_block_id", "to_part", "basis")
_FORMULA_WORD = re.compile(r"公式\s*[（(]?[A-Z]?\d|(?:equation|formula)\s+[\d(]|^[ \t]*[A-Za-zα-ω]\s*=(?![=])", re.I | re.M)
_PARAMETER_WORD = re.compile(
    r"(?:参数|温度|压力|速度|电压|电流|转速|功率|流量|距离|频率|设定值|额定值|setpoint|temperature|pressure|voltage|speed|power|current|frequency)"
    r"[ \t：:=]*(?:为|约|至少|不超过|应为|must\s+be)?[ \t：:=]*[+−-]?\d", re.I)
_UNIT_WORD = re.compile(r"\d\s*(?:°\s*[CF]|℃|℉|%|[kMGTµμm]?(?:Pa|V|A|Hz|g|W|L|m|s)|rpm|bar|N·m|Ω|dB|K|N|h)(?![A-Za-z])", re.I)
_CONDITION_WORD = re.compile(r"前置条件|操作条件|适用条件|仅在|只有|必须先|\b(?:precondition|only\s+if|applies\s+when|required\s+before)\b", re.I)
_STEP_WORD = re.compile(r"(?:^|[\n。；])\s*(?:步骤\s*\d+\s*[:：、.]|step\s+\d+\s*[:.]|按下|打开|关闭|开启|切换|press\b|hold\b|switch\b)", re.I)
_WARNING_WORD = re.compile(r"(?:^|[\s\n。；])(?:警告|危险|小心|注意|warning|caution|danger)\s*[:：]|"
                           r"步骤\s*\d+\s*警告|\bwarning\s+for\s+step\b", re.I)
_NUMBER_WORD = re.compile(r"[+−-]?\d+(?:[.,]\d+)?")


def _digest(value) -> bool:
    return isinstance(value, str) and _HASH.fullmatch(value) is not None


def _in(source, values) -> bool:
    return isinstance(source, str) and source in values


def _interval(start, end, length=None) -> bool:
    return (type(start) is int and type(end) is int and 0 <= start < end and (length is None or end <= length))


def _critical_tags(block: dict) -> list[str]:
    text = block["text_content"]
    structured = block["structured_content"]
    kind = block["kind"]
    tags = set()
    if block.get("source_kind") == "manual":
        tags.add("manual_correction")
    if kind == "table":
        tags.add("table")
    parts = structured.get("parts")
    if kind == "formula" or _FORMULA_WORD.search(text) or (isinstance(parts, list) and any(
            isinstance(part, dict) and part.get("kind") == "equation_inline" for part in parts)):
        tags.add("formula")
    if _PARAMETER_WORD.search(text):
        tags.add("parameter")
    if _UNIT_WORD.search(text):
        tags.add("unit")
    if kind == "condition" or _CONDITION_WORD.search(text):
        tags.add("operation_condition")
    if kind == "step" or structured.get("step") or _STEP_WORD.search(text):
        tags.add("operation_step")
    if kind == "warning" or _WARNING_WORD.search(text):
        tags.add("warning")
    return sorted(tags)


class _QC:
    def __init__(self, ir: dict, inputs: dict) -> None:
        self.ir = ir
        self.identity = {key: ir[key] for key in _IDENTITY}
        self.run_id = self.identity["parse_run_id"]
        self.inputs = inputs
        self.checks = {name: {"status": "passed", "issue_refs": []} for name in _CHECKS}
        self.issues: list[dict] = []
        self.artifacts: dict[str, dict] = {}
        self.pages: dict[int, dict] = {}
        self.blocks: dict[str, dict] = {}
        self.by_page: dict[int, list[dict]] = defaultdict(list)
        self.children: dict[str, list[str]] = defaultdict(list)
        self.leaves: list[str] = []
        self.containers: list[str] = []
        self.observations: list[str] = []
        self.observation_disposition: dict[str, str] = {}
        self.critical: dict[str, list[str]] = {}
        self.leaf_fingerprints: dict[str, str] = {}
        self.relations: dict[str, dict] = {}
        self.by_from: dict[str, list[dict]] = defaultdict(list)
        self.by_to: dict[str, list[dict]] = defaultdict(list)
        self.relation_fingerprints: dict[str, str] = {}

    def issue(self, check, code, *, failed=False, block_id=None, file_page=None, relation_id=None, **basis) -> None:
        identifier = f"source-qc-{len(self.issues) + 1}"
        if _in(block_id, self.blocks) and file_page is None:
            file_page = self.blocks[block_id].get("file_page")
        status = "failed" if failed else "needs_review"
        issue = {"id": identifier, "checker_revision": CHECKER_REVISION, "check": check, "code": code,
                 "severity": "blocking" if failed else "review", "status": status, "state": "unresolved",
                 "parse_run_id": self.run_id, "document_version_id": self.identity["document_version_id"],
                 "file_page": file_page, "basis": basis}
        if isinstance(block_id, str) and block_id:
            issue["block_id"] = block_id
        if relation_id:
            issue["relation_id"] = relation_id
        self.issues.append(issue)
        record = self.checks[check]
        record["issue_refs"].append(identifier)
        if record["status"] != "failed":
            record["status"] = status

    def refs(self, ref, *, owner=None, preflight=False) -> bool:
        if not isinstance(ref, dict):
            return False
        artifact = ref.get("artifact_id")
        pointer = ref.get("json_pointer")
        if not _in(artifact, self.artifacts) or not isinstance(pointer, str) or not _POINTER.fullmatch(pointer):
            return False
        kind = self.artifacts[artifact]["kind"]
        if not (kind == "raw_archive" and ref.get("raw_member_name") == RAW_MEMBER
                or kind == "structured" and self.artifacts[artifact].get("role") == "manual_correction" and ref.get("raw_member_name") is None
                or preflight and kind == "preflight" and ref.get("raw_member_name") is None):
            return False
        if owner is not None:
            return isinstance(owner, dict) and owner.get("artifact_id") == artifact and (
                owner.get("json_pointer") == pointer or (isinstance(owner.get("json_pointer"), str)
                    and pointer.startswith(owner["json_pointer"] + "/")))
        return True

    def integrity(self) -> None:
        normalized = self.ir.get("normalized_input")
        if (self.ir.get("recovery_version") != RECOVERY_VERSION or not isinstance(normalized, dict)
                or not _digest(normalized.get("content_hash")) or not normalized.get("artifact_id")):
            self.issue("integrity", "QC_NORMALIZED_INPUT_BINDING_MISSING", failed=True)
        if any(not _digest(value) for value in self.inputs.values()) or not _digest(self.identity["source_sha256"]):
            self.issue("integrity", "QC_ARTIFACT_DIGEST_INVALID", failed=True)
        refs = self.ir.get("artifact_refs")
        if not isinstance(refs, list):
            self.issue("sources", "QC_ARTIFACT_REFS_INVALID", failed=True)
            return
        paths = set()
        for ref in refs:
            artifact, kind = (ref.get("artifact_id"), ref.get("kind")) if isinstance(ref, dict) else (None, None)
            if not (isinstance(artifact, str) and artifact and isinstance(kind, str) and artifact not in self.artifacts):
                self.issue("sources", "QC_ARTIFACT_REFS_INVALID", failed=True)
                continue
            path = ref.get("raw_member_name")
            standalone = (kind == "structured" and ref.get("role") == "manual_correction"
                          or kind == "document_ir" and ref.get("role") == "parent_normalized_ir")
            if standalone and path is not None:
                self.issue("sources", "QC_STANDALONE_SOURCE_INVALID", failed=True, artifact_id=artifact)
                continue
            if kind not in {"raw_archive", "preflight"} and not standalone:
                try:
                    if not isinstance(path, str) or _member_name(path) != path or path in paths:
                        raise ParseContractError("PARSE_ARCHIVE_UNSAFE")
                except ParseContractError:
                    self.issue("sources", "QC_ASSET_MEMBER_INVALID", failed=True, artifact_id=artifact)
                    continue
                paths.add(path)
            if "parse_run_id" in ref and ref["parse_run_id"] != self.run_id:
                self.issue("sources", "QC_ASSET_RUN_MISMATCH", failed=True, artifact_id=artifact)
                continue
            self.artifacts[artifact] = ref
        if not self.refs(self.ir.get("raw_ref")) or self.ir["raw_ref"]["json_pointer"] != "":
            self.issue("sources", "QC_ROOT_RAW_REF_INVALID", failed=True)

    def read_pages(self) -> None:
        count, pages = self.ir.get("page_count"), self.ir.get("pages")
        if type(count) is not int or not 0 < count <= 600 or not isinstance(pages, list):
            self.issue("pages", "QC_PAGE_MANIFEST_INVALID", failed=True)
            return
        for index, page in enumerate(pages, 1):
            number = page.get("file_page") if isinstance(page, dict) else None
            if type(number) is not int or not 1 <= number <= count or number in self.pages:
                self.issue("pages", "QC_PAGE_MANIFEST_INVALID", failed=True)
                continue
            self.pages[number] = page
            if number != index or page.get("parse_run_id") != self.run_id:
                self.issue("pages", "QC_PAGE_MANIFEST_INVALID", failed=True, file_page=number)
            state = page.get("state")
            if state == "failed":
                self.issue("pages", "QC_PAGE_FAILED", failed=True, file_page=number)
            elif state in ("not_requested", "pending"):
                self.issue("pages", "QC_PAGE_NOT_PROCESSED", file_page=number, page_state=state)
            elif state not in ("processed", "needs_review", "blank"):
                self.issue("pages", "QC_PAGE_STATE_INVALID", failed=True, file_page=number)
            elif (not self.refs(page.get("raw_ref"), owner=self.ir.get("raw_ref"))
                  or re.fullmatch(r"/pages/(?:0|[1-9][0-9]*)", page["raw_ref"]["json_pointer"]) is None
                  or page.get("provider_page_idx") != number - 1):
                self.issue("sources", "QC_PAGE_RAW_REF_INVALID", failed=True, file_page=number)
            if state == "needs_review":
                self.issue("pages", "QC_PAGE_REVIEW_NEEDED", file_page=number)
        for number in sorted(set(range(1, count + 1)) - self.pages.keys()):
            self.issue("pages", "QC_PAGE_MISSING", failed=True, file_page=number)

    def read_blocks(self) -> None:
        blocks = self.ir.get("blocks")
        if not isinstance(blocks, list):
            self.issue("structure", "QC_BLOCK_MANIFEST_INVALID", failed=True)
            return
        ordinals = set()
        for raw in blocks:
            identifier = raw.get("id") if isinstance(raw, dict) else None
            if (not isinstance(identifier, str) or not identifier or identifier in self.blocks
                    or raw.get("parse_run_id") != self.run_id or not _in(raw.get("structure_role"), _ROLES)
                    or not isinstance(raw.get("structured_content"), dict) or not isinstance(raw.get("locator"), dict)
                    or not isinstance(raw.get("text_content"), str) or not isinstance(raw.get("kind"), str)
                    or type(raw.get("ordinal")) is not int or raw["ordinal"] < 0 or raw["ordinal"] in ordinals):
                self.issue("structure", "QC_BLOCK_MANIFEST_INVALID", failed=True)
                continue
            self.blocks[identifier] = raw
            ordinals.add(raw["ordinal"])
        for identifier, block in sorted(self.blocks.items(), key=lambda item: item[1]["ordinal"]):
            role = block["structure_role"]
            selected = {"leaf": self.leaves, "container": self.containers, "observation": self.observations}
            selected[role].append(identifier)
            parent = block.get("parent_block_id")
            page = block.get("file_page")
            if parent is not None:
                owner = self.blocks.get(parent) if isinstance(parent, str) else None
                if owner and owner["structure_role"] == "container" and owner.get("file_page") in (None, page):
                    self.children[parent].append(identifier)
                else:
                    self.issue("structure", "QC_BLOCK_PARENT_INVALID", failed=True, block_id=identifier)
            if page is not None:
                if type(page) is int and page in self.pages:
                    self.by_page[page].append(block)
                else:
                    self.issue("pages", "QC_BLOCK_PAGE_INVALID", failed=True, block_id=identifier)
            if role == "leaf":
                self.critical[identifier] = _critical_tags(block)
            immutable = {key: value for key, value in block.items() if key not in _REVIEW_FIELDS}
            expected = fingerprint(immutable)
            if block.get("content_hash") != expected:
                self.issue("integrity", "QC_BLOCK_CONTENT_HASH_MISMATCH", failed=True, block_id=identifier)
            if role == "leaf":
                self.leaf_fingerprints[identifier] = expected

    def structure(self) -> None:
        completed = set()
        for identifier in self.blocks:
            current = identifier
            chain = set()
            while current is not None and _in(current, self.blocks) and current not in completed:
                if current in chain:
                    self.issue("structure", "QC_PARENT_CYCLE", failed=True, block_id=identifier)
                    break
                chain.add(current)
                current = self.blocks[current].get("parent_block_id")
            completed.update(chain)
        orders = []
        for identifier, item in sorted(self.blocks.items(), key=lambda pair: pair[1]["ordinal"]):
            role = item["structure_role"]
            order = item.get("reading_order")
            if role == "leaf":
                if order is None:
                    self.issue("structure", "QC_READING_ORDER_UNKNOWN", block_id=identifier)
                elif type(order) is not int or order < 0:
                    self.issue("structure", "QC_READING_ORDER_INVALID", failed=True, block_id=identifier)
                else:
                    orders.append(order)
            elif order is not None:
                self.issue("structure", "QC_READING_ORDER_INVALID", failed=True, block_id=identifier)
            if role == "container":
                refs = item["structured_content"].get("child_refs")
                if (item["text_content"] or not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs)
                        or len(set(refs)) != len(refs) or set(refs) != set(self.children[identifier])):
                    self.issue("structure", "QC_CONTAINER_CONTENT_INVALID", failed=True, block_id=identifier)
            if role == "observation" and item["text_content"]:
                self.issue("inventory", "QC_OBSERVATION_AS_BODY_TEXT", failed=True, block_id=identifier)
        if orders != list(range(len(orders))):
            self.issue("structure", "QC_READING_ORDER_INVALID", failed=True)

    def source_bindings(self) -> None:
        for identifier, block in self.blocks.items():
            locator = block["locator"]
            source = block.get("source_kind")
            role = block["structure_role"]
            page = block.get("file_page")
            if (locator.get("schema_version") != "dc.source-locator.v1" or locator.get("block_id") != identifier
                    or any(locator.get(key) != value for key, value in self.identity.items())):
                self.issue("sources", "QC_LOCATOR_IDENTITY_INVALID", failed=True, block_id=identifier)
            if source in {"parser", "manual"}:
                page_ref = self.pages[page].get("raw_ref") if type(page) is int and page in self.pages else None
                ref = block.get("raw_ref")
                if (not page_ref or not self.refs(ref, owner=page_ref) or locator.get("raw_ref") != ref
                        or not ref["json_pointer"].startswith(page_ref["json_pointer"] + "/blocks/")):
                    self.issue("sources", "QC_BLOCK_RAW_REF_INVALID", failed=True, block_id=identifier)
                if source == "manual":
                    manual = block["structured_content"].get("manual_correction", {})
                    if (not isinstance(block.get("source_block_ref"), str) or not self.refs(manual.get("request_ref"))
                            or manual.get("requires_source_review") is not True):
                        self.issue("sources", "QC_MANUAL_SOURCE_INVALID", failed=True, block_id=identifier)
            elif source == "native_inventory":
                ref = block.get("raw_ref")
                if (role != "observation" or not self.refs(ref, preflight=True) or locator.get("raw_ref") != ref
                        or self.artifacts[ref["artifact_id"]]["kind"] != "preflight"):
                    self.issue("sources", "QC_OBSERVATION_RAW_REF_INVALID", failed=True, block_id=identifier)
            elif source != "derived_structure" or role != "container" or block.get("raw_ref") is not None or page is not None:
                self.issue("sources", "QC_BLOCK_SOURCE_KIND_UNSUPPORTED", failed=True, block_id=identifier)
            regions = locator.get("regions")
            if not isinstance(regions, list) or (role == "container" and regions):
                self.issue("sources", "QC_REGION_MANIFEST_INVALID", failed=True, block_id=identifier)
            elif role != "container":
                if any(not self.region(region, page) for region in regions):
                    self.issue("sources", "QC_REGION_INVALID", failed=True, block_id=identifier)
                if role == "leaf" and not any(isinstance(region, dict) and region.get("geometry_status") == "exact" for region in regions):
                    if self.critical[identifier]:
                        self.issue("sources", "QC_CRITICAL_REGION_UNRESOLVED", block_id=identifier, tags=self.critical[identifier])
                    else:
                        self.issue("sources", "QC_LEAF_REGION_UNRESOLVED", block_id=identifier)
            self.block_assets(identifier, block)

    def region(self, region, page) -> bool:
        if not isinstance(region, dict) or region.get("file_page") != page:
            return False
        coordinates = region.get("bbox_normalized")
        status = region.get("geometry_status")
        if status in ("exact", "table_region_only"):
            polygon = region.get("polygon")
            return normalized_bbox(coordinates) is not None and (polygon is None or (
                isinstance(polygon, list) and len(polygon) >= 3 and all(isinstance(point, list) and len(point) == 2
                    and all(type(coord) in {int, float} and isfinite(coord) and 0 <= coord <= 1 for coord in point)
                    for point in polygon)))
        return status in ("page_only", "missing") and coordinates is None

    def block_assets(self, identifier: str, block: dict) -> None:
        structured = block["structured_content"]
        refs = structured.get("asset_refs", [])
        parts = structured.get("asset_parts", [])
        declared = set()
        if not isinstance(refs, list) or not isinstance(parts, list):
            self.issue("sources", "QC_ASSET_BINDING_INVALID", failed=True, block_id=identifier)
            return
        for asset_id in refs:
            if (not _in(asset_id, self.artifacts) or self.artifacts[asset_id]["kind"] in {"raw_archive", "preflight"}
                    or asset_id in declared):
                self.issue("sources", "QC_ASSET_BINDING_INVALID", failed=True, block_id=identifier)
            else:
                declared.add(asset_id)
        actual = set()
        for item in parts:
            if (not isinstance(item, dict) or not _in(item.get("artifact_id"), declared)
                    or not self.refs(item.get("raw_ref"), owner=block.get("raw_ref"))):
                self.issue("sources", "QC_ASSET_SOURCE_INVALID", failed=True, block_id=identifier)
            else:
                actual.add(item["artifact_id"])
        if actual != declared:
            self.issue("sources", "QC_ASSET_BINDING_INVALID", failed=True, block_id=identifier)
        if block["structure_role"] == "leaf" and block["kind"] in {"image", "chart"} and not declared:
            self.issue("sources", "QC_VISUAL_ASSET_UNRESOLVED", block_id=identifier)

    def text_mapping(self) -> None:
        for identifier, block in self.blocks.items():
            if block.get("source_kind") not in {"parser", "manual"}:
                continue
            text = block["text_content"]
            structured = block["structured_content"]
            text_map, assembly = structured.get("text_map"), structured.get("assembly")
            owner = structured.get("manual_correction", {}).get("text_source_ref") if block.get("source_kind") == "manual" else None
            owner = owner or block.get("raw_ref")
            if (structured.get("renderer") != RENDERER or not isinstance(text_map, list) or not isinstance(assembly, list)
                    or not self.mapped_range(text_map, assembly, text, owner, 0, len(text))):
                self.issue("text_mapping", "QC_TEXT_PROVENANCE_INVALID", failed=True, block_id=identifier)
            parts = structured.get("source_parts", [])
            if not isinstance(parts, list) or any(not self.text_part(part, text, owner, text_map) for part in parts):
                self.issue("text_mapping", "QC_SOURCE_PART_INVALID", failed=True, block_id=identifier)
            if any(key in structured for key in ("html", "html_raw", "raw_html")):
                self.issue("text_mapping", "QC_RAW_HTML_EXPOSED", failed=True, block_id=identifier)

    def text_part(self, part, text, owner, parent_map) -> bool:
        if not isinstance(part, dict):
            return False
        start, end = part.get("block_start"), part.get("block_end")
        if not (_interval(start, end, len(text)) and part.get("text") == text[start:end]
                and isinstance(part.get("sources"), list) and isinstance(parent_map, list)
                and self.mapped_range(part["sources"], [], text, owner, start, end)):
            return False
        for source in part["sources"]:
            lo, hi = source["block_start"], source["block_end"]
            if not any(self.same_source_range(source, span, lo, hi) for span in parent_map):
                return False
        return True

    @staticmethod
    def same_source_range(source, span, lo, hi) -> bool:
        if (not isinstance(span, dict) or not _interval(span.get("block_start"), span.get("block_end"))
                or not _interval(span.get("source_start"), span.get("source_end"))
                or not span["block_start"] <= lo < hi <= span["block_end"]
                or span.get("raw_ref") != source["raw_ref"] or span.get("encoding") != source.get("encoding")):
            return False
        if source.get("encoding"):
            return ((lo, hi) == (span["block_start"], span["block_end"])
                    and (source["source_start"], source["source_end"]) == (span["source_start"], span["source_end"]))
        return (source["source_start"] == span["source_start"] + lo - span["block_start"]
                and source["source_end"] == span["source_end"] - (span["block_end"] - hi))

    def mapped_range(self, text_map, assembly, text, owner, start, end) -> bool:
        spans = []
        for is_separator, items in ((False, text_map), (True, assembly)):
            for span in items:
                begin, finish = (span.get("block_start"), span.get("block_end")) if isinstance(span, dict) else (None, None)
                if not _interval(begin, finish, end) or begin < start:
                    return False
                if is_separator:
                    if (not isinstance(span.get("kind"), str) or not span["kind"] or "raw_ref" in span
                            or span.get("text") != text[begin:finish] or not re.fullmatch(r"[\s|]+", span["text"])):
                        return False
                else:
                    lo, hi = span.get("source_start"), span.get("source_end")
                    encoding = span.get("encoding")
                    if (not self.refs(span.get("raw_ref"), owner=owner) or not _interval(lo, hi)
                            or encoding not in (None, "html_entity") or (encoding is None and (finish - begin) != (hi - lo))):
                        return False
                spans.append((begin, finish))
        cursor = start
        for begin, finish in sorted(spans):
            if cursor != begin:
                return False
            cursor = finish
        return cursor == end

    def read_relations(self) -> None:
        relations = self.ir.get("relations")
        keys = set()
        if not isinstance(relations, list):
            self.issue("relations", "QC_RELATION_MANIFEST_INVALID", failed=True)
            return
        for relation in relations:
            identifier = relation.get("id") if isinstance(relation, dict) else None
            if (not isinstance(identifier, str) or not identifier or identifier in self.relations
                    or relation.get("parse_run_id") != self.run_id or not _in(relation.get("relation_type"), _RELATION_TYPES)
                    or not all(isinstance(relation.get(key), str) for key in ("from_part", "to_part"))
                    or not all(_in(relation.get(key), self.blocks) for key in ("from_block_id", "to_block_id"))):
                self.issue("relations", "QC_RELATION_RECORD_INVALID", failed=True)
                continue
            left, right = self.blocks[relation["from_block_id"]], self.blocks[relation["to_block_id"]]
            kind = relation["relation_type"]
            origin, target = relation["from_part"], relation["to_part"]
            key = (left["id"], origin, kind, right["id"], target)
            try:
                if not _POINTER.fullmatch(origin) or not _POINTER.fullmatch(target):
                    raise ParseContractError("INVALID_RELATION_PART")
                a, b = _part_value(left, origin), _part_value(right, target)
            except ParseContractError:
                self.issue("relations", "QC_RELATION_PART_INVALID", failed=True, relation_id=identifier)
                continue
            basis = relation.get("basis")
            if key in keys or not self.relation_basis(relation, basis, left, right, a, b):
                self.issue("relations", "QC_RELATION_BASIS_INVALID", failed=True, relation_id=identifier)
                continue
            keys.add(key)
            self.relations[identifier] = relation
            self.by_from[left["id"]].append(relation)
            self.by_to[right["id"]].append(relation)
            self.relation_fingerprints[identifier] = fingerprint({key: relation[key] for key in _RELATION_KEYS})

    def relation_basis(self, relation, basis, left, right, a, b) -> bool:
        if (not isinstance(basis, dict) or not isinstance(basis.get("rule"), str) or not basis["rule"]
                or basis.get("recovery_version") != RECOVERY_VERSION or not isinstance(basis.get("scope"), dict)
                or not isinstance(basis.get("evidence"), list) or not basis["evidence"]):
            return False
        kind = relation["relation_type"]
        evidence = basis["evidence"]
        if any(not self.refs(ref, preflight=kind == "covered_by") for ref in evidence):
            return False
        for block in (left, right):
            ref = block.get("raw_ref")
            if ref is not None and not any(self.refs(item, owner=ref, preflight=kind == "covered_by") for item in evidence):
                return False
        scope = basis["scope"]
        hierarchy = {"section_id": {"section", "document"}, "document_root_id": {"document"},
                     "procedure_id": {"procedure"}, "visual_group_id": {"table_group", "image_group", "chart_group", "code_group"},
                     "group_id": {"paragraph_group", "table_continuation"}}
        for key, allowed in hierarchy.items():
            if key not in scope:
                continue
            identifier = scope[key]
            if not _in(identifier, self.blocks) or self.blocks[identifier]["kind"] not in allowed:
                return False
            for block in (left, right):
                if key in {"section_id", "document_root_id", "visual_group_id", "group_id"} and not self.within(block, identifier):
                    return False
        if "table_id" in scope and (scope["table_id"] != left["id"] or left is not right or left["kind"] != "table"):
            return False
        origin, target = relation["from_part"], relation["to_part"]
        if kind == "continues":
            return (left["ordinal"] < right["ordinal"] and left.get("kind") == right.get("kind")
                    and "group_id" in scope and "section_id" in scope and not origin and not target)
        if kind == "caption_of":
            return (left["kind"] == "caption" and right["kind"] in {"table", "image", "chart", "code", "algorithm"}
                    and "visual_group_id" in scope)
        if kind == "defines":
            return (right["kind"] == "formula" and isinstance(a, dict) and a.get("role") == "variable_definition"
                    and isinstance(b, dict) and b.get("symbol") == scope.get("symbol") and isinstance(b.get("symbol"), str)
                    and target.startswith("/formula_symbols/") and self.symbol_ranges(right, b))
        if kind == "unit_of":
            parameter = scope.get("parameter_part")
            if not isinstance(parameter, str) or not parameter.startswith("/source_parts/"):
                return False
            try:
                source = _part_value(left, parameter)
            except ParseContractError:
                return False
            return (left is right and origin != target and isinstance(source, dict) and source.get("role") == "parameter"
                    and isinstance(a, dict) and a.get("role") == "parameter_unit"
                    and isinstance(b, dict) and b.get("role") == "parameter_value")
        if kind == "header_of":
            manual = left["structured_content"].get("manual_correction", {}).get("column_headers_ref")
            if manual is not None and isinstance(a, dict) and a.get("row") == 0:
                if (left.get("source_kind") != "manual" or not self.refs(manual)
                        or type(a.get("column")) is not int
                        or not any(item == {**manual, "json_pointer": f"{manual['json_pointer']}/{a['column']}"}
                                   for item in evidence)):
                    return False
            return (left is right and left["kind"] == "table" and origin.startswith("/rows/") and target.startswith("/rows/")
                    and isinstance(a, dict) and a.get("header") is True and isinstance(b, dict) and b.get("header") is False
                    and self.header_scope(a, b, scope))
        if kind in {"requires", "warns"}:
            return self.requirement_scope(kind, scope, left, right, a, b)
        if kind == "covered_by":
            return (left["structure_role"] == "observation" and right["structure_role"] == "leaf"
                    and left.get("file_page") == right.get("file_page"))
        if right["kind"] in {"image", "chart"}:
            return (isinstance(a, dict) and a.get("role") == "figure_reference"
                    and self.step_scope(left, scope))
        return (left is right and left["kind"] == "table" and origin.startswith("/rows/") and target.startswith("/asset_parts/")
                and isinstance(a, dict) and isinstance(b, dict) and b.get("cell_ref") == scope.get("cell_ref")
                and scope.get("cell_ref") == {"row": a.get("row"), "column": a.get("column")})

    def within(self, block, ancestor) -> bool:
        current = block["id"]
        seen = set()
        while _in(current, self.blocks) and current not in seen:
            if current == ancestor:
                return True
            seen.add(current)
            current = self.blocks[current].get("parent_block_id")
        return False

    @staticmethod
    def header_scope(header, cell, scope) -> bool:
        axis = scope.get("axis")
        if (scope.get("header_cell") != {"row": header.get("row"), "column": header.get("column")}
                or scope.get("value_cell") != {"row": cell.get("row"), "column": cell.get("column")}):
            return False
        if any(type(item.get(key)) is not int for item in (header, cell) for key in ("row", "column", "rowspan", "colspan")):
            return False
        a, b = ("column", "row") if axis == "column" else ("row", "column") if axis == "row" else (None, None)
        if a is None:
            return False
        span = lambda dim: "colspan" if dim == "column" else "rowspan"
        return (header[b] + header[span(b)] <= cell[b]
                and header[a] < cell[a] + cell[span(a)] and cell[a] < header[a] + header[span(a)])

    def symbol_ranges(self, block, endpoint) -> bool:
        raw = block["structured_content"].get("latex_raw")
        ranges = endpoint.get("ranges")
        if not isinstance(raw, str) or not isinstance(ranges, list) or not ranges or not self.refs(endpoint.get("raw_ref"), owner=block.get("raw_ref")):
            return False
        return all(isinstance(span, dict) and _interval(span.get("source_start"), span.get("source_end"), len(raw))
                   and isinstance(endpoint.get("symbol"), str)
                   and _symbol_value(raw[span["source_start"]:span["source_end"]]) == endpoint["symbol"] for span in ranges)

    def step_scope(self, step, scope) -> bool:
        detail = step["structured_content"].get("step")
        group = scope.get("procedure_id")
        return (isinstance(detail, dict) and isinstance(group, str) and group == detail.get("procedure_id")
                and scope.get("step_number") == detail.get("number") and self.within(step, group))

    def requirement_scope(self, kind, scope, left, right, a, b) -> bool:
        if kind == "warns":
            if isinstance(a, dict) and a.get("role") == "global_warning":
                return right["kind"] == "document" and right["id"] == scope.get("document_root_id")
            return isinstance(a, dict) and a.get("role") == "warning" and self.step_scope(right, scope)
        if left["kind"] == "formula":
            return isinstance(b, dict) and b.get("role") == "formula_condition" and "section_id" in scope
        return isinstance(b, dict) and b.get("role") == "precondition" and self.step_scope(left, scope)

    def critical_content(self) -> None:
        for identifier in self.leaves:
            block = self.blocks[identifier]
            content = block["structured_content"]
            outgoing, incoming = self.by_from[identifier], self.by_to[identifier]
            after = {item["relation_type"] for item in outgoing}
            before = {item["relation_type"] for item in incoming}
            tags = set(self.critical[identifier])
            if "warns" in after:
                tags.add("warning")
            if "requires" in before:
                tags.add("operation_condition")
            if "unit_of" in after:
                tags.update({"parameter", "unit"})
            self.critical[identifier] = sorted(tags)
            if block["kind"] == "formula":
                self.formula(block, incoming, outgoing)
            elif "formula" in tags and not ("defines" in after or "requires" in before):
                self.issue("critical_content", "QC_FORMULA_SCOPE_UNRESOLVED", block_id=identifier)
            if block["kind"] == "table":
                self.table(block, incoming)
            elif {"parameter", "unit"} & tags and "unit_of" not in after:
                if not ("formula" in tags and "defines" in after):
                    self.issue("critical_content", "QC_PARAMETER_UNIT_UNRESOLVED", block_id=identifier)
            if "operation_step" in tags and not isinstance(content.get("step"), dict) and block["kind"] != "table":
                self.issue("critical_content", "QC_OPERATION_STEP_SCOPE_UNRESOLVED", block_id=identifier)
            if "operation_condition" in tags and "requires" not in before:
                self.issue("critical_content", "QC_OPERATION_CONDITION_SCOPE_UNRESOLVED", block_id=identifier)
            if "warning" in tags and "warns" not in after:
                self.issue("critical_content", "QC_WARNING_SCOPE_UNRESOLVED", block_id=identifier)
            if block["kind"] in {"image", "chart"}:
                for relation_type, code in (("caption_of", "QC_VISUAL_CAPTION_UNRESOLVED"),
                                            ("refers_to", "QC_VISUAL_ACTION_UNRESOLVED")):
                    if relation_type not in before:
                        self.issue("critical_content", code, block_id=identifier)

    def formula(self, block, incoming, outgoing) -> None:
        identifier = block["id"]
        content = block["structured_content"]
        if not any(isinstance(content.get(key), str) and content[key].strip() for key in ("latex_raw", "mathml_raw")):
            self.issue("critical_content", "QC_FORMULA_EXPRESSION_MISSING", block_id=identifier)
        symbols = content.get("formula_symbols")
        definitions = [item for item in incoming if item["relation_type"] == "defines"]
        conditions = [item for item in outgoing if item["relation_type"] == "requires"]
        if not isinstance(symbols, list):
            self.issue("critical_content", "QC_FORMULA_SYMBOLS_UNRESOLVED", block_id=identifier)
        else:
            if any(not isinstance(symbol, dict) or not self.symbol_ranges(block, symbol) for symbol in symbols):
                self.issue("critical_content", "QC_FORMULA_SYMBOL_SOURCE_INVALID", failed=True, block_id=identifier)
            target_parts = {item["to_part"] for item in definitions}
            for index in range(len(symbols)):
                if f"/formula_symbols/{index}" not in target_parts:
                    self.issue("critical_content", "QC_FORMULA_DEFINITION_MISSING", block_id=identifier, symbol_index=index)
        if not conditions:
            self.issue("critical_content", "QC_FORMULA_CONDITION_MISSING", block_id=identifier)
        for field, expected in (("definition_refs", {item["from_block_id"] for item in definitions}),
                                ("condition_refs", {item["to_block_id"] for item in conditions})):
            refs = content.get(field)
            if not isinstance(refs, list) or any(not isinstance(ref, str) for ref in refs) or set(refs) != expected:
                self.issue("relations", "QC_FORMULA_RELATION_PROJECTION_INVALID", failed=True, block_id=identifier, field=field)

    def table(self, block, incoming) -> None:
        identifier = block["id"]
        content = block["structured_content"]
        rows = content.get("rows")
        width = content.get("column_count")
        if not rows:
            self.issue("critical_content", "QC_TABLE_GRID_UNRESOLVED", block_id=identifier)
            return
        if (not isinstance(rows, list) or not 0 < len(rows) <= 2048 or any(not isinstance(row, list) for row in rows)
                or content.get("row_count") != len(rows) or type(width) is not int or not 0 < width <= 512
                or content.get("grid_origin") != 0 or not self.refs(content.get("html_raw_ref"), owner=block.get("raw_ref"))):
            self.issue("critical_content", "QC_TABLE_GRID_INVALID", failed=True, block_id=identifier)
            return
        by_cell = defaultdict(set)
        for relation in incoming:
            if relation["relation_type"] == "header_of":
                by_cell[relation["to_part"]].add(relation["id"])
        occupied = set()
        for r, row in enumerate(rows):
            for n, cell in enumerate(row):
                if not self.cell(block, cell, occupied, r, width, len(rows)):
                    self.issue("critical_content", "QC_TABLE_GRID_INVALID", failed=True, block_id=identifier, row=r, cell_position=n)
                    continue
                linked_headers = by_cell[f"/rows/{r}/{n}"]
                refs = cell.get("header_refs")
                if (not isinstance(refs, list) or any(not isinstance(item, dict) or not _in(item.get("relation_id"), linked_headers)
                        or self.relations[item["relation_id"]]["basis"]["scope"]["header_cell"] !=
                           {"row": item.get("row"), "column": item.get("column")} for item in refs)
                        or {item["relation_id"] for item in refs} != linked_headers):
                    self.issue("relations", "QC_TABLE_HEADER_PROJECTION_INVALID", failed=True, block_id=identifier,
                               row=r, column=cell["column"])
                if not cell["header"] and not linked_headers:
                    self.issue("critical_content", "QC_TABLE_HEADER_RELATION_MISSING", block_id=identifier,
                               row=r, column=cell["column"])
                asset_parts = content.get("asset_parts")
                if cell.get("value_state") in {"unreadable", "missing"} and not any(
                        isinstance(part, dict) and part.get("cell_ref") == {"row": r, "column": cell["column"]}
                        for part in (asset_parts if isinstance(asset_parts, list) else [])):
                    self.issue("critical_content", "QC_TABLE_CELL_VALUE_UNRESOLVED", block_id=identifier,
                               row=r, column=cell["column"])
        if len(occupied) != len(rows) * width:
            self.issue("critical_content", "QC_TABLE_GRID_INCOMPLETE", block_id=identifier)

    def cell(self, block, cell, occupied, r, width, length) -> bool:
        if (not isinstance(cell, dict) or cell.get("row") != r or type(cell.get("column")) is not int
                or type(cell.get("rowspan")) is not int or type(cell.get("colspan")) is not int
                or type(cell.get("header")) is not bool or not isinstance(cell.get("text"), str)
                or not _in(cell.get("value_state"), {"extracted", "empty_verified", "unreadable", "missing"})):
            return False
        c, rs, cs = cell["column"], cell["rowspan"], cell["colspan"]
        if not (0 <= c < width and 0 < rs <= length - r and 0 < cs <= width - c and len(occupied) + rs * cs <= 65536):
            return False
        coordinates = {(row, column) for row in range(r, r + rs) for column in range(c, c + cs)}
        if coordinates & occupied:
            return False
        occupied.update(coordinates)
        locator = cell.get("locator")
        if (not isinstance(locator, dict) or locator.get("cell_ref") != {"row": r, "column": c}
                or not self.refs(locator.get("raw_ref"), owner=block.get("raw_ref"))
                or not _interval(cell.get("html_start"), cell.get("html_end"))
                or not isinstance(locator.get("regions"), list) or not locator["regions"]):
            return False
        return all(self.region(region, block.get("file_page")) and region["geometry_status"] != "exact"
                   for region in locator["regions"])

    def inventory(self) -> None:
        observations = defaultdict(list)
        for identifier in self.observations:
            self.observation_disposition[identifier] = "pending_human"
            block = self.blocks[identifier]
            inventory = block["structured_content"].get("inventory_candidate")
            page = block.get("file_page")
            if block.get("source_kind") == "native_inventory" and isinstance(inventory, dict) and isinstance(inventory.get("id"), str) and type(page) is int:
                observations[(page, inventory["id"])].append(block)
            else:
                self.issue("inventory", "QC_OBSERVATION_SEMANTICS_UNRESOLVED", block_id=identifier)
        for number, page in self.pages.items():
            coverage = page.get("coverage")
            native = page.get("native_text", "")
            if not isinstance(coverage, dict) or not isinstance(native, str):
                self.issue("inventory", "QC_PAGE_INVENTORY_INVALID", failed=True, file_page=number)
                continue
            if page.get("state") == "blank":
                if self.by_page[number] or native.strip() or any(coverage.get(f"{kind}_candidate_count", 0) for kind in ("image", "vector")):
                    self.issue("pages", "QC_BLANK_PAGE_CONTENT_CONFLICT", failed=True, file_page=number)
                elif not coverage.get("blank_basis"):
                    self.issue("pages", "QC_BLANK_PAGE_BASIS_MISSING", file_page=number)
            elif page.get("state") in ("processed", "needs_review"):
                self.native_comparison(native, self.by_page[number], number, coverage)
            for kind in ("image", "vector"):
                declared = coverage.get(f"{kind}_candidate_count", 0)
                candidates = coverage.get(f"{kind}_candidates", [])
                if type(declared) is not int or declared < 0 or not isinstance(candidates, list):
                    self.issue("inventory", "QC_PAGE_INVENTORY_INVALID", failed=True, file_page=number, inventory_kind=kind)
                    continue
                if declared != len(candidates):
                    self.issue("inventory", "QC_VISUAL_INVENTORY_INCOMPLETE", file_page=number, inventory_kind=kind)
                seen = set()
                for index, candidate in enumerate(candidates):
                    key = candidate.get("id") if isinstance(candidate, dict) else None
                    if not isinstance(key, str) or key in seen:
                        self.issue("inventory", "QC_VISUAL_CANDIDATE_INVALID", failed=True, file_page=number, inventory_kind=kind)
                        continue
                    seen.add(key)
                    blocks = observations.pop((number, key), [])
                    if not blocks:
                        self.issue("inventory", "QC_VISUAL_CANDIDATE_SOURCE_MISSING", file_page=number, candidate_id=key)
                    elif len(blocks) > 1:
                        self.issue("inventory", "QC_VISUAL_CANDIDATE_DUPLICATED", failed=True, file_page=number, candidate_id=key)
                    else:
                        self.visual_inventory(blocks[0], candidate, page, kind, index)
        for blocks in observations.values():
            for block in blocks:
                self.issue("inventory", "QC_OBSERVATION_CANDIDATE_DANGLING", failed=True, block_id=block["id"])

    def visual_inventory(self, block, candidate, page, kind, index) -> None:
        identifier = block["id"]
        content = block["structured_content"]
        ref = block.get("raw_ref")
        regions = block["locator"].get("regions")
        number = page["file_page"]
        pointer = f"/pages/{number - 1}/coverage/{kind}_candidates/{index}"
        if (content.get("inventory_kind") != kind or content.get("inventory_candidate") != candidate
                or not isinstance(ref, dict) or ref.get("json_pointer") != pointer or not isinstance(regions, list)
                or len(regions) != 1 or not isinstance(regions[0], dict) or regions[0].get("bbox_normalized") != candidate.get("bbox")
                or regions[0].get("geometry_status") != candidate.get("geometry_status")):
            self.issue("inventory", "QC_OBSERVATION_CANDIDATE_BINDING_INVALID", failed=True, block_id=identifier)
            return
        bbox = normalized_bbox(candidate.get("bbox"))
        if not bbox:
            self.issue("inventory", "QC_VISUAL_CANDIDATE_GEOMETRY_UNRESOLVED", block_id=identifier)
            return
        if (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) >= .90 and page.get("page_class") in ("scan", "mixed"):
            if not any(self.interior_text(leaf, bbox) for leaf in self.by_page[number]):
                self.issue("inventory", "QC_SCAN_INTERNAL_COVERAGE_UNRESOLVED", block_id=identifier)
                return
            self.observation_disposition[identifier] = "pending_human_scan_internals"
        for relation in self.by_from[identifier]:
            if relation["relation_type"] != "covered_by":
                continue
            target = self.blocks[relation["to_block_id"]]
            target_regions = target["locator"].get("regions")
            for region in target_regions if isinstance(target_regions, list) else []:
                target_box = normalized_bbox(region.get("bbox_normalized")) if isinstance(region, dict) else None
                if (target_box and target["kind"] in {"image", "chart", "table"} and target_box[0] <= bbox[0]
                        and target_box[1] <= bbox[1] and target_box[2] >= bbox[2] and target_box[3] >= bbox[3]):
                    self.observation_disposition[identifier] = "candidate_attribution_pending_human"
                    return

    @staticmethod
    def interior_text(leaf, surrounding) -> bool:
        if (leaf["structure_role"] != "leaf" or leaf["kind"] in {"image", "chart"}
                or not leaf["text_content"].strip()):
            return False
        regions = leaf["locator"].get("regions")
        for region in regions if isinstance(regions, list) else []:
            if not isinstance(region, dict) or region.get("geometry_status") != "exact":
                continue
            bbox = normalized_bbox(region.get("bbox_normalized"))
            if (bbox and (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]) < .90
                    and surrounding[0] <= bbox[0] and surrounding[1] <= bbox[1]
                    and surrounding[2] >= bbox[2] and surrounding[3] >= bbox[3]):
                return True
        return False

    def native_comparison(self, native, blocks, number, coverage) -> None:
        if not native and coverage.get("native_text_present"):
            self.issue("inventory", "QC_NATIVE_TEXT_INVENTORY_MISSING", file_page=number)
            return
        parsed = "\n".join(block["text_content"] for block in blocks if block["structure_role"] == "leaf")
        expected = _comparison_chars(native)
        length = expected.total()
        if length == 0:
            return
        explained = (expected & _comparison_chars(parsed)).total()
        expected_numbers = set(_NUMBER_WORD.findall(unicodedata.normalize("NFKC", native)))
        observed_numbers = set(_NUMBER_WORD.findall(unicodedata.normalize("NFKC", parsed)))
        if explained < length or expected_numbers - observed_numbers:
            manual_blocks = [block for block in blocks if block.get("source_kind") == "manual"]
            declared_removals = Counter()
            removed_numbers = set()
            for block in manual_blocks:
                original = block["structured_content"].get("manual_correction", {}).get("original_text_for_inventory")
                if isinstance(original, str):
                    declared_removals.update(_comparison_chars(original) - _comparison_chars(block["text_content"]))
                    removed_numbers.update(set(_NUMBER_WORD.findall(unicodedata.normalize("NFKC", original)))
                                           - set(_NUMBER_WORD.findall(unicodedata.normalize("NFKC", block["text_content"]))))
            deficit = expected - _comparison_chars(parsed)
            if manual_blocks and not (deficit - declared_removals) and expected_numbers - observed_numbers <= removed_numbers:
                # The difference remains visible and requires professional
                # confirmation of every edited leaf; it is not OCR certainty.
                self.checks["inventory"].setdefault("manual_differences", []).append({"file_page": number,
                    "state": "pending_human", "block_refs": [block["id"] for block in manual_blocks],
                    "native_chars": length, "explained_chars": explained,
                    "unmatched_number_count": len(expected_numbers - observed_numbers)})
                return
            self.issue("inventory", "QC_NATIVE_TEXT_DISCREPANCY", file_page=number,
                       native_chars=length, explained_chars=explained, unmatched_number_count=len(expected_numbers - observed_numbers))

    def inherited(self) -> None:
        issues = self.ir.get("issues")
        if not isinstance(issues, list):
            self.issue("integrity", "QC_IR_ISSUES_INVALID", failed=True)
            return
        for issue in issues:
            if not isinstance(issue, dict) or not isinstance(issue.get("code"), str) or not isinstance(issue.get("id"), str):
                self.issue("integrity", "QC_IR_ISSUES_INVALID", failed=True)
                continue
            self.issue("critical_content" if issue["code"].startswith("STRUCTURE_") else "sources",
                       "QC_UPSTREAM_ISSUE_UNRESOLVED", block_id=issue.get("block_id"), file_page=issue.get("file_page"),
                       upstream_issue_id=issue["id"], upstream_code=issue["code"])

    def report(self) -> dict:
        coverage = {identifier: [] for identifier in self.containers}
        for leaf in self.leaves:
            current = self.blocks[leaf].get("parent_block_id")
            seen = set()
            while _in(current, coverage) and current not in seen:
                seen.add(current)
                coverage[current].append(leaf)
                current = self.blocks[current].get("parent_block_id")
        statuses = {check["status"] for check in self.checks.values()}
        status = "failed" if "failed" in statuses else "needs_review" if "needs_review" in statuses else "passed"
        return {"schema": "dc.source-qc", "schema_version": "1", **self.identity, "checker_revision": CHECKER_REVISION,
                "inputs": self.inputs, "status": status, "checks": self.checks, "issues": self.issues,
                "required_leaf_refs": self.leaves, "leaf_fingerprints": self.leaf_fingerprints,
                "container_refs": self.containers, "container_coverage": coverage, "observation_refs": self.observations,
                "observation_disposition": self.observation_disposition,
                "critical_refs": {identifier: tags for identifier, tags in self.critical.items() if tags},
                "required_relation_refs": list(self.relations), "relation_fingerprints": self.relation_fingerprints,
                "required_human_checks": ["original_page_text_and_layout", "key_fact_and_relation_correctness", "observation_disposition"]}


def _comparison_chars(text: str) -> Counter:
    return Counter(char for char in unicodedata.normalize("NFKC", text).casefold() if char.isalnum())


def evaluate_source_qc(structured, *, normalized_sha256, structured_sha256, raw_manifest_sha256) -> dict:
    """Check technical evidence for an archived source, independently of review state."""
    if (not isinstance(structured, dict) or (structured.get("schema"), structured.get("schema_version"),
            structured.get("phase")) != ("dc.document-ir", "1.0", "structured")
            or any(not isinstance(structured.get(key), str) or not structured[key] for key in _IDENTITY)):
        raise ParseContractError("SOURCE_QC_INPUT_INVALID")
    qc = _QC(structured, {"normalized_ir_sha256": normalized_sha256, "structured_ir_sha256": structured_sha256,
                          "raw_manifest_sha256": raw_manifest_sha256})
    qc.integrity()
    qc.read_pages()
    qc.read_blocks()
    qc.structure()
    qc.source_bindings()
    qc.text_mapping()
    qc.read_relations()
    qc.critical_content()
    qc.inventory()
    qc.inherited()
    return qc.report()
