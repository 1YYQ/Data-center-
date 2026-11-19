from __future__ import annotations

from io import BytesIO
import json
import stat
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

import pytest

from src.server.parsing import mineru
from src.server.parsing.mineru import MinerUAdapter
from src.server.provider_transport import ProviderError

PDF = b"%PDF-1.4\nSYNTHETIC\n%%EOF"
DATA_ID = "synthetic.v1"
BATCH_ID = "batch-42"
UPLOAD = "http://127.0.0.1:31459/upload?signature=fixture"
DOWNLOAD = "http://127.0.0.1:31459/download?signature=fixture"
POLL = f"/api/v4/extract-results/batch/{BATCH_ID}"


class ScriptedTransport:
    def __init__(self, *responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, target, *, body=b"", purpose="api", content_type="application/json"):
        self.calls.append((method, target, body, purpose, content_type))
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def encoded(value):
    return json.dumps(value, ensure_ascii=True).encode("utf-8")


def success(**data):
    return encoded({"code": 0, "data": data})


def row(**updates):
    result = {"state": "done", "data_id": DATA_ID, "file_name": "synthetic.pdf", "full_zip_url": DOWNLOAD}
    result.update(updates)
    return result


def zip_bytes(*members, compression=0):
    buffer = BytesIO()
    with ZipFile(buffer, "w", compression=compression) as archive:
        for name, content in members:
            archive.writestr(name, content)
    return buffer.getvalue()


def content_zip(value, name="results/synthetic_content_list.json"):
    return zip_bytes((name, encoded(value)))


def done_with(payload):
    return ScriptedTransport(success(batch_id=BATCH_ID, extract_result=[row()]), payload)


def assert_error(code, function):
    with pytest.raises(ProviderError) as exc:
        function()
    assert str(exc.value) == exc.value.code == code


def test_submit_checkpoints_before_put_and_polls_legacy_content():
    transport = ScriptedTransport(
        success(batch_id=BATCH_ID, file_urls=[UPLOAD]), b"",
        success(batch_id=BATCH_ID, extract_result=[row(state="running")]),
        success(batch_id=BATCH_ID, extract_result=[row()]),
        content_zip([
            {"type": "text", "page_idx": 0, "text": "SYNTHETIC 警告：先断电。", "bbox": [5, 8, 10, 12]},
            {"type": "table", "page_idx": 2, "table_caption": ["SYNTHETIC 表头"],
             "table_body": "<table><tr><th>单位</th><td>V</td></tr></table>", "table_footnote": ["SYNTHETIC 仅限断电"]},
            {"type": "image", "page_idx": 2, "image_caption": ["SYNTHETIC 图注"],
             "content": "SYNTHETIC 注释", "image_footnote": ["SYNTHETIC 图脚注"]},
            {"type": "equation", "page_idx": 3, "text": "$$U=RI$$", "text_format": "latex"},
            {"type": "page_number", "page_idx": 3, "text": "42"},
            {"type": "image", "page_idx": 4, "img_path": "images/visual.jpg"},
        ]),
    )
    adapter = MinerUAdapter(transport)
    checkpoints = []

    def checkpoint(batch_id):
        assert [call[:2] for call in transport.calls] == [("POST", "/api/v4/file-urls/batch")]
        checkpoints.append(batch_id)

    assert adapter.submit(PDF, DATA_ID, checkpoint=checkpoint) == BATCH_ID
    assert checkpoints == [BATCH_ID]
    assert json.loads(transport.calls[0][2]) == {
        "model_version": "vlm", "enable_table": True, "enable_formula": True,
        "files": [{"name": "synthetic.pdf", "data_id": DATA_ID}],
    }
    assert transport.calls[1] == ("PUT", UPLOAD, PDF, "upload", None)
    assert adapter.poll(BATCH_ID, DATA_ID) is None
    blocks = adapter.poll(BATCH_ID, DATA_ID)
    assert [call[:2] for call in transport.calls[2:4]] == [("GET", POLL), ("GET", POLL)]
    assert transport.calls[4] == ("GET", DOWNLOAD, b"", "download", None)
    assert [block[2] for block in blocks] == ["paragraph", "table", "image", "equation", "page_number", "image"]
    assert blocks[0] == ("SYNTHETIC 警告：先断电。", {
        "file_page": 1, "printed_page": None, "needs_review": True,
        "provider_type": "text", "provider_block_index": 0, "provider_bbox": [5, 8, 10, 12],
    }, "paragraph")
    assert blocks[1][0] == "SYNTHETIC 表头\n<table><tr><th>单位</th><td>V</td></tr></table>\nSYNTHETIC 仅限断电"
    assert blocks[2][0] == "SYNTHETIC 图注\nSYNTHETIC 注释\nSYNTHETIC 图脚注"
    assert blocks[3][0] == "$$U=RI$$"
    assert blocks[5][1]["visual_only"] is True
    assert [block[1]["file_page"] for block in blocks] == [1, 3, 3, 4, 4, 5]
    assert all(block[1]["needs_review"] and block[1]["printed_page"] is None for block in blocks)


