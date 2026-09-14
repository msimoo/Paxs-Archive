"""add clinical protocols library (roadmap #31)

Revision ID: d5e6f70819a2
Revises: c4d5e6f70819
Create Date: 2026-08-07
"""

from alembic import op
import sqlalchemy as sa


revision = "d5e6f70819a2"
down_revision = "c4d5e6f70819"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "clinical_protocols",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("description", sa.String(length=512), nullable=True),
        sa.Column("standardized_name", sa.String(length=255), nullable=True),
        sa.Column("ris_code", sa.String(length=64), nullable=True),
        sa.Column("modality", sa.String(length=16), nullable=True),
        sa.Column("body_part_examined", sa.String(length=64), nullable=True),
        sa.Column("study_description_pattern", sa.String(length=255), nullable=True),
        sa.Column("priority", sa.Integer(), server_default="10", nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("1"), nullable=False),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name"),
    )
    op.create_index("ix_clinical_protocols_standardized_name", "clinical_protocols", ["standardized_name"])
    op.create_index("ix_clinical_protocols_ris_code", "clinical_protocols", ["ris_code"])
    op.create_index("ix_clinical_protocols_modality", "clinical_protocols", ["modality"])
    op.create_index("ix_clinical_protocols_body_part_examined", "clinical_protocols", ["body_part_examined"])
    op.create_index(
        "ix_clinical_protocols_modality_bodypart",
        "clinical_protocols",
        ["modality", "body_part_examined"],
    )
    op.create_index(
        "ix_clinical_protocols_enabled_priority",
        "clinical_protocols",
        ["enabled", "priority"],
    )


def downgrade() -> None:
    op.drop_index("ix_clinical_protocols_enabled_priority", table_name="clinical_protocols")
    op.drop_index("ix_clinical_protocols_modality_bodypart", table_name="clinical_protocols")
    op.drop_index("ix_clinical_protocols_body_part_examined", table_name="clinical_protocols")
    op.drop_index("ix_clinical_protocols_modality", table_name="clinical_protocols")
    op.drop_index("ix_clinical_protocols_ris_code", table_name="clinical_protocols")
    op.drop_index("ix_clinical_protocols_standardized_name", table_name="clinical_protocols")
    op.drop_table("clinical_protocols")
