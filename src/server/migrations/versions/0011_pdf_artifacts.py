"""Persist PDF raw artifacts and extend the existing parse job, forward only."""
from alembic import op
import sqlalchemy as sa

revision = "0011_pdf_artifacts"
down_revision = "0010_document_feedback"
branch_labels = depends_on = None


def _create_artifacts():
    op.create_table(
        "parse_artifacts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("parse_run_id", sa.String(36), sa.ForeignKey("parse_runs.id"), nullable=False),
        sa.Column("file_id", sa.String(36), sa.ForeignKey("file_objects.id"), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("logical_name", sa.String(500), nullable=False),
        sa.Column("producer", sa.JSON(), nullable=False),
        sa.Column("source_locator", sa.JSON(), nullable=True),
        sa.Column("derived_from_artifact_id", sa.String(36), sa.ForeignKey("parse_artifacts.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("parse_run_id", "logical_name"),
        sa.UniqueConstraint("parse_run_id", "id"),
    )
    op.create_index("ix_parse_artifacts_parse_run_id", "parse_artifacts", ["parse_run_id"])


def _nullable_pin(bind):
    if bind.dialect.name != "sqlite":
        op.alter_column("parse_service_jobs", "pin_id", existing_type=sa.String(36), nullable=True)
        return

    # SQLite needs a table copy for nullability. Keep triggers owned by the
    # table and let references from other tables use the same stable name.
    triggers = bind.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND tbl_name='parse_service_jobs' ORDER BY rowid"
    ).scalars().all()
    previous = bind.exec_driver_sql("PRAGMA legacy_alter_table").scalar_one()
    bind.exec_driver_sql("PRAGMA legacy_alter_table=ON")
    try:
        with op.batch_alter_table("parse_service_jobs") as batch:
            batch.alter_column("pin_id", existing_type=sa.String(36), nullable=True)
        for statement in triggers:
            bind.exec_driver_sql(statement)
    finally:
        bind.exec_driver_sql(f"PRAGMA legacy_alter_table={previous}")


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("parse_runs")}
    # The original baseline imports live ORM metadata. Fresh databases can
    # already have these objects, while a persisted 0010 database does not.
    if not inspector.has_table("parse_artifacts"):
        _create_artifacts()

    for name, table in (("parent_run_id", "parse_runs"), ("ir_artifact_id", "parse_artifacts")):
        if name in columns:
            continue
        if bind.dialect.name == "sqlite":
            # With a NULL default SQLite supports an inline reference and can
            # retain the original parse_runs table, dependents and triggers.
            bind.exec_driver_sql(f"ALTER TABLE parse_runs ADD COLUMN {name} VARCHAR(36) REFERENCES {table}(id)")
        else:
            op.add_column("parse_runs", sa.Column(name, sa.String(36), sa.ForeignKey(f"{table}.id"), nullable=True))

    # No reference or status is invented for legacy rows. Empty checkpoints mean
    # that the old run has no archived source proof, not that it passed P1.
    for column in (
        sa.Column("provider", sa.String(80), nullable=True),
        sa.Column("engine_version", sa.String(80), nullable=True),
        sa.Column("model_revision", sa.String(200), nullable=True),
        sa.Column("request_config", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("actual_profile", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("raw_contract", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("adapter_version", sa.String(80), nullable=True),
        sa.Column("recovery_version", sa.String(80), nullable=True),
        sa.Column("ir_schema_version", sa.String(40), nullable=True),
        sa.Column("input_fingerprint", sa.String(64), nullable=True),
        sa.Column("processing_stage", sa.String(30), server_default="ingestion", nullable=False),
        sa.Column("stage_checkpoint", sa.JSON(), server_default="{}", nullable=False),
    ):
        if column.name not in columns:
            op.add_column("parse_runs", column)

    jobs = {column["name"]: column for column in inspector.get_columns("parse_service_jobs")}
    for column in (
        sa.Column("mode", sa.String(30), server_default="selected_service", nullable=False),
        sa.Column("checkpoint", sa.JSON(), server_default="{}", nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), server_default="0", nullable=False),
    ):
        if column.name not in jobs:
            op.add_column("parse_service_jobs", column)
    if not jobs["pin_id"]["nullable"]:
        _nullable_pin(bind)


def downgrade():
    raise RuntimeError("PDF raw artifacts and execution history must be retained; use a forward migration")
