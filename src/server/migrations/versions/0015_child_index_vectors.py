"""Store generation-bound child vectors and accepted embedding batches."""
from alembic import op
import sqlalchemy as sa

revision = "0015_child_index_vectors"
down_revision = "0014_semantic_chunks"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    from pgvector.sqlalchemy import Vector

    op.create_unique_constraint("uq_index_sources_exact_child_source", "index_sources",
                                ["index_generation_id", "chunk_set_id", "parse_run_id", "document_version_id"])
    op.create_table("chunk_embeddings",
        sa.Column("generation_id", sa.String(36), primary_key=True),
        sa.Column("chunk_id", sa.String(36), primary_key=True),
        sa.Column("chunk_set_id", sa.String(36), nullable=False),
        sa.Column("parse_run_id", sa.String(36), nullable=False),
        sa.Column("document_version_id", sa.String(36), nullable=False),
        sa.Column("entry_hash", sa.String(64), nullable=False),
        sa.Column("profile_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),
        sa.ForeignKeyConstraint(["chunk_id", "chunk_set_id", "parse_run_id"],
            ["chunks.id", "chunks.chunk_set_id", "chunks.parse_run_id"], name="fk_child_vector_chunk"),
        sa.ForeignKeyConstraint(["generation_id", "chunk_set_id", "parse_run_id", "document_version_id"],
            ["index_sources.index_generation_id", "index_sources.chunk_set_id", "index_sources.parse_run_id",
             "index_sources.document_version_id"], name="fk_child_vector_index_source"),
        sa.CheckConstraint("vector_norm(embedding) > 0", name="ck_child_vector_nonzero"))
    op.create_table("index_embedding_batches",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("generation_id", sa.String(36), sa.ForeignKey("index_generations.id"), nullable=False),
        sa.Column("batch_no", sa.Integer(), nullable=False),
        sa.Column("actor_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("pin_id", sa.String(36), sa.ForeignKey("service_use_pins.id"), nullable=False, unique=True),
        sa.Column("input_hash", sa.String(64), nullable=False),
        sa.Column("state", sa.String(30), nullable=False),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("generation_id", "batch_no", name="uq_index_embedding_batch"))


def downgrade():
    raise RuntimeError("Index membership and service intents require controlled forward migration")
