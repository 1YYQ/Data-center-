"""Isolated FastAPI protocol fixture, with synthetic state and loopback upstream.

Never imported by the application. No business accounts, documents or providers.
This exercises actual SDK HTTP serialization, not production authorization.
"""
import hashlib
import http.client
import json
import os
import socket
import sys
import threading
import uuid
from contextlib import asynccontextmanager
from urllib.parse import urlparse

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response


def sha(body):
    return hashlib.sha256(body).hexdigest()


def main():
    upstream = urlparse(sys.argv[1])
    if upstream.scheme != "http" or upstream.hostname != "127.0.0.1" or not upstream.port or upstream.path not in ("", "/"):
        raise ValueError("loopback upstream required")
    key = os.environ["PI00_SYNTHETIC_KEY"]
    state = {"allowed": True, "suspended": False, "revoke_on_authorize": False}
    projections, calls = {}, {}
    lock = threading.Lock()
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)

    @asynccontextmanager
    async def lifespan(app):
        print(json.dumps({"port": listener.getsockname()[1]}), flush=True)
        yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def authorize(request):
        if request.headers.get("X-PI00-Key") != key:
            raise HTTPException(401, "private fixture credential required")

    def eligible():
        if not state["allowed"] or state["suspended"]:
            raise HTTPException(403, "synthetic source/run is no longer eligible")

    @app.post("/context/project")
    async def project(request: Request):
        authorize(request)
        body = await request.json()
        if body.get("purpose") not in {"answer", "compaction", "summary"}:
            raise HTTPException(403, "purpose required")
        eligible()
        # Only this server's known synthetic source is projected, never client text.
        context = {"systemPrompt": "PI00 SYNTHETIC SYSTEM", "messages": [
            {"role": "user", "content": [{"type": "text", "text": "PI00 AUTHORIZED SYNTHETIC SOURCE " + body["purpose"]}], "timestamp": 1}
        ]}
        context_json = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        result = {"id": str(uuid.uuid4()), "purpose": body["purpose"], "context_json": context_json,
                  "context_digest": sha(context_json.encode())}
        projections[result["id"]] = result
        return result

    @app.post("/model/authorize")
    async def admit(request: Request):
        authorize(request)
        body = await request.json()
        eligible()
        p = projections.get(body.get("projection_id"))
        if not p or body.get("purpose") != p["purpose"] or body.get("context_digest") != p["context_digest"] or body.get("adapter") != "pi-ai-1.5.2/openai-completions":
            raise HTTPException(403, "projection/adapter mismatch")
        fingerprint = body.get("payload_digest", "")
        if len(fingerprint) != 64 or any(ch not in "0123456789abcdef" for ch in fingerprint):
            raise HTTPException(403, "payload digest required")
        receipt = {"id": str(uuid.uuid4()), "state": "reserved", "purpose": p["purpose"], "payload_digest": fingerprint}
        with lock:
            calls[receipt["id"]] = receipt
            if state["revoke_on_authorize"]:
                state["allowed"] = False
        return {"id": receipt["id"]}

    @app.post("/v1/chat/completions")
    async def forward(request: Request):
        authorize(request)
        raw = await request.body()
        with lock:
            eligible()
            receipt = calls.get(request.headers.get("X-Model-Call"))
            if not receipt or receipt["payload_digest"] != sha(raw) or receipt["state"] != "reserved":
                raise HTTPException(409, "missing/mismatched/consumed model admission")
            receipt["state"] = "dispatching"
        # Fixed synthetic upstream, no redirects or caller-selected target.
        connection = http.client.HTTPConnection(upstream.hostname, upstream.port, timeout=5)
        try:
            connection.request("POST", "/v1/chat/completions", body=raw, headers={"Content-Type": "application/json"})
            response = connection.getresponse()
            result = response.read()
            receipt["state"] = "completed" if response.status == 200 else "failed"
            eligible()  # Refuse stale responses after intervening synthetic revocation.
            return Response(result, status_code=response.status, media_type=response.getheader("Content-Type", "application/json"))
        except (OSError, http.client.HTTPException):
            receipt["state"] = "unknown"
            raise HTTPException(502, "synthetic upstream outcome unknown")
        finally:
            connection.close()

    @app.post("/fixture/control")
    async def control(request: Request):
        authorize(request)
        body = await request.json()
        if not set(body).issubset(state) or any(not isinstance(value, bool) for value in body.values()):
            raise HTTPException(400, "fixture controls only")
        state.update(body)
        return {"calls": list(calls.values())}

    import asyncio
    asyncio.run(uvicorn.Server(uvicorn.Config(app, log_level="error", access_log=False)).serve(sockets=[listener]))


if __name__ == "__main__":
    main()
