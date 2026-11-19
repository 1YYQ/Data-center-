"""Keep exact child spans and private source dependencies; never rebind history."""
import hashlib

from alembic import op
import sqlalchemy as sa

revision = "0016_exact_evidence"
down_revision = "0015_child_index_vectors"
branch_labels = depends_on = None


def _sqlite_guard(bind, sql):
    bind.exec_driver_sql(sql)
    name = sql.split()[2]
    op.get_context().config.attributes.setdefault("declared_sqlite_triggers", {})[name] = hashlib.sha256(sql.encode()).hexdigest()


def _sqlite_exact_child(bind):
    for action, event in (("insert", "INSERT"), ("update", "UPDATE OF chunk_id, chunk_set_id")):
        _sqlite_guard(bind, f"""CREATE TRIGGER dc_exact_evidence_{action}
            BEFORE {event} ON task_evidence
            WHEN (NEW.chunk_id IS NULL) <> (NEW.chunk_set_id IS NULL)
            OR (NEW.chunk_id IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM chunks c WHERE c.id=NEW.chunk_id AND c.chunk_set_id=NEW.chunk_set_id AND c.level='child'))
            BEGIN SELECT RAISE(ABORT, 'evidence must reference an exact child in its chunk set'); END""")
    _sqlite_guard(bind, """CREATE TRIGGER dc_exact_evidence_chunk_update
        BEFORE UPDATE OF id, chunk_set_id, level ON chunks
        WHEN (NEW.id IS NOT OLD.id OR NEW.chunk_set_id IS NOT OLD.chunk_set_id OR NEW.level IS NOT OLD.level)
        AND EXISTS (SELECT 1 FROM task_evidence e WHERE e.chunk_id=OLD.id AND e.chunk_set_id=OLD.chunk_set_id)
        BEGIN SELECT RAISE(ABORT, 'evidence references this child identity'); END""")
    _sqlite_guard(bind, """CREATE TRIGGER dc_exact_evidence_chunk_delete
        BEFORE DELETE ON chunks
        WHEN EXISTS (SELECT 1 FROM task_evidence e WHERE e.chunk_id=OLD.id AND e.chunk_set_id=OLD.chunk_set_id)
        BEGIN SELECT RAISE(ABORT, 'evidence references this child identity'); END""")


def _postgres_exact_child():
    op.create_check_constraint("ck_evidence_chunk_identity_complete", "task_evidence",
                               "(chunk_id IS NULL) = (chunk_set_id IS NULL)")
    op.execute("""CREATE FUNCTION dc_check_evidence_child() RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE chunk_level varchar(10);
        BEGIN
            IF NEW.chunk_id IS NOT NULL AND NEW.chunk_set_id IS NOT NULL THEN
                SELECT level INTO chunk_level FROM chunks WHERE id=NEW.chunk_id FOR SHARE;
                IF chunk_level = 'parent' THEN
                    RAISE EXCEPTION 'evidence must reference a child chunk' USING ERRCODE='23514';
                END IF;
            END IF;
            RETURN NEW;
        END;
    $$""")
    op.execute("""CREATE TRIGGER dc_exact_evidence_child_level
        BEFORE INSERT OR UPDATE OF chunk_id, chunk_set_id ON task_evidence
        FOR EACH ROW EXECUTE FUNCTION dc_check_evidence_child()""")
    op.execute("""CREATE FUNCTION dc_keep_evidence_child() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            IF NEW.level IS DISTINCT FROM OLD.level AND EXISTS
                (SELECT 1 FROM task_evidence e WHERE e.chunk_id=OLD.id AND e.chunk_set_id=OLD.chunk_set_id) THEN
                RAISE EXCEPTION 'evidence references this child identity' USING ERRCODE='23514';
            END IF;
            RETURN NEW;
        END;
    $$""")
    op.execute("""CREATE TRIGGER dc_exact_evidence_chunk_level
        BEFORE UPDATE OF level ON chunks FOR EACH ROW EXECUTE FUNCTION dc_keep_evidence_child()""")


def upgrade():
    bind = op.get_bind()
    columns = {row["name"] for row in sa.inspect(bind).get_columns("task_evidence")}
    for name, parent in (("chunk_id", "chunks"), ("chunk_set_id", "chunk_sets")):
        if name not in columns:
            if bind.dialect.name == "sqlite":
                bind.exec_driver_sql(f"ALTER TABLE task_evidence ADD COLUMN {name} VARCHAR(36) REFERENCES {parent}(id)")
            else:
                op.add_column("task_evidence", sa.Column(name, sa.String(36), sa.ForeignKey(f"{parent}.id"), nullable=True))
    for column in (sa.Column("evidence_kind", sa.String(20), nullable=False, server_default="text"),
                   sa.Column("source_spans", sa.JSON(), nullable=False, server_default="[]"),
                   sa.Column("asset_refs", sa.JSON(), nullable=False, server_default="[]"),
                   sa.Column("source_binding_hash", sa.String(64), nullable=True),
                   sa.Column("source_context", sa.JSON(), nullable=False, server_default="{}")):
        if column.name not in columns:
            op.add_column("task_evidence", column)
    if bind.dialect.name == "postgresql":
        constraints = {row["name"] for row in sa.inspect(bind).get_foreign_keys("task_evidence")}
        if "fk_evidence_exact_chunk" not in constraints:
            op.create_foreign_key("fk_evidence_exact_chunk", "task_evidence", "chunks", ["chunk_set_id", "chunk_id"], ["chunk_set_id", "id"])
        _postgres_exact_child()
    elif bind.dialect.name == "sqlite":
        _sqlite_exact_child(bind)


def downgrade():
    raise RuntimeError("History and exact evidence identities must be retained; use a forward migration")
