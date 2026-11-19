"""Private loopback-only transport for the worker's pinned provider.

No cookie authentication, caller context, provider URL, headers or registration.
The worker obtains a run token from runtime_gate.open_run and passes it only to
the process it started. App startup can register these routes safely by default:
without a trusted local profile and accepted task, no run is admitted.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from .db import get_db
from .runtime_gate import authorize_model, call_receipt, dispatch_model, project_context
from .services import SafeError


class ProjectInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    operation: str
    business_purpose: str


class AuthorizeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    projection_id: str
    context_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    adapter: str
    payload_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_key: str = Field(min_length=1, max_length=200)


def _credential(request: Request) -> str:
    if (not request.client or request.client.host not in {"127.0.0.1", "::1"}
            or request.headers.get("cookie") or not request.headers.get("X-Runtime-Token")):
        raise SafeError("RUNTIME_AUTH_REQUIRED", "需要私有运行凭据", 401)
    return request.headers["X-Runtime-Token"]


def register_runtime_routes(app: FastAPI) -> None:
    prefix = "/private/runtime/runs/{run_id}"

    @app.post(prefix + "/context-project", include_in_schema=False)
    def project(run_id: str, body: ProjectInput, request: Request, response: Response, db: Annotated[Session, Depends(get_db)]):
        projection = project_context(db, run_id, _credential(request), **body.model_dump())
        response.headers["Cache-Control"] = "no-store"
        return {"id": projection.id, "context_json": projection.context_json, "context_digest": projection.context_digest,
                "operation": projection.operation, "business_purpose": projection.business_purpose,
                "generation": projection.generation, "profile_configuration_hash": projection.profile_configuration_hash,
                "expires_at": projection.expires_at.isoformat()}

    @app.post(prefix + "/model-authorizations", include_in_schema=False)
    def authorize(run_id: str, body: AuthorizeInput, request: Request, db: Annotated[Session, Depends(get_db)]):
        admission = authorize_model(db, run_id, _credential(request), **body.model_dump())
        return {"id": admission.id, "state": admission.state}

    @app.post(prefix + "/models/chat/completions", include_in_schema=False)
    async def forward(run_id: str, request: Request, db: Annotated[Session, Depends(get_db)]):
        token = _credential(request)
        # The body bound is checked while reading; do not trust Content-Length.
        parts, size = [], 0
        async for part in request.stream():
            size += len(part)
            if size > 262144:
                raise SafeError("RUNTIME_PAYLOAD_BUDGET", "模型请求超出本地大小限制", 413)
            parts.append(part)
        receipt = await run_in_threadpool(dispatch_model, db, run_id, token,
                                         request.headers.get("X-Model-Call", ""), b"".join(parts))
        if receipt["state"] == "received" and "body" in receipt:
            raw = receipt["body"]
            media = "application/json" if raw.lstrip().startswith(("{", "[")) else "text/event-stream"
            return Response(raw, status_code=200, media_type=media, headers={"Cache-Control": "no-store"})
        code = 502 if receipt["state"] == "unknown" else (receipt.get("status_code") or 409)
        return JSONResponse(receipt, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get(prefix + "/model-calls/{call_id}", include_in_schema=False)
    def receipt(run_id: str, call_id: str, request: Request, db: Annotated[Session, Depends(get_db)]):
        return call_receipt(db, run_id, _credential(request), call_id)
