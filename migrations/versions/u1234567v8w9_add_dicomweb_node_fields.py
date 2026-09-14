"""Add DICOMweb fields to dicom_nodes (QIDO-RS/WADO-RS remote-node transport).

Revision ID: u1234567v8w9
Revises: t0123456q6r7
Create Date: 2026-08-06
"""

from alembic import op
import sqlalchemy as sa

revision = "u1234567v8w9"
down_revision = "t0123456q6r7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("dicom_nodes", sa.Column("web_url", sa.String(512), nullable=True))
    op.add_column("dicom_nodes", sa.Column("web_username", sa.String(128), nullable=True))
    op.add_column("dicom_nodes", sa.Column("web_password", sa.String(256), nullable=True))


def downgrade() -> None:
    op.drop_column("dicom_nodes", "web_password")
    op.drop_column("dicom_nodes", "web_username")
    op.drop_column("dicom_nodes", "web_url")
