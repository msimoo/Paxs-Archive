"""add radiology reports

Revision ID: w1234567x8y9
Revises: v1234567w8x9
Create Date: 2026-08-06

Adds the `reports` table (Phase E2 — HL7 ORU report write-back to the EMR):
one finalized radiology report per study, with findings/impression text and
the ORU^R01 delivery state so failed EMR deliveries can be re-sent.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "w1234567x8y9"
down_revision: Union[str, None] = "v1234567w8x9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _bigint() -> sa.BigInteger:
    """BIGINT on MySQL, INTEGER on SQLite (matches the ORM pk() helper)."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "reports",
        sa.Column("id", _bigint(), primary_key=True, autoincrement=True),
        sa.Column(
            "study_id",
            _bigint(),
            sa.ForeignKey("studies.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("study_instance_uid", sa.String(length=128), nullable=False),
        sa.Column("patient_id", sa.String(length=64), nullable=True),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="DRAFT",
        ),
        sa.Column("clinical_history", sa.Text(), nullable=True),
        sa.Column("findings", sa.Text(), nullable=True),
        sa.Column("impression", sa.Text(), nullable=True),
        sa.Column("radiologist", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("finalized_at", sa.DateTime(), nullable=True),
        sa.Column(
            "oru_status",
            sa.String(length=16),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("oru_sent_at", sa.DateTime(), nullable=True),
        sa.Column("oru_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_reports_study_instance_uid", "reports", ["study_instance_uid"])
    op.create_index("ix_reports_patient_id", "reports", ["patient_id"])
    op.create_index("ix_reports_status", "reports", ["status"])


def downgrade() -> None:
    op.drop_index("ix_reports_status", table_name="reports")
    op.drop_index("ix_reports_patient_id", table_name="reports")
    op.drop_index("ix_reports_study_instance_uid", table_name="reports")
    op.drop_table("reports")
