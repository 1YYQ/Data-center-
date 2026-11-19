"""PostgreSQL-only vector persistence; embeddings are supplied by an admitted provider."""
from __future__ import annotations

import math
import struct
from sqlalchemy import DateTime, Float, JSON, String, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import TypeDecorator

from .models import new_id, utc_now


class Vector(TypeDecorator):
    """Load the optional PostgreSQL driver only for a PostgreSQL operation.

    Ordinary SQLite startup/migrations do not need the local-stack extra. This
    does not emulate a vector store: vector persistence still requires PG.
    """

    impl = JSON
    cache_ok = True

    def __init__(self, dimensions):
        self.dimensions = dimensions
        super().__init__()

    def load_dialect_impl(self, dialect):
        if dialect.name != "postgresql":
            return dialect.type_descriptor(JSON())
        try:
            from pgvector.sqlalchemy import Vector as PgVector
        except ModuleNotFoundError as error:
            raise RuntimeError("PostgreSQL vector operations require the local-stack dependency extra") from error
        return dialect.type_descriptor(PgVector(self.dimensions))

    class comparator_factory(TypeDecorator.Comparator):
        def cosine_distance(self, other):
            return self.op("<=>", return_type=Float())(other)


class VectorBase(DeclarativeBase):
    """Separate metadata so 0001 and ordinary SQLite create_all stay unchanged."""


class ChildVectorBase(DeclarativeBase):
    """The frozen 0015 migration owns these tables, independently of 0007."""


class ChunkEmbedding(ChildVectorBase):
    __tablename__ = "chunk_embeddings"
    generation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    chunk_set_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parse_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    document_version_id: Mapped[str] = mapped_column(String(36), nullable=False)
    entry_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    profile_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)


def stored_vector(values) -> list[float]:
    """Validate the actual float32 values pgvector stores, including underflow."""
    raw = values.tolist() if hasattr(values, "tolist") else values
    validate_embedding_values(raw)
    try:
        result = list(struct.unpack("!1024f", struct.pack("!1024f", *raw)))
    except (OverflowError, struct.error) as error:
        raise ValueError("Vector cannot be stored in pgvector") from error
    validate_embedding_values(result)
    return result


class DocumentEmbedding(VectorBase):
    __tablename__ = "document_embeddings"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    document_version_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    parse_run_id: Mapped[str] = mapped_column(String(36), nullable=False)
    chunk_set_id: Mapped[str] = mapped_column(String(36), nullable=False)
    block_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model_revision: Mapped[str] = mapped_column(String(100), nullable=False)
    dimensions: Mapped[int] = mapped_column(nullable=False, default=1024)
    created_at: Mapped[DateTime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024).with_variant(JSON(), "sqlite"), nullable=False)


def upsert_embedding(db, *, block_id, version_id, parse_run_id, chunk_set_id,
                     provider, model_revision, values):
    """Persist one exact block embedding after caller has authorized/provider-checked it."""
    from .models import ChunkSet, ContentBlock, DocumentVersion, ParseRun
    if db.bind.dialect.name != "postgresql":
        raise RuntimeError("Vector persistence requires PostgreSQL with pgvector")
    validate_embedding_values(values)
    if not provider or not model_revision:
        raise ValueError("Provider and immutable model revision are required")
    block = db.get(ContentBlock, block_id)
    version = db.get(DocumentVersion, version_id)
    parse = db.get(ParseRun, parse_run_id)
    chunk = db.get(ChunkSet, chunk_set_id)
    validate_source_binding(block, version, parse, chunk, version_id=version_id,
        parse_run_id=parse_run_id, chunk_set_id=chunk_set_id)
    row = db.get(DocumentEmbedding, block_id)
    if row is not None:
        validate_embedding_identity(row, version_id=version_id, parse_run_id=parse_run_id,
            chunk_set_id=chunk_set_id, provider=provider, model_revision=model_revision)
    payload = dict(id=block_id, block_id=block_id, document_version_id=version_id, parse_run_id=parse_run_id,
        chunk_set_id=chunk_set_id, provider=provider, model_revision=model_revision, dimensions=1024,
        created_at=utc_now(), embedding=[float(x) for x in values])
    if row is None:
        db.execute(DocumentEmbedding.__table__.insert().values(**payload))
        row = db.get(DocumentEmbedding, block_id, populate_existing=True)
    else:
        db.execute(DocumentEmbedding.__table__.update().where(DocumentEmbedding.id == row.id).values(
            embedding=payload["embedding"], created_at=payload["created_at"]))
        db.expire(row)
    db.flush()
    return row


