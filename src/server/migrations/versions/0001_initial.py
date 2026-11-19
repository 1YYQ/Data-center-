"""Create the first-release device-query schema."""

from alembic import op

from src.server.models import Base

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Keeping the authoritative table definitions in models avoids drift between
    # the local SQLite reference path and the first migration.  PostgreSQL uses
    # the same portable columns and can add provider-specific indexes later.
    bind = op.get_bind()
    Base.metadata.create_all(bind=bind)


def downgrade() -> None:
    raise RuntimeError("初始业务表包含资料与审计数据，禁止无条件 downgrade；请使用受控数据清理和向前迁移。")
