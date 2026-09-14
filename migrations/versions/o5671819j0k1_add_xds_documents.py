"""Add xds_documents table for XDS-I.b support.

Revision ID: o5671819j0k1
Revises: n4561718i9j0
Create Date: 2026-08-05
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "o5671819j0k1"
down_revision = "n4561718i9j0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "xds_documents",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer, "sqlite"), primary_key=True),
        sa.Column("entry_uuid", sa.String(128), nullable=False, unique=True, index=True),
        sa.Column("unique_id", sa.String(128), nullable=False, unique=True, index=True),
        sa.Column("patient_id", sa.String(128), nullable=False, index=True),
        sa.Column("study_instance_uid", sa.String(128), nullable=True, index=True),
        sa.Column("document_title", sa.String(255), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False, server_default="application/dicom"),
        sa.Column("class_code", sa.String(64), nullable=False, server_default="IMAGES"),
        sa.Column("type_code", sa.String(64), nullable=False, server_default="RADIOLOGY"),
        sa.Column("format_code", sa.String(128), nullable=False, server_default="urn:ihe:rad:TEXT:2011"),
        sa.Column("repository_unique_id", sa.String(128), nullable=False, server_default="1.2.826.0.1.3680043.8.498.REPO"),
        sa.Column("availability_status", sa.String(32), nullable=False, server_default="APPROVED", index=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sha256_hash", sa.String(64), nullable=True),
        sa.Column("file_path", sa.String(768), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table("xds_documents")
