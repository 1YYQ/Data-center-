"""Database setup shared by the HTTP app, migrations and tests."""

from __future__ import annotations

from collections.abc import Generator
from fastapi import Request
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session


def build_engine(database_url: str):
    connect_args = {"check_same_thread": False} if database_url.startswith("sqlite") else {}
    engine = create_engine(database_url, future=True, pool_pre_ping=True, connect_args=connect_args)
    if database_url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, record):
            from .migration_safety import MigrationRefused, RESTORE_MARKER
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT 1 FROM sqlite_master WHERE name=?", (RESTORE_MARKER,))
                if cursor.fetchone():
                    raise MigrationRefused("The SQLite backup or restore is sealed for isolated validation.")
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()
    elif database_url.startswith("postgresql+psycopg:"):
        @event.listens_for(engine, "connect")
        def reject_sealed_restore(connection, record):
            from .migration_safety import MigrationRefused, RESTORE_MARKER
            cursor = connection.cursor()
            try:
                cursor.execute("SELECT to_regclass(%s)", (f"public.{RESTORE_MARKER}",))
                if cursor.fetchone()[0] is not None:
                    raise MigrationRefused("The PostgreSQL database is sealed for isolated validation.")
            finally:
                cursor.close()
                connection.rollback()
    @event.listens_for(engine, "do_connect")
    def validate_before_dbapi_connect(dialect, record, args, kwargs):
        from .migration_safety import target_identity
        # Also protects a server incorrectly launched with lifespan disabled.
        target_identity(engine.url.render_as_string(hide_password=False))
    return engine


def init_db(target_engine=None, config=None, *, migrate: bool = True) -> None:
    from .migration_safety import MigrationRefused, apply_local_migrations
    if target_engine is None or config is None:
        raise MigrationRefused("Schema maintenance requires explicit matching engine and settings.")
    if not migrate:
        raise MigrationRefused("create_all bypass is disabled; initialize an isolated schema through guarded migrations.")
    apply_local_migrations(target_engine, config)


def get_db(request: Request) -> Generator[Session, None, None]:
    factory = request.app.state.session_factory
    session = factory()
    try:
        yield session
    finally:
        session.close()
