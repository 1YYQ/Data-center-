"""Deterministic, source-bound child/parent chunking for Structured PDF IR."""

from __future__ import annotations

from bisect import bisect_right
from collections import defaultdict
import hashlib
from pathlib import Path
import re
from uuid import NAMESPACE_URL, uuid5

from .local_pdf import normalized_bbox
from .local_pipeline import ParseContractError, fingerprint
from .quality import _critical_tags
from .structure import _part_value


SPLITTER_VERSION = "dc.semantic-bge-m3/1"
TOKENIZER_SHA256 = "21106b6d7dab2952c1d496fb21d5dc9db75c28ed361a05f5020bbba27810dd08"
TOKENIZER_REVISION = "BAAI/bge-m3@tokenizer-21106b6d7dab"
PROFILE = {"splitter_version": SPLITTER_VERSION, "tokenizer_revision": TOKENIZER_REVISION,
           "tokenizer_sha256": TOKENIZER_SHA256, "embedding_max_tokens": 1536, "embedding_max_bytes": 8192}
COLLECTION_CHECKER_REVISION = "dc.pdf-chunk-qc/1"
_NAMESPACE = uuid5(NAMESPACE_URL, "dc.chunk-set/1.0")
_SHA = re.compile(r"[0-9a-f]{64}")
_SENTENCE_END = re.compile(r"[。！？!?](?=\s*\S)|(?<=[a-z]{2})\.(?=\s+[A-Z])|\n[ \t]*\n")
_UNRESOLVED_CONTEXT = re.compile(r"上述|如下|该|此时|否则|须先|仅在|警告|注意|\b(?:it|this|these|those|above|below|otherwise|except)\b", re.I)
_SCOPE_WORD = re.compile(r"^\s*(?:型号|适用型号|适用范围|环境条件|applicability|model\s+(?:no|number|type)|scope)\s*[:：=]|仅限.{1,30}型", re.I)
_AUXILIARY = {"title", "header", "footer", "page_number", "aside_text", "caption", "footnote", "ref_text"}


def load_tokenizer(path: str | Path):
    """Read and verify the exact local tokenizer used by the registered profile."""
    try:
        from tokenizers import Tokenizer

        raw = Path(path).read_bytes()
        if hashlib.sha256(raw).hexdigest() != TOKENIZER_SHA256:
            raise ParseContractError("CHUNK_TOKENIZER_DIGEST_MISMATCH")
        tokenizer = Tokenizer.from_str(raw.decode("utf-8"))
    except ParseContractError:
        raise
    except (ImportError, OSError, ValueError, TypeError, RuntimeError):
        raise ParseContractError("CHUNK_TOKENIZER_UNAVAILABLE") from None
    if tokenizer.truncation is not None or tokenizer.padding is not None:
        raise ParseContractError("CHUNK_TOKENIZER_CONFIGURATION_UNSUPPORTED")
    return tokenizer


def _profile(profile: dict) -> dict:
    identity = ("splitter_version", "tokenizer_revision", "tokenizer_sha256")
    bindings = ("structured_ir_sha256", "source_qc_ref", "source_qc_sha256")
    if (not isinstance(profile, dict) or set(profile) - (PROFILE.keys() | {*bindings, "splitter_code_sha256"})
            or any(profile.get(key) != PROFILE[key] for key in identity)
            or not isinstance(profile.get("source_qc_ref"), str) or not profile["source_qc_ref"]
            or any(not isinstance(profile.get(key), str) or not _SHA.fullmatch(profile[key]) for key in (bindings[0], bindings[2]))):
        raise ParseContractError("CHUNK_PROFILE_UNSUPPORTED")
    if "splitter_code_sha256" in profile and (not isinstance(profile["splitter_code_sha256"], str)
            or not _SHA.fullmatch(profile["splitter_code_sha256"])):
        raise ParseContractError("CHUNK_PROFILE_UNSUPPORTED")
    values = {**PROFILE, **profile}
    if any(type(values[key]) is not int or values[key] <= 0 for key in ("embedding_max_tokens", "embedding_max_bytes")):
        raise ParseContractError("CHUNK_PROFILE_UNSUPPORTED")
    return {**values, "max_tokens": min(1536, values["embedding_max_tokens"]),
            "max_utf8_bytes": min(8192, values["embedding_max_bytes"]), "target_tokens": min(768, values["embedding_max_tokens"])}


def _id(*parts) -> str:
    return str(uuid5(_NAMESPACE, fingerprint(list(parts))))


def _text(block: dict, role="body", start=0, end=None, **values) -> dict | None:
    content = block["text_content"]
    end = len(content) if end is None else end
    if not (type(start) is int and type(end) is int and 0 <= start <= end <= len(content)):
        raise ParseContractError("CHUNK_TEXT_RANGE_INVALID")
    return {"block_id": block["id"], "kind": "text", "block_start": start, "block_end": end,
            "role": role, **values} if start < end else None


def _nonempty(*items) -> list:
    return [item for item in items if item is not None]


def _ids(relations) -> list[str]:
    return list(dict.fromkeys(item["id"] if isinstance(item, dict) else item for item in relations))


