"""Immutable service revisions, synthetic self-tests and durable request intents."""
from alembic import op
from src.server import service_models as m
revision = "0008_service_admission"
down_revision = "0007_pgvector"
branch_labels = depends_on = None


def upgrade():
    for cls in (m.ServiceProfile, m.ServiceRevision, m.ServiceTestRun, m.ServiceChangePreview, m.ServiceRequestCall):
        cls.__table__.create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Call intents must be retained; use a controlled forward migration")
