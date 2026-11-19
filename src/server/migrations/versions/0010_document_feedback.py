"""Persist scoped feedback and its exact version dependencies."""
from alembic import op

revision = "0010_document_feedback"
down_revision = "0009_service_use"
branch_labels = depends_on = None


def upgrade():
    from src.server.feedback_models import DocumentFeedback, FeedbackDependency

    DocumentFeedback.__table__.create(op.get_bind(), checkfirst=True)
    FeedbackDependency.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Feedback and its audit history must be preserved")
