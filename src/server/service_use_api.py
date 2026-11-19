"""Purpose policy controls. No endpoint issues a synthetic or enterprise proof."""
from typing import Literal
from fastapi import Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from .db import get_db
from .models import utc_now
from .service_models import ServiceRevision
from .service_use_models import ServiceSelection, ServiceSelectionPreview, ServiceUseCall, ServiceUsePin
from . import service_use as gate
from .service_api import _authority
from .services import (SafeError, current_session, has_permission, require_csrf, check_if_match,
    idempotency_record, complete_idempotency, audit)


class PreviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["select", "disable"]
    revision_id: str | None = None
    reason: str = Field(min_length=1, max_length=500)


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_id: str
    reason: str = Field(min_length=1, max_length=500)


def selection_view(db, purpose):
    row = db.get(ServiceSelection, purpose, populate_existing=True)
    revision = db.get(ServiceRevision, row.revision_id) if row and row.revision_id else None
    return {"purpose": purpose, "revision": row.revision if row else 0,
        "etag": f'"{row.revision if row else 0}"', "enabled": bool(row and row.enabled),
        "epoch": row.epoch if row else 0, "revision_id": row.revision_id if row else None,
        "profile_id": revision.profile_id if revision else None, "test_run_id": row.test_run_id if row else None,
        "binding_hash": row.binding_hash if row else None, "data_scope": "synthetic_only",
        "input_admission": "exact_private_synthetic_attestation_required", "enterprise_admission_state": "not_configured"}


def preview_view(row):
    return {"id": row.id, "purpose": row.purpose, "action": row.action, "revision_id": row.revision_id,
        "expires_at": row.expires_at.isoformat(), "consumed": row.consumed,
        "test_run_id": row.test_run_id, "binding_hash": row.binding_hash,
        "data_scope": "synthetic_only", "enterprise_admission_state": "not_configured"}


def register_service_use_routes(app):
    def auth(request, db, write=False):
        user, session = current_session(request, db, app.state.settings)
        if not has_permission(user, "system.configure"):
            raise SafeError("FORBIDDEN", "需要系统配置权限", 403)
        if write:
            require_csrf(request, session)
        return user, session

    def mutate(request, db, purpose, payload, *, applying=None):
        gate._purpose(purpose)
        actor, session = auth(request, db, True)
        import json
        receipt, replay = idempotency_record(db, actor, request, request.headers.get("Idempotency-Key", ""),
            json.dumps(payload, sort_keys=True).encode())
        if replay:
            _authority(request, db, actor, session)
            preview = db.get(ServiceSelectionPreview, receipt.resource_refs["selection_preview_id"])
            if not preview or preview.purpose != purpose:
                raise SafeError("NOT_FOUND", "原选用回执不存在", 404)
            db.commit()
            return selection_view(db, purpose) if applying else preview_view(preview)
        action = applying or payload["action"]
        preview = db.get(ServiceSelectionPreview, payload["preview_id"]) if applying else None
        revision_id = preview.revision_id if preview else payload.get("revision_id")
        # Every path that needs both locks uses Profile -> Selection -> Session/User.
        if action == "select":
            gate.selected_revision(db, purpose, revision_id, app.state.service_endpoints)
        row = db.get(ServiceSelection, purpose, populate_existing=True)
        expected = row.revision if row else 0
        check_if_match(request.headers.get("If-Match"), expected)
        if not row:
            try:
                with db.begin_nested():
                    row = ServiceSelection(purpose=purpose, revision=0, enabled=False, epoch=0)
                    db.add(row)
                    db.flush()
            except IntegrityError:
                raise SafeError("PRECONDITION_FAILED", "用途选用已发生变化", 412) from None
        changed = db.execute(update(ServiceSelection).where(ServiceSelection.purpose == purpose,
            ServiceSelection.revision == expected).values(revision=ServiceSelection.revision)
            .execution_options(synchronize_session=False))
        if changed.rowcount != 1:
            raise SafeError("PRECONDITION_FAILED", "用途选用已发生变化", 412)
        db.refresh(row)
        _authority(request, db, actor, session)
        if applying:
            gate.apply_selection(db, row, actor.id, action, payload["preview_id"], app.state.service_endpoints)
        else:
            preview = gate.preview_selection(db, actor.id, purpose, action, revision_id, expected, app.state.service_endpoints)
        audit(db, actor.id, "service.selection." + (action if applying else "preview"), "service_selection", purpose,
            scope="synthetic_only", reason=payload["reason"])
        complete_idempotency(db, receipt, status_code=200, refs={"selection_preview_id": preview.id})
        db.commit()
        return selection_view(db, purpose) if applying else preview_view(preview)

    @app.get("/admin/service-selections")
    def selections(request: Request, db: Session = Depends(get_db)):
        auth(request, db)
        return {"items": [selection_view(db, p) for p in gate.PURPOSES], "purposes": list(gate.PURPOSES),
            "data_scope": "synthetic_only"}

    @app.post("/admin/service-selections/{purpose}/previews")
    def preview(purpose: str, body: PreviewInput, request: Request, db: Session = Depends(get_db)):
        return mutate(request, db, purpose, body.model_dump())

    @app.post("/admin/service-selections/{purpose}/select")
    def select_service(purpose: str, body: ApplyInput, request: Request, response: Response, db: Session = Depends(get_db)):
        result = mutate(request, db, purpose, body.model_dump(), applying="select")
        response.headers["ETag"] = result["etag"]
        return result

    @app.post("/admin/service-selections/{purpose}/disable")
    def disable(purpose: str, body: ApplyInput, request: Request, response: Response, db: Session = Depends(get_db)):
        result = mutate(request, db, purpose, body.model_dump(), applying="disable")
        response.headers["ETag"] = result["etag"]
        return result

    @app.get("/admin/service-use-calls")
    def calls(request: Request, db: Session = Depends(get_db)):
        auth(request, db)
        rows = db.execute(select(ServiceUseCall, ServiceUsePin.purpose, ServiceUsePin.revision_id)
            .join(ServiceUsePin, ServiceUsePin.id == ServiceUseCall.pin_id)
            .order_by(ServiceUseCall.created_at.desc()).limit(100)).all()
        return {"items": [{"id": call.id, "pin_id": call.pin_id, "purpose": purpose, "revision_id": revision,
            "ordinal": call.ordinal, "method": call.method, "state": call.state, "safe_error": call.safe_error,
            "payload_hash": call.payload_hash, "input_hash": call.input_hash, "response_status": call.response_status,
            "response_hash": call.response_hash, "usage": call.usage, "settled_cost": call.settled_cost}
            for call, purpose, revision in rows], "data_scope": "synthetic_only"}
