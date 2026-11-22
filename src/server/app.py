"""FastAPI application for the device-operation knowledge query MVP."""

from __future__ import annotations

import json
import hashlib
from contextlib import asynccontextmanager
from datetime import timezone
from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, File, Form, Header, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, settings
from .db import build_engine, get_db
from .migration_safety import require_current_schema
from .models import (
    AuthorizationGrant,
    ApiIdempotencyRecord,
    BusinessApproval,
    ContentBlock,
    ChunkSet,
    Document,
    DocumentVersion,
    FileObject,
    IndexGeneration,
    IndexSource,
    KnowledgeBase,
    ParseRun,
    QueryTask,
    Role,
    AuthSession,
    TaskEvidence,
    TaskArtifact,
    TaskAttempt,
    TaskOutbox,
    TaskStep,
    TaskShare,
    User,
    utc_now,
)
from .schemas import (
    BlockReviewInput,
    BootstrapInput,
    BusinessApprovalInput,
    CancelInput,
    CorrectionInput,
    CredentialInput,
    GrantInput,
    IndexInput,
    ParseInput,
    KnowledgeBaseInput,
    NewDocumentInput,
    DocumentDeleteInput,
    LoginInput,
    PublishInput,
    ProviderCallInput,
    QueryInput,
    RetryInput,
    RolesInput,
    ShareInput,
    UserCreateInput,
    UserPatchInput,
    VersionPatchInput,
    WithdrawInput,
    BackupDrillInput,
)
from .services import (
    ROLE_PERMISSIONS,
    SafeError,
    _utc,
    approval_allows_grant,
    audit,
    build_query_result,
    can_access_document,
    can_manage_kb,
    document_creation_scopes,
    require_document_creation_scope,
    fence_management_authority,
    can_query_kb,
    can_read_task,
    check_if_match,
    complete_idempotency,
    create_index,
    create_session,
    current_session,
    document_view,
    document_has_formal_version,
    etag,
    evidence_view,
    file_path,
    has_permission,
    idempotency_record,
    login,
    logout,
    make_parse_run,
    process_query_task,
    save_upload,
    seed_reference_data,
    selected_parse,
    task_view,
    user_view,
    version_is_formal,
    version_view,
)
from .security import hash_password
from .operations import invalidate_dependents, logical_delete_document, record_provider_call, sanitized_provider_view, sanitized_task_view


STATIC_DIR = Path(__file__).resolve().parent / "static"
WEB_DIST = Path(__file__).resolve().parents[1] / "web/dist"


def _user_or_401(request: Request, db: Session) -> tuple[User, Any]:
    return current_session(request, db, request.app.state.settings)


def _require_permission(user: User, permission: str) -> None:
    if not has_permission(user, permission):
        raise SafeError("FORBIDDEN", "当前账号没有执行此操作的能力", 403)


def _require_manage_document(db: Session, user: User, document: Document, *, request: Request | None = None) -> None:
    if request is not None:
        user = fence_management_authority(db, user, document.knowledge_base_id, document_id=document.id, request=request)
    if not can_access_document(db, user, document, "knowledge.manage"):
        raise SafeError("FORBIDDEN", "当前账号没有管理该资料范围的权限", 403)


def _claim_managed_document(db: Session, user: User, document: Document) -> None:
    """Serialize legacy maintenance with the parent tombstone before child reads."""
    with db.no_autoflush:
        claimed = db.execute(update(Document).where(Document.id == document.id, Document.deleted_at.is_(None))
            .values(revision=Document.revision).execution_options(synchronize_session=False))
    if claimed.rowcount != 1:
        raise SafeError("NOT_FOUND", "资源不存在", 404)
    db.refresh(document)
    _require_manage_document(db, user, document)


def _require_document(db: Session, user: User, document_id: str, *, operation: str = "document.read", manage: bool = False) -> Document:
    document = db.get(Document, document_id)
    if not document:
        raise SafeError("NOT_FOUND", "资源不存在", 404)
    if manage:
        _require_manage_document(db, user, document)
    elif not can_access_document(db, user, document, operation):
        raise SafeError("NOT_FOUND", "资源不存在", 404)
    return document


def _version_or_404(db: Session, document: Document, version_id: str) -> DocumentVersion:
    version = db.get(DocumentVersion, version_id)
    if not version or version.document_id != document.id:
        raise SafeError("NOT_FOUND", "资源不存在", 404)
    return version


def _json_response(data: Any, response: Response, revision: int | None = None) -> Any:
    if revision is not None:
        response.headers["ETag"] = etag(revision)
    return data


