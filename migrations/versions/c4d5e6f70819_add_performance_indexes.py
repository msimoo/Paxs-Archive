"""add performance indexes for large archives

MySQL 8 is the default database (roadmap request). This migration adds the
indexes every hot query path needs so large archives stay fast:

  * tenant-scoped study browsing (scope + date/created sort) — composites
  * viewer/series/instance traversal (study -> series -> instance, ordered)
  * status-filtered queues (worklist, triage, critical, reports, replication)
  * audit trail (action + performed_at) and storage-backend scans

Revision ID: c4d5e6f70819
Revises: b3c4d5e6f708
Create Date: 2026-08-07
"""

from alembic import op


revision = "c4d5e6f70819"
down_revision = "b3c4d5e6f708"
branch_labels = None
depends_on = None


INDEXES = [
    # --- studies ---
    ("ix_studies_patient_id", "studies", ["patient_id"]),
    ("ix_studies_created_at", "studies", ["created_at"]),
    ("ix_studies_tenant_created", "studies", ["tenant_id", "created_at"]),
    ("ix_studies_tenant_date", "studies", ["tenant_id", "study_date"]),
    ("ix_studies_state_created", "studies", ["lifecycle_state", "created_at"]),
    # --- series ---
    ("ix_series_study_id", "series", ["study_id"]),
    ("ix_series_triage_status", "series", ["triage_status"]),
    ("ix_series_study_number", "series", ["study_id", "series_number"]),
    ("ix_series_study_modality", "series", ["study_id", "modality"]),
    # --- instances ---
    ("ix_instances_series_id", "instances", ["series_id"]),
    ("ix_instances_series_number", "instances", ["series_id", "instance_number"]),
    ("ix_instances_backend_created", "instances", ["storage_backend", "created_at"]),
    # --- audit trail ---
    ("ix_audit_log_action", "audit_log", ["action"]),
    ("ix_audit_log_performed_at", "audit_log", ["performed_at"]),
    # --- scheduled procedure steps (MWL) ---
    ("ix_sps_status_station", "scheduled_procedure_steps", ["status", "scheduled_station_aet"]),
    ("ix_sps_status_modality", "scheduled_procedure_steps", ["status", "modality"]),
    # --- triage / critical ---
    ("ix_triage_flags_reviewed", "triage_flags", ["reviewed"]),
    ("ix_triage_reviewed_severity", "triage_flags", ["reviewed", "severity"]),
    ("ix_critical_status_study", "critical_results", ["status", "study_instance_uid"]),
    ("ix_critical_created", "critical_results", ["created_at"]),
    # --- reports ---
    ("ix_reports_oru_status", "reports", ["oru_status"]),
    ("ix_reports_status_oru", "reports", ["status", "oru_status"]),
    # --- DR replication ---
    ("ix_replication_status_attempts", "replication_records", ["status", "attempts"]),
]


def upgrade() -> None:
    for name, table, cols in INDEXES:
        op.create_index(name, table, cols)


def downgrade() -> None:
    for name, _table, _cols in reversed(INDEXES):
        op.drop_index(name, table_name=_table)
