"""Preserve legacy facts; add quarantined local governance records and bindings."""
import sqlalchemy as sa
from alembic import op

revision = "0004_governance"
down_revision = "0003_approval_identity"
branch_labels = None
depends_on = None


def upgrade():
    from src.server.models import Base
    from src.server import governance_models  # noqa: F401 -- metadata registration
    bind = op.get_bind()
    # 0001 uses current metadata for fresh installs. Older databases need the
    # new tables first; create_all(checkfirst) does not rewrite existing rows.
    names = ["authority_bases", "governance_evidence", "governance_verification_events",
        "document_business_confirmations", "review_sets", "document_use_holds",
        "publication_previews", "document_publication_events", "governance_outbox",
        "approval_supersessions", "approval_revocations"]
    for name in names:
        Base.metadata.tables[name].create(bind, checkfirst=True)
    additions = {
        "document_versions": [
            sa.Column("source_metadata", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("selected_review_set_id", sa.String(36), nullable=True),
            sa.Column("reference_access_state", sa.String(20), nullable=False, server_default="closed"),
            sa.Column("reference_access_revision", sa.Integer(), nullable=False, server_default="1")],
        "business_approvals": [
            sa.Column("actual_approver_snapshot", sa.JSON(), nullable=False, server_default="{}"),
            sa.Column("actual_decision", sa.String(30), nullable=True),
            sa.Column("proof_refs", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("verification_revision", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("verified_by", sa.String(36), nullable=True),
            sa.Column("verified_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("dependency_fingerprint", sa.String(64), nullable=True),
            sa.Column("revision", sa.Integer(), nullable=False, server_default="0")],
        "content_blocks": [sa.Column("revision", sa.Integer(), nullable=False, server_default="0")],
        "review_sets": [sa.Column("block_bindings", sa.JSON(), nullable=False, server_default="{}")]
    }
    for table_name, columns in additions.items():
        if table_name not in sa.inspect(bind).get_table_names():
            # The frozen minimal-v1 compatibility fixture did not have parsed
            # blocks yet. Add the absent domain table; never fabricate a row.
            Base.metadata.tables[table_name].create(bind, checkfirst=True)
        existing = {x["name"] for x in sa.inspect(bind).get_columns(table_name)}
        with op.batch_alter_table(table_name) as batch:
            for column in columns:
                if column.name not in existing:
                    batch.add_column(column)
                    if column.name == "selected_review_set_id":
                        batch.create_foreign_key("fk_version_review_set", "review_sets", [column.name], ["id"])
                    if column.name == "verified_by":
                        batch.create_foreign_key("fk_approval_verified_by", "users", [column.name], ["id"])
    # Do not invent recording times, attest proofs or promote old approved rows.


def downgrade():
    raise RuntimeError("治理依据与发布回执参与授权；仅允许经审阅的向前迁移。")