def test_interrupted_upload_retains_checkpoint_for_query_only_recovery():
    transport = ScriptedTransport(
        success(batch_id=BATCH_ID, file_urls=[UPLOAD]), ProviderError("PROVIDER_OUTCOME_UNKNOWN"),
        success(batch_id=BATCH_ID, extract_result=[row(state="waiting-file")]),
    )
    checkpoints = []
    assert_error("PROVIDER_OUTCOME_UNKNOWN", lambda: MinerUAdapter(transport).submit(
        PDF, DATA_ID, checkpoint=checkpoints.append,
    ))
    assert checkpoints == [BATCH_ID]
    assert MinerUAdapter(transport).poll(checkpoints[0], DATA_ID) is None
    assert [call[:2] for call in transport.calls] == [
        ("POST", "/api/v4/file-urls/batch"), ("PUT", UPLOAD), ("GET", POLL),
    ]


def test_bad_url_still_preserves_batch_and_failed_checkpoint_blocks_upload():
    transport = ScriptedTransport(success(batch_id=BATCH_ID, file_urls=["file:///synthetic"]))
    checkpoints = []
    assert_error("MINERU_INVALID_RESPONSE", lambda: MinerUAdapter(transport).submit(
        PDF, DATA_ID, checkpoint=checkpoints.append,
    ))
    assert checkpoints == [BATCH_ID]
    assert len(transport.calls) == 1

    transport = ScriptedTransport(success(batch_id=BATCH_ID, file_urls=[UPLOAD]))

    def fail(_):
        raise ProviderError("CHECKPOINT_FAILED")

    assert_error("CHECKPOINT_FAILED", lambda: MinerUAdapter(transport).submit(PDF, DATA_ID, checkpoint=fail))
    assert len(transport.calls) == 1


@pytest.mark.parametrize("body, code", [
    (b"{bad synthetic secret", "MINERU_INVALID_RESPONSE"),
    (b'{"code":0,"code":0,"data":{}}', "MINERU_INVALID_RESPONSE"),
    (b'{"code":false,"data":{}}', "MINERU_INVALID_RESPONSE"),
    (encoded({"code": 9, "msg": "synthetic secret"}), "MINERU_API_REJECTED"),
    (success(batch_id="../../else?mock=secret", file_urls=[UPLOAD]), "MINERU_INVALID_RESPONSE"),
])
def test_rejects_malformed_submit_response(body, code):
    transport = ScriptedTransport(body)
    checkpoints = []
    assert_error(code, lambda: MinerUAdapter(transport).submit(PDF, DATA_ID, checkpoint=checkpoints.append))
    assert not checkpoints and len(transport.calls) == 1


@pytest.mark.parametrize("batch_id, results, code", [
    ("different-id", [row()], "MINERU_INVALID_RESPONSE"),
    (BATCH_ID, [], "MINERU_INVALID_RESPONSE"),
    (BATCH_ID, [row(), row()], "MINERU_INVALID_RESPONSE"),
    (BATCH_ID, [row(data_id="different-data-id")], "MINERU_INVALID_RESPONSE"),
    (BATCH_ID, [row(file_name="other.pdf")], "MINERU_INVALID_RESPONSE"),
    (BATCH_ID, [row(state="unexpected")], "MINERU_INVALID_RESPONSE"),
    (BATCH_ID, [row(state={})], "MINERU_INVALID_RESPONSE"),
    (BATCH_ID, [row(state="failed", err_msg="synthetic secret")], "MINERU_EXTRACTION_FAILED"),
    (BATCH_ID, [row(full_zip_url="http://user:secret@127.0.0.1/file")], "MINERU_INVALID_RESPONSE"),
])
def test_poll_validates_response_before_download(batch_id, results, code):
    transport = ScriptedTransport(success(batch_id=batch_id, extract_result=results))
    assert_error(code, lambda: MinerUAdapter(transport).poll(BATCH_ID, DATA_ID))
    assert [call[:2] for call in transport.calls] == [("GET", POLL)]


def test_invalid_input_never_accesses_transport():
    transport = ScriptedTransport()
    adapter = MinerUAdapter(transport)
    assert_error("MINERU_INVALID_INPUT", lambda: adapter.submit(b"SYNTHETIC", DATA_ID, checkpoint=lambda _: None))
    assert_error("MINERU_INVALID_INPUT", lambda: adapter.submit(PDF, "invalid/id", checkpoint=lambda _: None))
    assert_error("MINERU_INVALID_INPUT", lambda: adapter.poll("..", DATA_ID))
    assert not transport.calls


