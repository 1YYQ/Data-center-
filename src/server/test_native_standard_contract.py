"""Regression for the blank-page shape emitted by native MinerU 4.2.3."""
from hashlib import sha256
from io import BytesIO
import json
from uuid import uuid4
from zipfile import ZipFile

import pymupdf
import pytest

from .parsing.ir import normalize_ir
from .parsing.local_pipeline import PROFILE, ParseContractError, read_bundle, source_preflight
from .test_local_parse_pipeline import SYNTHETIC_TEXT, saved_bundle


@pytest.fixture
def input_artifacts(tmp_path):
    source = tmp_path / "synthetic-standard.pdf"
    with pymupdf.open() as pdf:
        pdf.new_page(width=595, height=842).insert_text((72, 72), SYNTHETIC_TEXT)
        pdf.new_page(width=595, height=842)
        pdf.save(source)
    source_sha = sha256(source.read_bytes()).hexdigest()
    preflight = source_preflight(source, source_sha, source.stat().st_size)
    with ZipFile(BytesIO(saved_bundle())) as archive:
        members = {name: archive.read(name) for name in archive.namelist()}
    middle = json.loads(members["middle_json.json"])
    return middle, members, preflight


def _package(middle, members):
    buffer = BytesIO()
    with ZipFile(buffer, "w") as archive:
        for name, body in members.items():
            archive.writestr(name, json.dumps(middle).encode() if name == "middle_json.json" else body)
    return buffer.getvalue()


def _normalize(middle, preflight):
    return normalize_ir(run_id=str(uuid4()), version_id=str(uuid4()), source_file_id=str(uuid4()),
                        source_sha256=preflight["source_sha256"], profile=PROFILE, preflight=preflight,
                        middle=middle, raw_artifact_id=str(uuid4()), preflight_artifact_id=str(uuid4()))


def test_explicit_blank_source_page_accepts_sdk_omitted_default_blocks(input_artifacts):
    middle, members, preflight = input_artifacts
    del middle["pages"][1]["blocks"]  # Middle skips defaults while Structured emits [].
    saved_middle, saved_members = read_bundle(_package(middle, members), preflight, PROFILE)
    assert saved_middle == middle and json.loads(saved_members["middle_json.json"]) == middle
    normalized = _normalize(saved_middle, preflight)
    blank = normalized["pages"][1]
    assert (blank["file_page"], blank["provider_page_idx"], blank["state"]) == (2, 1, "blank")
    assert blank["raw_ref"]["json_pointer"] == "/pages/1"


@pytest.mark.parametrize("index", [0, 1], ids=["nonblank-omission", "blank-explicit-null"])
def test_missing_default_does_not_mask_nonblank_or_malformed_pages(input_artifacts, index):
    middle, members, preflight = input_artifacts
    if index == 0:
        del middle["pages"][index]["blocks"]
    else:
        middle["pages"][index]["blocks"] = None
    with pytest.raises(ParseContractError, match="PARSE_PAGE_COVERAGE_INCOMPLETE"):
        read_bundle(_package(middle, members), preflight, PROFILE)
    with pytest.raises(ParseContractError, match="PARSE_PAGE_COVERAGE_INCOMPLETE"):
        _normalize(middle, preflight)