class _ChunkBuilder:
    def __init__(self, ir: dict, chunk_set_id: str, profile: dict, tokenizer) -> None:
        self.ir = ir
        self.run = ir["parse_run_id"]
        self.set_id = chunk_set_id
        self.config = _profile(profile)
        if (not callable(getattr(tokenizer, "encode", None)) or getattr(tokenizer, "truncation", None) is not None
                or getattr(tokenizer, "padding", None) is not None):
            raise ParseContractError("CHUNK_TOKENIZER_CONFIGURATION_UNSUPPORTED")
        self.tokenizer = tokenizer
        self.blocks = self._blocks(ir["blocks"])
        self.renders = {key: self._render_map(block) for key, block in self.blocks.items() if block["structure_role"] == "leaf"}
        self.assets = {ref["artifact_id"] for ref in ir["artifact_refs"] if isinstance(ref, dict)
                       and isinstance(ref.get("artifact_id"), str) and ref.get("kind") not in ("raw_archive", "preflight")}
        self.leaves = sorted((block for block in self.blocks.values() if block["structure_role"] == "leaf"),
            key=lambda block: (block["reading_order"] is None, block["reading_order"] if block["reading_order"] is not None else block["ordinal"]))
        self.ancestors = self._ancestors()
        self.outgoing, self.incoming = self._relations(ir["relations"])
        self.relations = {relation["id"]: relation for relation in ir["relations"]}
        self.titles = self._titles()
        self.blocked, self.blocked_pages = self._issues(ir["issues"])
        self.used: set[str] = set()
        self.chunks: list[dict] = []
        self.mappings: list[dict] = []
        self.groups: list[dict] = []
        self.issues: list[dict] = []
        self.tables_data: dict = {}
        self.asset_parts_by_cell: dict = {}

    def _blocks(self, blocks: list) -> dict:
        output = {}
        ordinals = set()
        orders = set()
        for block in blocks:
            if not isinstance(block, dict) or block.get("parse_run_id") != self.run:
                raise ParseContractError("CHUNK_SOURCE_BINDING_INVALID")
            identifier, ordinal, order = (block.get(name) for name in ("id", "ordinal", "reading_order"))
            if (not isinstance(identifier, str) or not identifier or identifier in output or type(ordinal) is not int
                    or ordinal < 0 or ordinal in ordinals or not isinstance(block.get("structured_content"), dict)
                    or not isinstance(block.get("locator"), dict) or not isinstance(block.get("text_content"), str)
                    or block.get("structure_role") not in ("leaf", "container", "observation")
                    or not isinstance(block.get("kind"), str) or (order is not None and (type(order) is not int or order < 0 or order in orders))):
                raise ParseContractError("CHUNK_SOURCE_BINDING_INVALID")
            output[identifier] = block
            ordinals.add(ordinal)
            if order is not None:
                orders.add(order)
        return output

    def _ancestors(self) -> dict:
        result = {}
        for identifier, block in self.blocks.items():
            path, seen = [], {identifier}
            parent_id = block.get("parent_block_id")
            while parent_id is not None:
                if not isinstance(parent_id, str) or parent_id in seen or parent_id not in self.blocks:
                    raise ParseContractError("CHUNK_SOURCE_BINDING_INVALID")
                parent = self.blocks[parent_id]
                if parent["structure_role"] != "container":
                    raise ParseContractError("CHUNK_SOURCE_BINDING_INVALID")
                path.append(parent_id)
                seen.add(parent_id)
                parent_id = parent.get("parent_block_id")
            result[identifier] = path
        return result

    def _relations(self, relations: list) -> tuple:
        outgoing, incoming = defaultdict(list), defaultdict(list)
        seen = set()
        for relation in relations:
            if not isinstance(relation, dict) or relation.get("parse_run_id") != self.run:
                raise ParseContractError("CHUNK_SOURCE_BINDING_INVALID")
            identifier, left, right = (relation.get(key) for key in ("id", "from_block_id", "to_block_id"))
            if (not isinstance(identifier, str) or not identifier or identifier in seen or not isinstance(left, str)
                    or not isinstance(right, str) or left not in self.blocks or right not in self.blocks
                    or any(not isinstance(relation.get(key), str) for key in ("from_part", "to_part", "relation_type"))):
                raise ParseContractError("CHUNK_SOURCE_BINDING_INVALID")
            _part_value(self.blocks[left], relation["from_part"])
            _part_value(self.blocks[right], relation["to_part"])
            outgoing[left].append(relation)
            incoming[right].append(relation)
            seen.add(identifier)
        return outgoing, incoming

    def _titles(self) -> dict:
        result = {}
        for block in self.blocks.values():
            basis = block["structured_content"].get("basis")
            if block["kind"] == "section" and isinstance(basis, dict):
                title_id = basis.get("heading_block_id")
                if isinstance(title_id, str) and (title := self.blocks.get(title_id)) and title["kind"] == "title":
                    result[block["id"]] = title
        return result

    @staticmethod
    def _issues(issues: list) -> tuple:
        blocks, pages = set(), set()
        for item in issues:
            if not isinstance(item, dict):
                raise ParseContractError("CHUNK_SOURCE_BINDING_INVALID")
            if isinstance(item.get("block_id"), str):
                blocks.add(item["block_id"])
            elif type(item.get("file_page")) is int:
                pages.add(item["file_page"])
        return blocks, pages

    def root(self, block, *kind) -> str | None:
        return next((identifier for identifier in self.ancestors[block["id"]] if self.blocks[identifier]["kind"] in kind), None)

    def contextual_titles(self, block) -> list:
        return [span for root in reversed(self.ancestors[block["id"]]) if root in self.titles
                if (span := _text(self.titles[root], "context", context_kind="title")) is not None]

    def complete_source(self, block) -> bool:
        return (block["reading_order"] is not None and block["file_page"] not in self.blocked_pages
                and not ({block["id"], *self.ancestors[block["id"]]} & self.blocked))

    def issue(self, code, group=None, **values) -> None:
        self.issues.append({"code": code, "support_group_id": group["id"] if group else None, **values})

    def _region(self, block, role="body", cell=None, **values) -> list[dict]:
        identifier = block["id"]
        structured = block["structured_content"]
        assets = structured.get("asset_parts", [])
        if not isinstance(assets, list):
            raise ParseContractError("CHUNK_ASSET_BINDING_INVALID")
        if cell is not None:
            if identifier not in self.asset_parts_by_cell:
                indexed = defaultdict(list)
                for asset in assets:
                    if isinstance(asset, dict) and isinstance(asset.get("cell_ref"), dict):
                        position = asset["cell_ref"]
                        indexed[position.get("row"), position.get("column")].append(asset)
                self.asset_parts_by_cell[identifier] = indexed
            assets = self.asset_parts_by_cell[identifier].get((cell["row"], cell["column"]), [])
        output = []
        for asset in assets:
            if not isinstance(asset, dict) or not isinstance(asset.get("artifact_id"), str) or asset["artifact_id"] not in self.assets:
                raise ParseContractError("CHUNK_ASSET_BINDING_INVALID")
            located = asset.get("cell_ref")
            if cell is not None and located != cell:
                continue
            if located:
                found = self.table_data(block)["cells"].get((located["row"], located["column"]))
                if found is None:
                    raise ParseContractError("CHUNK_ASSET_BINDING_INVALID")
                path, item = found
                region = self._located_region(item.get("locator", {}))
                region_ref = f"{path}/locator/regions/{region}" if region is not None else None
            else:
                region = self._located_region(block["locator"])
                region_ref = f"/locator/regions/{region}" if region is not None else None
            if region_ref:
                record = {"block_id": identifier, "kind": "region", "role": role,
                          "region_ref": region_ref, "asset_ref": asset["artifact_id"], **values}
                if located:
                    record["cell_range"] = {"row_start": located["row"], "row_end": located["row"] + item["rowspan"],
                                            "column_start": located["column"], "column_end": located["column"] + item["colspan"]}
                output.append(record)
        return output

    @staticmethod
    def _located_region(locator) -> int | None:
        if not isinstance(locator, dict) or not isinstance(locator.get("regions"), list):
            return None
        for index, region in enumerate(locator["regions"]):
            if (isinstance(region, dict) and region.get("geometry_status") in ("exact", "table_region_only", "page_only")
                    and (region.get("geometry_status") == "page_only" or normalized_bbox(region.get("bbox_normalized")))):
                return index
        return None

    def part(self, relation: dict, side: str, role: str) -> list[dict]:
        block = self.blocks[relation[f"{side}_block_id"]]
        pointer = relation[f"{side}_part"]
        if not pointer:
            return _nonempty(_text(block, role, relation_id=relation["id"]))
        part = _part_value(block, pointer)
        if not isinstance(part, dict) or type(part.get("block_start")) is not int or type(part.get("block_end")) is not int:
            raise ParseContractError("CHUNK_SOURCE_PART_INVALID")
        return _nonempty(_text(block, role, part["block_start"], part["block_end"], relation_id=relation["id"]))

    def captions(self, block, role="context") -> tuple[list, list]:
        refs, relations = [], []
        for relation in self.incoming[block["id"]]:
            if relation["relation_type"] == "caption_of":
                relations.append(relation)
                refs.extend(self.part(relation, "from", role))
        return refs, relations

    def _measure(self, content: str) -> tuple[int, int]:
        try:
            ids = self.tokenizer.encode(content, add_special_tokens=True).ids
            if not isinstance(ids, list) or not ids or any(type(identifier) is not int for identifier in ids):
                raise ValueError
        except (AttributeError, ValueError, TypeError, RuntimeError):
            raise ParseContractError("CHUNK_TOKENIZER_COUNT_FAILED") from None
        return len(ids), len(content.encode("utf-8"))

    def fits(self, refs: list, *, target=False) -> bool:
        content, _, _ = self.render(refs)
        if not content:
            return True
        if len(content.encode("utf-8")) > self.config["max_utf8_bytes"]:
            return False
        return self._measure(content)[0] <= self.config["target_tokens" if target else "max_tokens"]

    @staticmethod
    def _render_map(block: dict) -> list:
        text = block["text_content"]
        structured = block["structured_content"]
        maps, separators = structured.get("text_map"), structured.get("assembly")
        if not isinstance(maps, list) or not isinstance(separators, list):
            raise ParseContractError("CHUNK_TEXT_RANGE_INVALID")
        spans = []
        for source, is_separator in ((maps, False), (separators, True)):
            for row in source:
                if not isinstance(row, dict):
                    raise ParseContractError("CHUNK_TEXT_RANGE_INVALID")
                start, end = row.get("block_start"), row.get("block_end")
                if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(text):
                    raise ParseContractError("CHUNK_TEXT_RANGE_INVALID")
                if is_separator and (row.get("text") != text[start:end] or not re.fullmatch(r"[\s|]+", row["text"])):
                    raise ParseContractError("CHUNK_TEXT_RANGE_INVALID")
                if not is_separator and not isinstance(row.get("raw_ref"), dict):
                    raise ParseContractError("CHUNK_TEXT_RANGE_INVALID")
                spans.append((start, end, row if is_separator else None))
        cursor = 0
        for start, end, _ in sorted(spans, key=lambda row: row[0]):
            if start != cursor:
                raise ParseContractError("CHUNK_TEXT_RANGE_INVALID")
            cursor = end
        if cursor != len(text):
            raise ParseContractError("CHUNK_TEXT_RANGE_INVALID")
        return sorted((row for row in separators), key=lambda row: row["block_start"])

    @staticmethod
    def unique(refs: list) -> list:
        seen = set()
        body = defaultdict(list)
        for ref in refs:
            if ref["kind"] == "text" and ref["role"] == "body":
                body[ref["block_id"]].append((ref["block_start"], ref["block_end"]))
        output = []
        for ref in refs:
            identifier = ref["block_id"]
            if ref["kind"] == "text":
                start, end = ref["block_start"], ref["block_end"]
                if ref["role"] != "body" and any(a <= start and end <= b for a, b in body[identifier]):
                    continue
                key = (identifier, start, end)
            else:
                key = (identifier, ref["region_ref"], ref["asset_ref"], ref.get("step_number"))
            if key not in seen:
                seen.add(key)
                output.append(ref)
        return output

    def render(self, refs: list) -> tuple[str, list, list]:
        chunks, mappings, assembly = [], [], []
        offset = 0

        def join(text: str, kind: str, **basis):
            nonlocal offset
            assembly.append({"text": text, "kind": kind, "chunk_start": offset, "chunk_end": offset + len(text), **basis})
            chunks.append(text)
            offset += len(text)

        def copy_text(ref, text, start, end):
            nonlocal offset
            if end > start:
                chunks.append(text[start:end])
                mappings.append({**ref, "block_start": start, "block_end": end, "chunk_start": offset,
                                 "chunk_end": offset + end - start})
                offset += end - start

        for ref in self.unique(refs):
            identifier = ref["block_id"]
            if ref["kind"] == "region":
                mappings.append(dict(ref))
                continue
            if chunks and chunks[-1] and not chunks[-1][-1].isspace():
                join("\n", "chunk_join")
            start, end = ref["block_start"], ref["block_end"]
            text = self.blocks[identifier]["text_content"]
            cursor = start
            for sep in self.renders[identifier]:
                lower, upper = max(start, sep["block_start"]), min(end, sep["block_end"])
                if lower >= upper:
                    continue
                copy_text(ref, text, cursor, lower)
                join(text[lower:upper], sep["kind"], block_id=identifier, block_start=lower, block_end=upper)
                cursor = upper
            copy_text(ref, text, cursor, end)
        return "".join(chunks), mappings, assembly

    def group(self, category, anchor, root, refs, relations, *, ordered_steps=(), reason=None) -> dict:
        unique = self.unique(refs)
        _, mappings, _ = self.render(unique)
        members = [{key: value for key, value in item.items() if key not in ("chunk_start", "chunk_end")}
                   for item in mappings]
        required = _ids(relations)
        scope = _id(self.run, "scope", root)
        identifier = _id(self.run, category, anchor, scope, members, required)
        group = {"id": identifier, "kind": category, "parse_run_id": self.run, "scope_key": scope,
                 "structure_root_id": root, "members": members, "required_relation_ids": required,
                 "ordered_step_refs": list(ordered_steps), "child_chunk_ids": [], "complete": False, "predecessor_refs": None}
        if reason is None and (not members or not any(item["role"] == "body" for item in members)):
            reason = "CHUNK_GROUP_BODY_MISSING"
        if reason is None and any(not self.complete_source(self.blocks[ref["block_id"]]) for ref in members):
            reason = "CHUNK_GROUP_SOURCE_UNRESOLVED"
        if reason is None and required:
            member_index = defaultdict(list)
            for ref in members:
                member_index[ref["block_id"]].append(ref)
            coverage = {identifier: self.text_coverage(identifier, spans) for identifier, spans in member_index.items()}
            if any(not self.covers_relation(member_index, coverage, self.relations[relation]) for relation in required):
                reason = "CHUNK_GROUP_DEPENDENCY_UNRESOLVED"
        if reason is not None:
            group["reason"] = reason
            self.issue(reason, group)
        self.groups.append(group)
        return group

    def covers_relation(self, members: dict, coverage: dict, relation: dict) -> bool:
        for side in ("from", "to"):
            block = self.blocks[relation[f"{side}_block_id"]]
            identifier = block["id"]
            matches = members.get(identifier, [])
            part = relation[f"{side}_part"]
            if block["structure_role"] == "container" and not part:
                if any(identifier in self.ancestors[item] for item in members):
                    continue
                return False
            if not matches:
                return False
            if not part:
                continue
            if part.startswith("/rows/"):
                position = self.table_data(block)["positions"].get(part)
                if position is None or not any(self.covers_cell(span, position) for span in matches):
                    return False
            elif part.startswith("/asset_parts/"):
                asset = _part_value(block, part)
                if not any(span.get("asset_ref") == asset.get("artifact_id") for span in matches):
                    return False
            elif part.startswith("/source_parts/"):
                detail = _part_value(block, part)
                if not isinstance(detail, dict) or not self.covers_text(coverage[identifier], detail.get("block_start"), detail.get("block_end")):
                    return False
            elif part not in {"/latex_raw", "/mathml_raw"} and not part.startswith("/formula_symbols/"):
                return False
        return True

    @staticmethod
    def covers_cell(span, position) -> bool:
        area = span.get("cell_range")
        r, c = position
        return bool(area and area["row_start"] <= r < area["row_end"] and area["column_start"] <= c < area["column_end"])

    def text_coverage(self, identifier, spans) -> list:
        bounds = [(item["block_start"], item["block_end"]) for item in spans if item["kind"] == "text"]
        bounds.extend((row["block_start"], row["block_end"]) for row in self.renders[identifier])
        merged = []
        for begin, high in sorted(bounds):
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], high))
            else:
                merged.append((begin, high))
        return merged

    @staticmethod
    def covers_text(bounds, start, end) -> bool:
        if type(start) is not int or type(end) is not int or not 0 <= start < end:
            return False
        index = bisect_right(bounds, (start, float("inf"))) - 1
        return index >= 0 and end <= bounds[index][1]

    def row(self, *, group, parent, kind, eligibility, refs=(), required=()) -> dict:
        level = "parent" if refs is None else "child"
        if level == "parent":
            content, mappings, assembly = None, [], []
        else:
            content, mappings, assembly = self.render(list(refs))
        tokens, size = self._measure(content) if content else (None, 0)
        if level == "child" and (not mappings or not any(item["role"] == "body" for item in mappings)):
            raise ParseContractError("CHUNK_GROUP_BODY_MISSING")
        if level == "child" and eligibility != "oversized" and content and (
                tokens > self.config["max_tokens"] or size > self.config["max_utf8_bytes"]):
            eligibility = "oversized"
        if eligibility == "candidate" and (not content or not content.strip()):
            eligibility = "asset_only" if any(item["kind"] == "region" for item in mappings) else "navigation_only"
        proven = _ids(required)
        ordinal = len(self.chunks)
        identifier = _id(self.set_id, group["id"], level, ordinal)
        digest = fingerprint({"level": level, "kind": kind, "content": content, "mapping": mappings,
                              "assembly": assembly, "required_relation_ids": proven, "support_group_id": group["id"]})
        block = {"id": identifier, "chunk_set_id": self.set_id, "ordinal": ordinal, "content": content,
                 "content_hash": digest, "token_count": tokens, "utf8_byte_count": size, "level": level,
                 "parent_chunk_id": parent["id"] if parent else None, "structure_root_id": group["structure_root_id"],
                 "kind": kind, "eligibility": eligibility, "required_relation_ids": proven,
                 "support_group_refs": [group["id"]], "assembly": assembly}
        self.chunks.append(block)
        if level == "child":
            group["child_chunk_ids"].append(identifier)
            by_block = defaultdict(list)
            for item in mappings:
                marker = {key: value for key, value in item.items() if key != "block_id"}
                if item["kind"] == "region":
                    marker["locator_ref"] = marker.pop("region_ref")
                by_block[item["block_id"]].append(marker)
            self.mappings.extend({"chunk_id": identifier, "block_id": source, "chunk_set_id": self.set_id,
                                 "parse_run_id": self.run, "ordinal": index, "segments": segments}
                                 for index, (source, segments) in enumerate(by_block.items()))
        return block

    def emit(self, group: dict, kind: str, parts, *, force_parent=False, category="candidate") -> None:
        parent = None
        if force_parent or len(parts) > 1:
            parent = self.row(group=group, parent=None, kind=kind, eligibility="navigation_only", refs=None,
                              required=group["required_relation_ids"])
        oversized = False
        for index, (refs, required, child_kind) in enumerate(parts):
            # Empty, non-textual objects are retained in the IR and issue list.
            if not refs:
                continue
            row = self.row(group=group, parent=parent, kind=child_kind or kind,
                           eligibility="navigation_only" if group.get("reason") and category == "candidate" else category,
                           refs=refs, required=required)
            if row["eligibility"] == "oversized":
                oversized = True
                self.issue("CHUNK_ATOM_OVERSIZED", group, chunk_id=row["id"], child_index=index,
                           token_count=row["token_count"], utf8_byte_count=row["utf8_byte_count"])
        if oversized:
            group.setdefault("reason", "CHUNK_ATOM_OVERSIZED")
        group["complete"] = not group.get("reason") and bool(group["child_chunk_ids"])
        if group["complete"]:
            group["predecessor_refs"] = []

    def pack(self, units: list, prefix: list) -> list:
        output = []
        pending, required = [], []
        for refs, relations in units:
            combined = [*prefix, *pending, *refs]
            if pending and not self.fits(combined, target=True):
                output.append(([*prefix, *pending], required, None))
                pending, required = [], []
            pending.extend(refs)
            required.extend(relations)
        if pending:
            output.append(([*prefix, *pending], required, None))
        return output

    def _split_text(self, block: dict, prefix: list) -> list:
        body = _text(block)
        if body is None:
            return []
        if self.fits([*prefix, body]) or _UNRESOLVED_CONTEXT.search(block["text_content"]):
            return [body]
        text = block["text_content"]
        bounds = [0, *(item.end() for item in _SENTENCE_END.finditer(text)), len(text)]
        return [span for start, end in zip(bounds, bounds[1:]) if (span := _text(block, start=start, end=end))]

    def prose(self, blocks: list, *, continuation=None) -> None:
        first = blocks[0]
        prefix = self.contextual_titles(first)
        root = continuation or self.root(first, "section", "list", "document")
        members = {item["id"] for item in blocks}
        chain = [relation for block in blocks for relation in self.outgoing[block["id"]]
                 if relation["relation_type"] == "continues" and relation["to_block_id"] in members]
        self.used.update(block["id"] for block in blocks)
        units = [([span], []) for block in blocks for span in self._split_text(block, prefix)]
        if not units:
            return
        children = [(refs, [*relations, *chain], child_kind) for refs, relations, child_kind in self.pack(units, prefix)]
        if continuation:
            group = self.group("fact_group", first["id"], root, [*prefix, *(span for refs, _ in units for span in refs)], chain)
            self.emit(group, "continued_paragraph", children, force_parent=True)
        else:
            for refs, _, _ in children:
                group = self.group("fact_group", first["id"], root, refs, [])
                self.emit(group, "paragraph", [(refs, [], None)])

    def parameter(self, block) -> None:
        prefix = self.contextual_titles(block)
        body = _nonempty(_text(block))
        if not body:
            return
        links = [item for item in self.outgoing[block["id"]] if item["relation_type"] == "unit_of"]
        group = self.group("fact_group", block["id"], self.root(block, "section", "document"), [*prefix, *body], links,
                           reason=None if links else "CHUNK_PARAMETER_UNIT_UNRESOLVED")
        self.emit(group, "parameter", [([*prefix, *body], links, None)])

    def formula(self, block) -> None:
        identifier = block["id"]
        prefix = self.contextual_titles(block)
        body = _nonempty(_text(block))
        definitions = [item for item in self.incoming[identifier] if item["relation_type"] == "defines"]
        conditions = [item for item in self.outgoing[identifier] if item["relation_type"] == "requires"]
        definition_refs = [ref for link in definitions for ref in self.part(link, "from", "definition")]
        condition_refs = [ref for link in conditions for ref in self.part(link, "to", "condition")]
        links = [*definitions, *conditions]
        payload = [*prefix, *body, *definition_refs, *condition_refs]
        symbols = block["structured_content"].get("formula_symbols")
        reason = None
        if not isinstance(symbols, list) or {f"/formula_symbols/{index}" for index in range(len(symbols))} != {
                item["to_part"] for item in definitions} or not conditions or not body:
            reason = "CHUNK_FORMULA_DEPENDENCY_UNRESOLVED"
        group = self.group("fact_group", identifier, self.root(block, "section", "document"), payload, links, reason=reason)
        if not body:
            return
        parts = [(payload, links, None)]
        if reason is None and not self.fits(payload) and definition_refs:
            equation_part = [*prefix, *body, *condition_refs]
            per_definition = [([*prefix, {**ref, "role": "body"}], links, "formula_definition") for ref in self.unique(definition_refs)]
            if self.fits(equation_part) and all(self.fits(refs) for refs, _, _ in per_definition):
                parts = [(equation_part, links, "formula"), *per_definition]
        self.emit(group, "formula", parts, force_parent=True)

    def procedure(self, root: str, steps: list) -> None:
        first = steps[0]
        titles = self.contextual_titles(first)
        doc_root = self.root(first, "document")
        global_warns = [item for item in self.incoming[doc_root] if item["relation_type"] == "warns"]
        global_refs = [ref for item in global_warns for ref in self.part(item, "from", "warning")]
        units, ordered_steps = [], []
        unresolved = False
        for expected, block in enumerate(steps, 1):
            self.used.add(block["id"])
            step = block["structured_content"]["step"]
            number = step.get("number")
            if number != expected:
                unresolved = True
            ordered_steps.append({"block_id": block["id"], "step_number": number, "part": step.get("part")})
            links = [item for item in self.incoming[block["id"]] if item["relation_type"] == "warns"]
            required = [item for item in self.outgoing[block["id"]] if item["relation_type"] == "requires"]
            warnings = [{**ref, "step_number": number} for item in links for ref in self.part(item, "from", "warning")]
            preconditions = [{**ref, "step_number": number} for item in required for ref in self.part(item, "to", "condition")]
            regions = []
            for ref in self.outgoing[block["id"]]:
                if ref["relation_type"] != "refers_to":
                    continue
                target = self.blocks[ref["to_block_id"]]
                if target["kind"] not in {"image", "chart"}:
                    unresolved = True
                    continue
                assets = self._region(target, "context", relation_id=ref["id"], step_number=number)
                caption, caption_links = self.captions(target)
                unresolved |= not assets or not caption_links
                links.extend([ref, *caption_links])
                regions.extend([*assets, *caption])
            body = _nonempty(_text(block, step_number=number))
            unresolved |= not body
            local = [*warnings, *preconditions, *body, *regions]
            units.append((local, [*global_warns, *links, *required]))
        prefix = [*titles, *global_refs]
        group = self.group("procedure_group", root, root, [*prefix, *(ref for refs, _ in units for ref in refs)],
                           [item for _, links in units for item in links], ordered_steps=ordered_steps,
                           reason="CHUNK_PROCEDURE_DEPENDENCY_UNRESOLVED" if unresolved else None)
        self.emit(group, "procedure", self.pack(units, prefix), force_parent=True)

    def figure(self, block) -> None:
        identifier = block["id"]
        prefix = self.contextual_titles(block)
        captions, links = self.captions(block, "body")
        regions = self._region(block)
        # Visual assertions in provider body text may be inferred. Retrieval uses
        # actual captions and parser resources, with any action preserved separately.
        body = [*captions, *regions]
        if not body:
            group = self.group("fact_group", identifier, self.root(block, "image_group", "chart_group", "section", "document"),
                               [], links, reason="CHUNK_VISUAL_EVIDENCE_MISSING")
            if fallback := _text(block):
                self.emit(group, "figure", [([fallback], links, None)])
            return
        refs = [*prefix, *body] if captions else regions
        group = self.group("fact_group", identifier, self.root(block, "image_group", "chart_group", "section", "document"),
                           refs, links, reason="CHUNK_VISUAL_REGION_UNRESOLVED" if not regions else None)
        self.emit(group, "figure", [(refs, links, None)], category="candidate" if captions else "asset_only")

    def navigation(self, block, reason=None) -> None:
        identifier = block["id"]
        refs = _nonempty(_text(block))
        if not refs:
            if reason:
                self.group("fact_group", identifier, self.root(block, "section", "document"), [], [], reason=reason)
            return
        group = self.group("fact_group", identifier, self.root(block, "section", "index", "document"), refs, [], reason=reason)
        self.emit(group, block["kind"], [(refs, [], None)], category="navigation_only")

    def table_data(self, block) -> dict:
        identifier = block["id"]
        if identifier in self.tables_data:
            return self.tables_data[identifier]
        data = {"cells": {}, "positions": {}, "row_text": {}, "cell_text": {}, "inline_caption": None, "atomic_rows": []}
        self.tables_data[identifier] = data
        content = block["structured_content"]
        rows = content.get("rows")
        if not isinstance(rows, list) or not rows:
            return data
        width = content.get("column_count")
        if type(width) is not int or not 0 < width <= 512 or len(rows) > 2048:
            raise ParseContractError("CHUNK_TABLE_GRID_INVALID")
        blocked_cuts, occupied = set(), set()
        for row_number, row in enumerate(rows):
            if not isinstance(row, list):
                raise ParseContractError("CHUNK_TABLE_GRID_INVALID")
            for position, cell in enumerate(row):
                if not isinstance(cell, dict):
                    raise ParseContractError("CHUNK_TABLE_GRID_INVALID")
                r, c, rs, cs = (cell.get(key) for key in ("row", "column", "rowspan", "colspan"))
                if (any(type(value) is not int for value in (r, c, rs, cs)) or r != row_number
                        or not 0 <= c < width or not 0 < rs <= len(rows) - r or not 0 < cs <= width - c
                        or len(occupied) + rs * cs > 65536):
                    raise ParseContractError("CHUNK_TABLE_GRID_INVALID")
                area = {(row_index, column) for row_index in range(r, r + rs) for column in range(c, c + cs)}
                if occupied & area:
                    raise ParseContractError("CHUNK_TABLE_GRID_INVALID")
                occupied.update(area)
                blocked_cuts.update(range(r + 1, r + rs))
                path = f"/rows/{r}/{position}"
                data["cells"][(r, c)] = (path, cell)
                data["positions"][path] = (r, c)
        starts, ends = [], []
        for span in content["text_map"]:
            begin, end = span["block_start"], span["block_end"]
            if span.get("kind") == "table_caption":
                starts.append(begin)
                ends.append(end)
            if span.get("kind") != "table_cell" or not isinstance(span.get("cell_ref"), dict):
                continue
            r, c = span["cell_ref"].get("row"), span["cell_ref"].get("column")
            if type(r) is not int or type(c) is not int or (r, c) not in data["cells"]:
                raise ParseContractError("CHUNK_TABLE_GRID_INVALID")
            first, last = data["row_text"].get(r, (begin, end))
            data["row_text"][r] = min(first, begin), max(last, end)
            first, last = data["cell_text"].get((r, c), (begin, end))
            data["cell_text"][(r, c)] = min(first, begin), max(last, end)
        if starts:
            data["inline_caption"] = _text(block, "context", min(starts), max(ends), context_kind="table_caption")
        boundaries = [index for index in range(len(rows) + 1) if index not in blocked_cuts]
        data["atomic_rows"] = list(zip(boundaries, boundaries[1:]))
        data["column_count"] = width
        data["header_signature"] = [(r, c, cell["rowspan"], cell["colspan"], cell.get("text"))
                                     for (r, c), (_, cell) in data["cells"].items()
                                     if cell.get("header") and cell.get("header_scope") not in {"row", "rowgroup"}]
        return data

    def table_parts(self, block) -> tuple[list, str | None]:
        identifier = block["id"]
        data = self.table_data(block)
        captions, caption_links = self.captions(block)
        prefix = [*self.contextual_titles(block), *captions]
        prefix.extend(region for region in self._region(block, "context") if "cell_range" not in region)
        if data["inline_caption"]:
            prefix.append(data["inline_caption"])
        parent = self.root(block, "table_group")
        if parent:
            prefix.extend(span for foot in self.blocks[parent]["structured_content"].get("footnote_refs", [])
                          if foot in self.blocks and self.blocks[foot]["parent_block_id"] == parent
                          if (span := _text(self.blocks[foot], "context", context_kind="table_footnote")))
        if not data["atomic_rows"]:
            fallback = _nonempty(_text(block)) or self._region(block)
            return ([([*prefix, *fallback], caption_links, None)] if fallback else []), "CHUNK_TABLE_GRID_UNRESOLVED"
        column_count = data["column_count"]
        locate = data["positions"]
        by_headers = defaultdict(list)
        by_asset = defaultdict(list)
        for relation in self.outgoing[identifier]:
            if relation["relation_type"] == "header_of":
                if relation["from_part"] not in locate or relation["to_part"] not in locate:
                    raise ParseContractError("CHUNK_TABLE_GRID_INVALID")
                by_headers[locate[relation["to_part"]]].append(relation)
            if relation["relation_type"] == "refers_to" and relation["from_part"] in locate:
                by_asset[locate[relation["from_part"]]].append(relation)
        by_row = defaultdict(list)
        for position in data["cells"]:
            by_row[position[0]].append(position)
        reason = None
        units = []
        for start, end in data["atomic_rows"]:
            coords = [position for row in range(start, end) for position in by_row[row]]
            if coords and all(data["cells"][position][1].get("header") for position in coords):
                continue  # Source-column headers are inherited from each value-cell relation.
            header_refs, relations = [], list(caption_links)
            prototype = {"row_start": start, "row_end": end, "column_start": 0, "column_end": column_count}
            row_ranges = [data["row_text"][row] for row in range(start, end) if row in data["row_text"]]
            body = _nonempty(_text(block, "body", min(pair[0] for pair in row_ranges), max(pair[1] for pair in row_ranges),
                                   cell_range=prototype)) if row_ranges else []
            for cell_coordinates in coords:
                row, column = cell_coordinates
                cell = data["cells"][cell_coordinates][1]
                if not cell.get("header") and not by_headers[cell_coordinates]:
                    reason = "CHUNK_TABLE_HEADER_UNRESOLVED"
                for relation in by_headers[cell_coordinates]:
                    head_coordinates = locate[relation["from_part"]]
                    span = data["cell_text"].get(head_coordinates)
                    if span:
                        r, c = head_coordinates
                        header = data["cells"][head_coordinates][1]
                        header_refs.append(_text(block, "context", *span, context_kind="table_header", relation_id=relation["id"],
                            cell_range={"row_start": r, "row_end": r + header["rowspan"],
                                        "column_start": c, "column_end": c + header["colspan"]}))
                    else:
                        reason = "CHUNK_TABLE_HEADER_UNRESOLVED"
                    relations.append(relation)
                assets = self._region(block, cell={"row": row, "column": column})
                if assets and not by_asset[cell_coordinates]:
                    reason = "CHUNK_TABLE_ASSET_ACTION_UNRESOLVED"
                body.extend(assets)
                relations.extend(by_asset[cell_coordinates])
            if body:
                units.append(([*header_refs, *body], relations))
        if not units:
            fallback = _nonempty(_text(block)) or self._region(block)
            return ([([*prefix, *fallback], caption_links, None)] if fallback else []), "CHUNK_TABLE_ROWS_UNRESOLVED"
        return self.pack(units, prefix), reason

    def table(self, tables: list, *, continuation=None) -> None:
        self.used.update(block["id"] for block in tables)
        parts, refs, relations = [], [], []
        reason = None
        for block in tables:
            pieces, issue = self.table_parts(block)
            reason = reason or issue
            for payload, links, _ in pieces:
                refs.extend(payload)
                relations.extend(links)
            parts.extend(pieces)
        if continuation:
            page_links = []
            for previous, current in zip(tables, tables[1:]):
                p_root, c_root = self.root(previous, "table_group"), self.root(current, "table_group")
                bridge = [item for item in self.outgoing[p_root] if item["relation_type"] == "continues" and item["to_block_id"] == c_root]
                left, right = self.table_data(previous), self.table_data(current)
                if (len(bridge) != 1 or previous["file_page"] + 1 != current["file_page"]
                        or left.get("column_count") != right.get("column_count") or not left.get("header_signature")
                        or left["header_signature"] != right.get("header_signature")):
                    reason = "CHUNK_TABLE_CONTINUATION_UNRESOLVED"
                else:
                    page_links.extend(bridge)
            relations.extend(page_links)
            parts = [(payload, [*links, *page_links], kind) for payload, links, kind in parts]
        first = tables[0]
        root = continuation or self.root(first, "table_group", "section", "document")
        group = self.group("fact_group", root or first["id"], root, refs, relations, reason=reason)
        self.emit(group, "table_continuation" if continuation else "table", parts, force_parent=True)

    def assemble(self) -> None:
        procedures, continuations, tables = defaultdict(list), defaultdict(list), defaultdict(list)
        support_sources = set()
        unresolved_scope = set()
        tags = {block["id"]: set(_critical_tags(block)) for block in self.leaves}
        for relation in self.relations.values():
            kind = relation["relation_type"]
            if kind in {"caption_of", "defines", "warns"}:
                support_sources.add(relation["from_block_id"])
            elif kind == "requires":
                support_sources.add(relation["to_block_id"])
        scope_barriers = set()
        for block in self.leaves:
            identifier = block["id"]
            detail = block["structured_content"].get("step")
            scope = self.root(block, "section", "document")
            if block["kind"] == "paragraph" and _SCOPE_WORD.search(block["text_content"]):
                scope_barriers.add(scope)
            if scope in scope_barriers:
                unresolved_scope.add(identifier)
            if isinstance(detail, dict) and (root := self.root(block, "procedure")) and detail.get("procedure_id") == root:
                procedures[root].append(block)
            elif block["kind"] == "table" and (root := self.root(block, "table_continuation")):
                tables[root].append(block)
            elif block["kind"] == "paragraph" and not tags[identifier] and (root := self.root(block, "paragraph_group")):
                continuations[root].append(block)
            if block["kind"] == "table" and (parent := self.root(block, "table_group")):
                support_sources.update(self.blocks[parent]["structured_content"].get("footnote_refs", []))
        self.blocked.update(unresolved_scope)

        def prose_key(block):
            identifier = block["id"]
            if (block["kind"] != "paragraph" or not block["text_content"].strip() or tags[identifier]
                    or identifier in support_sources or _SCOPE_WORD.search(block["text_content"])
                    or _UNRESOLVED_CONTEXT.search(block["text_content"]) or not self.complete_source(block)):
                return None
            if self.root(block, "paragraph_group", "procedure", "index"):
                return None
            return self.root(block, "section", "list", "document"), block["file_page"]

        for index, block in enumerate(self.leaves):
            identifier = block["id"]
            if identifier in self.used:
                continue
            if identifier in unresolved_scope and block["kind"] == "paragraph" and not tags[identifier]:
                self.navigation(block, "CHUNK_APPLICABILITY_SCOPE_UNRESOLVED")
            elif (root := self.root(block, "procedure")) and block in procedures[root]:
                self.procedure(root, procedures[root])
            elif block["kind"] == "table":
                root = self.root(block, "table_continuation")
                self.table(tables[root] if root else [block], continuation=root)
            elif block["kind"] in {"image", "chart"}:
                self.figure(block)
            elif identifier in support_sources:
                continue
            elif block["kind"] == "formula":
                self.formula(block)
            elif tags[identifier] & {"operation_step", "operation_condition", "warning", "formula"}:
                self.navigation(block, "CHUNK_KEY_RELATION_UNRESOLVED")
            elif tags[identifier] & {"parameter", "unit"}:
                self.parameter(block)
            elif (root := self.root(block, "paragraph_group")) and block in continuations[root]:
                siblings = [item for item in continuations[root] if item["id"] not in support_sources]
                self.prose(siblings, continuation=root)
            elif (key := prose_key(block)) is not None:
                siblings = [block]
                for following in self.leaves[index + 1:]:
                    if prose_key(following) != key or following["id"] in self.used:
                        break
                    siblings.append(following)
                self.prose(siblings)
            elif block["kind"] == "paragraph" and not _UNRESOLVED_CONTEXT.search(block["text_content"]):
                self.prose([block])
            elif block["kind"] in _AUXILIARY or self.root(block, "index"):
                self.navigation(block)
            else:
                self.navigation(block, "CHUNK_LEAF_ROLE_UNRESOLVED")
            self.used.add(identifier)

        # Preserve support leaves omitted from an incomplete family as source navigation.
        represented = {item["block_id"] for group in self.groups for item in group["members"]}
        represented.update(group["structure_root_id"] for group in self.groups if group.get("reason"))
        for block in self.leaves:
            identifier = block["id"]
            if identifier not in represented and identifier in support_sources:
                self.navigation(block, "CHUNK_REQUIRED_SOURCE_UNMAPPED")

    def verify_collection(self) -> None:
        """Reassemble the serialized projection and verify support can be replayed."""
        by_chunk = {row["id"]: row for row in self.chunks}
        by_mapping = defaultdict(list)
        for row in self.mappings:
            if (row["chunk_id"] not in by_chunk or row["block_id"] not in self.blocks
                    or row["chunk_set_id"] != self.set_id or row["parse_run_id"] != self.run):
                raise ParseContractError("CHUNK_COLLECTION_MAPPING_INVALID")
            for item in row["segments"]:
                by_mapping[row["chunk_id"]].append({"block_id": row["block_id"], **item})
        regions = {}
        for row in self.chunks:
            mapped = by_mapping[row["id"]]
            if row["level"] == "parent":
                if row["parent_chunk_id"] is not None or mapped or row["content"] is not None:
                    raise ParseContractError("CHUNK_COLLECTION_MAPPING_INVALID")
                continue
            if row["parent_chunk_id"] and (row["parent_chunk_id"] not in by_chunk or by_chunk[row["parent_chunk_id"]]["level"] != "parent"):
                raise ParseContractError("CHUNK_COLLECTION_MAPPING_INVALID")
            fragments = [(item["chunk_start"], item["chunk_end"], item["text"]) for item in row["assembly"]]
            for span in mapped:
                src = self.blocks[span["block_id"]]
                if span["kind"] == "text":
                    text = src["text_content"][span["block_start"]:span["block_end"]]
                    fragments.append((span["chunk_start"], span["chunk_end"], text))
                    continue
                locator = span["locator_ref"]
                if src["id"] not in regions:
                    regions[src["id"]] = {(ref["region_ref"], ref["asset_ref"]) for ref in self._region(src)}
                if (span["asset_ref"] not in self.assets or (locator, span["asset_ref"]) not in regions[src["id"]]):
                    raise ParseContractError("CHUNK_COLLECTION_MAPPING_INVALID")
            cursor = 0
            for start, end, text in sorted(fragments):
                if start != cursor or end - start != len(text) or row["content"][start:end] != text:
                    raise ParseContractError("CHUNK_COLLECTION_MAPPING_INVALID")
                cursor = end
            if cursor != len(row["content"]) or row["utf8_byte_count"] != len(row["content"].encode("utf-8")):
                raise ParseContractError("CHUNK_COLLECTION_MAPPING_INVALID")
        for group in self.groups:
            if not group["complete"]:
                continue
            requirements = set()
            sources = defaultdict(list)
            for chunk_id in group["child_chunk_ids"]:
                requirements.update(by_chunk[chunk_id]["required_relation_ids"])
                for part in by_mapping[chunk_id]:
                    sources[part["block_id"]].append(part)
            if requirements != set(group["required_relation_ids"]):
                raise ParseContractError("CHUNK_COLLECTION_MAPPING_INVALID")
            coverage = {identifier: self.text_coverage(identifier, spans) for identifier, spans in sources.items()}
            for item in group["members"]:
                mapped = sources[item["block_id"]]
                if item["kind"] == "text":
                    valid = self.covers_text(coverage.get(item["block_id"], []), item["block_start"], item["block_end"])
                else:
                    valid = any(span.get("locator_ref") == item["region_ref"] and span.get("asset_ref") == item["asset_ref"] for span in mapped)
                if not valid:
                    raise ParseContractError("CHUNK_COLLECTION_MAPPING_INVALID")

    def result(self) -> dict:
        self.verify_collection()
        profile = {key: value for key, value in self.config.items() if key not in {"max_tokens", "max_utf8_bytes", "target_tokens"}}
        config_hash = fingerprint({"parse_run_id": self.run, "profile": profile})
        inputs = {key: self.config[key] for key in ("structured_ir_sha256", "source_qc_ref", "source_qc_sha256")}
        quality = {"schema": "dc.chunk-qc", "schema_version": "1", "checker_revision": COLLECTION_CHECKER_REVISION,
                   "parse_run_id": self.run, "chunk_set_id": self.set_id, "inputs": inputs, "config_hash": config_hash,
                   "status": "needs_review" if self.issues else "passed", "issues": self.issues,
                   "checks": {"projection_and_source_mapping": "passed", "required_support_and_budgets": "needs_review" if self.issues else "passed"},
                   "chunk_count": len(self.chunks), "mapping_count": len(self.mappings),
                   "support_group_count": len(self.groups), "max_tokens": self.config["max_tokens"],
                   "max_utf8_bytes": self.config["max_utf8_bytes"],
                   "manifest_hash": fingerprint({"chunks": self.chunks, "chunk_blocks": self.mappings, "support_groups": self.groups})}
        return {"schema": "dc.chunk-set", "schema_version": "1.0", "id": self.set_id, "chunk_set_id": self.set_id,
                "parse_run_id": self.run, "document_version_id": self.ir["document_version_id"], "status": "ready",
                "splitter_version": SPLITTER_VERSION, "splitter_config": profile, "config_hash": config_hash,
                "ir_content_hash": self.config["structured_ir_sha256"], "tokenizer_revision": TOKENIZER_REVISION,
                "chunks": self.chunks, "chunk_blocks": self.mappings, "support_groups": self.groups, "quality": quality}


def build_chunks(structured, *, chunk_set_id, profile, tokenizer) -> dict:
    """Freeze a collection candidate and independent technical QC over immutable inputs."""
    if (not isinstance(structured, dict) or (structured.get("schema"), structured.get("schema_version"),
            structured.get("phase")) != ("dc.document-ir", "1.0", "structured")
            or any(not isinstance(structured.get(key), list) for key in ("blocks", "relations", "issues", "artifact_refs"))
            or any(not isinstance(structured.get(key), str) or not structured[key] for key in ("parse_run_id", "document_version_id"))
            or not isinstance(chunk_set_id, str) or not chunk_set_id):
        raise ParseContractError("CHUNK_SOURCE_BINDING_INVALID")
    builder = _ChunkBuilder(structured, chunk_set_id, profile, tokenizer)
    builder.assemble()
    return builder.result()
