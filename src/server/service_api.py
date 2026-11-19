"""SC service configuration and fixed synthetic self-test workflow."""
from __future__ import annotations
import json
from typing import Annotated, Literal
from fastapi import Depends, FastAPI, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from .db import get_db
from .models import AuthSession, User
from .provider_transport import ProviderError
from .service_models import ServiceProfile, ServiceRevision, ServiceTestRun, ServiceRequestCall, ServiceChangePreview
from . import service_admission as gate
from .services import (SafeError, current_session, has_permission, require_csrf, check_if_match,
                       idempotency_record, complete_idempotency, audit)


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Create(Input):
    kind: Literal["generation", "embedding", "parsing"]
    label: str = Field(min_length=1, max_length=120)


class Revision(Input):
    endpoint_ref: str = Field(min_length=1, max_length=80)
    adapter: str = Field(min_length=1, max_length=40)
    model: str = Field(min_length=1, max_length=100)


class Test(Input):
    fixture_id: Literal["service-synthetic-v1"]


class Preview(Input):
    action: Literal["activate", "disable"]
    revision_id: str | None = None
    reason: str = Field(min_length=1, max_length=500)


class Change(Input):
    preview_id: str
    reason: str = Field(min_length=1, max_length=500)


class Resume(Input):
    reason: str = Field(min_length=1, max_length=500)


def _authority(request, db, actor, session):
    # Same lock order as current account-stop handler: session then user.
    db.scalar(select(AuthSession).where(AuthSession.id == session.id).with_for_update(read=True))
    db.scalar(select(User).where(User.id == actor.id).with_for_update(read=True))
    user, _ = current_session(request, db, request.app.state.settings, persist_heartbeat=False)
    if user.id != actor.id or not has_permission(user, "system.configure"):
        raise SafeError("FORBIDDEN", "需要系统配置权限", 403)


def _profile(db, ident):
    value = db.get(ServiceProfile, ident)
    if not value:
        raise SafeError("NOT_FOUND", "服务配置不存在", 404)
    return value


def _revision_view(value):
    return {"id": value.id, "profile_id": value.profile_id, "endpoint_ref": value.endpoint_ref,
            "adapter": value.adapter, "model": value.model, "configuration_hash": value.configuration_hash,
            "data_scope": "synthetic_only", "enterprise_admission_state": "not_configured"}


def _test_view(value):
    return {"id": value.id, "revision_id": value.revision_id, "fixture_id": value.fixture_id,
            "state": value.state, "safe_error": value.safe_error, "result": value.result,
            "lease_until": value.lease_until.isoformat() if value.lease_until else None,
            "checkpoint": value.checkpoint, "valid_until": value.valid_until.isoformat() if value.valid_until else None,
            "data_scope": "synthetic_only"}


def profile_view(db, value, endpoints):
    revisions = db.scalars(select(ServiceRevision).where(ServiceRevision.profile_id == value.id).order_by(ServiceRevision.created_at)).all()
    tests = db.scalars(select(ServiceTestRun).join(ServiceRevision, ServiceRevision.id == ServiceTestRun.revision_id)
        .where(ServiceRevision.profile_id == value.id)).all()
    current = []
    for test in tests:
        try:
            if test.state == "succeeded" and gate.future(test.valid_until) and test.binding_hash == gate.binding(db.get(ServiceRevision, test.revision_id), endpoints):
                current.append(test.id)
        except ProviderError:
            pass
    credential_state = "not_configured"
    if revisions:
        latest = db.get(ServiceRevision, value.active_revision_id) if value.active_revision_id else revisions[-1]
        try:
            gate.binding(latest, endpoints)
            credential_state = "configured"
        except ProviderError:
            pass
    return {"id": value.id, "kind": value.kind, "label": value.label, "revision": value.revision,
            "etag": f'"{value.revision}"', "enabled": value.enabled, "active_revision_id": value.active_revision_id,
            "data_admission_state": "synthetic_only", "enterprise_admission_state": "not_configured",
            "configuration_state": "configured" if revisions else "empty", "health": {"state": "unknown", "observed_at": None},
            "credential_state": credential_state, "usage_and_budget": {"scope": "synthetic_only", "max_requests_per_test": 12,
                "reported_usage": [x.result["usage"] for x in tests if x.result.get("usage") is not None], "settled_cost": None},
            "current_test_ids": current, "revisions": [_revision_view(x) for x in revisions],
            "tests": [_test_view(x) for x in tests], "business_dispatch_enabled": False,
            "business_dispatch_scope": "exact_attested_synthetic_only", "purpose_selection_required": True}


