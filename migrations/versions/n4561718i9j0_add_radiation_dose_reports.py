"""Add radiation_dose_reports table for RDSR support.

Revision ID: n4561718i9j0
Revises: m3451617h8i9
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "n4561718i9j0"
down_revision = "m3451617h8i9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "radiation_dose_reports",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("sop_instance_uid", sa.String(128), nullable=False, unique=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("series_instance_uid", sa.String(128), nullable=False, index=True),
        sa.Column("patient_id", sa.String(64), nullable=False, index=True),
        sa.Column("modality", sa.String(16), nullable=False, server_default="CT"),
        sa.Column("total_dlp_mgy_cm", sa.Float(), nullable=True),
        sa.Column("total_ctdivol_mgy", sa.Float(), nullable=True),
        sa.Column("total_dose_rp_mgy", sa.Float(), nullable=True),
        sa.Column("irradiation_events_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manufacturer", sa.String(128), nullable=True),
        sa.Column("device_model", sa.String(128), nullable=True),
        sa.Column("raw_payload_json", sa.Text(), nullable=True),
        sa.Column("file_path", sa.String(768), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("radiation_dose_reports")
