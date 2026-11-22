"""Persistent, isolated local pilot runtime entry point.

The pilot manifest is non-secret. PostgreSQL credentials are read from the
named local Docker container into process memory and are never written here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus

from .config import Settings

ROOT = Path(__file__).resolve().parents[2]
LIVE_ROOT = ROOT / "outputs" / "pilot-runtime" / "2026-11-19" / "live"
DEFAULT_CONFIG = LIVE_ROOT / "pilot.json"
CONTAINER = "dc-local-pgvector-20261117"
DB_PREFIX = "dc_local_pilot_20261119_"
MODEL_DIR = ROOT / "ai_models" / "modelscope_cache" / "models" / "models" / "BAAI--bge-m3" / "snapshots" / "master"
TOKENIZER = MODEL_DIR / "tokenizer.json"


def _container_env() -> dict[str, str]:
    """Return the local PostgreSQL container environment without logging it."""
    result = subprocess.run(["docker", "inspect", "--format", "{{json .Config.Env}}", CONTAINER],
        check=True, capture_output=True, text=True, timeout=10)
    rows = json.loads(result.stdout)
    values = dict(item.split("=", 1) for item in rows if "=" in item)
    if not values.get("POSTGRES_USER") or not values.get("POSTGRES_PASSWORD"):
        raise RuntimeError("Local PostgreSQL container lacks required credentials")
    return values


def _database_url(config: dict) -> str:
    if config.get("container") != CONTAINER:
        raise ValueError("Pilot config is not bound to the approved local database container")
    name = config.get("database_name", "")
    if not re.fullmatch(re.escape(DB_PREFIX) + r"[0-9a-f]{32}", name):
        raise ValueError("Pilot database name is outside the isolated pilot namespace")
    env = _container_env()
    user, password = quote_plus(env["POSTGRES_USER"]), quote_plus(env["POSTGRES_PASSWORD"])
    host, port = config.get("host"), config.get("port")
    if host != "127.0.0.1" or port != 55440:
        raise ValueError("Pilot database endpoint differs from the approved loopback mapping")
    return f"postgresql+psycopg://{user}:{password}@127.0.0.1:55440/{name}"


def _read_config(config_path: Path | str = DEFAULT_CONFIG) -> tuple[dict, Path]:
    path = Path(config_path).resolve()
    if not path.is_relative_to(LIVE_ROOT.resolve()):
        raise ValueError("Pilot config must remain under outputs/pilot-runtime/2026-11-19/live")
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema") != "dc-pilot-runtime/1" or data.get("synthetic_pilot") is not True:
        raise ValueError("Missing explicit synthetic pilot configuration")
    return data, path


def database_url_for_config(config_path: Path | str = DEFAULT_CONFIG) -> str:
    """Load the pilot PostgreSQL URL in memory; callers must never log it."""
    config, _ = _read_config(config_path)
    return _database_url(config)


def load_settings(config_path: Path | str = DEFAULT_CONFIG) -> Settings:
    config, path = _read_config(config_path)
    storage = Path(config["storage_dir"]).resolve()
    if not storage.is_relative_to(LIVE_ROOT.resolve()):
        raise ValueError("Pilot storage must remain under the isolated live root")
    model_dir = Path(config.get("local_bge_model_dir", str(MODEL_DIR))).resolve()
    os.environ["DATACENTER_LOCAL_BGE_MODEL_DIR"] = str(model_dir)
    tokenizer = config.get("local_bge_tokenizer") or (str(TOKENIZER.resolve()) if TOKENIZER.is_file() else None)
    if tokenizer:
        os.environ["DATACENTER_BGE_TOKENIZER_PATH"] = tokenizer
    else:
        os.environ.pop("DATACENTER_BGE_TOKENIZER_PATH", None)
    endpoint = config.get("bailian_endpoint")
    if endpoint:
        os.environ["DATACENTER_BAILIAN_SERVICE_ENDPOINT"] = json.dumps(endpoint, separators=(",", ":"))
    else:
        os.environ.pop("DATACENTER_BAILIAN_SERVICE_ENDPOINT", None)
    settings = Settings(database_url=_database_url(config), storage_dir=storage,
        local_bge_model_dir=model_dir,
        local_bge_tokenizer=Path(tokenizer) if tokenizer else None,
        local_mineru_executable=Path(config["local_mineru_executable"]).resolve() if config.get("local_mineru_executable") else Settings.local_mineru_executable,
        local_mineru_home=Path(config["local_mineru_home"]).resolve() if config.get("local_mineru_home") else Settings.local_mineru_home,
        query_service_mode="selected_service", query_max_inflight=4)
    settings.ensure_dirs()
    return settings


def load_live_settings() -> Settings:
    """Convenience adapter for backup/restore and scheduled pilot workers."""
    return load_settings(DEFAULT_CONFIG)


def init_pilot(config_path: Path | str = DEFAULT_CONFIG, *, synthetic_pilot: bool = False,
               bailian_peer: str | None = None, bailian_ca: Path | None = None) -> dict:
    if not synthetic_pilot:
        raise ValueError("Initialization requires the explicit --synthetic-pilot opt-in")
    path = Path(config_path).resolve()
    if not path.is_relative_to(LIVE_ROOT.resolve()):
        raise ValueError("Pilot config must remain under outputs/pilot-runtime/2026-11-19/live")
    if path.exists():
        raise FileExistsError(f"Pilot config already exists: {path}")
    LIVE_ROOT.mkdir(parents=True, exist_ok=True)
    import uuid
    if bool(bailian_peer) != bool(bailian_ca):
        raise ValueError("--bailian-peer and --bailian-ca must be supplied together")
    endpoint = None
    if bailian_peer:
        import ipaddress, ssl
        address = ipaddress.ip_address(bailian_peer)
        if not isinstance(address, ipaddress.IPv4Address) or not address.is_global:
            raise ValueError("Bailian peer must be a globally routable IPv4 address")
        ca_path = bailian_ca.resolve(strict=True)
        context = ssl.create_default_context(cafile=str(ca_path))
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        endpoint = {"peer_address": str(address), "tls_ca_file": str(ca_path)}
    config = {"schema": "dc-pilot-runtime/1", "synthetic_pilot": True,
        "created_at": __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
        "container": CONTAINER, "host": "127.0.0.1", "port": 55440,
        "database_name": DB_PREFIX + uuid.uuid4().hex,
        "storage_dir": str((LIVE_ROOT / "storage").resolve()),
        "local_bge_model_dir": str(MODEL_DIR.resolve()), "local_bge_tokenizer": str(TOKENIZER.resolve()) if TOKENIZER.is_file() else None,
        "bailian_endpoint": endpoint,
        "credentials_file": str((path.parent / "credentials.dpapi").resolve()),
        "synthetic_allowlist": None, "setup_stage": "config_created"}
    credential_values = {key: {"username": username, "password": secrets.token_urlsafe(24)} for key, username in
        (("admin", "pilot-admin"), ("knowledge_manager", "pilot-km"), ("employee", "pilot-employee"))}
    Path(config["credentials_file"]).write_bytes(_protect_credential_bytes(json.dumps(credential_values, separators=(",", ":")).encode("utf-8")))
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    url = _database_url(config)
    from sqlalchemy import create_engine, text
    from .db import build_engine, init_db
    # Create a brand-new database using the explicit, fixed pilot identifier.
    admin_url = url.rsplit("/", 1)[0] + "/postgres"
    admin = create_engine(admin_url, isolation_level="AUTOCOMMIT", future=True, pool_pre_ping=True)
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{config["database_name"]}"'))
    finally:
        admin.dispose()
    settings = Settings(database_url=url, storage_dir=Path(config["storage_dir"]),
        local_bge_model_dir=Path(config["local_bge_model_dir"]),
        local_bge_tokenizer=Path(config["local_bge_tokenizer"]) if config.get("local_bge_tokenizer") else None,
        query_service_mode="selected_service")
    engine = build_engine(url)
    try:
        init_db(engine, settings)
    finally:
        engine.dispose()
    config["setup_stage"] = "schema_ready"
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    _initialize_synthetic_pilot(path, config, settings)
    return {"config": str(path), "database_name": config["database_name"], "storage_dir": config["storage_dir"], "schema": "migrated"}


def _protect_credential_bytes(raw: bytes) -> bytes:
    if os.name != "nt":
        raise RuntimeError("Pilot credentials require Windows DPAPI encryption")
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    data_buf = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
    source = Blob(len(raw), data_buf)
    target = Blob()
    protect = ctypes.windll.crypt32.CryptProtectData
    protect.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    protect.restype = wintypes.BOOL
    if not protect(ctypes.byref(source), "Data Center pilot credentials", None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)


def resume_pilot(config_path: Path | str = DEFAULT_CONFIG) -> dict:
    config, path = _read_config(config_path)
    if config.get("setup_stage") in {"sample_ready", "query_ready"}:
        if not config.get("source_setup_complete"):
            config["source_setup_complete"] = True
            path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return {"config": str(path), "database_name": config["database_name"], "stage": "sample_ready"}
    url = _database_url(config)
    import psycopg
    from psycopg import sql
    from sqlalchemy.engine import make_url
    parsed = make_url(url)
    admin_url = parsed.set(database="postgres").render_as_string(hide_password=False).replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(admin_url, autocommit=True) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (config["database_name"],)).fetchone()
        if not exists:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(config["database_name"])))
    settings = Settings(database_url=url, storage_dir=Path(config["storage_dir"]),
        local_bge_model_dir=Path(config["local_bge_model_dir"]),
        local_bge_tokenizer=Path(config["local_bge_tokenizer"]) if config.get("local_bge_tokenizer") else None,
        query_service_mode="selected_service")
    from .db import build_engine, init_db
    engine = build_engine(url)
    try:
        init_db(engine, settings)
    finally:
        engine.dispose()
    _initialize_synthetic_pilot(path, config, settings)
    return {"config": str(path), "database_name": config["database_name"],
            "stage": "sample_ready" if config.get("synthetic_allowlist") else "schema_ready"}


def load_credentials(config_path: Path | str = DEFAULT_CONFIG) -> dict[str, dict[str, str]]:
    """Decrypt credentials for the current Windows user; never logs them."""
    config, path = _read_config(config_path)
    encrypted = Path(config["credentials_file"])
    if not encrypted.is_relative_to(path.parent) or not encrypted.is_file() or os.name != "nt":
        raise RuntimeError("Pilot credential file is unavailable for this Windows user")
    import ctypes
    from ctypes import wintypes
    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]
    raw = encrypted.read_bytes()
    data_buf = (ctypes.c_byte * len(raw)).from_buffer_copy(raw)
    source = Blob(len(raw), data_buf)
    target, description = Blob(), wintypes.LPWSTR()
    unprotect = ctypes.windll.crypt32.CryptUnprotectData
    unprotect.argtypes = [ctypes.POINTER(Blob), ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
    unprotect.restype = wintypes.BOOL
    if not unprotect(ctypes.byref(source), ctypes.byref(description), None, None, None, 1, ctypes.byref(target)):
        raise ctypes.WinError()
    try:
        return json.loads(ctypes.string_at(target.pbData, target.cbData).decode("utf-8"))
    finally:
        ctypes.windll.kernel32.LocalFree(target.pbData)
        if description:
            ctypes.windll.kernel32.LocalFree(description)


def _initialize_synthetic_pilot(config_path: Path, config: dict, settings: Settings) -> None:
    """Create separately authenticated roles and persist a contract parse sample."""
    from sqlalchemy.orm import Session
    from . import governance as governance
    from .db import build_engine
    from .models import (AuthorizationGrant, Document, DocumentVersion, KnowledgeBase, User,
        utc_now)
    from .security import hash_password
    from .services import save_upload, seed_reference_data
    from .testing_governance import seed_verified_approval, seed_confirmation
    from .parse_jobs import accept_local_parse, run_local_parse_job
    from .parsing.ir_projection import normalize_parse_run
    from .parsing.processing import run_source_qc, source_confirmation_scope, source_verification, structure_parse_run
    from .chunk_jobs import accept_chunk_set, run_chunk_job
    from .models import ContentBlock, ParseRun
    from . import pilot_sample

    roles = {"admin": "pilot-admin", "knowledge_manager": "pilot-km", "employee": "pilot-employee"}
    credential_data = load_credentials(config_path)
    passwords = {key: credential_data[key]["password"] for key in roles}
    engine = build_engine(settings.database_url)
    try:
        with Session(engine, expire_on_commit=False, autoflush=False) as db:
            seed_reference_data(db)
            accounts = {}
            for key, username in roles.items():
                role = {"admin": "system_admin", "knowledge_manager": "knowledge_manager", "employee": "employee"}[key]
                user = db.scalar(__import__("sqlalchemy").select(User).where(User.username == username))
                if user is None:
                    user = User(username=username, display_name={"admin": "SYNTHETIC system administrator", "knowledge_manager": "SYNTHETIC knowledge manager", "employee": "SYNTHETIC employee"}[key],
                        status="active", credential_state="active", credential_hash=hash_password(passwords[key]),
                        credential_revision=1, roles=[role])
                    db.add(user)
                    db.flush()
                accounts[key] = user
            admin, km, employee = accounts["admin"], accounts["knowledge_manager"], accounts["employee"]
            kb = db.scalar(__import__("sqlalchemy").select(KnowledgeBase).where(KnowledgeBase.name == "SYNTHETIC pilot knowledge base"))
            if kb is None:
                kb = KnowledgeBase(name="SYNTHETIC pilot knowledge base")
                db.add(kb)
                db.flush()
            scope = {"knowledge_base_id": kb.id, "document_id": None, "level": "internal", "category": "manual", "need_class": "maintenance"}
            for subject, operations in ((km, ("knowledge.manage", "document.read", "knowledge.query")),
                                        (employee, ("document.read", "knowledge.query"))):
                for operation in operations:
                    existing = db.scalar(__import__("sqlalchemy").select(AuthorizationGrant).where(
                        AuthorizationGrant.subject_user_id == subject.id, AuthorizationGrant.knowledge_base_id == kb.id,
                        AuthorizationGrant.operation == operation, AuthorizationGrant.revoked_at.is_(None)))
                    if existing is None:
                        approval = governance.register_approval(db, admin, {"applicant": subject.id, "scope": scope, "operations": [operation]})
                        seed_verified_approval(db, approval)
                        db.add(AuthorizationGrant(subject_user_id=subject.id, knowledge_base_id=kb.id,
                            operation=operation, level="internal", category="manual", need_class="maintenance",
                            approval_id=approval.id, valid_until=approval.valid_until))
            original = pilot_sample.create_pdf()
            doc = db.scalar(__import__("sqlalchemy").select(Document).where(Document.knowledge_base_id == kb.id, Document.title == "SYNTHETIC-1 training manual"))
            version = None
            file = None
            if doc:
                version = db.scalar(__import__("sqlalchemy").select(DocumentVersion).where(DocumentVersion.document_id == doc.id, DocumentVersion.version_no == 1))
                if version:
                    from .models import FileObject
                    file = db.get(FileObject, version.source_file_id)
            if doc is None:
                doc = Document(knowledge_base_id=kb.id, title="SYNTHETIC-1 training manual", access_level="internal", category="manual", need_class="maintenance")
                db.add(doc)
                db.flush()
            if version is None:
                file = save_upload([original], "synthetic-1-training-manual.pdf", "application/pdf", km, settings)
                db.add(file)
                db.flush()
                version = DocumentVersion(document_id=doc.id, version_no=1, model_scope=[pilot_sample.MODEL],
                    source_file_id=file.id, revision_label="SYNTHETIC R1",
                    source_metadata={"channel": "synthetic_pilot", "source_description": "SYNTHETIC non-business training manual"}, created_by=km.id)
                db.add(version)
                db.flush()
            parse_run = db.scalar(__import__("sqlalchemy").select(ParseRun).where(ParseRun.document_version_id == version.id,
                ParseRun.origin == "local").order_by(ParseRun.created_at.desc()))
            if parse_run is None:
                parse_run = accept_local_parse(db, km, version, file, settings)
            parse_id = parse_run.id
            db.commit()
            config["setup_stage"] = "source_registered"
            config["synthetic_allowlist"] = {"source_sha256": file.sha256, "pdf_sha256": file.sha256,
                "knowledge_base_id": kb.id, "document_id": doc.id, "version_id": version.id,
                "parse_run_id": parse_id, "employee_user_id": employee.id,
                "knowledge_manager_user_id": km.id, "admin_user_id": admin.id,
                "model": pilot_sample.MODEL, "query": pilot_sample.QUERY,
                "parse_method": "explicit-contract-synthetic-bundle"}
            credential_data = {key: {"username": roles[key], "password": passwords[key], "user_id": accounts[key].id}
                               for key in roles}
            Path(config["credentials_file"]).write_bytes(_protect_credential_bytes(json.dumps(credential_data, separators=(",", ":")).encode("utf-8")))
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            bundle = pilot_sample.contract_parse_bundle()
            def contract_runner(source, workspace, executable, home, heartbeat):
                target = workspace / "result.zip"
                if not target.exists():
                    target.write_bytes(bundle)
                return target
            run_local_parse_job(db, parse_id, settings, runner=contract_runner)
            normalize_parse_run(db, parse_id, settings)
            structure_parse_run(db, parse_id, settings)
            run_source_qc(db, parse_id, settings)
            parse_run = db.get(ParseRun, parse_id)
            if parse_run.stage_checkpoint.get("source_qc", {}).get("status") != "passed":
                raise RuntimeError("Synthetic contract parse did not pass source quality checks")
            config["setup_stage"] = "source_qc_passed"
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

            chunk_set = accept_chunk_set(db, km, parse_run, "dc.semantic-bge-m3/1")
            db.commit()
            if settings.local_bge_tokenizer is None:
                raise RuntimeError("Fixed BGE-M3 tokenizer is required for synthetic pilot chunks")
            run_chunk_job(db, chunk_set.id, settings)
            db.refresh(chunk_set)
            if chunk_set.status != "ready":
                raise RuntimeError("Synthetic pilot chunk build failed")
            config["setup_stage"] = "chunk_set_ready"
            config["synthetic_allowlist"]["chunk_set_id"] = chunk_set.id
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            leaves = db.scalars(__import__("sqlalchemy").select(ContentBlock).where(
                ContentBlock.parse_run_id == parse_id, ContentBlock.structure_role == "leaf")).all()
            for block in leaves:
                if block.review_status != "approved":
                    governance.review_content_block(db, km, parse_run, block, "approved", "SYNTHETIC contract content review")
            scope_binding = {**governance.source_binding(db, version), **source_confirmation_scope(db, parse_run)}
            from .governance_models import ReviewSet
            review_set = db.scalar(__import__("sqlalchemy").select(ReviewSet).where(ReviewSet.chunk_set_id == chunk_set.id).order_by(ReviewSet.reviewed_at.desc()))
            if not review_set:
                professional = seed_confirmation(db, km, version, "key_content", "accepted", scope_binding)
                review_set = governance.create_review_set(db, km, version, {"parse_run_id": parse_id, "chunk_set_id": chunk_set.id,
                    "reviewed_block_refs": [{"id": row.id, "revision": row.revision, "content_hash": governance.block_binding(row)} for row in leaves],
                    "professional_confirmation_refs": [professional.id], "reason": "SYNTHETIC contract review"})
            doc = db.get(Document, doc.id)
            binding = governance.source_binding(db, version)
            if not governance.publication_is_current(db, version):
                seed_confirmation(db, km, version, "applicability", "applicable", binding)
                seed_confirmation(db, km, version, "classification", "classified", {**binding, "access_level": doc.access_level,
                    "category": doc.category, "need_class": doc.need_class, "rule_version": "synthetic-pilot-v1"})
                governance.select_source_candidate(db, km, version, {"chunk_set_id": chunk_set.id, "review_set_id": review_set.id})
                preview = governance.create_publication_preview(db, km, doc, version, {"action": "publish"})
                if not preview.allowed:
                    raise RuntimeError("Synthetic publication was denied by current governance: " + ",".join(preview.blocked_reasons))
                governance.publish_from_preview(db, km, doc, version, {"preview_id": preview.id})
            db.commit()
            if source_verification(db, parse_run)["state"] != "verified" or not governance.publication_is_current(db, version):
                raise RuntimeError("Synthetic pilot source is not fully verified and published")
            source_sha = file.sha256
            config["synthetic_allowlist"] = {"source_sha256": source_sha, "knowledge_base_id": kb.id,
                "document_id": doc.id, "version_id": version.id, "parse_run_id": parse_id,
                "chunk_set_id": chunk_set.id, "employee_user_id": employee.id,
                "knowledge_manager_user_id": km.id, "admin_user_id": admin.id,
                "model": pilot_sample.MODEL, "query": pilot_sample.QUERY,
                "pdf_sha256": source_sha, "parse_method": "explicit-contract-synthetic-bundle"}
            config["setup_stage"] = "sample_ready"
            config["source_setup_complete"] = True
            encrypted_path = Path(config["credentials_file"])
            encrypted_path.write_bytes(_protect_credential_bytes(json.dumps(credential_data, separators=(",", ":")).encode("utf-8")))
            config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    finally:
        engine.dispose()


def status(config_path: Path | str = DEFAULT_CONFIG) -> dict:
    config, path = _read_config(config_path)
    settings = load_settings(path)
    from .db import build_engine
    from sqlalchemy import inspect, text
    engine = build_engine(settings.database_url)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            tables = inspect(conn).get_table_names()
        from types import SimpleNamespace
        readiness = _readiness(path, SimpleNamespace(state=SimpleNamespace(engine=engine)))
        ready_stage = config.get("setup_stage") in {"sample_ready", "active_index_ready", "query_ready"} or bool(config.get("source_setup_complete"))
        return {"status": "setup_ready" if ready_stage else "setup_incomplete",
                "query_ready": not readiness, "query_blockers": readiness,
                "config": str(path), "database_name": config["database_name"],
                "storage_dir": str(settings.storage_dir), "table_count": len(tables),
                "allowlist_configured": bool(config.get("synthetic_allowlist", {}).get("source_sha256")),
                "setup_stage": config.get("setup_stage", "unknown")}
    finally:
        engine.dispose()


def query(config_path: Path | str = DEFAULT_CONFIG, *, base_url: str = "http://127.0.0.1:8000",
          text: str | None = None, timeout_seconds: int = 90) -> dict:
    """Submit one allowlisted synthetic question and poll with read-only GETs."""
    from http.cookiejar import CookieJar
    from urllib.parse import urlsplit
    from urllib.request import HTTPCookieProcessor, ProxyHandler, Request, build_opener
    import time
    import uuid
    config, _ = _read_config(config_path)
    sample = config.get("synthetic_allowlist") or {}
    prompt = text if text is not None else sample.get("query")
    if not sample or prompt != sample.get("query"):
        raise ValueError("This synthetic CLI accepts only the fixed question in this pilot's explicit allowlist")
    address = urlsplit(base_url)
    if address.scheme != "http" or address.hostname not in {"127.0.0.1", "localhost"} or address.port is None or address.username:
        raise ValueError("Pilot credentials are sent only to an explicit loopback HTTP server")
    credentials = load_credentials(config_path)["employee"]
    jar = CookieJar()
    client = build_opener(ProxyHandler({}), HTTPCookieProcessor(jar))
    origin = f"http://{address.netloc}"

    def request(path: str, *, method="GET", payload=None, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request_headers = dict(headers or {})
        if body is not None:
            request_headers["Content-Type"] = "application/json"
        req = Request(origin + path, data=body, headers=request_headers, method=method)
        with client.open(req, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))

    login = request("/auth/login", method="POST", payload={"username": credentials["username"], "password": credentials["password"]})
    csrf = login.get("csrf_token")
    if not csrf:
        raise RuntimeError("Pilot login did not return a CSRF token")
    created = request("/knowledge/queries", method="POST", payload={"service_mode": "selected_service", "query": prompt,
        "knowledge_base_ids": [sample["knowledge_base_id"]], "model": sample["model"], "mode": "answer"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid.uuid4())})
    task_id = created.get("task_id")
    if not task_id:
        raise RuntimeError("Pilot query was not accepted")
    deadline = time.monotonic() + timeout_seconds
    last = created
    while time.monotonic() < deadline:
        last = request(f"/tasks/{task_id}")
        if last.get("status") in {"succeeded", "failed", "blocked", "waiting_input", "reconciliation_required", "cancelled"}:
            return {"task_id": task_id, "query": prompt, "status": last.get("status"), "task": last}
        time.sleep(.5)
    return {"task_id": task_id, "query": prompt, "status": "still_running", "task": last}


def _pilot_source_is_current(config: dict, settings: Settings) -> bool:
    """Validate the persisted exact synthetic source, independent of phase labels."""
    sample = config.get("synthetic_allowlist") or {}
    required = {"source_sha256", "knowledge_base_id", "document_id", "version_id", "parse_run_id", "chunk_set_id", "model"}
    if not required.issubset(sample):
        return False
    import hashlib
    from sqlalchemy.orm import Session
    from .db import build_engine
    from .governance import publication_is_current
    from .models import ChunkSet, Document, DocumentVersion, FileObject, KnowledgeBase, ParseRun
    from .services import file_path

    engine = build_engine(settings.database_url)
    try:
        with Session(engine) as db:
            kb = db.get(KnowledgeBase, sample["knowledge_base_id"])
            document = db.get(Document, sample["document_id"])
            version = db.get(DocumentVersion, sample["version_id"])
            run = db.get(ParseRun, sample["parse_run_id"])
            chunk_set = db.get(ChunkSet, sample["chunk_set_id"])
            source = db.get(FileObject, version.source_file_id) if version else None
            if not all((kb, document, version, run, chunk_set, source)):
                return False
            if (kb.status != "active" or document.knowledge_base_id != kb.id or document.title != "SYNTHETIC-1 training manual"
                    or version.document_id != document.id or version.publish_status != "effective"
                    or version.model_scope != [sample["model"]]
                    or version.source_metadata.get("channel") != "synthetic_pilot"
                    or version.source_file_id != source.id or source.sha256 != sample["source_sha256"]
                    or source.status != "available" or source.storage_name != "local" or source.mime_type != "application/pdf"
                    or run.document_version_id != version.id or run.status != "succeeded"
                    or run.stage_checkpoint.get("source_qc", {}).get("status") != "passed"
                    or chunk_set.parse_run_id != run.id or chunk_set.status != "ready"
                    or version.selected_chunk_set_id != chunk_set.id
                    or not publication_is_current(db, version, historical=False)):
                return False
            path = file_path(source, settings)
            if not path.is_file():
                return False
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest() == sample["source_sha256"]
    finally:
        engine.dispose()


def configure(config_path: Path | str = DEFAULT_CONFIG) -> dict:
    """Admit both local providers and activate an actual BGE child generation.

    HTTP service admission/selection and index acceptance use the production
    FastAPI routes. Only the fixed synthetic input attestation and existing
    worker function are invoked in-process as trusted local harness seams.
    """
    config, path = _read_config(config_path)
    import uuid
    sample = config.get("synthetic_allowlist") or {}
    if not sample or not sample.get("chunk_set_id"):
        raise RuntimeError("Run pilot setup and publish the explicit synthetic source before provider configuration")
    if not config.get("bailian_endpoint"):
        raise RuntimeError("Pilot config needs a reviewed public Bailian peer and CA certificate")
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    os.environ.setdefault("MKL_NUM_THREADS", "2")
    settings = load_settings(path)
    if not _pilot_source_is_current(config, settings):
        raise RuntimeError("The allowlisted synthetic source is missing, changed, unpublished, or no longer current")
    from fastapi.testclient import TestClient
    from sqlalchemy import select
    from sqlalchemy.orm import Session
    from .app import create_app
    from .models import IndexGeneration, KnowledgeBase, utc_now
    from .runtime_gate import create_local_profile
    from .runtime_models import RuntimeProfile
    from .service_admission import BAILIAN_ENDPOINT_REF, BAILIAN_MODEL
    from .service_admission import binding as service_binding
    from .service_models import ServiceProfile, ServiceRevision, ServiceTestRun, ServiceRequestCall
    from .service_use_models import ServiceSelection, SyntheticServiceInput
    from .testing_service_use import attest_synthetic_input

    application = create_app(settings)
    credentials = load_credentials(path)
    config.setdefault("service_profiles", {})
    config.setdefault("setup_stage", "sample_ready")

    def save():
        path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def use_client(role: str):
        client = TestClient(application)
        client.__enter__()
        response = client.post("/auth/login", json={"username": credentials[role]["username"], "password": credentials[role]["password"]})
        if response.status_code != 200:
            client.close()
            raise RuntimeError("Pilot service configuration login failed")
        client.headers["X-CSRF-Token"] = response.json()["csrf_token"]
        return client

    def send(client, method, endpoint, payload=None, *, key=None, etag=None):
        headers = {}
        if key:
            headers["Idempotency-Key"] = key
        if etag:
            headers["If-Match"] = etag
        response = client.request(method, endpoint, json=payload, headers=headers)
        if response.status_code < 200 or response.status_code >= 300:
            try:
                error = response.json().get("code") or response.json().get("detail", "request rejected")
            except ValueError:
                error = "request rejected"
            raise RuntimeError(f"Pilot configuration API {method} {endpoint} returned {response.status_code}: {error}")
        return response.json()

    def ensure_service(client, *, kind, label, endpoint_ref, adapter, model, purpose):
        listing = send(client, "GET", "/admin/service-profiles")
        profile = next((row for row in listing["items"] if row["label"] == label and row["kind"] == kind), None)
        if profile is None:
            profile = send(client, "POST", "/admin/service-profiles", {"kind": kind, "label": label},
                key=f"pilot-profile-{kind}-20261119")
        detail = send(client, "GET", f"/admin/service-profiles/{profile['id']}")
        matching = [row for row in detail["revisions"] if row["endpoint_ref"] == endpoint_ref and row["adapter"] == adapter and row["model"] == model]
        if matching:
            revision = matching[-1]
        else:
            revision = send(client, "POST", f"/admin/service-profiles/{profile['id']}/revisions",
                {"endpoint_ref": endpoint_ref, "adapter": adapter, "model": model},
                key=f"pilot-revision-{kind}-{endpoint_ref}-{adapter}-{model}", etag=detail["etag"])
            detail = send(client, "GET", f"/admin/service-profiles/{profile['id']}")

        pending_tests = config.setdefault("pending_service_tests", {})
        pending = pending_tests.get(purpose)
        with application.state.session_factory() as db:
            revision_row = db.get(ServiceRevision, revision["id"])
            bound = service_binding(revision_row, application.state.service_endpoints)
            test_rows = db.scalars(select(ServiceTestRun).where(ServiceTestRun.revision_id == revision["id"])
                .order_by(ServiceTestRun.completed_at, ServiceTestRun.id)).all()
            test_calls = db.scalars(select(ServiceRequestCall).join(ServiceTestRun,
                ServiceRequestCall.test_run_id == ServiceTestRun.id).where(ServiceTestRun.revision_id == revision["id"])).all()
            bound_tests = [row for row in test_rows if row.binding_hash == bound]
            unresolved = any(row.state in {"pending", "running", "awaiting_remote", "unknown", "reconciliation_required"} for row in test_rows)
            unresolved = unresolved or any(row.state in {"dispatching", "unknown"} for row in test_calls)
            successful = [row for row in bound_tests if row.state == "succeeded"]
            current_test = successful[-1] if successful else None
            if pending and pending.get("revision_id") == revision["id"]:
                prior_receipts = [row for row in test_rows if row.binding_hash == pending.get("binding_hash")]
                if (not prior_receipts or any(row.state not in {"succeeded", "failed"} for row in prior_receipts)
                        or any(row.state in {"dispatching", "unknown"} for row in test_calls)):
                    raise RuntimeError(f"{purpose} has an unconfirmed earlier test intent; no new idempotency key was generated")
                # A known terminal receipt closes a prior interrupted local
                # checkpoint. Only then may a changed binding start a fresh test.
                pending_tests.pop(purpose, None)
                save()
            if bound_tests and current_test is None:
                states = ",".join(row.state for row in bound_tests)
                raise RuntimeError(f"{purpose} current-binding self-test is {states}; it will not be replayed")
            if current_test and (not current_test.valid_until or current_test.valid_until <= utc_now()):
                raise RuntimeError(f"{purpose} current-binding self-test expired; automatic provider retest is disabled")
            if not current_test and unresolved:
                raise RuntimeError(f"{purpose} has an unsettled earlier provider call; new-binding test is disabled")
            test = ({"id": current_test.id, "state": current_test.state,
                     "valid_until": current_test.valid_until.isoformat()} if current_test else None)

        if not test:
            # Persist intent before the only provider POST. An interrupted
            # request with no receipt is reconciled manually, never replayed.
            key = f"pilot-self-test-{revision['id']}-{bound}"
            pending_tests[purpose] = {"revision_id": revision["id"], "binding_hash": bound, "idempotency_key": key}
            save()
            test = send(client, "POST", f"/admin/service-profiles/{profile['id']}/revisions/{revision['id']}/test-runs",
                {"fixture_id": "service-synthetic-v1"}, key=key)
            with application.state.session_factory() as db:
                saved_test = db.get(ServiceTestRun, test["id"])
                if not saved_test or saved_test.binding_hash != bound:
                    raise RuntimeError(f"{purpose} self-test receipt is not bound to the current source fingerprint")
            if test["state"] != "succeeded" or not test.get("valid_until"):
                raise RuntimeError(f"{purpose} service self-test is {test['state']}; it will not be resent automatically")
            pending_tests.pop(purpose, None)
            config["setup_stage"] = f"{purpose}_self_test_recorded"
            config.setdefault("service_profiles", {})[purpose] = {"profile_id": profile["id"], "revision_id": revision["id"],
                "test_run_id": test["id"], "binding_hash": bound}
            save()
        if profile.get("active_revision_id") != revision["id"] or not profile.get("enabled"):
            preview = send(client, "POST", f"/admin/service-profiles/{profile['id']}/change-preview",
                {"action": "activate", "revision_id": revision["id"], "reason": "SYNTHETIC pilot provider admission"},
                key=f"pilot-activate-preview-{revision['id']}-{uuid.uuid4().hex}", etag=detail["etag"])
            send(client, "POST", f"/admin/service-profiles/{profile['id']}/activate",
                {"preview_id": preview["preview_id"], "reason": "SYNTHETIC pilot provider admission"},
                key=f"pilot-activate-{preview['preview_id']}", etag=preview["profile_etag"])
        selections = send(client, "GET", "/admin/service-selections")
        selected = next(item for item in selections["items"] if item["purpose"] == purpose)
        if (not selected["enabled"] or selected["revision_id"] != revision["id"]
                or selected["binding_hash"] != bound or selected["test_run_id"] != test["id"]):
            preview = send(client, "POST", f"/admin/service-selections/{purpose}/previews",
                {"action": "select", "revision_id": revision["id"], "reason": "SYNTHETIC pilot purpose selection"},
                key=f"pilot-select-preview-{purpose}-{test['id']}-{uuid.uuid4().hex}", etag=selected["etag"])
            selected = send(client, "POST", f"/admin/service-selections/{purpose}/select",
                {"preview_id": preview["id"], "reason": "SYNTHETIC pilot purpose selection"},
                key=f"pilot-select-{preview['id']}", etag=selected["etag"])
        config.setdefault("service_profiles", {})[purpose] = {"profile_id": profile["id"], "revision_id": revision["id"],
            "test_run_id": test["id"], "binding_hash": bound, "endpoint_ref": endpoint_ref, "model": model}
        config["setup_stage"] = purpose + "_selected"
        save()
        return revision["id"]

    admin = use_client("admin")
    try:
        answer_revision = ensure_service(admin, kind="generation", label="SYNTHETIC pilot Bailian Qwen 3.8 Flash",
            endpoint_ref=BAILIAN_ENDPOINT_REF, adapter="openai-chat-v1", model=BAILIAN_MODEL, purpose="query.answer")
        from . import local_bge
        embedding_revision = ensure_service(admin, kind="embedding", label="SYNTHETIC pilot local BGE-M3",
            endpoint_ref=local_bge.ENDPOINT_REF, adapter=local_bge.ADAPTER, model=local_bge.MODEL, purpose="document.embed")
    finally:
        admin.close()

    # The KM builds and accepts each durable batch through application HTTP;
    # only the exact synthetic input assertion is issued by the trusted harness.
    km = use_client("knowledge_manager")
    try:
        generation_id = config.get("index_generation_id")
        if not generation_id:
            built = send(km, "POST", f"/knowledge-bases/{sample['knowledge_base_id']}/index-generations",
                {"embedding_profile_id": embedding_revision}, key=f"pilot-index-generation-{sample['knowledge_base_id']}-{embedding_revision}")
            generation_id = built["index_generation_id"]
            config["index_generation_id"] = generation_id
            config["setup_stage"] = "index_generation_created"
            save()

        generation = send(km, "GET", f"/knowledge-bases/{sample['knowledge_base_id']}/index-generations/{generation_id}")
        if generation["status"] == "building":
            from .embedding_jobs import ChildEmbeddingBatch, child_batch_payload, run_child_embedding_batch
            from .models import User
            for batch in generation.get("embedding_batches", []):
                number, job_id = batch["batch_no"], batch.get("id")
                if batch["state"] == "succeeded":
                    continue
                if batch["state"] == "not_accepted":
                    with application.state.session_factory() as db:
                        manifest = db.get(IndexGeneration, generation_id).build_report["manifest"]
                        payload_bytes = child_batch_payload(db, manifest, number)[2]
                        existing = db.scalar(select(SyntheticServiceInput).where(
                            SyntheticServiceInput.actor_id == sample["knowledge_manager_user_id"],
                            SyntheticServiceInput.purpose == "document.embed",
                            SyntheticServiceInput.resource_key == f"index:{generation_id}:{number}",
                            SyntheticServiceInput.revoked.is_(False)))
                        from .testing_service_use import digest as input_digest
                        if (not existing or existing.input_hash != input_digest(payload_bytes)
                                or existing.expires_at <= utc_now()):
                            attest_synthetic_input(db, sample["knowledge_manager_user_id"], "document.embed",
                                f"index:{generation_id}:{number}", payload_bytes)
                            db.commit()
                    accepted = send(km, "POST", f"/knowledge-bases/{sample['knowledge_base_id']}/index-generations/{generation_id}/embedding-batches/{number}",
                        {}, key=f"pilot-index-batch-{generation_id}-{number}")
                    job_id = accepted["embedding_job_id"]
                with application.state.session_factory() as db:
                    durable_job = db.get(ChildEmbeddingBatch, job_id) if job_id else None
                    job_state, attempt = (durable_job.state, durable_job.attempt) if durable_job else (None, None)
                if (not job_id or not durable_job or attempt > 0 or job_state != "pending"
                        or batch["state"] not in {"not_accepted", "pending"}):
                    raise RuntimeError(f"Embedding batch {number} is unsettled; no automatic retry was attempted")
                with application.state.session_factory() as db:
                    job = run_child_embedding_batch(db, job_id, application.state.service_endpoints)
                    if job.state != "succeeded":
                        raise RuntimeError(f"Local BGE batch {number} stopped with {job.error_code or job.state}")
                config["setup_stage"] = f"embedding_batch_{number}_succeeded"
                save()
            generation = send(km, "GET", f"/knowledge-bases/{sample['knowledge_base_id']}/index-generations/{generation_id}")
        if generation["status"] != "ready" or generation.get("integrity", {}).get("status") != "passed":
            raise RuntimeError("Full BGE index generation did not reach complete ready integrity")

        current = send(km, "GET", f"/knowledge-bases/{sample['knowledge_base_id']}/index-status")
        if current.get("active_generation_id") != generation_id:
            send(km, "PUT", f"/knowledge-bases/{sample['knowledge_base_id']}/active-index",
                {"generation_id": generation_id, "expected_active_id": current.get("active_generation_id"),
                 "expected_source_revision": current["content_revision"], "action": "activate"},
                key=f"pilot-index-activate-{generation_id}", etag=current["etag"])
        config["setup_stage"] = "active_index_ready"
        save()
    finally:
        km.close()

    # A runtime profile is immutable and binds this exact selected Bailian
    # revision, SDK artifacts, tools, and request budget.
    from .worker import runtime_artifacts
    manifest = runtime_artifacts()
    latest = config.get("pi_profile_revision", 0)
    with application.state.session_factory() as db:
        selected = db.get(ServiceSelection, "query.answer")
        if not selected or not selected.enabled or not selected.revision_id:
            raise RuntimeError("Bailian purpose selection is unavailable for runtime profile binding")
        target_revision = max(1, int(latest or 0))
        from sqlalchemy import func
        persisted_max = db.scalar(select(func.max(RuntimeProfile.revision)).where(RuntimeProfile.id == "local-pi")) or 0
        last_candidate = max(target_revision, persisted_max) + 1
        while target_revision <= last_candidate:
            try:
                create_local_profile(db, artifact_manifest_hash=manifest["artifact_manifest_hash"],
                    adapter_fingerprint=manifest["adapter_fingerprint"], artifact_manifest=manifest["manifest"],
                    profile_id=config.get("pi_profile_id", "local-pi"), revision=target_revision,
                    generation_revision_id=selected.revision_id, max_calls=8, max_reserved_tokens=262144)
                break
            except ValueError as error:
                # Runtime profiles are immutable. Only this exact conflict
                # means the persisted revision belongs to an older SDK,
                # artifact, tool set, or provider-binding fingerprint.
                if str(error) != "Profile revision cannot be changed":
                    raise
                target_revision += 1
        else:
            raise RuntimeError("Could not allocate a new immutable runtime profile revision")
    config["pi_profile_id"] = "local-pi"
    config["pi_profile_revision"] = target_revision
    config["setup_stage"] = "query_ready"
    save()
    application.state.engine.dispose()
    return {"status": "query_ready", "database_name": config["database_name"],
        "bailian_revision_id": answer_revision, "embedding_revision_id": embedding_revision,
        "index_generation_id": generation_id, "pi_profile_id": "local-pi", "pi_profile_revision": target_revision,
        "calls_per_query": 8, "max_output_tokens": 2048}


def _readiness(config_path: Path | str, app) -> list[str]:
    config, _ = _read_config(config_path)
    sample = config.get("synthetic_allowlist") or {}
    required_sample = {"source_sha256", "knowledge_base_id", "version_id", "parse_run_id", "chunk_set_id", "employee_user_id"}
    missing = sorted(required_sample - sample.keys())
    if missing:
        return ["synthetic_allowlist:" + ",".join(missing)]
    from sqlalchemy.orm import Session
    from .models import DocumentVersion, FileObject, IndexGeneration, KnowledgeBase, ParseRun, utc_now
    from .runtime_models import RuntimeProfile
    from .service_models import ServiceRevision, ServiceTestRun
    from .service_use_models import ServiceSelection
    from .service_admission import binding as service_binding, ProviderError
    from .governance import publication_is_current
    issues = []
    with Session(app.state.engine) as db:
        version = db.get(DocumentVersion, sample["version_id"])
        parse = db.get(ParseRun, sample["parse_run_id"])
        file = db.get(FileObject, version.source_file_id) if version else None
        kb = db.get(KnowledgeBase, sample["knowledge_base_id"])
        if (not version or not parse or not file or file.sha256 != sample["source_sha256"]
                or parse.document_version_id != version.id or version.publish_status != "effective"
                or parse.status != "succeeded" or version.selected_chunk_set_id != sample["chunk_set_id"]
                or not publication_is_current(db, version, historical=False)):
            issues.append("source:unpublished_or_changed")
        profile_revision = int(config.get("pi_profile_revision", 1))
        runtime_profile = db.get(RuntimeProfile, (config.get("pi_profile_id", "local-pi"), profile_revision))
        if not runtime_profile:
            issues.append("runtime_profile:missing")
        active_embedding_revision = None
        active_answer_revision = None
        active_answer_binding = None
        for purpose, expected_ref in (("query.answer", "bailian-beijing-qwen38-flash"),
                                      ("document.embed", "local-bge-m3")):
            selected = db.get(ServiceSelection, purpose)
            revision = db.get(ServiceRevision, selected.revision_id) if selected and selected.revision_id else None
            test = db.get(ServiceTestRun, selected.test_run_id) if selected and selected.test_run_id else None
            try:
                current_binding = service_binding(revision, app.state.service_endpoints) if revision else None
            except ProviderError:
                current_binding = None
            if (not selected or not selected.enabled or not revision or revision.endpoint_ref != expected_ref
                    or not test or test.revision_id != revision.id or test.binding_hash != selected.binding_hash
                    or test.binding_hash != current_binding
                    or test.state != "succeeded" or not test.valid_until or test.valid_until <= utc_now()):
                issues.append(purpose + ":not_selected")
            elif purpose == "document.embed":
                if revision.model != "BAAI/bge-m3" or revision.adapter != "bge-m3-local-v1":
                    issues.append("document.embed:revision_mismatch")
                active_embedding_revision = revision.id
            else:
                if revision.model != "qwen3.8-flash" or revision.adapter != "openai-chat-v1":
                    issues.append("query.answer:revision_mismatch")
                active_answer_revision = revision.id
                active_answer_binding = selected.binding_hash
        if runtime_profile and runtime_profile.configuration.get("generation_revision_id") != active_answer_revision:
            issues.append("runtime_profile:generation_revision_mismatch")
        if runtime_profile and runtime_profile.configuration.get("generation_binding_hash") != active_answer_binding:
            issues.append("runtime_profile:generation_binding_mismatch")
        generation = db.get(IndexGeneration, kb.active_generation_id) if kb and kb.active_generation_id else None
        if (not generation or generation.knowledge_base_id != kb.id or generation.status != "ready"
                or generation.embedding_profile_id != active_embedding_revision):
            issues.append("active_index:not_ready")
    return issues


def serve(config_path: Path | str = DEFAULT_CONFIG, host: str = "127.0.0.1", port: int = 8000,
          *, admin_only: bool = False) -> None:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Pilot server is loopback-only")
    # Bind before loading weights: a second launch on the live port must fail
    # without creating another model or touching the configured database.
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind((host, port))
        listener.listen(socket.SOMAXCONN)
        settings = load_settings(config_path)
        from .app import create_app
        import uvicorn
        import threading
        application = create_app(settings)
        application.state.query_acceptance_enabled = not admin_only
        if not admin_only:
            problems = _readiness(config_path, application)
            if problems:
                application.state.engine.dispose()
                raise RuntimeError("Pilot query runtime is not ready: " + ", ".join(problems) + "; use `serve --admin-only` for setup")
            # Loading the approved offline weights is startup work, not a business
            # query. Complete it before opening the listener or spending a task's
            # finite runtime lease. embed() reuses this exact integrity-checked
            # cache and still rechecks the file identity on every real invocation.
            from . import local_bge
            endpoint = application.state.service_endpoints.get(local_bge.ENDPOINT_REF)
            if not isinstance(endpoint, local_bge.LocalBgeEndpoint):
                application.state.engine.dispose()
                raise RuntimeError("Pilot requires the approved local embedding endpoint")
            try:
                endpoint.preload()
            except Exception:
                application.state.engine.dispose()
                raise
            from .testing_service_use import install_synthetic_answer_fixture
            sample = _read_config(config_path)[0]["synthetic_allowlist"]
            install_synthetic_answer_fixture(application, actor_id=sample["employee_user_id"],
                knowledge_base_id=sample["knowledge_base_id"], version_id=sample["version_id"],
                parse_run_id=sample["parse_run_id"], source_sha256=sample["source_sha256"],
                query=sample["query"], model=sample["model"], allowed_modes=("answer", "retrieve"))
        server = uvicorn.Server(uvicorn.Config(application, host=host, port=port, access_log=False))
        stopped = threading.Event()

        def consume_queries() -> None:
            from .pilot_dispatch import advance_once, advance_processing_once
            gateway = f"http://127.0.0.1:{port}"
            while not stopped.is_set():
                if not server.started:
                    stopped.wait(.25)
                    continue
                try:
                    if admin_only:
                        advance_processing_once(application)
                    else:
                        advance_once(application, gateway_url=gateway,
                            profile_revision=int(_read_config(config_path)[0].get("pi_profile_revision", 1)))
                except Exception as error:
                    # Do not blindly rescan after an unclassified failure: it may
                    # have happened after an external effect. Stop the service and
                    # leave the durable task/call ledger for explicit reconciliation.
                    print(f"Pilot consumer stopped ({type(error).__name__}); inspect the durable task ledger before restart.", file=sys.stderr)
                    stopped.set()
                    server.should_exit = True
                    return
                stopped.wait(.35)

        consumer = threading.Thread(target=consume_queries, name="pilot-consumer", daemon=False)
        consumer.start()
        try:
            server.run(sockets=[listener])
        finally:
            stopped.set()
            if consumer:
                consumer.join()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    init_parser = sub.add_parser("init")
    init_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    init_parser.add_argument("--synthetic-pilot", action="store_true")
    init_parser.add_argument("--bailian-peer", help="Pinned public IPv4 address from the approved DNS/TLS check")
    init_parser.add_argument("--bailian-ca", type=Path, help="Public CA PEM used by the pinned TLS connection")
    setup_parser = sub.add_parser("setup", help="Resume migrations and synthetic sample setup for an existing pilot config")
    setup_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    status_parser = sub.add_parser("status")
    status_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument("--admin-only", action="store_true", help="Serve document preparation APIs/workers without new query acceptance or BGE preload")
    credentials_parser = sub.add_parser("credentials", help="Read locally DPAPI-encrypted pilot credentials")
    credentials_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    credentials_parser.add_argument("--show", action="store_true", help="Display passwords in this local terminal")
    query_parser = sub.add_parser("query", help="Submit the allowlisted synthetic query over the persistent HTTP service")
    query_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    query_parser.add_argument("--url", default="http://127.0.0.1:8000")
    query_parser.add_argument("--text")
    query_parser.add_argument("--timeout", type=int, default=90)
    configure_parser = sub.add_parser("configure", help="Run real provider admission, BGE batch indexing, and Pi profile binding")
    configure_parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args(argv)
    if args.command == "init":
        result = init_pilot(args.config, synthetic_pilot=args.synthetic_pilot,
            bailian_peer=args.bailian_peer, bailian_ca=args.bailian_ca)
    elif args.command == "setup":
        result = resume_pilot(args.config)
    elif args.command == "status":
        result = status(args.config)
    elif args.command == "credentials":
        rows = load_credentials(args.config)
        result = rows if args.show else {key: {"username": row["username"], "user_id": row.get("user_id")} for key, row in rows.items()}
    elif args.command == "query":
        result = query(args.config, base_url=args.url, text=args.text, timeout_seconds=args.timeout)
    elif args.command == "configure":
        result = configure(args.config)
    else:
        serve(args.config, args.host, args.port, admin_only=args.admin_only)
        return 0
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