def register_service_routes(app: FastAPI):
    # Deliberately empty. Trusted local harness supplies LocalEndpoint entries;
    # the HTTP API cannot add an IP, URL, secret or enterprise permission.
    app.state.service_endpoints = gate.load_local_endpoints()

    def auth(request, db, write=False):
        user, session = current_session(request, db, app.state.settings)
        if not has_permission(user, "system.configure"):
            raise SafeError("FORBIDDEN", "需要系统配置权限", 403)
        if write:
            require_csrf(request, session)
        return user, session

    def mutation(request, db, payload, create, model, view, profile=None, *, status=200):
        user, session = auth(request, db, True)
        receipt, replay = idempotency_record(db, user, request, request.headers.get("Idempotency-Key", ""),
            json.dumps(payload, sort_keys=True).encode())
        if not replay and profile is not None:
            expected = profile.revision
            check_if_match(request.headers.get("If-Match"), expected)
            changed = db.execute(update(ServiceProfile).where(ServiceProfile.id == profile.id, ServiceProfile.revision == expected)
                .values(revision=ServiceProfile.revision).execution_options(synchronize_session=False))
            if changed.rowcount != 1:
                raise SafeError("PRECONDITION_FAILED", "服务已发生变化", 412)
            db.refresh(profile)
        _authority(request, db, user, session)
        row = db.get(model, receipt.resource_refs["service_resource_id"]) if replay else create(user)
        db.flush()
        if not replay:
            audit(db, user.id, "service." + model.__tablename__, model.__tablename__, row.id,
                  scope="synthetic_only", reason=payload.get("reason"))
            complete_idempotency(db, receipt, status_code=status, refs={"service_resource_id": row.id})
        db.commit()
        return row, view(row), replay

    @app.exception_handler(ProviderError)
    async def provider_error(request, exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({"code": exc.code, "message": "服务自检未满足准入条件"}, status_code=409)

    @app.get("/admin/service-profiles")
    def profiles(request: Request, db: Session = Depends(get_db)):
        auth(request, db)
        return {"items": [profile_view(db, x, app.state.service_endpoints) for x in db.scalars(select(ServiceProfile)).all()],
                "endpoint_refs": sorted(app.state.service_endpoints), "data_scope": "synthetic_only"}

    @app.get("/admin/service-profiles/{profile_id}")
    def profile(profile_id: str, request: Request, response: Response, db: Session = Depends(get_db)):
        auth(request, db)
        row = _profile(db, profile_id)
        response.headers["ETag"] = f'"{row.revision}"'
        return profile_view(db, row, app.state.service_endpoints)

    @app.post("/admin/service-profiles", status_code=201)
    def create(body: Create, request: Request, response: Response, db: Session = Depends(get_db)):
        def make(user):
            row = ServiceProfile(**body.model_dump(), created_by=user.id)
            db.add(row)
            return row
        row, result, _ = mutation(request, db, body.model_dump(), make, ServiceProfile,
            lambda x: profile_view(db, x, app.state.service_endpoints), status=201)
        response.headers["ETag"] = f'"{row.revision}"'
        return result

    @app.post("/admin/service-profiles/{profile_id}/revisions", status_code=201)
    def revision(profile_id: str, body: Revision, request: Request, db: Session = Depends(get_db)):
        auth(request, db, True)
        profile = _profile(db, profile_id)
        return mutation(request, db, body.model_dump(), lambda user: gate.create_revision(db, profile, **body.model_dump(), endpoints=app.state.service_endpoints),
            ServiceRevision, _revision_view, profile, status=201)[1]

    @app.get("/admin/service-profiles/{profile_id}/revisions/{revision_id}")
    def revision_detail(profile_id: str, revision_id: str, request: Request, db: Session = Depends(get_db)):
        auth(request, db)
        return _revision_view(gate.revision_for(db, profile_id, revision_id))

    @app.post("/admin/service-profiles/{profile_id}/revisions/{revision_id}/test-runs", status_code=202)
    def test(profile_id: str, revision_id: str, body: Test, request: Request, db: Session = Depends(get_db)):
        user, session = auth(request, db, True)
        revision = gate.revision_for(db, profile_id, revision_id)
        row, _, replay = mutation(request, db, body.model_dump(), lambda actor: gate.new_test(db, revision, actor.id, app.state.service_endpoints),
                          ServiceTestRun, _test_view, status=202)
        # Replay returns the same record; terminal/unknown/running is never resent.
        if not replay or row.state == "pending" and row.attempt == 0:
            row = gate.run_test(db, row.id, app.state.service_endpoints, authority_check=lambda: _authority(request, db, user, session))
        _authority(request, db, user, session)
        return _test_view(row)

    @app.get("/admin/service-profiles/{profile_id}/revisions/{revision_id}/test-runs/{test_id}")
    def test_detail(profile_id: str, revision_id: str, test_id: str, request: Request, db: Session = Depends(get_db)):
        actor, _ = auth(request, db)
        gate.revision_for(db, profile_id, revision_id)
        row = db.get(ServiceTestRun, test_id)
        if not row or row.revision_id != revision_id:
            raise SafeError("NOT_FOUND", "服务测试不存在", 404)
        result = _test_view(row)
        result["operator_can_resume"] = row.actor_id == actor.id
        result["calls"] = [{"id": x.id, "ordinal": x.ordinal, "method": x.method, "purpose": x.purpose,
            "state": x.state, "response_status": x.response_status, "safe_error": x.safe_error}
            for x in db.scalars(select(ServiceRequestCall).where(ServiceRequestCall.test_run_id == row.id).order_by(ServiceRequestCall.ordinal)).all()]
        return result

    @app.post("/admin/service-profiles/{profile_id}/revisions/{revision_id}/test-runs/{test_id}/resume", status_code=202)
    def resume(profile_id: str, revision_id: str, test_id: str, body: Resume, request: Request, db: Session = Depends(get_db)):
        user, session = auth(request, db, True)
        gate.revision_for(db, profile_id, revision_id)
        profile = _profile(db, profile_id)
        def prepare(actor):
            row = db.get(ServiceTestRun, test_id)
            if not row or row.revision_id != revision_id or row.actor_id != actor.id:
                raise SafeError("NOT_FOUND", "服务测试不存在", 404)
            gate.prepare_resume(db, row)
            return row
        row, _, replay = mutation(request, db, body.model_dump(), prepare, ServiceTestRun, _test_view, profile)
        if not replay:
            row = gate.run_test(db, row.id, app.state.service_endpoints, authority_check=lambda: _authority(request, db, user, session))
        _authority(request, db, user, session)
        return _test_view(row)

    @app.post("/admin/service-profiles/{profile_id}/change-preview")
    def preview(profile_id: str, body: Preview, request: Request, db: Session = Depends(get_db)):
        auth(request, db, True)
        profile = _profile(db, profile_id)
        row, _, _ = mutation(request, db, body.model_dump(), lambda user: gate.preview_change(db, profile, user.id, body.action, body.revision_id, app.state.service_endpoints),
            ServiceChangePreview, lambda x: {}, profile)
        return {"preview_id": row.id, "action": row.action, "allowed": True, "profile_etag": f'"{row.profile_revision}"',
                "expires_at": row.expires_at.isoformat(), "impact": "synthetic_self_test_only; business dispatch unchanged"}

    def change(action, profile_id, body, request, response, db):
        auth(request, db, True)
        profile = _profile(db, profile_id)
        def apply(user):
            gate.apply_change(db, profile, user.id, action, body.preview_id, app.state.service_endpoints)
            return profile
        row, result, _ = mutation(request, db, body.model_dump(), apply, ServiceProfile,
            lambda x: profile_view(db, x, app.state.service_endpoints), profile)
        response.headers["ETag"] = f'"{row.revision}"'
        return result

    @app.post("/admin/service-profiles/{profile_id}/activate")
    def activate(profile_id: str, body: Change, request: Request, response: Response, db: Session = Depends(get_db)):
        return change("activate", profile_id, body, request, response, db)

    @app.post("/admin/service-profiles/{profile_id}/disable")
    def disable(profile_id: str, body: Change, request: Request, response: Response, db: Session = Depends(get_db)):
        return change("disable", profile_id, body, request, response, db)
