"""Loopback-only HTTP fixture for generation, embeddings and MinerU self-tests."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BytesIO
import json
import os
import re
import ssl
from threading import Event, Lock, Thread
from zipfile import ZipFile


CHAT_PATH = "/v1/chat/completions"
EMBEDDING_PATH = "/v1/embeddings"
BATCH_PATH = "/api/v4/file-urls/batch"
POLL_PATH = "/api/v4/extract-results/batch/"
UPLOAD_PATH = "/synthetic-upload/"
DOWNLOAD_PATH = "/synthetic-download/"
TRANSFER_TICKET = "synthetic-transfer-ticket"
RESULT_MARKER = "SYNTHETIC OK"


@dataclass(frozen=True)
class FixtureRequest:
    method: str
    target: str
    body: bytes
    authenticated: bool
    authorization_present: bool


@dataclass
class _Batch:
    data_id: str
    uploaded: bool = False
    polls: int = 0


def _content_archive() -> bytes:
    content = [
        {"type": "text", "text": "SYNTHETIC WARNING: Disconnect power before maintenance.", "page_idx": 0},
        {"type": "table", "table_caption": ["SYNTHETIC maintenance schedule"],
         "table_body": "<table><tr><td>Demo inspection</td><td>30 days</td></tr></table>",
         "table_footnote": ["SYNTHETIC interval requires manual verification."], "page_idx": 0},
        {"type": "image", "image_caption": ["SYNTHETIC isolator diagram"],
         "image_footnote": ["SYNTHETIC only. Verify position on the original page."], "page_idx": 0},
    ]
    stream = BytesIO()
    with ZipFile(stream, "w") as archive:
        archive.writestr("synthetic/synthetic_content_list.json", json.dumps(content))
    return stream.getvalue()


class _Handler(BaseHTTPRequestHandler):
    server: SyntheticProvider

    def log_message(self, format, *args):
        # Paths may contain a synthetic transfer ticket. Keep them out of logs.
        pass

    def _reply(self, status: int, body: bytes = b"", *, content_type: str = "application/json", location=None,
               declared_length: int | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body) if declared_length is None else declared_length))
        if location:
            self.send_header("Location", location)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data: dict):
        self._reply(200, json.dumps(data).encode("utf-8"))

    def _transfer_url(self, target: str, port: int) -> str:
        return f"{self.server.scheme}://127.0.0.1:{port}{target}?ticket={TRANSFER_TICKET}"

    def _batch_create(self, payload: dict):
        files = payload.get("files")
        file = files[0] if isinstance(files, list) and len(files) == 1 else None
        if (not isinstance(file, dict) or file.get("name") != "synthetic.pdf"
                or not isinstance(file.get("data_id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", file["data_id"])
                or payload.get("model_version") != "vlm"):
            self._reply(400)
            return
        pack = _Batch(data_id=file["data_id"])
        batch_id = f"synthetic-{pack.data_id}"
        with self.server.lock:
            self.server.batches[batch_id] = pack
        self._json({"code": 0, "data": {
            "batch_id": batch_id,
            "file_urls": [self._transfer_url(UPLOAD_PATH + batch_id, self.server.transfer_port or self.server.port)],
        }})

    def _poll(self, batch_id: str):
        with self.server.lock:
            batch = self.server.batches.get(batch_id)
            if batch:
                batch.polls += 1
                ready = batch.uploaded and (self.server.mode != "pending" or batch.polls > 1)
        if not batch:
            self._reply(404)
            return
        result = {"file_name": "synthetic.pdf", "data_id": batch.data_id, "state": "done" if ready else "running"}
        if ready:
            result["full_zip_url"] = self._transfer_url(DOWNLOAD_PATH + batch_id, self.server.port)
        self._json({"code": 0, "data": {"batch_id": batch_id, "extract_result": [result]}})

    def _transfer(self, path: str, body: bytes):
        if self.path.partition("?")[2] != f"ticket={TRANSFER_TICKET}":
            self._reply(400)
            return
        batch_id = path.rsplit("/", 1)[-1]
        with self.server.lock:
            batch = self.server.batches.get(batch_id)
            upload = self.command == "PUT" and path.startswith(UPLOAD_PATH) and body.startswith(b"%PDF-")
            download = self.command == "GET" and path.startswith(DOWNLOAD_PATH)
            if batch and upload:
                batch.uploaded = True
        if batch and upload:
            if self.server.mode != "disconnect-upload":
                self._reply(200)
        elif batch and download and batch.uploaded:
            self._reply(200, _content_archive(), content_type="application/zip")
        else:
            self._reply(404)

    def _inference(self, path: str, payload: dict):
        model = payload.get("model")
        if path == CHAT_PATH and model == self.server.model and payload.get("stream") is False:
            result_text = "SYNTHETIC unexpected reply" if self.server.mode == "wrong-content" else RESULT_MARKER
            body = json.dumps({"object": "chat.completion", "model": model, "choices": [
                {"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": result_text}},
            ], "usage": {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9}}).encode("utf-8")
            declared_length = len(body) + 10 if self.server.mode == "short-body" else None
            self._reply(200, body, declared_length=declared_length)
        elif (path == EMBEDDING_PATH and model in ("BAAI/bge-m3", "Pro/BAAI/bge-m3")
              and payload.get("encoding_format") == "float" and "dimensions" not in payload
              and isinstance(payload.get("input"), list) and 1 <= len(payload["input"]) <= 32):
            self._json({"object": "list", "model": model, "data": [
                {"object": "embedding", "index": i, "embedding": [1.0] + [0.0] * 1023}
                for i in reversed(range(len(payload["input"])))
            ], "usage": {"prompt_tokens": 6, "total_tokens": 6}})
        else:
            self._reply(400)

    def _dispatch(self):
        path = self.path.partition("?")[0]
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self._reply(400)
            return
        if not 0 <= length <= 65536:
            self._reply(413)
            return
        body = self.rfile.read(length)
        auth = self.headers.get("Authorization")
        authenticated = auth == f"Bearer {self.server.credential}"
        if self.server.record_calls:
            with self.server.lock:
                self.server.requests.append(FixtureRequest(self.command, self.path, body, authenticated, auth is not None))
        if path.startswith((UPLOAD_PATH, DOWNLOAD_PATH)):
            if auth is not None:
                self._reply(400)
                return
            self._transfer(path, body)
            return
        if not authenticated:
            self._reply(401)
            return
        if self.command == "GET" and path.startswith(POLL_PATH):
            self._poll(path.removeprefix(POLL_PATH))
            return
        if self.command != "POST" or path not in (CHAT_PATH, EMBEDDING_PATH, BATCH_PATH):
            self._reply(404)
            return
        if self.server.mode == "disconnect":
            return  # The request arrived, but the client cannot know its result.
        if self.server.mode == "timeout":
            self.server.release_stalled_request.wait()
            return
        if self.server.mode == "redirect":
            self._reply(307, location=f"{self.server.scheme}://127.0.0.1:{self.server.port}/synthetic-redirect-target")
            return
        if self.server.mode == "oversize":
            self._reply(200, b" " * (1024 * 1024 + 1))
            return
        if self.server.mode == "malformed":
            self._reply(200, b'{"SYNTHETIC upstream body must stay private":')
            return
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError):
            self._reply(400)
            return
        if not isinstance(payload, dict):
            self._reply(400)
        elif path == BATCH_PATH:
            self._batch_create(payload)
        else:
            self._inference(path, payload)

    do_GET = do_POST = do_PUT = _dispatch


class SyntheticProvider(ThreadingHTTPServer):
    """A local fixture server with no upstream calls or recorded credentials."""

    def __init__(self, port: int, *, credential: str, model: str = "synthetic-chat", record_calls: bool = False):
        super().__init__(("127.0.0.1", port), _Handler)
        self.credential = credential
        self.model = model
        self.record_calls = record_calls
        self.scheme = "http"
        self.mode = "normal"
        self.transfer_port: int | None = None
        self.lock = Lock()
        self.release_stalled_request = Event()
        self.requests: list[FixtureRequest] = []
        self.batches: dict[str, _Batch] = {}

    @property
    def port(self) -> int:
        return self.server_address[1]


@contextmanager
def run_synthetic_provider(
    *, credential: str, model: str = "synthetic-chat", tls_context: ssl.SSLContext | None = None,
) -> Iterator[SyntheticProvider]:
    with SyntheticProvider(0, credential=credential, model=model, record_calls=True) as server:
        if tls_context is not None:
            server.socket = tls_context.wrap_socket(server.socket, server_side=True)
            server.scheme = "https"
        thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        try:
            yield server
        finally:
            server.release_stalled_request.set()
            server.shutdown()
            thread.join()


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve SYNTHETIC fixtures on 127.0.0.1 only.")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--model", default="synthetic-chat")
    parser.add_argument("--credential-env", default="DATACENTER_SYNTHETIC_TEST_KEY")
    arguments = parser.parse_args()
    if not re.fullmatch(r"DATACENTER_SYNTHETIC_[A-Z0-9_]{1,60}", arguments.credential_env):
        parser.error("Use a DATACENTER_SYNTHETIC_ credential environment reference.")
    credential = os.environ.get(arguments.credential_env)
    if not credential:
        parser.error("The synthetic credential environment reference is empty.")
    if not 1024 <= arguments.port <= 65535:
        parser.error("Use an unprivileged port from 1024 to 65535.")
    with SyntheticProvider(arguments.port, credential=credential, model=arguments.model) as server:
        print(f"SYNTHETIC provider listening on 127.0.0.1:{server.port}", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