def validate_source_binding(block, version, parse, chunk, *, version_id, parse_run_id, chunk_set_id):
    if (not block or not version or not parse or not chunk or block.parse_run_id != parse_run_id
            or parse.document_version_id != version_id or chunk.parse_run_id != parse_run_id
            or chunk.id != chunk_set_id):
        raise ValueError("Embedding must bind one exact version, parse run, chunk set and block")


def validate_embedding_values(values):
    if (len(values) != 1024 or any(isinstance(x, bool) or not isinstance(x, (int, float))
            or not math.isfinite(x) for x in values) or not any(float(x) != 0.0 for x in values)):
        raise ValueError("The selected embedding contract is 1024 finite, nonzero numeric dimensions")


def validate_embedding_identity(row, *, version_id, parse_run_id, chunk_set_id, provider, model_revision):
    if (row.document_version_id != version_id or row.parse_run_id != parse_run_id
            or row.chunk_set_id != chunk_set_id or row.provider != provider or row.model_revision != model_revision):
        raise ValueError("An embedding identity cannot be rebound to another source or model")


def authorized_nearest(db, *, actor, query_vector, knowledge_base_ids, model, provider, model_revision, context=None, limit=20):
    """Return only current formally readable sources; vector similarity is ranking only."""
    from sqlalchemy import select
    from .governance import can_read_version, publication_is_current
    from .models import ContentBlock, Document, DocumentVersion, IndexGeneration, IndexSource, KnowledgeBase
    from .services import can_access_document, selected_parse
    if (db.bind.dialect.name != "postgresql" or len(query_vector) != 1024
            or any(isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(x) for x in query_vector)
            or not any(float(x) != 0.0 for x in query_vector) or not model or not provider or not model_revision):
        raise RuntimeError("Nearest-neighbor retrieval requires the admitted PostgreSQL vector profile")
    if not 1 <= limit <= 100:
        raise ValueError("Bounded candidate limit required")
    embedding = DocumentEmbedding.__table__.c.embedding
    distance = embedding.cosine_distance(query_vector)
    # Walk ranked candidates in bounded pages and authorize before filling the
    # caller's limit. Filtering only after SQL LIMIT would starve allowed hits.
    batch = max(limit, min(limit * 4, 400))
    offset, accepted = 0, []
    while len(accepted) < limit:
        rows = db.execute(select(ContentBlock, DocumentVersion, Document, DocumentEmbedding, distance.label("distance"))
        .join(DocumentEmbedding, DocumentEmbedding.block_id == ContentBlock.id)
        .join(DocumentVersion, DocumentVersion.id == DocumentEmbedding.document_version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .where(Document.knowledge_base_id.in_(knowledge_base_ids), DocumentEmbedding.provider == provider,
               DocumentEmbedding.model_revision == model_revision, DocumentEmbedding.dimensions == 1024)
        .order_by(distance).limit(batch).offset(offset)).all()
        if not rows:
            break
        offset += len(rows)
        for block, version, document, embedding_row, score in rows:
            kb = db.get(KnowledgeBase, document.knowledge_base_id)
            generation = db.get(IndexGeneration, kb.active_generation_id) if kb and kb.active_generation_id else None
            selected = selected_parse(db, version)
            index_source = db.scalar(select(IndexSource.id).where(IndexSource.index_generation_id == generation.id,
                IndexSource.document_version_id == version.id, IndexSource.parse_run_id == embedding_row.parse_run_id,
                IndexSource.chunk_set_id == embedding_row.chunk_set_id)) if generation else None
            if (not generation or generation.status not in {"ready", "ready_partial"}
                    or generation.source_revision != kb.content_revision or not index_source or not selected
                    or selected[0].id != embedding_row.parse_run_id or selected[1].id != embedding_row.chunk_set_id
                    or block.parse_run_id != embedding_row.parse_run_id or not block.locator
                    or block.review_status != "approved" or embedding_row.dimensions != 1024
                    or (version.model_scope and model not in version.model_scope)
                    or not publication_is_current(db, version) or not can_read_version(db, actor, version)
                    or not can_read_version(db, actor, version, "knowledge.query")
                    or not can_access_document(db, actor, document, context=context)
                    or not can_access_document(db, actor, document, "knowledge.query", context=context)):
                continue
            accepted.append((block, version, document, score))
            if len(accepted) == limit:
                break
    return accepted
