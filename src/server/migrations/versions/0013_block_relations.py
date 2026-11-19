"""Frozen structured-IR relations, with same-run endpoints and no review flags."""
from alembic import op
import sqlalchemy as sa

revision = "0013_block_relations"
down_revision = "0012_document_ir"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("block_relations"):
        op.create_table("block_relations",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("parse_run_id", sa.String(36), sa.ForeignKey("parse_runs.id"), nullable=False),
            sa.Column("from_block_id", sa.String(36), nullable=False), sa.Column("to_block_id", sa.String(36), nullable=False),
            sa.Column("from_part", sa.Text(), nullable=False, server_default=""),
            sa.Column("to_part", sa.Text(), nullable=False, server_default=""),
            sa.Column("relation_type", sa.String(30), nullable=False), sa.Column("basis", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["parse_run_id", "from_block_id"], ["content_blocks.parse_run_id", "content_blocks.id"], name="fk_relation_from"),
            sa.ForeignKeyConstraint(["parse_run_id", "to_block_id"], ["content_blocks.parse_run_id", "content_blocks.id"], name="fk_relation_to"),
            sa.UniqueConstraint("parse_run_id", "from_block_id", "from_part", "relation_type", "to_block_id", "to_part", name="uq_block_relation_endpoints"),
            sa.CheckConstraint("from_block_id <> to_block_id OR from_part <> to_part", name="ck_relation_not_same_endpoint"))
        op.create_index("ix_relations_from", "block_relations", ["parse_run_id", "from_block_id", "relation_type"])
        op.create_index("ix_relations_to", "block_relations", ["parse_run_id", "to_block_id", "relation_type"])


def downgrade():
    raise RuntimeError("Frozen relationship evidence must be retained; use a forward migration")