@pytest.mark.parametrize("name", ["../image.jpg", "/image.jpg", "images\\entry.jpg", "C:/entry.jpg", "images//entry.jpg"])
def test_unsafe_archive_path_is_rejected_even_when_unused(name):
    # Windows' ZIP writer normalizes backslashes, so patch the raw ZIP names.
    archive_name = name.replace("\\", "|")
    payload = zip_bytes(("content_list.json", b"[]"), (archive_name, b"SYNTHETIC"))
    if archive_name != name:
        payload = payload.replace(archive_name.encode("ascii"), name.encode("ascii"))
    assert_error("MINERU_INVALID_ARCHIVE", lambda: MinerUAdapter(done_with(payload)).poll(BATCH_ID, DATA_ID))


def test_duplicate_archive_identities_multiple_matches_legacy_only_and_symlinks():
    symbolic = ZipInfo("images/link.jpg")
    symbolic.create_system = 3
    symbolic.external_attr = (stat.S_IFLNK | 0o777) << 16
    content = ("result/content_list.json", b'[{"type":"text","page_idx":0,"text":"SYNTHETIC"}]')
    for extra in [(("RESULT/CONTENT_LIST.JSON", b"[]"),), ((symbolic, b"data"),),
                  (("result", b"file occupying parent path"),)]:
        payload = zip_bytes(content, *extra)
        assert_error("MINERU_INVALID_ARCHIVE", lambda: MinerUAdapter(done_with(payload)).poll(BATCH_ID, DATA_ID))
    for payload in [zip_bytes(content, ("other_content_list.json", b"[]")),
                    zip_bytes(("structured_content.json", b'{"pages":[]}'))]:
        assert_error("MINERU_UNSUPPORTED_CONTENT_LIST", lambda: MinerUAdapter(done_with(payload)).poll(BATCH_ID, DATA_ID))
    assert MinerUAdapter(done_with(zip_bytes(content))).poll(BATCH_ID, DATA_ID)[0][0] == "SYNTHETIC"


def test_rejects_encrypted_and_corrupted_zip():
    payload = zip_bytes(("content_list.json", b"SYNTHETIC"))
    encrypted = bytearray(payload)
    directory = encrypted.index(b"PK\x01\x02")
    encrypted[directory + 8] |= 1
    corrupted = payload.replace(b"SYNTHETIC", b"sYNTHETIC", 1)
    for bad_payload in [bytes(encrypted), corrupted, b"invalid zip"]:
        assert_error("MINERU_INVALID_ARCHIVE", lambda: MinerUAdapter(done_with(bad_payload)).poll(BATCH_ID, DATA_ID))


@pytest.mark.parametrize("bound, limit", [
    ("_MAX_ARCHIVE_BYTES", 8), ("_MAX_MEMBERS", 1), ("_MAX_DECOMPRESSED_BYTES", 128),
    ("_MAX_CONTENT_BYTES", 16), ("_MAX_COMPRESSION_RATIO", 1),
])
def test_archive_limits(monkeypatch, bound, limit):
    payload = zip_bytes(("content_list.json", b'[{"type":"text","page_idx":0,"text":"SYNTHETIC"}]'),
                        ("images/ratio.jpg", b"a" * 256), compression=ZIP_DEFLATED)
    monkeypatch.setattr(mineru, bound, limit)
    assert_error("MINERU_ARCHIVE_LIMIT", lambda: MinerUAdapter(done_with(payload)).poll(BATCH_ID, DATA_ID))


@pytest.mark.parametrize("entry, code", [
    ({"type": "text", "page_idx": True, "text": "SYNTHETIC"}, "MINERU_INVALID_CONTENT_LIST"),
    ({"type": "text", "page_idx": -1, "text": "SYNTHETIC"}, "MINERU_INVALID_CONTENT_LIST"),
    ({"type": "text", "page_idx": "1", "text": "SYNTHETIC"}, "MINERU_INVALID_CONTENT_LIST"),
    ({"type": "text", "page_idx": 0, "text": "\ud800"}, "MINERU_INVALID_CONTENT_LIST"),
    ({"type": "text", "page_idx": 0, "text": "SYNTHETIC", "bbox": [0, 0, True, 4]}, "MINERU_INVALID_CONTENT_LIST"),
    ({"type": "image", "page_idx": 0, "image_caption": ["SYNTHETIC", {"text": "warning"}]}, "MINERU_INVALID_CONTENT_LIST"),
    ({"type": "text", "page_idx": 0, "text": "SYNTHETIC", "footnote": "warning"}, "MINERU_UNSUPPORTED_CONTENT_LIST"),
    ({"type": "unexpected", "page_idx": 0, "text": "warning"}, "MINERU_UNSUPPORTED_CONTENT_LIST"),
])
def test_unknown_and_malformed_content_cannot_return_partial_success(entry, code):
    payload = content_zip([{"type": "text", "page_idx": 0, "text": "SYNTHETIC"}, entry])
    assert_error(code, lambda: MinerUAdapter(done_with(payload)).poll(BATCH_ID, DATA_ID))
