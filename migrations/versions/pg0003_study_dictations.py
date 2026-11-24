"""Store study-scoped radiologist dictations."""

import sqlalchemy as sa
from alembic import op

revision = "pg0003_study_dictations"
down_revision = "pg0002_replication_generation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "study_dictations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("study_id", sa.BigInteger(), sa.ForeignKey("studies.id", ondelete="CASCADE"), nullable=False),
        sa.Column("recorded_by", sa.String(64), nullable=False),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("audio", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_study_dictations_study_id", "study_dictations", ["study_id"])


def downgrade() -> None:
    op.drop_table("study_dictations")
