from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from src.server.models import Base
from src.server.migration_safety import MigrationRefused, target_identity

config = context.config
if config.config_file_name:
    fileConfig(config.config_file_name)
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    raise MigrationRefused("Use the explicit local migration plan/apply command; bare Alembic has no target.")


def run_migrations_online() -> None:
    connection = config.attributes.get("guarded_connection")
    if connection is None or connection.closed or not connection.in_transaction():
        raise MigrationRefused("Bare Alembic is disabled. Use python -m src.server.migrate plan/apply.")
    identity = target_identity(connection.engine.url.render_as_string(hide_password=False))
    if identity != config.attributes.get("guarded_target"):
        raise MigrationRefused("Migration connection does not match the planned target.")
    context.configure(connection=connection, target_metadata=target_metadata, transactional_ddl=True)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
