"""Bind private runtime journals to exact sources and restore compatibility."""

from alembic import op
import sqlalchemy as sa


revision = "0017_private_journal_sources"
down_revision = "0016_exact_evidence"
branch_labels = depends_on = None


def upgrade():
    existing = {field["name"] for field in sa.inspect(op.get_bind()).get_columns("runtime_journals")}
    frozen_columns = (
        sa.Column("source_bindings", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("source_binding_hash", sa.String(64), nullable=True),
        sa.Column("runtime_compatibility", sa.JSON(none_as_null=True), nullable=True),
        sa.Column("recovery_expires_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in frozen_columns:
        if column.name not in existing:
            op.add_column("runtime_journals", column)


def downgrade():
    raise RuntimeError("Private journal source identities require a controlled forward migration")
