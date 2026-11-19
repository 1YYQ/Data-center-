"""Add scoped conditions and durable logical query tool/answer receipts."""
import sqlalchemy as sa
from alembic import op

revision = "0006_execution"
down_revision = "0005_runtime"
branch_labels = depends_on = None


def upgrade():
    from src.server import execution_models as records
    for name in ("Conversation", "ConversationMessage", "ContextFact", "TaskInputRequest", "TaskInputResolution", "ToolInvocation", "QueryAnswerSnapshot", "ToolCallBinding", "RuntimeJournal"):
        getattr(records, name).__table__.create(op.get_bind(), checkfirst=True)
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("query_tasks")}
    with op.batch_alter_table("query_tasks") as batch:
        for name, table in (("conversation_id", "conversations"), ("source_message_id", "conversation_messages")):
            if name not in columns:
                batch.add_column(sa.Column(name, sa.String(36), nullable=True))
                batch.create_foreign_key("fk_query_" + name, table, [name], ["id"])
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("task_evidence")}
    if "execution_revision" not in columns:
        with op.batch_alter_table("task_evidence") as batch:
            batch.add_column(sa.Column("execution_revision", sa.Integer(), nullable=False, server_default="1"))
    columns = {c["name"] for c in sa.inspect(op.get_bind()).get_columns("task_shares")}
    if "answer_snapshot_id" not in columns:
        with op.batch_alter_table("task_shares") as batch:
            batch.add_column(sa.Column("answer_snapshot_id", sa.String(36), nullable=True))
            batch.create_foreign_key("fk_share_answer_snapshot", "answer_snapshots", ["answer_snapshot_id"], ["id"])


def downgrade():
    raise RuntimeError("条件和执行回执参与证据保留，使用受控向前迁移。")
