"""Add prefetch_rules and prefetch_jobs tables.

Revision ID: m3451617h8i9
Revises: l2341516g7h8
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "m3451617h8i9"
down_revision = "l2341516g7h8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prefetch_rules",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False, unique=True),
        sa.Column("description", sa.String(255), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="10"),
        sa.Column("modality", sa.String(16), nullable=True),
        sa.Column("body_part_examined", sa.String(64), nullable=True),
        sa.Column("max_prior_age_days", sa.Integer(), nullable=False, server_default="730"),
        sa.Column("max_priors_count", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("source_node_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "prefetch_jobs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("patient_id", sa.String(64), nullable=False, index=True),
        sa.Column("worklist_step_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.ForeignKey("scheduled_procedure_steps.id", ondelete="SET NULL"), nullable=True),
        sa.Column("source_node_id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), sa.ForeignKey("dicom_nodes.id", ondelete="CASCADE"), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="PENDING", index=True),
        sa.Column("priors_found", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("priors_retrieved", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("prefetch_jobs")
    op.drop_table("prefetch_rules")
