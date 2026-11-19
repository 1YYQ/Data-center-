"""Environment-backed settings for the local reference service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _optional_path(name: str) -> Path | None:
    value = os.getenv(name)
    return Path(value) if value else None


def _auxiliary_operations() -> tuple[str, ...]:
    values = tuple(value.strip() for value in os.getenv("DATACENTER_AUXILIARY_OPERATIONS", "").split(",") if value.strip())
    if len(set(values)) != len(values) or any(value not in {"summary", "compaction"} for value in values):
        raise ValueError("Only explicit summary and compaction idle operations are supported")
    return values


def _query_service_mode() -> str:
    value = os.getenv("DATACENTER_QUERY_SERVICE_MODE", "local").strip()
    if value not in {"local", "selected_service"}:
        raise ValueError("DATACENTER_QUERY_SERVICE_MODE must be local or selected_service")
    return value


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATACENTER_DATABASE_URL", "sqlite:///./outputs/data-center.db")
    storage_dir: Path = Path(os.getenv("DATACENTER_STORAGE_DIR", "storage"))
    max_upload_bytes: int = int(os.getenv("DATACENTER_MAX_UPLOAD_BYTES", str(50 * 1024 * 1024)))
    session_idle_seconds: int = int(os.getenv("DATACENTER_SESSION_IDLE_SECONDS", str(30 * 60)))
    session_absolute_seconds: int = int(os.getenv("DATACENTER_SESSION_ABSOLUTE_SECONDS", str(8 * 60 * 60)))
    secure_cookie: bool = _bool("DATACENTER_SECURE_COOKIE", False)
    cookie_name: str = os.getenv("DATACENTER_SESSION_COOKIE", "dc_session")
    bootstrap_token: str = os.getenv("DATACENTER_BOOTSTRAP_TOKEN", "")
    app_name: str = os.getenv("DATACENTER_APP_NAME", "数据中心 · 设备资料查询")
    local_mineru_executable: Path | None = _optional_path("DATACENTER_LOCAL_MINERU_EXECUTABLE")
    local_mineru_home: Path | None = _optional_path("DATACENTER_LOCAL_MINERU_HOME")
    local_bge_tokenizer: Path | None = _optional_path("DATACENTER_BGE_TOKENIZER_PATH")
    local_bge_model_dir: Path | None = _optional_path("DATACENTER_LOCAL_BGE_MODEL_DIR")
    auxiliary_operations: tuple[str, ...] = _auxiliary_operations()
    query_service_mode: str = _query_service_mode()
    query_max_inflight: int = int(os.getenv("DATACENTER_QUERY_MAX_INFLIGHT", "0"))

    def __post_init__(self) -> None:
        if self.query_service_mode not in {"local", "selected_service"}:
            raise ValueError("query_service_mode must be local or selected_service")
        if type(self.query_max_inflight) is not int or not 0 <= self.query_max_inflight <= 64:
            raise ValueError("query_max_inflight must be an integer from 0 to 64")

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        if self.database_url.startswith("sqlite:///"):
            database_path = Path(self.database_url.removeprefix("sqlite:///"))
            if database_path.parent != Path("."):
                database_path.parent.mkdir(parents=True, exist_ok=True)


settings = Settings()
