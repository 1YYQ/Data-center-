"""Small model views of exact evidence; provenance stays in the private ledger."""
from copy import deepcopy
import json


def model_fragment(fragment, *, evidence_id=None):
    locator = fragment["locator"]
    location = {key: deepcopy(locator[key]) for key in ("file_page", "printed_page", "section", "cell_range") if key in locator}
    region = locator.get("region")
    if isinstance(region, dict):
        location["region"] = {key: deepcopy(region[key]) for key in ("file_page", "geometry_status", "bbox_normalized") if key in region}
    return {"evidence_id": evidence_id or fragment["id"], "document_version_id": fragment["document_version_id"],
            "title": fragment["document_title"], "revision_label": fragment.get("version_label"),
            "text": fragment["text"], "evidence_kind": fragment["evidence_kind"], "locator": location,
            "role": fragment["role"], "step_number": fragment.get("step_number"),
            "block_id": fragment["block_id"], "block_start": fragment.get("block_start"), "block_end": fragment.get("block_end"),
            "asset_refs": list(fragment["asset_refs"])}


def support_projection_size(groups, fragments):
    """Budget the complete usable evidence, without repeated private QC manifests."""
    group_views = [{key: deepcopy(group[key]) for key in ("id", "kind", "scope_key", "members", "predecessor_refs")} for group in groups]
    return len(json.dumps([group_views, [model_fragment(fragment) for fragment in fragments]], ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8"))
