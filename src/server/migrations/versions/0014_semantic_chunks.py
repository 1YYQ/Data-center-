"""Immutable chunk sets, real members and exact Unicode/region mappings."""
from alembic import op
import sqlalchemy as sa

revision = "0014_semantic_chunks"
down_revision = "0013_block_relations"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    columns = {row["name"] for row in sa.inspect(bind).get_columns("chunk_sets")}
    for column in (sa.Column("splitter_config", sa.JSON(), nullable=False, server_default="{}"),
        sa.Column("config_hash", sa.String(64), nullable=True), sa.Column("ir_content_hash", sa.String(64), nullable=True),
        sa.Column("tokenizer_revision", sa.String(150), nullable=True), sa.Column("stage_checkpoint", sa.JSON(), nullable=False, server_default="{}")):
        if column.name not in columns:
            op.add_column("chunk_sets", column)
    if "quality_artifact_id" not in columns:
        if bind.dialect.name == "sqlite":
            bind.exec_driver_sql("ALTER TABLE chunk_sets ADD COLUMN quality_artifact_id VARCHAR(36) REFERENCES parse_artifacts(id)")
        else:
            op.add_column("chunk_sets", sa.Column("quality_artifact_id", sa.String(36), sa.ForeignKey("parse_artifacts.id"), nullable=True))
    constraints = {row["name"] for row in sa.inspect(bind).get_unique_constraints("chunk_sets")}
    indexes = {row["name"] for row in sa.inspect(bind).get_indexes("chunk_sets")}
    for name, fields in (("uq_chunk_sets_input", ["parse_run_id", "config_hash"]), ("uq_chunk_sets_run", ["id", "parse_run_id"])):
        if name not in constraints | indexes:
            op.create_index(name, "chunk_sets", fields, unique=True)
    if not sa.inspect(bind).has_table("chunks"):
        op.create_table("chunks",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("chunk_set_id", sa.String(36), sa.ForeignKey("chunk_sets.id"), nullable=False),
            sa.Column("parse_run_id", sa.String(36), sa.ForeignKey("parse_runs.id"), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False), sa.Column("content", sa.Text(), nullable=True),
            sa.Column("content_hash", sa.String(64), nullable=False), sa.Column("token_count", sa.Integer(), nullable=True),
            sa.Column("level", sa.String(10), nullable=False), sa.Column("parent_chunk_id", sa.String(36), nullable=True),
            sa.Column("structure_root_id", sa.String(36), nullable=True), sa.Column("kind", sa.String(30), nullable=False),
            sa.Column("eligibility", sa.String(30), nullable=False), sa.Column("required_relation_ids", sa.JSON(), nullable=False),
            sa.Column("assembly", sa.JSON(), nullable=False), sa.Column("support_group_refs", sa.JSON(), nullable=False),
            sa.UniqueConstraint("chunk_set_id", "id", name="uq_chunks_set_id"),
            sa.UniqueConstraint("chunk_set_id", "ordinal", name="uq_chunks_ordinal"),
            sa.UniqueConstraint("id", "chunk_set_id", "parse_run_id", name="uq_chunks_source"),
            sa.ForeignKeyConstraint(["chunk_set_id", "parse_run_id"], ["chunk_sets.id", "chunk_sets.parse_run_id"], name="fk_chunks_run"),
            sa.ForeignKeyConstraint(["chunk_set_id", "parent_chunk_id"], ["chunks.chunk_set_id", "chunks.id"], name="fk_chunks_parent"),
            sa.ForeignKeyConstraint(["parse_run_id", "structure_root_id"], ["content_blocks.parse_run_id", "content_blocks.id"], name="fk_chunks_structure"),
            sa.CheckConstraint("parent_chunk_id IS NULL OR parent_chunk_id <> id", name="ck_chunks_not_self"),
            sa.CheckConstraint("level IN ('child','parent') AND (level <> 'parent' OR parent_chunk_id IS NULL)", name="ck_chunks_level"))
        op.create_index("ix_chunks_parent", "chunks", ["chunk_set_id", "parent_chunk_id"])
    if not sa.inspect(bind).has_table("chunk_blocks"):
        op.create_table("chunk_blocks",
            sa.Column("chunk_id", sa.String(36), primary_key=True), sa.Column("block_id", sa.String(36), primary_key=True),
            sa.Column("chunk_set_id", sa.String(36), nullable=False), sa.Column("parse_run_id", sa.String(36), nullable=False),
            sa.Column("ordinal", sa.Integer(), nullable=False), sa.Column("segments", sa.JSON(), nullable=False),
            sa.ForeignKeyConstraint(["chunk_id", "chunk_set_id", "parse_run_id"], ["chunks.id", "chunks.chunk_set_id", "chunks.parse_run_id"], name="fk_mapping_chunk"),
            sa.ForeignKeyConstraint(["parse_run_id", "block_id"], ["content_blocks.parse_run_id", "content_blocks.id"], name="fk_mapping_block"))
        op.create_index("ix_mapping_block", "chunk_blocks", ["block_id", "chunk_id"])


def downgrade():
    raise RuntimeError("Historical chunk/evidence identities must be retained; use a forward migration")
