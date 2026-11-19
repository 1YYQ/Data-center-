"""Durable private runtime leases, projections, budgets and model call intent."""
from alembic import op
import sqlalchemy as sa
from src.server import runtime_models

revision = "0005_runtime"
down_revision = "0004_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("query_tasks")}
    additions = [sa.Column("auth_session_id", sa.String(36), nullable=True),
                 sa.Column("effective_input", sa.JSON(), nullable=False, server_default="{}"),
                 sa.Column("execution_revision", sa.Integer(), nullable=False, server_default="1"),
                 sa.Column("authorization_basis", sa.String(30), nullable=False, server_default="accepted_task"),
                 sa.Column("accepted_scope", sa.JSON(), nullable=False, server_default="{}")]
    with op.batch_alter_table("query_tasks") as batch:
        for column in additions:
            if column.name not in columns:
                batch.add_column(column)
        if "auth_session_id" not in columns:
            batch.create_foreign_key("fk_task_auth_session", "auth_sessions", ["auth_session_id"], ["id"])
    step_columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("task_steps")}
    with op.batch_alter_table("task_steps") as batch:
        if "lease_owner" not in step_columns:
            batch.add_column(sa.Column("lease_owner", sa.String(36), nullable=True))
        if "lease_epoch" not in step_columns:
            batch.add_column(sa.Column("lease_epoch", sa.Integer(), nullable=False, server_default="0"))
    # 0001 uses current metadata on fresh installs; do not recreate those tables.
    # Sort only this acyclic runtime subset, avoiding unrelated version/parse
    # cycles in the shared business metadata.
    for cls in (runtime_models.RuntimeProfile, runtime_models.TaskBudget,
                runtime_models.AgentSessionBinding, runtime_models.AgentRun,
                runtime_models.ModelInputProjection, runtime_models.ModelCallAdmission):
        cls.__table__.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    raise RuntimeError("Runtime ledgers fence uncertain external effects; use a controlled forward migration")
