"""Persist an explicit selected parse/chunk binding; never backfill by latest run."""

import sqlalchemy as sa
from alembic import op

revision = "0002_selected_source"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    # The original initial migration imports current Base. Fresh databases may
    # already have this column; existing v1 databases need the forward change.
    columns = {column["name"] for column in sa.inspect(bind).get_columns("document_versions")}
    if "selected_chunk_set_id" not in columns:
        with op.batch_alter_table("document_versions") as batch:
            batch.add_column(sa.Column("selected_chunk_set_id", sa.String(36), nullable=True))
            batch.create_foreign_key("fk_version_selected_chunk", "chunk_sets", ["selected_chunk_set_id"], ["id"])
    # Existing unbound versions remain unbound until explicitly reviewed.


def downgrade() -> None:
    raise RuntimeError("来源绑定参与历史证据与核验，禁止无条件移除；使用受控向前迁移。")
