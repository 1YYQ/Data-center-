"""Source-bound pages and immutable normalized-block projection, forward only."""
from alembic import op
import hashlib
import sqlalchemy as sa

revision = "0012_document_ir"
down_revision = "0011_pdf_artifacts"
branch_labels = depends_on = None


def _sqlite_guard(bind, sql):
    """Declare exact new trigger SQL to the guarded migration runner."""
    bind.exec_driver_sql(sql)
    normalized = sql.strip().replace(" IF NOT EXISTS", "", 1)
    name = normalized.split()[2]
    config = op.get_context().config
    config.attributes.setdefault("declared_sqlite_triggers", {})[name] = hashlib.sha256(normalized.encode()).hexdigest()


def upgrade():
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("document_pages"):
        op.create_table("document_pages",
            sa.Column("parse_run_id", sa.String(36), sa.ForeignKey("parse_runs.id"), primary_key=True),
            sa.Column("file_page", sa.Integer(), primary_key=True),
            sa.Column("media_box_pt", sa.JSON(), nullable=True), sa.Column("crop_box_pt", sa.JSON(), nullable=True),
            sa.Column("rotation", sa.Integer(), nullable=True), sa.Column("user_unit", sa.Float(), nullable=True),
            sa.Column("width_pt", sa.Float(), nullable=True), sa.Column("height_pt", sa.Float(), nullable=True),
            sa.Column("printed_page", sa.String(80), nullable=True), sa.Column("printed_page_basis", sa.JSON(), nullable=True),
            sa.Column("page_class", sa.String(20), nullable=False), sa.Column("state", sa.String(20), nullable=False),
            sa.Column("coverage", sa.JSON(), nullable=False), sa.Column("issue_refs", sa.JSON(), nullable=False),
            sa.Column("transforms", sa.JSON(), nullable=False), sa.Column("raw_ref", sa.JSON(), nullable=True),
            sa.CheckConstraint("file_page >= 1", name="ck_document_pages_positive"))
    columns = {column["name"] for column in sa.inspect(bind).get_columns("content_blocks")}
    additions = [sa.Column("file_page", sa.Integer(), nullable=True), sa.Column("source_kind", sa.String(30), nullable=True),
        sa.Column("raw_ref", sa.JSON(), nullable=True), sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("parent_block_id", sa.String(36), nullable=True), sa.Column("reading_order", sa.Integer(), nullable=True),
        sa.Column("heading_level", sa.Integer(), nullable=True), sa.Column("structure_role", sa.String(20), nullable=True)]
    for column in additions:
        if column.name not in columns:
            op.add_column("content_blocks", column)
    if "source_block_ref" not in columns:
        if bind.dialect.name == "sqlite":
            bind.exec_driver_sql("ALTER TABLE content_blocks ADD COLUMN source_block_ref VARCHAR(36) REFERENCES content_blocks(id)")
        else:
            op.add_column("content_blocks", sa.Column("source_block_ref", sa.String(36), sa.ForeignKey("content_blocks.id")))
    constraints = {row["name"] for row in sa.inspect(bind).get_unique_constraints("content_blocks")}
    indexes = {row["name"] for row in sa.inspect(bind).get_indexes("content_blocks")}
    for name, fields in (("uq_content_blocks_run_id", ["parse_run_id", "id"]),
                         ("uq_content_blocks_run_ordinal", ["parse_run_id", "ordinal"])):
        if name not in constraints | indexes:
            op.create_index(name, "content_blocks", fields, unique=True)
    if bind.dialect.name == "postgresql":
        foreign_keys = {row["name"] for row in sa.inspect(bind).get_foreign_keys("content_blocks")}
        if "fk_content_blocks_page" not in foreign_keys:
            op.create_foreign_key("fk_content_blocks_page", "content_blocks", "document_pages",
                ["parse_run_id", "file_page"], ["parse_run_id", "file_page"])
        if "fk_content_blocks_parent" not in foreign_keys:
            op.create_foreign_key("fk_content_blocks_parent", "content_blocks", "content_blocks",
                ["parse_run_id", "parent_block_id"], ["parse_run_id", "id"])
    else:
        # SQLite cannot add a compound FK without rebuilding a live table with
        # historical evidence references. Enforce the same invariant without
        # copying/deleting history; install these guards on fresh databases too.
        for action in ("INSERT", "UPDATE"):
            _sqlite_guard(bind, f"""CREATE TRIGGER IF NOT EXISTS dc_ir_page_{action.lower()}
                BEFORE {action} ON content_blocks WHEN NEW.file_page IS NOT NULL AND NOT EXISTS
                (SELECT 1 FROM document_pages p WHERE p.parse_run_id=NEW.parse_run_id AND p.file_page=NEW.file_page)
                BEGIN SELECT RAISE(ABORT, 'content block page must belong to its run'); END""")
            _sqlite_guard(bind, f"""CREATE TRIGGER IF NOT EXISTS dc_ir_parent_{action.lower()}
                BEFORE {action} ON content_blocks WHEN NEW.parent_block_id IS NOT NULL AND
                (NEW.parent_block_id=NEW.id OR NOT EXISTS (SELECT 1 FROM content_blocks p
                 WHERE p.parse_run_id=NEW.parse_run_id AND p.id=NEW.parent_block_id))
                BEGIN SELECT RAISE(ABORT, 'content block parent must belong to its run'); END""")
        for action in ("DELETE", "UPDATE"):
            statement = "UPDATE OF parse_run_id, file_page" if action == "UPDATE" else action
            _sqlite_guard(bind, f"""CREATE TRIGGER IF NOT EXISTS dc_ir_page_reference_{action.lower()}
                BEFORE {statement} ON document_pages WHEN EXISTS (SELECT 1 FROM content_blocks b
                 WHERE b.parse_run_id=OLD.parse_run_id AND b.file_page=OLD.file_page)
                BEGIN SELECT RAISE(ABORT, 'document page is referenced'); END""")
        _sqlite_guard(bind, """CREATE TRIGGER IF NOT EXISTS dc_ir_parent_reference_delete
            BEFORE DELETE ON content_blocks WHEN EXISTS (SELECT 1 FROM content_blocks b
              WHERE b.parse_run_id=OLD.parse_run_id AND b.parent_block_id=OLD.id)
            BEGIN SELECT RAISE(ABORT, 'content block parent is referenced'); END""")
        _sqlite_guard(bind, """CREATE TRIGGER IF NOT EXISTS dc_ir_parent_reference_update
            BEFORE UPDATE OF id, parse_run_id ON content_blocks WHEN EXISTS (SELECT 1 FROM content_blocks b
              WHERE b.parse_run_id=OLD.parse_run_id AND b.parent_block_id=OLD.id)
            BEGIN SELECT RAISE(ABORT, 'content block parent is referenced'); END""")


def downgrade():
    raise RuntimeError("Original page and block locators must be retained; use a forward migration")
