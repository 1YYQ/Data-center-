"""Purpose selections and exact synthetic business-use ledger; forward only."""
from alembic import op
import sqlalchemy as sa
from src.server import service_use_models as m
revision = "0009_service_use"
down_revision = "0008_service_admission"
branch_labels = depends_on = None


def upgrade():
    bind = op.get_bind()
    if "use_epoch" not in {x["name"] for x in sa.inspect(bind).get_columns("service_profiles")}:
        op.add_column("service_profiles", sa.Column("use_epoch", sa.Integer(), server_default="0", nullable=False))
    for cls in (m.ServiceSelection, m.ServiceSelectionPreview, m.SyntheticServiceInput,
                m.ServiceUsePin, m.ServiceTaskSelection, m.ServiceUseCall):
        cls.__table__.create(bind, checkfirst=True)
    from src.server.parse_jobs import ParseServiceJob
    ParseServiceJob.__table__.create(bind, checkfirst=True)
    from src.server.embedding_jobs import EmbeddingServiceJob
    EmbeddingServiceJob.__table__.create(bind, checkfirst=True)


def downgrade():
    raise RuntimeError("Business call intents must be retained; use a controlled forward migration")
