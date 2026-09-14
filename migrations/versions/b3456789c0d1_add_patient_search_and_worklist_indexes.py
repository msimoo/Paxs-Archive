"""Add additional performance indexes for patient lookup and reading worklist.

Revision ID: b3456789c0d1
Revises: a2345678b9c0
Create Date: 2026-08-12
"""

from alembic import op

revision = "b3456789c0d1"
down_revision = "a2345678b9c0"
branch_labels = None
depends_on = None

INDEXES = [
    ("ix_patients_name_id", "patients", ["patient_name", "patient_id"]),
    ("ix_patients_birth_date", "patients", ["patient_birth_date"]),
    ("ix_studies_assigned_rad", "studies", ["assigned_radiologist", "stat_flag"]),
    ("ix_studies_stat_flag", "studies", ["stat_flag"]),
]


def upgrade() -> None:
    for name, table, cols in INDEXES:
        op.create_index(name, table, cols)


def downgrade() -> None:
    for name, table, _cols in reversed(INDEXES):
        op.drop_index(name, table_name=table)
