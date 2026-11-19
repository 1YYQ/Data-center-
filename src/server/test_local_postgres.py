"""Opt-in checks against a disposable PostgreSQL/pgvector test database."""
import os

import pytest
from sqlalchemy import bindparam, create_engine, select, text
from sqlalchemy.orm import Session


@pytest.fixture
def pg_engine():
    url = os.getenv("DATACENTER_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("Set DATACENTER_TEST_POSTGRES_URL to a disposable local database")
    if not url.startswith("postgresql+psycopg://") or "127.0.0.1" not in url:
        pytest.fail("Integration tests require an explicit loopback PostgreSQL test URL")
    engine = create_engine(url, pool_pre_ping=True)
    with engine.connect() as connection:
        if connection.execute(text("select count(*) from pg_extension where extname='vector'")).scalar_one() != 1:
            pytest.fail("Disposable PostgreSQL test database must have pgvector installed")
    yield engine
    engine.dispose()


def test_vector_1024_roundtrip_and_cosine_operator(pg_engine):
    from pgvector.sqlalchemy import Vector
    values = [1.0] + [0.0] * 1023
    with pg_engine.begin() as connection:
        connection.execute(text("create temp table vector_probe (id integer, embedding vector(1024)) on commit drop"))
        connection.execute(text("insert into vector_probe values (1, :v)"), {"v": values})
        statement = text("select vector_dims(embedding), embedding <=> :v from vector_probe").bindparams(
            bindparam("v", type_=Vector(1024)))
        result = connection.execute(statement, {"v": values}).one()
    assert result[0] == 1024
    assert result[1] == pytest.approx(0.0)


def test_synthetic_formal_source_upsert_nearest_and_current_acl(pg_engine, tmp_path):
    from src.server.config import Settings
    from src.server.models import AuthorizationGrant
    from src.server.vector_store import DocumentEmbedding
    from src.server.services import selected_parse
    from src.server.test_services import _ready_fixture
    from src.server.vector_store import authorized_nearest, upsert_embedding
    settings = Settings(database_url=str(pg_engine.url), storage_dir=tmp_path / "storage")
    with Session(pg_engine, expire_on_commit=False) as db:
        # This helper creates a synthetic approved version, selected parse/chunk,
        # source/index rows and a complete management/read/query grant tuple.
        actor, kb, document, version, block = _ready_fixture(db, settings)
        parse, chunk = selected_parse(db, version)
        vector = [1.0] + [0.0] * 1023
        row = upsert_embedding(db, block_id=block.id, version_id=version.id,
            parse_run_id=parse.id, chunk_set_id=chunk.id, provider="synthetic-local",
            model_revision="SYNTHETIC-1024-v1", values=vector)
        db.commit()
        assert row is not None and row.embedding == pytest.approx(vector)
        found = authorized_nearest(db, actor=actor, query_vector=vector,
            knowledge_base_ids=[kb.id], model="DEMO-1", provider="synthetic-local", model_revision="SYNTHETIC-1024-v1", context={"level": document.access_level,
                "category": document.category, "need_class": document.need_class}, limit=5)
        assert [item[1].id for item in found] == [version.id]
        grant = db.scalar(select(AuthorizationGrant).where(
            AuthorizationGrant.subject_user_id == actor.id, AuthorizationGrant.operation == "knowledge.query"))
        grant.revoked_at = __import__("src.server.models", fromlist=["utc_now"]).utc_now()
        db.commit()
        assert authorized_nearest(db, actor=actor, query_vector=vector,
            knowledge_base_ids=[kb.id], model="DEMO-1", provider="synthetic-local", model_revision="SYNTHETIC-1024-v1", context={"level": document.access_level,
                "category": document.category, "need_class": document.need_class}, limit=5) == []
        assert authorized_nearest(db, actor=actor, query_vector=vector,
            knowledge_base_ids=[kb.id], model="DEMO-1", provider="synthetic-local", model_revision="SYNTHETIC-v2", limit=5) == []
        with pytest.raises(ValueError):
            upsert_embedding(db, block_id=block.id, version_id=version.id,
                parse_run_id=parse.id, chunk_set_id="wrong-chunk", provider="synthetic-local",
                model_revision="SYNTHETIC-1024-v1", values=vector)


@pytest.mark.parametrize("bad", [([0.0] * 1024), ([float("nan")] + [0.0] * 1023),
    ([float("inf")] + [0.0] * 1023), ([True] + [0.0] * 1023), ([1.0] + [0.0] * 1022)])
def test_embeddings_reject_invalid_dimensions_values_and_zero_norm(bad):
    from src.server.vector_store import validate_embedding_values
    with pytest.raises(ValueError):
        validate_embedding_values(bad)


def test_schema_and_database_are_explicitly_test_only(pg_engine):
    with pg_engine.connect() as connection:
        database = connection.execute(text("select current_database()" )).scalar_one()
    assert database.startswith("dc_local_pgvector_")


def test_embedding_binding_and_model_identity_are_immutable():
    from types import SimpleNamespace
    from src.server.vector_store import validate_embedding_identity, validate_source_binding
    version, run, chunk = "v", "r", "c"
    validate_source_binding(SimpleNamespace(parse_run_id=run), SimpleNamespace(id=version),
        SimpleNamespace(document_version_id=version), SimpleNamespace(id=chunk, parse_run_id=run),
        version_id=version, parse_run_id=run, chunk_set_id=chunk)
    with pytest.raises(ValueError, match="exact version"):
        validate_source_binding(SimpleNamespace(parse_run_id="other"), SimpleNamespace(id=version),
            SimpleNamespace(document_version_id=version), SimpleNamespace(id=chunk, parse_run_id=run),
            version_id=version, parse_run_id=run, chunk_set_id=chunk)
    row = SimpleNamespace(document_version_id=version, parse_run_id=run, chunk_set_id=chunk,
        provider="synthetic", model_revision="SYNTHETIC-v1")
    validate_embedding_identity(row, version_id=version, parse_run_id=run, chunk_set_id=chunk,
        provider="synthetic", model_revision="SYNTHETIC-v1")
    with pytest.raises(ValueError, match="cannot be rebound"):
        validate_embedding_identity(row, version_id=version, parse_run_id=run, chunk_set_id=chunk,
            provider="synthetic", model_revision="SYNTHETIC-v2")
