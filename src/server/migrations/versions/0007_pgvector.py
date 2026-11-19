"""Add the version/run/chunk bound pgvector embedding store on PostgreSQL."""
from alembic import op
import sqlalchemy as sa

revision = "0007_pgvector"
down_revision = "0006_execution"
branch_labels = depends_on = None


def upgrade():
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    from src.server.vector_store import VectorBase
    VectorBase.metadata.create_all(op.get_bind(), checkfirst=True)
    op.create_check_constraint("ck_document_embedding_dimensions", "document_embeddings", "dimensions = 1024")
    op.execute("CREATE INDEX IF NOT EXISTS ix_document_embeddings_hnsw_cosine ON document_embeddings USING hnsw (embedding vector_cosine_ops)")


def downgrade():
    raise RuntimeError("向量是可重建但具来源/权限关联的派生数据；使用受控向前迁移。")