def create_app(config: Settings | None = None) -> FastAPI:
    selected_settings = config or settings

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        require_current_schema(application.state.engine)
        yield

    app = FastAPI(title=selected_settings.app_name, version="0.1.0", lifespan=lifespan)
    app.state.settings = selected_settings
    app.state.engine = build_engine(app.state.settings.database_url)
    app.state.session_factory = sessionmaker(bind=app.state.engine, autoflush=False, expire_on_commit=False, class_=Session, info={"settings": config})

    @app.middleware("http")
    async def api_prefix_compatibility(request: Request, call_next):
        # The design document omits the documented /api/v1 prefix in tables;
        # accepting both forms keeps local links simple while preserving the
        # production contract.
        if request.scope["path"].startswith("/api/v1/"):
            request.scope["path"] = request.scope["path"][len("/api/v1"):]
            request.scope["root_path"] = "/api/v1"
        response = await call_next(request)
        if any(request.scope["path"].startswith(prefix) for prefix in ("/documents", "/tasks", "/knowledge", "/parse-runs", "/admin", "/auth", "/me", "/capabilities", "/conversations", "/workspace-data", "/operation-receipts", "/private/runtime")):
            response.headers.setdefault("Cache-Control", "private, no-store")
        return response

    if STATIC_DIR.exists():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")
    if (WEB_DIST / "assets").is_dir():
        app.mount("/workspace/assets", StaticFiles(directory=WEB_DIST / "assets"), name="workspace-assets")

    @app.get("/workspace", include_in_schema=False)
    def workspace_entry():
        return RedirectResponse("/workspace/query")

    @app.get("/workspace/{page}", include_in_schema=False)
    def workspace_page(page: str):
        if page not in {"query", "manuals", "manage", "access-review", "services", "operations", "accounts"}:
            return HTMLResponse("<h1>页面不存在</h1>", status_code=404)
        index = WEB_DIST / "index.html"
        if not index.is_file():
            return HTMLResponse("<h1>网页暂未就绪</h1><p>请联系管理员。</p>", status_code=503,
                                headers={"Cache-Control": "private, no-store"})
        return FileResponse(index, media_type="text/html", headers={"Cache-Control": "private, no-store"})

    @app.exception_handler(SafeError)
    async def safe_error_handler(_: Request, exc: SafeError):
        from fastapi.responses import JSONResponse

        return JSONResponse(status_code=exc.status_code, content=exc.detail, headers=exc.headers)

    @app.get("/access-review", response_class=HTMLResponse)
    def access_review_workspace() -> HTMLResponse:
        page = STATIC_DIR / "approvals.html"
        if not page.exists():
            return HTMLResponse("<h1>审批工作区暂不可用</h1>", status_code=503)
        return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "private, no-store"})

    @app.get("/manage", response_class=HTMLResponse)
    def governance_workspace() -> HTMLResponse:
        page = STATIC_DIR / "governance.html"
        if not page.exists():
            return HTMLResponse("<h1>管理工作区暂不可用</h1>", status_code=503)
        return HTMLResponse(page.read_text(encoding="utf-8"), headers={"Cache-Control": "private, no-store"})

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "device-query", "mode": "local-reference"}

    @app.get("/", response_class=HTMLResponse)
    def home() -> HTMLResponse:
        index = STATIC_DIR / "index.html"
        if not index.exists():
            return HTMLResponse("<h1>数据中心</h1><p>网页资源尚未构建。</p>")
        return HTMLResponse(index.read_text(encoding="utf-8"))

    @app.post("/auth/bootstrap", status_code=201)
    def bootstrap(payload: BootstrapInput, response: Response, x_bootstrap_token: Annotated[str | None, Header()] = None,
                  db: Session = Depends(get_db)) -> dict[str, Any]:
        config: Settings = app.state.settings
        if not config.bootstrap_token or x_bootstrap_token != config.bootstrap_token:
            raise SafeError("BOOTSTRAP_DISABLED", "部署初始化凭据无效", 403)
        if db.scalar(select(User.id)) is not None:
            raise SafeError("BOOTSTRAP_COMPLETED", "首个管理员已经初始化", 409)
        user = User(display_name=payload.display_name, username=payload.username, status="active", credential_state="active",
                    credential_hash=hash_password(payload.password), credential_revision=1, roles=["system_admin"])
        db.add(user)
        db.flush()
        audit(db, user.id, "auth.bootstrap", "user", user.id)
        db.commit()
        session = create_session(db, user, response, config)
        return {"user": user_view(user), **session}

    @app.post("/auth/login")
    def auth_login(payload: LoginInput, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
        user, csrf = login(db, payload.username, payload.password, response, app.state.settings)
        return {"user": user_view(user), **csrf}

    @app.get("/me")
    def me(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        user, _ = _user_or_401(request, db)
        return {"user": user_view(user), "capabilities": sorted({item for role in user.roles or [] for item in ROLE_PERMISSIONS.get(role, set())})}

    @app.get("/capabilities")
    def capabilities(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        user, _ = _user_or_401(request, db)
        return {"roles": user.roles or [], "permissions": sorted({item for role in user.roles or [] for item in ROLE_PERMISSIONS.get(role, set())})}

    @app.post("/auth/logout", status_code=204)
    def auth_logout(request: Request, response: Response, db: Session = Depends(get_db)) -> Response:
        user, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        logout(db, user, session, response, request.app.state.settings)
        response.status_code = 204
        return response

    @app.get("/admin/users")
    def list_users(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        user, _ = _user_or_401(request, db)
        _require_permission(user, "user.manage")
        return {"items": [user_view(item) for item in db.scalars(select(User).order_by(User.created_at)).all()]}

    @app.post("/admin/users", status_code=201)
    def create_user(payload: UserCreateInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "user.manage")
        from .services import require_csrf

        require_csrf(request, session)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if replayed:
            return {"user": user_view(db.get(User, record.resource_refs["user_id"]))}
        if db.scalar(select(User).where(User.username == payload.username)):
            raise SafeError("USERNAME_EXISTS", "账号已存在", 409)
        roles = [role for role in payload.roles if role in ROLE_PERMISSIONS]
        user = User(display_name=payload.display_name, username=payload.username, status="disabled", credential_state="pending", roles=roles or ["employee"])
        db.add(user)
        db.flush()
        audit(db, actor.id, "user.created", "user", user.id)
        complete_idempotency(db, record, status_code=201, refs={"user_id": user.id})
        db.commit()
        return {"user": user_view(user)}

    @app.patch("/admin/users/{user_id}")
    def patch_user(user_id: str, payload: UserPatchInput, request: Request, response: Response, if_match: Annotated[str | None, Header()] = None,
                   db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "user.manage")
        from .services import require_csrf

        require_csrf(request, session)
        target = db.get(User, user_id)
        if not target:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if not replayed:
            check_if_match(if_match, target.authz_revision)
        if not replayed:
            if payload.display_name is not None:
                target.display_name = payload.display_name
            if payload.status is not None:
                target.status = payload.status
                target.authz_revision += 1
                if payload.status == "disabled":
                    for auth in db.scalars(select(AuthSession).where(AuthSession.user_id == target.id)).all():
                        auth.revoked_at = utc_now()
            target.authz_revision += 1
            audit(db, actor.id, "user.updated", "user", target.id)
            db.flush()
            complete_idempotency(db, record, status_code=200, refs={"user_id": target.id})
            db.commit()
        else:
            target = db.get(User, record.resource_refs["user_id"])
        return _json_response({"user": user_view(target)}, response, target.authz_revision)

    @app.post("/admin/users/{user_id}/credential-activation", status_code=202)
    def activate_credential(user_id: str, payload: CredentialInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "user.manage")
        from .services import require_csrf

        require_csrf(request, session)
        target = db.get(User, user_id)
        if not target:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if not replayed:
            target.credential_hash = hash_password(payload.password)
            target.credential_state = "active"
            target.credential_revision += 1
            target.status = "active"
            target.authz_revision += 1
            audit(db, actor.id, "user.credential_activated", "user", target.id, reason=payload.reason)
            db.flush()
            complete_idempotency(db, record, status_code=202, refs={"user_id": target.id})
            db.commit()
        return {"status": "issued", "user_id": target.id}

    @app.post("/admin/users/{user_id}/credential-reset", status_code=202)
    def reset_credential(user_id: str, payload: CredentialInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        return activate_credential(user_id, payload, request, db, idempotency_key)

    @app.put("/admin/users/{user_id}/roles")
    def replace_roles(user_id: str, payload: RolesInput, request: Request, response: Response, if_match: Annotated[str | None, Header()] = None,
                      db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "access.manage")
        from .services import require_csrf

        require_csrf(request, session)
        target = db.get(User, user_id)
        if not target:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if not replayed:
            check_if_match(if_match, target.authz_revision)
        if not replayed:
            invalid = [role for role in payload.role_codes if role not in ROLE_PERMISSIONS]
            if invalid:
                raise SafeError("INVALID_ROLE", "角色不存在", 400)
            target.roles = payload.role_codes
            target.authz_revision += 1
            audit(db, actor.id, "access.roles_replaced", "user", target.id)
            db.flush()
            complete_idempotency(db, record, status_code=200, refs={"user_id": target.id})
            db.commit()
        return _json_response({"user": user_view(target)}, response, target.authz_revision)

    @app.get("/admin/grants")
    def list_grants(request: Request, user_id: str | None = None, limit: int = Query(50, ge=1, le=100),
                    offset: int = Query(0, ge=0), db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        _require_permission(actor, "access.manage")
        from .governance import approval_is_manageable
        from .services import _utc
        statement = select(AuthorizationGrant).order_by(AuthorizationGrant.id)
        if user_id is not None:
            statement = statement.where(AuthorizationGrant.subject_user_id == user_id)
        rows = db.scalars(statement.offset(offset).limit(limit + 1)).all()
        items = []
        for row in rows[:limit]:
            approval = db.get(BusinessApproval, row.approval_id) if row.approval_id else None
            if not approval or not approval_is_manageable(db, actor, approval):
                continue
            target = db.get(User, row.subject_user_id)
            items.append({"id": row.id, "user_id": row.subject_user_id, "approval_id": row.approval_id,
                "knowledge_base_id": row.knowledge_base_id, "document_id": row.document_id,
                "operation": row.operation, "level": row.level, "category": row.category, "need_class": row.need_class,
                "valid_until": row.valid_until.isoformat() if row.valid_until else None,
                "effective": bool(target and target.status == "active" and row.revoked_at is None
                    and (row.valid_from is None or _utc(row.valid_from) <= utc_now())
                    and (row.valid_until is None or _utc(row.valid_until) > utc_now())
                    and approval_allows_grant(approval, row, utc_now()))})
        return {"items": items, "next_offset": offset + limit if len(rows) > limit else None}

    @app.post("/admin/grants", status_code=201)
    def create_grant(payload: GrantInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "access.manage")
        from .services import require_csrf

        require_csrf(request, session)
        target = db.get(User, payload.user_id)
        if not target:
            raise SafeError("NOT_FOUND", "用户不存在", 404)
        if not payload.knowledge_base_id and not payload.document_id:
            raise SafeError("GRANT_SCOPE_REQUIRED", "授权必须指定知识库或文档范围", 400)
        if not payload.approval_id:
            raise SafeError("APPROVAL_REQUIRED", "授权必须引用独立的业务审批记录", 409)
        approval = db.get(BusinessApproval, payload.approval_id)
        proposed = AuthorizationGrant(subject_user_id=target.id, knowledge_base_id=payload.knowledge_base_id, document_id=payload.document_id,
                                      operation=payload.operation, level=payload.level, category=payload.category, need_class=payload.need_class, valid_until=payload.valid_until)
        if not approval_allows_grant(approval, proposed, utc_now()):
            raise SafeError("APPROVAL_INVALID", "业务审批记录不可用于该用户或尚未通过", 409)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if replayed:
            grant = db.get(AuthorizationGrant, record.resource_refs["grant_id"])
            return {"id": grant.id, "user_id": grant.subject_user_id, "operation": grant.operation, "knowledge_base_id": grant.knowledge_base_id, "document_id": grant.document_id, "valid_until": grant.valid_until.isoformat() if grant.valid_until else None}
        grant = AuthorizationGrant(subject_user_id=target.id, knowledge_base_id=payload.knowledge_base_id, document_id=payload.document_id, operation=payload.operation, level=payload.level, category=payload.category, need_class=payload.need_class, valid_until=payload.valid_until, approval_id=approval.id, granted_by=actor.id)
        db.add(grant)
        target.authz_revision += 1
        if payload.knowledge_base_id:
            kb = db.get(KnowledgeBase, payload.knowledge_base_id)
            if kb:
                kb.acl_revision += 1
        audit(db, actor.id, "access.grant_created", "authorization_grant", grant.id, operation=payload.operation)
        complete_idempotency(db, record, status_code=201, refs={"grant_id": grant.id})
        db.commit()
        return {"id": grant.id, "user_id": target.id, "operation": grant.operation, "knowledge_base_id": grant.knowledge_base_id, "document_id": grant.document_id, "valid_until": grant.valid_until.isoformat() if grant.valid_until else None}

    @app.post("/admin/business-approvals", status_code=201)
    def create_approval(payload: BusinessApprovalInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "access.manage")
        from .services import require_csrf

        require_csrf(request, session)
        if not db.get(User, payload.applicant):
            raise SafeError("NOT_FOUND", "用户不存在", 404)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if replayed:
            approval = db.get(BusinessApproval, record.resource_refs["approval_id"])
            return {"id": approval.id, "status": approval.status, "applicant": approval.applicant}
        from .governance import register_approval
        approval = register_approval(db, actor, payload)
        audit(db, actor.id, "access.approval_created", "business_approval", approval.id)
        complete_idempotency(db, record, status_code=201, refs={"approval_id": approval.id})
        db.commit()
        return {"id": approval.id, "status": approval.status, "applicant": approval.applicant}

    @app.post("/admin/business-approvals/{approval_id}/approve")
    def approve_approval(approval_id: str, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "access.manage")
        from .services import require_csrf

        require_csrf(request, session)
        approval = db.get(BusinessApproval, approval_id)
        if not approval:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        raise SafeError("EXTERNAL_APPROVAL_REQUIRED", "必须登记并核查真实业务批准；管理员点击不能生成业务批准", 409)

    @app.post("/admin/knowledge-bases", status_code=201)
    def create_kb(payload: KnowledgeBaseInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        if not (has_permission(actor, "system.configure") or has_permission(actor, "knowledge.manage")):
            raise SafeError("FORBIDDEN", "当前账号没有创建知识库的权限", 403)
        from .services import require_csrf

        require_csrf(request, session)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if replayed:
            kb = db.get(KnowledgeBase, record.resource_refs["knowledge_base_id"])
            return {"id": kb.id, "name": kb.name, "status": kb.status, "revision": kb.content_revision}
        if db.scalar(select(KnowledgeBase).where(KnowledgeBase.name == payload.name)):
            raise SafeError("NAME_EXISTS", "知识库名称已存在", 409)
        kb = KnowledgeBase(name=payload.name)
        db.add(kb)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            raise SafeError("NAME_EXISTS", "知识库名称已存在", 409)
        audit(db, actor.id, "knowledge_base.created", "knowledge_base", kb.id)
        complete_idempotency(db, record, status_code=201, refs={"knowledge_base_id": kb.id})
        db.commit()
        return {"id": kb.id, "name": kb.name, "status": kb.status, "revision": kb.content_revision}

    @app.get("/admin/knowledge-bases/registrations")
    def own_kb_registrations(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        if not (has_permission(actor, "system.configure") or has_permission(actor, "knowledge.manage")):
            raise SafeError("FORBIDDEN", "当前账号没有知识库登记能力", 403)
        records = db.scalars(select(ApiIdempotencyRecord).where(ApiIdempotencyRecord.actor_user_id == actor.id,
            ApiIdempotencyRecord.method == "POST", ApiIdempotencyRecord.status == "completed",
            ApiIdempotencyRecord.request_path.in_(["/admin/knowledge-bases", "/api/v1/admin/knowledge-bases"]))).all()
        rows, seen = [], set()
        for record in records:
            identity = record.resource_refs.get("knowledge_base_id")
            if identity in seen:
                continue
            kb = db.get(KnowledgeBase, identity) if identity else None
            if not kb:
                continue
            seen.add(identity)
            rows.append({"id": kb.id, "name": kb.name, "status": kb.status, "revision": kb.content_revision,
                "access_state": "manageable" if document_creation_scopes(db, actor, kb.id) else "awaiting_authorization"})
        return {"items": rows}

    @app.get("/knowledge-bases/{kb_id}/document-creation-options")
    def creation_options(kb_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        scopes = document_creation_scopes(db, actor, kb_id)
        if not scopes and not can_manage_kb(db, actor, kb_id):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return {"knowledge_base_id": kb_id, "can_create": bool(scopes), "management_scopes": scopes}

    @app.get("/knowledge-bases")
    def list_kbs(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        user, _ = _user_or_401(request, db)
        rows = []
        for kb in db.scalars(select(KnowledgeBase).where(KnowledgeBase.status == "active")).all():
            docs = db.scalars(select(Document).where(Document.knowledge_base_id == kb.id, Document.deleted_at.is_(None))).all()
            if can_manage_kb(db, user, kb.id) or any(can_access_document(db, user, doc) for doc in docs):
                rows.append({"id": kb.id, "name": kb.name, "status": kb.status, "content_revision": kb.content_revision})
        return {"items": rows}

    @app.post("/knowledge-bases/{kb_id}/documents", status_code=201)
    async def upload_document(kb_id: str, request: Request, file: UploadFile = File(...), title: str | None = None,
                              category: str | None = None, model_scope: str | None = None,
                              db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None,
                              metadata: str | None = Form(None)) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        kb = db.get(KnowledgeBase, kb_id)
        if not kb:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        scopes = document_creation_scopes(db, actor, kb_id)
        if not scopes:
            raise SafeError("FORBIDDEN", "当前账号没有管理该知识库的权限", 403)
        if metadata is not None:
            if any(value is not None for value in (title, category, model_scope)):
                raise SafeError("AMBIGUOUS_UPLOAD_METADATA", "不能同时提交metadata和旧查询参数", 422)
            try:
                details = NewDocumentInput.model_validate_json(metadata)
            except ValueError:
                raise SafeError("INVALID_UPLOAD_METADATA", "资料登记字段不合法；不接收授权或状态字段", 422)
        else:
            details = NewDocumentInput(title=title, category=category,
                model_scope=[item.strip() for item in (model_scope or "").split(",") if item.strip()])
        require_document_creation_scope(db, actor, kb_id, details.category)
        data = await file.read(request.app.state.settings.max_upload_bytes + 1)
        if metadata is None:
            # Preserve legacy request fingerprints for existing receipts.
            normalized = json.dumps({"kb_id": kb_id, "title": title, "category": category, "model_scope": model_scope, "filename": file.filename, "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)}, ensure_ascii=False).encode()
        else:
            normalized = json.dumps({"kb_id": kb_id, "metadata": details.model_dump(mode="json", exclude_none=True),
                "filename": file.filename, "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)}, ensure_ascii=False, sort_keys=True).encode()
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", normalized)
        if replayed:
            document = db.get(Document, record.resource_refs["document_id"], populate_existing=True)
            version = db.get(DocumentVersion, record.resource_refs["version_id"], populate_existing=True)
            if not document or not version or version.document_id != document.id or document.knowledge_base_id != kb.id:
                raise SafeError("NOT_FOUND", "资源不存在", 404)
            actor = fence_management_authority(db, actor, kb.id, document_id=document.id, request=request)
            if not can_access_document(db, actor, document, "knowledge.manage"):
                raise SafeError("NOT_FOUND", "资源不存在", 404)
            return {"document": document_view(db, document), "version": version_view(version)}
        require_document_creation_scope(db, actor, kb_id, details.category)
        storage_identity = hashlib.sha256(f"{actor.id}\0{record.idempotency_key}\0{record.request_hash}".encode()).hexdigest()
        file_object = save_upload([data], file.filename or "upload.pdf", file.content_type or "application/pdf", actor, request.app.state.settings,
                                  storage_identity=storage_identity)
        db.add(file_object)
        db.flush()
        document = Document(knowledge_base_id=kb_id, title=details.title or file.filename or "未命名 PDF", category=details.category)
        db.add(document)
        db.flush()
        version = DocumentVersion(document_id=document.id, version_no=1, revision_label=details.revision_label,
            model_scope=details.model_scope, source_metadata=details.source_metadata.model_dump(mode="json", exclude_none=True),
            source_file_id=file_object.id, created_by=actor.id)
        db.add(version)
        db.flush()
        audit(db, actor.id, "document.uploaded", "document", document.id, sha256=file_object.sha256)
        actor = fence_management_authority(db, actor, kb.id, request=request)
        require_document_creation_scope(db, actor, kb_id, details.category)
        complete_idempotency(db, record, status_code=201, refs={"document_id": document.id, "version_id": version.id})
        db.commit()
        return {"document": document_view(db, document), "version": version_view(version)}

    @app.post("/documents/{document_id}/versions", status_code=201)
    async def upload_version(document_id: str, request: Request, file: UploadFile = File(...), revision_label: str | None = None,
                             model_scope: str | None = None, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_manage_document(db, actor, document)
        data = await file.read(request.app.state.settings.max_upload_bytes + 1)
        normalized = json.dumps({"document_id": document_id, "revision_label": revision_label, "model_scope": model_scope, "filename": file.filename, "sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)}, ensure_ascii=False).encode()
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", normalized)
        if replayed:
            _require_manage_document(db, actor, document, request=request)
            return {"version": version_view(db.get(DocumentVersion, record.resource_refs["version_id"]))}
        claimed = db.execute(update(Document).where(Document.id == document.id, Document.deleted_at.is_(None))
            .values(revision=Document.revision).execution_options(synchronize_session=False))
        if claimed.rowcount != 1:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        db.refresh(document)
        _require_manage_document(db, actor, document)
        storage_identity = hashlib.sha256(f"{actor.id}\0{record.idempotency_key}\0{record.request_hash}".encode()).hexdigest()
        file_object = save_upload([data], file.filename or "version.pdf", file.content_type or "application/pdf", actor, request.app.state.settings,
                                  storage_identity=storage_identity)
        db.add(file_object)
        db.flush()
        versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document.id)).all()
        version = DocumentVersion(document_id=document.id, version_no=max((item.version_no for item in versions), default=0) + 1,
                                  revision_label=revision_label, model_scope=[item.strip() for item in (model_scope or "").split(",") if item.strip()], source_file_id=file_object.id, created_by=actor.id)
        db.add(version)
        document.revision += 1
        db.flush()
        audit(db, actor.id, "document.version_created", "document_version", version.id, sha256=file_object.sha256)
        _require_manage_document(db, actor, document, request=request)
        complete_idempotency(db, record, status_code=201, refs={"version_id": version.id})
        db.commit()
        return {"version": version_view(version)}

    @app.get("/knowledge-bases/{kb_id}/documents")
    def list_documents(kb_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        user, _ = _user_or_401(request, db)
        kb = db.get(KnowledgeBase, kb_id)
        if not kb:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        can_manage = can_manage_kb(db, user, kb_id)
        documents = db.scalars(select(Document).where(Document.knowledge_base_id == kb_id, Document.deleted_at.is_(None))).all()
        if not can_manage and not can_query_kb(db, user, kb_id) and not any(document_has_formal_version(db, doc) and can_access_document(db, user, doc) for doc in documents):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        items = []
        for doc in documents:
            managed = can_access_document(db, user, doc, "knowledge.manage")
            view = document_view(db, doc, include_drafts=managed, user=user)
            if managed or view["versions"]:
                items.append(view)
        return {"items": items}

    @app.get("/documents/{document_id}")
    def get_document(document_id: str, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
        user, _ = _user_or_401(request, db)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        if can_access_document(db, user, document, "knowledge.manage"):
            return _json_response(document_view(db, document), response, document.revision)
        document = _require_document(db, user, document_id)
        view = document_view(db, document, include_drafts=False, user=user)
        if not view["versions"]:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return _json_response(view, response, document.revision)

    @app.get("/documents/{document_id}/versions")
    def list_versions(document_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        user, _ = _user_or_401(request, db)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        can_manage = can_access_document(db, user, document, "knowledge.manage")
        if not can_manage and not can_access_document(db, user, document):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id).order_by(DocumentVersion.version_no.desc())).all()
        from .governance import can_read_original_version
        return {"items": [version_view(v) for v in versions if can_manage or can_read_original_version(db, user, v)]}

    @app.delete("/documents/{document_id}", status_code=204)
    def delete_document(document_id: str, request: Request, response: Response, if_match: Annotated[str | None, Header()] = None,
                        db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None,
                        payload: DocumentDeleteInput | None = None) -> Response:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        from .document_lifecycle import delete_document as logical_delete
        normalized = payload.model_dump_json().encode() if payload else b"delete"
        logical_delete(db, actor, request, document, if_match, idempotency_key or "",
                       payload.reason if payload else "管理员逻辑删除", normalized, enforce_session=True)
        response.status_code = 204
        return response

    @app.patch("/documents/{document_id}/versions/{version_id}")
    def patch_version(document_id: str, version_id: str, payload: VersionPatchInput, request: Request, response: Response,
                      if_match: Annotated[str | None, Header()] = None, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_manage_document(db, actor, document)
        version = _version_or_404(db, document, version_id)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        _claim_managed_document(db, actor, document)
        db.refresh(version)
        if not replayed:
            check_if_match(if_match, version.revision)
            if version.publish_status != "draft":
                raise SafeError("VERSION_IMMUTABLE", "已发布版本的身份不能原位修改，请建立新修订", 409)
            for field in ("revision_label",):
                value = getattr(payload, field)
                if value is not None:
                    setattr(version, field, value)
            version.revision += 1
            document.revision += 1
            audit(db, actor.id, "version.updated", "document_version", version.id)
        _require_manage_document(db, actor, document, request=request)
        if not replayed:
            complete_idempotency(db, record, status_code=200, refs={"version_id": version.id})
            db.commit()
        return _json_response(version_view(version), response, version.revision)

    @app.post("/documents/{document_id}/versions/{version_id}/parse-runs", status_code=202)
    def parse_version(document_id: str, version_id: str, request: Request, payload: ParseInput | None = None,
                      db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_manage_document(db, actor, document)
        version = _version_or_404(db, document, version_id)
        mode = payload.mode if payload else "local"
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "",
            b"parse" if mode == "local" else b"parse:selected_service")
        _claim_managed_document(db, actor, document)
        db.refresh(version)
        if replayed:
            _require_manage_document(db, actor, document, request=request)
            run = db.get(ParseRun, record.resource_refs["parse_run_id"])
            if not run:
                raise SafeError("NOT_FOUND", "原解析记录不存在", 404)
            return {"parse_run_id": run.id, "status": run.status}
        file_object = db.get(FileObject, version.source_file_id)
        if not file_object or file_object.status != "available":
            raise SafeError("FILE_UNAVAILABLE", "原件当前不可用", 409)
        if mode == "selected_service":
            from .parse_jobs import accept_selected_parse
            run = accept_selected_parse(db, actor, version, file_object, app.state.settings, app.state.service_endpoints)
        else:
            from .parse_jobs import accept_local_parse
            run = accept_local_parse(db, actor, version, file_object, request.app.state.settings)
        _require_manage_document(db, actor, document, request=request)
        complete_idempotency(db, record, status_code=202, refs={"parse_run_id": run.id})
        db.commit()
        return {"parse_run_id": run.id, "status": run.status}

    @app.get("/documents/{document_id}/versions/{version_id}/parse-runs")
    def list_version_parse_runs(document_id: str, version_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        document = _require_document(db, actor, document_id, manage=True)
        version = _version_or_404(db, document, version_id)
        rows = db.scalars(select(ParseRun).where(ParseRun.document_version_id == version.id).order_by(ParseRun.created_at.desc(), ParseRun.id)).all()
        from .parse_jobs import parse_run_view
        return {"items": [parse_run_view(row, db) for row in rows]}

    @app.get("/parse-runs/{parse_run_id}")
    def get_parse_run(parse_run_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        run = db.get(ParseRun, parse_run_id)
        if not run:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        from .parse_jobs import parse_run_view
        return parse_run_view(run, db)

    @app.post("/parse-runs/{parse_run_id}/cancel")
    def cancel_parse_run(parse_run_id: str, request: Request, payload: dict[str, Any],
                         db: Session = Depends(get_db), if_match: Annotated[str | None, Header()] = None,
                         idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf
        from .parse_jobs import ParseServiceJob, parse_run_view
        require_csrf(request, session)
        if set(payload) != {"reason"} or not isinstance(payload["reason"], str) or not 0 < len(payload["reason"].strip()) <= 500:
            raise SafeError("INVALID_INPUT", "请填写取消原因", 422)
        run = db.get(ParseRun, parse_run_id)
        if not run:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", json.dumps(payload, sort_keys=True).encode())
        _claim_managed_document(db, actor, document)
        if replayed:
            _require_manage_document(db, actor, document, request=request)
            db.refresh(run)
            return parse_run_view(run, db)
        db.refresh(version)
        check_if_match(if_match, version.revision)
        db.refresh(run)
        job = db.get(ParseServiceJob, run.id, populate_existing=True)
        if not job:
            raise SafeError("PARSE_NOT_CANCELLABLE", "历史解析批次不能通过后台任务取消", 409)
        if run.status not in {"pending", "running", "waiting_external", "reconciliation_required"}:
            raise SafeError("PARSE_ALREADY_SETTLED", "该解析任务已结束", 409)
        job.cancel_requested = True
        run.status, run.error_code = "cancelled", "PARSE_CANCELLED"
        version.revision += 1
        audit(db, actor.id, "parse.cancelled", "parse_run", run.id)
        _require_manage_document(db, actor, document, request=request)
        complete_idempotency(db, record, status_code=200, refs={"parse_run_id": run.id})
        db.commit()
        return parse_run_view(run, db)

    @app.get("/parse-runs/{parse_run_id}/blocks")
    def list_blocks(parse_run_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        run = db.get(ParseRun, parse_run_id)
        if not run:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == parse_run_id).order_by(ContentBlock.ordinal)).all()
        from .governance import block_binding
        from .models import BlockRelation
        from .parsing.processing import RELATION_FIELDS, processing_view, relation_binding, relation_review_state
        relations = db.scalars(select(BlockRelation).where(BlockRelation.parse_run_id == run.id).order_by(BlockRelation.id)).all()
        return {"items": [{"id": block.id, "ordinal": block.ordinal, "kind": block.kind, "text": block.text_content, "locator": block.locator,
            "structured_content": block.structured_content, "review_status": block.review_status, "review_reason": block.review_reason,
            "revision": block.revision, "content_hash": block_binding(block), "file_page": block.file_page,
            "source_kind": block.source_kind, "raw_ref": block.raw_ref, "parent_block_id": block.parent_block_id,
            "structure_role": block.structure_role, "heading_level": block.heading_level,
            "source_content_hash": block.content_hash,
            "locator_schema_version": block.locator.get("schema_version")} for block in blocks],
            "relations": [{**{key: getattr(row, key) for key in RELATION_FIELDS}, "content_hash": relation_binding(row),
                           "state": relation_review_state(db, run, row)} for row in relations],
            **(processing_view(db, run) if run.ir_schema_version else {})}

    @app.get("/parse-runs/{parse_run_id}/pages")
    def list_parse_pages(parse_run_id: str, request: Request, limit: int = 100, after: int = 0,
                         db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        run = db.get(ParseRun, parse_run_id)
        if not run:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        _require_manage_document(db, actor, db.get(Document, version.document_id))
        if not 1 <= limit <= 200 or after < 0:
            raise SafeError("INVALID_INPUT", "分页范围无效", 422)
        from .models import DocumentPage
        from .parsing.ir_projection import PAGE_FIELDS
        rows = db.scalars(select(DocumentPage).where(DocumentPage.parse_run_id == run.id, DocumentPage.file_page > after)
                          .order_by(DocumentPage.file_page).limit(limit + 1)).all()
        return {"items": [{key: getattr(row, key) for key in PAGE_FIELDS} for row in rows[:limit]],
                "next_after": rows[limit - 1].file_page if len(rows) > limit else None,
                "total_page_count": (run.stage_checkpoint or {}).get("total_page_count"),
                "ir_schema_version": run.ir_schema_version}

    @app.get("/parse-runs/{parse_run_id}/assets/{artifact_id}")
    def read_parse_asset(parse_run_id: str, artifact_id: str, request: Request, db: Session = Depends(get_db)):
        actor, _ = _user_or_401(request, db)
        run = db.get(ParseRun, parse_run_id)
        if not run:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        _require_manage_document(db, actor, db.get(Document, version.document_id))
        from .models import ParseArtifact
        from .parsing.artifacts import ArtifactError, read_artifact
        artifact = db.get(ParseArtifact, artifact_id)
        if not artifact or artifact.parse_run_id != run.id or artifact.kind not in {"image", "page_preview", "region_crop"}:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        file = db.get(FileObject, artifact.file_id)
        if not file or file.mime_type not in {"image/png", "image/jpeg", "image/webp", "image/gif", "image/bmp", "image/tiff"}:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        try:
            raw = read_artifact(db, artifact, app.state.settings)
        except ArtifactError:
            raise SafeError("PARSE_ARTIFACT_UNAVAILABLE", "原图当前不可用，请核对资料状态", 409) from None
        return Response(raw, media_type=file.mime_type,
            headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff",
                     "Content-Security-Policy": "default-src 'none'; sandbox"})

    @app.get("/parse-runs/{parse_run_id}/blocks/{block_id}/source-image")
    def scan_table_source(parse_run_id: str, block_id: str, request: Request,
                          row: int | None = None, column: int | None = None, key: int | None = None,
                          region: int = 0,
                          db: Session = Depends(get_db)) -> Response:
        actor, _ = _user_or_401(request, db)
        block = db.get(ContentBlock, block_id)
        run = db.get(ParseRun, parse_run_id)
        if not block or not run or block.parse_run_id != run.id:
            raise SafeError("NOT_FOUND", "原表裁图不可用", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        document = db.get(Document, version.document_id)
        if not can_access_document(db, actor, document, "knowledge.manage"):
            raise SafeError("NOT_FOUND", "原表裁图不可用", 404)

        from .parsing.local_pdf import normalized_bbox, render_source_region
        locator = block.locator
        page = locator.get("file_page")
        bbox = normalized_bbox(locator.get("bbox_normalized"))
        original = db.get(FileObject, version.source_file_id)
        source_supported = ((block.kind == "table" and locator.get("extraction") == "mineru_basic_ocr")
                            or (block.kind == "image" and locator.get("extraction") == "pdf_visual_inventory"))
        if run.ir_schema_version:
            regions = locator.get("regions", [])
            chosen = regions[region] if 0 <= region < len(regions) else {}
            page = chosen.get("file_page")
            bbox = normalized_bbox(chosen.get("bbox_normalized"))
            source_supported = (chosen.get("geometry_status") in {"exact", "table_region_only"}
                and locator.get("document_version_id") == version.id and locator.get("parse_run_id") == run.id
                and locator.get("source_file_id") == version.source_file_id and original
                and locator.get("source_sha256") == original.sha256)
        if (not source_supported or not bbox
                or type(page) is not int or page < 1 or not original or original.status != "available"):
            raise SafeError("NOT_FOUND", "原表裁图不可用", 404)
        indices = (row, column, key)
        if any(index is not None for index in indices):
            if any(index is None or index < 0 for index in indices):
                raise SafeError("NOT_FOUND", "按键裁图不可用", 404)
            try:
                key_box = normalized_bbox(block.structured_content["rows"][row][column]["graphic_keys"][key]["bbox_normalized"])
            except (KeyError, IndexError, TypeError):
                key_box = None
            if not key_box or not (bbox[0] <= key_box[0] < key_box[2] <= bbox[2]
                                   and bbox[1] <= key_box[1] < key_box[3] <= bbox[3]):
                raise SafeError("NOT_FOUND", "按键裁图不可用", 404)
            bbox = key_box
        try:
            import pymupdf

            source_bytes = file_path(original, request.app.state.settings).read_bytes()
            if len(source_bytes) != original.byte_size or hashlib.sha256(source_bytes).hexdigest() != original.sha256:
                raise ValueError("source identity changed")
            with pymupdf.open(stream=source_bytes, filetype="pdf") as pdf:
                pixels = render_source_region(pdf[page - 1], bbox)
        except (ImportError, IndexError, OSError, RuntimeError, ValueError):
            raise SafeError("NOT_FOUND", "原表裁图不可用", 404) from None
        if not can_access_document(db, actor, document, "knowledge.manage"):
            raise SafeError("NOT_FOUND", "原表裁图不可用", 404)
        return Response(pixels, media_type="image/png", headers={"Cache-Control": "private, no-store"})

    @app.get("/parse-runs/{parse_run_id}/chunk-sets")
    def list_chunk_sets(parse_run_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        run = db.get(ParseRun, parse_run_id)
        if not run:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        sets = db.scalars(select(ChunkSet).where(ChunkSet.parse_run_id == run.id).order_by(ChunkSet.id)).all()
        from .chunk_jobs import chunk_set_view
        return {"items": [chunk_set_view(db, item) for item in sets]}

    @app.post("/parse-runs/{parse_run_id}/chunk-sets", status_code=202)
    def accept_chunks(parse_run_id: str, payload: dict[str, Any], request: Request,
                      db: Session = Depends(get_db)):
        from .models import ChunkSet
        from .chunk_jobs import accept_chunk_set, chunk_set_view
        from .services import require_csrf
        actor, session = _user_or_401(request, db)
        require_csrf(request, session)
        run = db.get(ParseRun, parse_run_id)
        if not run:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        if set(payload) != {"splitter_profile_revision_id"} or not isinstance(payload["splitter_profile_revision_id"], str):
            raise SafeError("INVALID_INPUT", "仅接收登记的切分配置标识", 422)
        body = json.dumps(payload, sort_keys=True).encode()
        record, replay = idempotency_record(db, actor, request, request.headers.get("Idempotency-Key", ""), body)
        _claim_managed_document(db, actor, document)
        db.refresh(version)
        if replay:
            row = db.get(ChunkSet, record.resource_refs["chunk_set_id"])
            if not row:
                raise SafeError("NOT_FOUND", "原候选不存在", 404)
        else:
            check_if_match(request.headers.get("If-Match"), version.revision)
            row = accept_chunk_set(db, actor, run, payload["splitter_profile_revision_id"])
            _require_manage_document(db, actor, document, request=request)
            complete_idempotency(db, record, status_code=202, refs={"chunk_set_id": row.id})
            db.commit()
        _require_manage_document(db, actor, document)
        return chunk_set_view(db, row)

    @app.post("/parse-runs/{parse_run_id}/corrections", status_code=202)
    def correct_parse_run(parse_run_id: str, payload: CorrectionInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        parent = db.get(ParseRun, parse_run_id)
        if not parent:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, parent.document_version_id)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        serialized = payload.model_dump_json(exclude=None if payload.graphic_tables else {"graphic_tables"})
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", serialized.encode())
        if parent.ir_schema_version:
            # No byte reads or inference in this request transaction. The
            # durable child is the only authorized target of the worker.
            _claim_managed_document(db, actor, document)
            db.refresh(parent)
            if replayed:
                run = db.get(ParseRun, record.resource_refs["parse_run_id"])
            else:
                if len(serialized.encode()) > 1024 * 1024:
                    raise SafeError("INVALID_CORRECTION", "纠正内容超过本次受理上限", 422)
                from .parse_jobs import accept_derived_parse
                run = accept_derived_parse(db, actor, parent, payload.model_dump())
                _require_manage_document(db, actor, document, request=request)
                complete_idempotency(db, record, status_code=202, refs={"parse_run_id": run.id})
                db.commit()
            if not run:
                raise SafeError("NOT_FOUND", "原纠正批次不可用", 404)
            _require_manage_document(db, actor, document, request=request)
            return {"parse_run_id": run.id, "status": run.status, "origin": run.origin}
        _claim_managed_document(db, actor, document)
        db.refresh(version)
        db.refresh(parent)
        if replayed:
            _require_manage_document(db, actor, document, request=request)
            run = db.get(ParseRun, record.resource_refs["parse_run_id"])
            if not run:
                raise SafeError("NOT_FOUND", "原解析记录不存在", 404)
            return {"parse_run_id": run.id, "status": run.status, "origin": run.origin}
        source_blocks = db.scalars(select(ContentBlock).where(ContentBlock.parse_run_id == parent.id).order_by(ContentBlock.ordinal)).all()
        edits = {str(item.get("ordinal")): item for item in payload.blocks if item.get("ordinal") is not None}
        graphics = {edit.block_id: edit for edit in payload.graphic_tables}
        if len(graphics) != len(payload.graphic_tables) or graphics.keys() - {source.id for source in source_blocks}:
            raise SafeError("INVALID_GRAPHIC_CORRECTION", "图形键校正块须唯一且属于本批次", 422)

        from .parsing.table_corrections import table_graphic_edits

        run = ParseRun(document_version_id=version.id, origin="manual",
                       status="needs_review" if graphics else "succeeded",
                       output_revision="manual-graphic-1" if graphics else "manual-1")
        db.add(run)
        db.flush()
        for ordinal, source in enumerate(source_blocks, 1):
            edit = edits.get(str(ordinal), {})
            graphic = graphics.get(source.id)
            kind = str(edit.get("kind", source.kind))
            text = str(edit.get("text", source.text_content))
            locator = dict(edit.get("locator", source.locator))
            structure = dict(source.structured_content or {})
            changed = (kind, text, locator) != (source.kind, source.text_content, source.locator)
            if changed and (graphic or structure.get("pending_graphic_keys") or structure.get("graphic_key_correction")):
                raise SafeError("GRAPHIC_CORRECTION_REQUIRED", "请在原表的单元格上校正图形键并保留原页定位", 422)
            if graphic:
                text, structure = table_graphic_edits(source, graphic)
            elif (kind, text) != (source.kind, source.text_content):
                structure = {}
            db.add(ContentBlock(parse_run_id=run.id, ordinal=ordinal, kind=kind, text_content=text,
                                locator=locator, structured_content=structure,
                                review_status="unreviewed"))
        from .models import ChunkSet

        db.add(ChunkSet(parse_run_id=run.id, status="ready"))
        audit(db, actor.id, "parse.corrected", "parse_run", run.id, parent_run_id=parent.id,
              graphic_cells=sum(len(edit.cells) for edit in graphics.values()))
        _require_manage_document(db, actor, document, request=request)
        complete_idempotency(db, record, status_code=202, refs={"parse_run_id": run.id})
        db.commit()
        return {"parse_run_id": run.id, "status": run.status, "origin": run.origin}

    @app.post("/parse-runs/{parse_run_id}/blocks/{block_id}/review")
    def review_block(parse_run_id: str, block_id: str, payload: BlockReviewInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None, if_match: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        run = db.get(ParseRun, parse_run_id)
        block = db.get(ContentBlock, block_id)
        if not run or not block or block.parse_run_id != run.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        _claim_managed_document(db, actor, document)
        db.refresh(version)
        db.refresh(run)
        db.refresh(block)
        if replayed:
            _require_manage_document(db, actor, document, request=request)
            block = db.get(ContentBlock, record.resource_refs["block_id"])
            return {"id": block.id, "review_status": block.review_status, "review_reason": block.review_reason}
        check_if_match(if_match, block.revision)
        from .governance import review_content_block
        review_content_block(db, actor, run, block, payload.review_status, payload.reason)
        _require_manage_document(db, actor, document, request=request)
        complete_idempotency(db, record, status_code=200, refs={"block_id": block.id})
        db.commit()
        return {"id": block.id, "review_status": block.review_status, "review_reason": block.review_reason}

    @app.post("/documents/{document_id}/versions/{version_id}/publish")
    def publish_version(document_id: str, version_id: str, payload: PublishInput, request: Request, response: Response,
                        if_match: Annotated[str | None, Header()] = None, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_manage_document(db, actor, document)
        version = _version_or_404(db, document, version_id)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        _claim_managed_document(db, actor, document)
        db.refresh(version)
        if not replayed:
            check_if_match(if_match, document.revision)
        from .governance import publish_from_preview
        operation = record.resource_refs.get("operation", {}) if replayed else {}
        if not replayed:
            operation = publish_from_preview(db, actor, document, version, payload)
        _require_manage_document(db, actor, document, request=request)
        if not replayed:
            complete_idempotency(db, record, status_code=200, refs={"version_id": version.id, "operation": operation})
            db.commit()
        return _json_response({"document": document_view(db, document), "version": version_view(version), **operation}, response, document.revision)

    @app.post("/documents/{document_id}/versions/{version_id}/withdraw")
    def withdraw_version(document_id: str, version_id: str, payload: WithdrawInput, request: Request, response: Response,
                         if_match: Annotated[str | None, Header()] = None, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_manage_document(db, actor, document)
        version = _version_or_404(db, document, version_id)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        _claim_managed_document(db, actor, document)
        db.refresh(version)
        if not replayed:
            check_if_match(if_match, document.revision)
        if replayed:
            _require_manage_document(db, actor, document, request=request)
            version = db.get(DocumentVersion, record.resource_refs["version_id"])
            document = db.get(Document, record.resource_refs["document_id"])
            return _json_response({"document": document_view(db, document), "version": version_view(version), **record.resource_refs.get("operation", {})}, response, document.revision)
        from .governance import withdraw_from_preview
        operation = withdraw_from_preview(db, actor, document, version, payload)
        _require_manage_document(db, actor, document, request=request)
        complete_idempotency(db, record, status_code=200, refs={"version_id": version.id, "document_id": document.id, "operation": operation})
        db.commit()
        return _json_response({"document": document_view(db, document), "version": version_view(version), **operation}, response, document.revision)

    @app.get("/documents/{document_id}/versions/{version_id}/file")
    def source_file(document_id: str, version_id: str, request: Request, db: Session = Depends(get_db)) -> FileResponse:
        user, _ = _user_or_401(request, db)
        document = db.get(Document, document_id)
        if not document:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = _version_or_404(db, document, version_id)
        from .governance import can_read_original_version
        managed = can_access_document(db, user, document, "knowledge.manage")
        if not managed and not can_read_original_version(db, user, version):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        file_object = db.get(FileObject, version.source_file_id)
        if not file_object or file_object.status != "available":
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        mode = "historical" if version.publish_status in {"superseded", "withdrawn"} else "reference_only" if version.publish_status == "draft" else "published_original"
        return FileResponse(file_path(file_object, request.app.state.settings), media_type="application/pdf", filename=document.title + ".pdf", content_disposition_type="inline",
            headers={"X-Document-Read-Mode": mode, "X-Document-Publication-State": version.publish_status,
                     "X-Formal-Use-Requires-Validation": "true", "Cache-Control": "private, no-store",
                     "X-Content-Type-Options": "nosniff"})

    @app.get("/documents/{document_id}/versions/{version_id}/file-access")
    def source_file_access(document_id: str, version_id: str, request: Request, db: Session = Depends(get_db)):
        # Use exactly the original-file gate, including current source availability.
        original = source_file(document_id, version_id, request, db)
        return {"read_mode": original.headers["X-Document-Read-Mode"],
                "publish_status": original.headers["X-Document-Publication-State"]}

    @app.get("/parse-runs/{parse_run_id}/service-job")
    def parse_service_job(parse_run_id: str, request: Request, db: Session = Depends(get_db)):
        from .parse_jobs import ParseServiceJob
        actor, _ = _user_or_401(request, db)
        job = db.get(ParseServiceJob, parse_run_id)
        run = db.get(ParseRun, parse_run_id)
        if not job or not run:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, run.document_version_id)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        return {"parse_run_id": run.id, "status": run.status, "error_code": run.error_code,
            "pin_id": job.pin_id, "attempt": job.attempt, "provider_task_id": run.provider_task_id,
            "lease_until": job.lease_until.isoformat() if job.lease_until else None,
            "data_scope": "synthetic_only", "next_action": "由受控本地解析Worker推进原任务；无远端标识的未知提交不得重发"}

    @app.post("/document-versions/{version_id}/embedding-jobs", status_code=202)
    def create_embedding_job(version_id: str, request: Request, db: Session = Depends(get_db),
                             idempotency_key: Annotated[str | None, Header()] = None):
        from .embedding_jobs import accept_selected_embedding
        from .services import require_csrf
        actor, session = _user_or_401(request, db)
        require_csrf(request, session)
        version = db.get(DocumentVersion, version_id)
        if not version:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        document = db.get(Document, version.document_id)
        _require_manage_document(db, actor, document)
        record, replay = idempotency_record(db, actor, request, idempotency_key or "", b"embedding:selected_service")
        job = accept_selected_embedding(db, actor, version, app.state.service_endpoints, request=request)
        if replay and job.id != record.resource_refs.get("embedding_job_id"):
            raise SafeError("EMBEDDING_JOB_CONFLICT", "原嵌入任务与当前资料不一致", 409)
        if not replay:
            complete_idempotency(db, record, status_code=202, refs={"embedding_job_id": job.id})
        db.commit()
        return {"embedding_job_id": job.id, "state": job.state, "pin_id": job.pin_id, "data_scope": "synthetic_only"}

    @app.get("/embedding-jobs/{job_id}")
    def get_embedding_job(job_id: str, request: Request, db: Session = Depends(get_db)):
        from .embedding_jobs import EmbeddingServiceJob
        actor, _ = _user_or_401(request, db)
        job = db.get(EmbeddingServiceJob, job_id)
        if not job:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        if job.actor_id != actor.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        version = db.get(DocumentVersion, job.version_id)
        _require_manage_document(db, actor, db.get(Document, version.document_id))
        return {"embedding_job_id": job.id, "state": job.state, "error_code": job.error_code,
            "pin_id": job.pin_id, "attempt": job.attempt, "result": job.result, "data_scope": "synthetic_only"}

    @app.post("/knowledge/queries", status_code=202)
    def create_query(payload: QueryInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "knowledge.query")
        from .services import require_csrf

        require_csrf(request, session)
        if not getattr(app.state, "query_acceptance_enabled", True):
            raise SafeError("QUERY_PREPARATION_MODE", "当前处于资料准备窗口，新查询尚未受理。可继续查阅已有记录，待管理员恢复查询服务后再提交。", 409)
        query_payload = payload.model_dump()
        mode = payload.service_mode if "service_mode" in payload.model_fields_set else getattr(config, "query_service_mode", "local")
        if mode not in {"local", "selected_service"}:
            raise SafeError("SERVICE_MODE_INVALID", "查询服务配置尚未就绪", 409)
        query_payload["service_mode"] = mode
        if mode == "local":
            query_payload.pop("service_mode")  # Preserve pre-existing local idempotency bytes.
        query_bytes = json.dumps(query_payload, ensure_ascii=False, separators=(",", ":")).encode()
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", query_bytes)
        if replayed:
            task = db.get(QueryTask, record.resource_refs["task_id"])
            return {"task_id": task.id, "status_url": f"/tasks/{task.id}", "status": task.status}
        from .execution import accept_query
        task = accept_query(db, actor, session, query_payload, idempotency_key or "")
        if mode == "selected_service":
            from .service_use import pin_use
            from .service_use_models import ServiceTaskSelection, ServiceSelection
            pin = pin_use(db, actor.id, "query.answer", "task:" + task.id, app.state.service_endpoints)
            pins = {"query.answer": pin.id}
            for purpose in ("query.summary", "query.compaction"):
                selection = db.get(ServiceSelection, purpose, populate_existing=True)
                if selection and selection.enabled:
                    pins[purpose] = pin_use(db, actor.id, purpose, "task:" + task.id, app.state.service_endpoints).id
            db.add(ServiceTaskSelection(task_id=task.id, required=True, pin_ids=pins))
        complete_idempotency(db, record, status_code=202, refs={"task_id": task.id})
        db.commit()
        return {"task_id": task.id, "status_url": f"/tasks/{task.id}", "status": task.status}

    @app.get("/tasks")
    def list_tasks(request: Request, db: Session = Depends(get_db),
                   limit: int = Query(20, ge=1, le=50), offset: int = Query(0, ge=0, le=2147483647)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        share = select(TaskShare.id).where(TaskShare.task_id == QueryTask.id,
            TaskShare.recipient_user_id == actor.id, TaskShare.granted_by == QueryTask.owner_user_id,
            TaskShare.status == "active", TaskShare.revoked_at.is_(None),
            or_(TaskShare.expires_at.is_(None), TaskShare.expires_at > utc_now())).exists()
        candidates = list(db.scalars(select(QueryTask).where(or_(QueryTask.owner_user_id == actor.id, share))
            .order_by(QueryTask.created_at.desc(), QueryTask.id.desc()).offset(offset).limit(limit + 1)))
        items = []
        for task in candidates[:limit]:
            if can_read_task(db, actor, task):
                items.append(task_view(task, viewer_user_id=actor.id))
            elif task.owner_user_id == actor.id:
                items.append({"id": task.id, "kind": task.kind, "status": "access_limited", "next_action": "访问条件已变化"})
        return {"items": items, "next_offset": offset + limit if len(candidates) > limit else None}

    @app.get("/tasks/{task_id}")
    def get_task(task_id: str, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        task = db.get(QueryTask, task_id)
        if not task or (task.owner_user_id != actor.id and not can_read_task(db, actor, task)):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        actor, _ = _user_or_401(request, db)
        if not can_read_task(db, actor, task):
            return {"id": task.id, "status": "access_limited", "next_action": "访问条件已变化"}
        view = task_view(task, viewer_user_id=actor.id)
        if task.owner_user_id == actor.id:
            from .execution import query_retry_eligible
            view["retry_eligible"] = query_retry_eligible(db, actor, task)
        return _json_response(view, response, task.revision)

    @app.get("/tasks/{task_id}/status")
    def task_status(task_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        task = db.get(QueryTask, task_id)
        if not task:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        if (task.owner_user_id == actor.id and task.kind == "query"
                and task.status in {"pending", "running"} and task.accepted_scope):
            from .services import can_query_kb
            libraries = task.accepted_scope.get("knowledge_base_ids", [])
            if (not has_permission(actor, "task.read") or not has_permission(actor, "knowledge.query")
                    or not libraries or any(not can_query_kb(db, actor, library) for library in libraries)):
                return {"id": task.id, "status": "access_limited", "message": "访问条件已变化"}
            # Only progress is disclosed. Do not replay every evidence manifest
            # and original-file hash on each second of an owner's live poll.
            # Terminal views and all content still use their full read gates.
            return {"id": task.id, "status": task.status, "revision": task.revision}
        actor, _ = _user_or_401(request, db)
        if not can_read_task(db, actor, task):
            if task.owner_user_id == actor.id:
                return {"id": task.id, "status": "access_limited", "message": "访问条件已变化"}
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        return {"id": task.id, "status": task.status, "next_action": task.next_action, "revision": task.revision}

    @app.post("/tasks/{task_id}/cancel")
    def cancel_task(task_id: str, payload: CancelInput, request: Request, response: Response, if_match: Annotated[str | None, Header()] = None,
                    db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        task = db.get(QueryTask, task_id)
        if not task or task.owner_user_id != actor.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        if not has_permission(actor, "task.read"):
            raise SafeError("FORBIDDEN", "当前账号不能取消该任务", 403)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if not replayed:
            check_if_match(if_match, task.revision)
        if replayed:
            task = db.get(QueryTask, record.resource_refs["task_id"])
            return _json_response({"id": task.id, "status": task.status, "revision": task.revision, "next_action": task.next_action}, response, task.revision)
        if task.status not in {"succeeded", "failed", "cancelled"}:
            from .services import task_write_guard
            task_write_guard(db, task, allowed_statuses=(task.status,))
            task.status = "cancelled"
            task.next_action = "如需重新查询，请从原入口提交新任务"
            task.revision += 1
            audit(db, actor.id, "task.cancelled", "task", task.id, reason=payload.reason)
        complete_idempotency(db, record, status_code=200, refs={"task_id": task.id})
        db.commit()
        return _json_response({"id": task.id, "status": task.status, "revision": task.revision, "next_action": task.next_action}, response, task.revision)

    @app.post("/tasks/{task_id}/retry", status_code=202)
    def retry_task(task_id: str, payload: RetryInput, request: Request, response: Response, if_match: Annotated[str | None, Header()] = None,
                   db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        task = db.get(QueryTask, task_id)
        if not task or task.owner_user_id != actor.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_permission(actor, "task.read")
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if not replayed:
            check_if_match(if_match, task.revision)
        if replayed:
            task = db.get(QueryTask, record.resource_refs["task_id"])
            return _json_response({"task_id": task.id, "status": task.status}, response, task.revision)
        from .execution import resume_query
        resume_query(db, actor, task)
        audit(db, actor.id, "task.retry_requested", "task", task.id, reason=payload.reason)
        complete_idempotency(db, record, status_code=202, refs={"task_id": task.id})
        db.commit()
        return _json_response({"task_id": task.id, "status": task.status}, response, task.revision)

    @app.get("/tasks/{task_id}/result")
    def task_result(task_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        task = db.get(QueryTask, task_id)
        from .evidence_api import visible_task_evidence
        from .query_evidence import shared_task_answer
        from .execution_models import QueryAnswerSnapshot
        evidence = visible_task_evidence(db, actor, task_id)
        if task.status != "succeeded":
            raise SafeError("RESULT_NOT_READY", "任务尚未完成", 409)
        snapshot = db.scalar(select(QueryAnswerSnapshot).where(QueryAnswerSnapshot.task_id == task.id,
            QueryAnswerSnapshot.execution_revision == task.execution_revision)) if task.owner_user_id == actor.id else shared_task_answer(db, actor, task)[0]
        result = snapshot.result if snapshot else task.result_summary
        evidence_items = []
        for item in evidence:
            version = db.get(DocumentVersion, item.document_version_id)
            evidence_items.append(evidence_view(item, version, db.get(Document, version.document_id)))
        _user_or_401(request, db)  # The browser session must still be current at the return boundary.
        return {"answer_snapshot_id": snapshot.id if snapshot else None,
            "task": task_view(task, viewer_user_id=actor.id, answer_result=result), "answer": result.get("answer"),
            "result_status": result.get("result_status"), "evidence": evidence_items, "warnings": task.warnings or []}

    @app.get("/tasks/{task_id}/artifacts")
    def task_artifacts(task_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        task = db.get(QueryTask, task_id)
        if not task or (not can_read_task(db, actor, task) and task.owner_user_id != actor.id):
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        if not can_read_task(db, actor, task):
            raise SafeError("ACCESS_LIMITED", "任务依赖的资料访问条件已变化", 403)
        # Internal candidates, validation bodies, SDK journals and auxiliary
        # inputs are never employee artifacts, including for a query owner.
        public_kinds = {"query.result.json"}
        return {"items": [{"id": item.id, "kind": item.kind, "validation_status": item.validation_status, "file_object_id": item.file_object_id}
            for item in db.scalars(select(TaskArtifact).where(TaskArtifact.task_id == task.id, TaskArtifact.kind.in_(public_kinds))).all()]}

    @app.get("/tasks/{task_id}/evidence")
    def task_evidence(task_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        from .evidence_api import visible_task_evidence
        evidence = visible_task_evidence(db, actor, task_id)
        items = []
        for item in evidence:
            version = db.get(DocumentVersion, item.document_version_id)
            document = db.get(Document, version.document_id)
            items.append(evidence_view(item, version, document))
        _user_or_401(request, db)
        return {"items": items}

    @app.get("/tasks/{task_id}/evidence/{evidence_id}")
    def task_evidence_detail(task_id: str, evidence_id: str, request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        from .evidence_api import read_task_evidence
        _, item = read_task_evidence(db, actor, task_id, evidence_id)
        version = db.get(DocumentVersion, item.document_version_id)
        document = db.get(Document, version.document_id)
        if not can_access_document(db, actor, document):
            raise SafeError("ACCESS_LIMITED", "任务依赖的资料访问条件已变化", 403)
        _user_or_401(request, db)
        return evidence_view(item, version, document)

    def share_snapshot(db, task):
        if not task.accepted_scope:
            return None
        from .execution_models import QueryAnswerSnapshot
        return db.scalar(select(QueryAnswerSnapshot.id).where(QueryAnswerSnapshot.task_id == task.id,
            QueryAnswerSnapshot.execution_revision == task.execution_revision))

    def share_rows(db, task, snapshot_id):
        return db.scalars(select(TaskShare).where(TaskShare.task_id == task.id,
            TaskShare.answer_snapshot_id.is_(None) if snapshot_id is None else TaskShare.answer_snapshot_id == snapshot_id)
            .order_by(TaskShare.recipient_user_id)).all()

    def share_view(item):
        expires = _utc(item.expires_at) if item.expires_at else None
        state = "expired" if item.status == "active" and expires and expires <= utc_now() else item.status
        return {"user_id": item.recipient_user_id, "answer_snapshot_id": item.answer_snapshot_id,
            "granted_at": _utc(item.granted_at).isoformat(), "expires_at": expires.isoformat() if expires else None,
            "revoked_at": _utc(item.revoked_at).isoformat() if item.revoked_at else None, "status": state}

    @app.get("/tasks/{task_id}/shares")
    def list_shares(task_id: str, request: Request, response: Response, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        task = db.get(QueryTask, task_id)
        if not task or task.owner_user_id != actor.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_permission(actor, "task.share")
        if not can_read_task(db, actor, task):
            raise SafeError("ACCESS_LIMITED", "回答来源访问条件已变化；仍可提交仅撤销分享的操作", 403)
        snapshot_id = share_snapshot(db, task)
        return _json_response({"answer_snapshot_id": snapshot_id,
            "items": [share_view(item) for item in share_rows(db, task, snapshot_id)]}, response, task.revision)

    @app.put("/tasks/{task_id}/shares")
    def replace_shares(task_id: str, payload: ShareInput, request: Request, response: Response, if_match: Annotated[str | None, Header()] = None,
                       db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        from .services import require_csrf

        require_csrf(request, session)
        task = db.get(QueryTask, task_id)
        if not task or task.owner_user_id != actor.id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_permission(actor, "task.share")
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if not replayed:
            check_if_match(if_match, task.revision)
        if replayed:
            snapshot_id = record.resource_refs.get("answer_snapshot_id", payload.answer_snapshot_id if task.accepted_scope else None)
            return _json_response({"answer_snapshot_id": snapshot_id,
                "items": [share_view(item) for item in share_rows(db, task, snapshot_id)]}, response, task.revision)
        snapshot_id = None
        if task.kind != "query":
            raise SafeError("ANSWER_SHARE_REQUIRED", "只能分享一条查询回答", 409)
        if task.accepted_scope:
            from .execution_models import QueryAnswerSnapshot
            snapshot = db.get(QueryAnswerSnapshot, payload.answer_snapshot_id) if payload.answer_snapshot_id else None
            if not snapshot or snapshot.task_id != task.id or snapshot.execution_revision != task.execution_revision or task.status != "succeeded":
                raise SafeError("ANSWER_SHARE_REQUIRED", "请指定当前已核验回答，不能分享整个任务", 409)
            snapshot_id = snapshot.id
        # Serialize replacement before reading the existing recipient set. A
        # concurrent replacement with the same revision must fail, never union
        # two independently chosen lists or restore a revoked recipient.
        from .services import task_write_guard
        try:
            task_write_guard(db, task, allowed_statuses=(task.status,))
        except SafeError as changed:
            if changed.code == "TASK_STATE_CHANGED":
                raise SafeError("PRECONDITION_FAILED", "分享已变化，请刷新后重试", 412) from None
            raise
        desired = {item.user_id: item for item in payload.entries}
        if len(desired) != len(payload.entries):
            raise SafeError("SHARE_RECIPIENT_INVALID", "分享接收者不能重复", 422)
        existing = {item.recipient_user_id: item for item in db.scalars(select(TaskShare).where(TaskShare.task_id == task.id)).all()}
        now = utc_now()
        revoke_only = all((old := existing.get(user_id)) is not None and old.status == "active" and old.revoked_at is None
            and old.granted_by == actor.id and old.answer_snapshot_id == snapshot_id
            and (old.expires_at is None or _utc(old.expires_at) > now)
            and (None if old.expires_at is None else _utc(old.expires_at)) == (None if item.expires_at is None else _utc(item.expires_at))
            for user_id, item in desired.items())
        if not revoke_only:
            from .execution import _lock_delivery_sources
            _lock_delivery_sources(db, task, auth_session_id=session.id)
        if not can_read_task(db, actor, task) and not revoke_only:
            raise SafeError("ACCESS_LIMITED", "回答来源访问条件已变化，只能撤销已有分享", 403)
        for user_id, share in existing.items():
            if share.answer_snapshot_id == snapshot_id and user_id not in desired:
                share.status = "revoked"
                share.revoked_at = utc_now()
        if not revoke_only and desired:
            # Approval revocation serializes on the beneficiary User. Keep the
            # recipients' current identity/authority stable through this write.
            db.scalars(select(User.id).where(User.id.in_(desired)).order_by(User.id).with_for_update(read=True)).all()
        for user_id, item in desired.items():
            recipient = db.get(User, user_id)
            if not recipient or recipient.status != "active":
                raise SafeError("SHARE_RECIPIENT_INVALID", "分享接收者不可用", 400)
            share = existing.get(user_id)
            if share:
                share.status = "active"
                share.revoked_at = None
                share.expires_at = _utc(item.expires_at) if item.expires_at else None
                share.answer_snapshot_id = snapshot_id
                share.granted_by = actor.id
            else:
                db.add(TaskShare(task_id=task.id, recipient_user_id=user_id, granted_by=actor.id,
                    expires_at=_utc(item.expires_at) if item.expires_at else None, answer_snapshot_id=snapshot_id))
        db.flush()
        if not revoke_only:
            for user_id in desired:
                recipient = db.get(User, user_id, populate_existing=True)
                if not recipient or not can_read_task(db, recipient, task):
                    raise SafeError("SHARE_RECIPIENT_UNAVAILABLE", "接收者当前不能读取这条回答所需的资料，分享未保存", 409)
        if revoke_only:
            db.scalar(select(AuthSession.id).where(AuthSession.id == session.id).with_for_update(read=True))
            db.scalar(select(User.id).where(User.id == actor.id).with_for_update(read=True))
        current_actor, _ = current_session(request, db, app.state.settings, persist_heartbeat=False)
        if current_actor.id != task.owner_user_id:
            raise SafeError("NOT_FOUND", "资源不存在", 404)
        _require_permission(current_actor, "task.share")
        if not revoke_only and not can_read_task(db, current_actor, task):
            raise SafeError("ACCESS_LIMITED", "回答来源访问条件已变化，分享未提交", 403)
        task.revision += 1
        audit(db, actor.id, "task.shared", "task", task.id)
        complete_idempotency(db, record, status_code=200, refs={"task_id": task.id, "answer_snapshot_id": snapshot_id})
        db.commit()
        return _json_response({"answer_snapshot_id": snapshot_id,
            "items": [share_view(item) for item in share_rows(db, task, snapshot_id)]}, response, task.revision)

    @app.get("/admin/audit")
    def audit_log(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        _require_permission(actor, "operations.read")
        from .models import AuditEvent

        events = db.scalars(select(AuditEvent).order_by(AuditEvent.created_at.desc()).limit(200)).all()
        return {"items": [{"id": item.id, "actor_user_id": item.actor_user_id, "action": item.action, "resource_type": item.resource_type, "resource_id": item.resource_id, "reason": item.reason, "metadata": item.metadata_json, "created_at": item.created_at.isoformat()} for item in events]}

    @app.get("/admin/operations/providers")
    def provider_status(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        _require_permission(actor, "operations.read")
        from .models import ProviderCall

        calls = db.scalars(select(ProviderCall).order_by(ProviderCall.created_at.desc()).limit(100)).all()
        return {"items": [sanitized_provider_view(call) for call in calls]}

    @app.post("/admin/operations/provider-calls", status_code=201)
    def add_provider_call(payload: ProviderCallInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "system.configure")
        from .services import require_csrf

        require_csrf(request, session)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if replayed:
            from .models import ProviderCall

            call = db.get(ProviderCall, record.resource_refs["provider_call_id"])
            return sanitized_provider_view(call)
        call = record_provider_call(db, actor.id, payload.provider, payload.operation, payload.status, payload.data_scope, payload.cost_units, payload.error_code)
        db.flush()
        complete_idempotency(db, record, status_code=201, refs={"provider_call_id": call.id})
        db.commit()
        return sanitized_provider_view(call)

    @app.post("/admin/operations/drills", status_code=201)
    def record_drill(payload: BackupDrillInput, request: Request, db: Session = Depends(get_db), idempotency_key: Annotated[str | None, Header()] = None) -> dict[str, Any]:
        actor, session = _user_or_401(request, db)
        _require_permission(actor, "operations.read")
        from .services import require_csrf

        require_csrf(request, session)
        record, replayed = idempotency_record(db, actor, request, idempotency_key or "", payload.model_dump_json().encode())
        if replayed:
            return {"status": "recorded", "resource_id": record.resource_refs.get("audit_id")}
        event = audit(db, actor.id, f"operations.{payload.kind}_drill", "operations", None, reason=payload.notes, result=payload.result)
        db.flush()
        complete_idempotency(db, record, status_code=201, refs={"audit_id": event.id})
        db.commit()
        return {"status": "recorded", "resource_id": event.id, "result": payload.result}

    @app.get("/admin/operations/tasks")
    def operation_tasks(request: Request, db: Session = Depends(get_db)) -> dict[str, Any]:
        actor, _ = _user_or_401(request, db)
        _require_permission(actor, "operations.read")
        return {"items": [sanitized_task_view(task) for task in db.scalars(select(QueryTask).order_by(QueryTask.created_at.desc()).limit(200)).all()]}

    from .governance_api import register_governance_routes
    from .runtime_api import register_runtime_routes
    from .execution_api import register_execution_routes
    from .runtime_gate import register_context_tools
    from .execution import ENABLED_TOOLS, model_tool_results, scope_resolution_is_valid
    register_context_tools(ENABLED_TOOLS, model_tool_results, scope_resolution_validator=scope_resolution_is_valid)
    register_governance_routes(app)
    from .lifecycle_api import register_lifecycle_routes
    register_lifecycle_routes(app)
    register_runtime_routes(app)
    register_execution_routes(app)
    from .service_api import register_service_routes
    register_service_routes(app)
    from .service_use_api import register_service_use_routes
    register_service_use_routes(app)
    from .feedback_api import register_feedback_routes
    register_feedback_routes(app)
    from .workspace_api import register_workspace_routes
    register_workspace_routes(app)
    from .index_api import register_index_routes
    from .evidence_api import register_evidence_routes
    register_index_routes(app)
    register_evidence_routes(app)
    return app


app = create_app()
