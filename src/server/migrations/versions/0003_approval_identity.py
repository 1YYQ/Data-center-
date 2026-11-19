"""Separate recording from actual approval; quarantine unverified legacy records."""

import sqlalchemy as sa
from alembic import op

revision = "0003_approval_identity"
down_revision = "0002_selected_source"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {c["name"]: c for c in sa.inspect(op.get_bind()).get_columns("business_approvals")}
    with op.batch_alter_table("business_approvals") as batch:
        if "recorded_by" not in columns:
            batch.add_column(sa.Column("recorded_by", sa.String(36), nullable=True))
            batch.create_foreign_key("fk_approval_recorded_by", "users", ["recorded_by"], ["id"])
        if "verification_state" not in columns:
            batch.add_column(sa.Column("verification_state", sa.String(30), nullable=False, server_default="unverified"))
        if not columns["approver"]["nullable"]:
            batch.alter_column("approver", existing_type=sa.String(36), nullable=True)
    # Preserve historical values. Neither old "approved" nor an actor ID proves
    # an external decision, so no legacy row is promoted to verified.


def downgrade() -> None:
    raise RuntimeError("审批身份与核查状态参与授权，禁止无条件移除；使用受控向前迁移。")
