"""MySQL-default + performance indexing (large-archive support).

Verifies that:
  * the code default DATABASE_URL is MySQL (SQLite remains opt-in),
  * MySQL URLs always carry utf8mb4 (full Unicode for DICOM names),
  * the hot query tables expose the composite + status indexes the
    migration adds (visible to SQLAlchemy metadata for fresh installs
    and to alembic-created schemas alike).
"""

from __future__ import annotations

from app.db.models import (
    AuditLog,
    CriticalResult,
    Instance,
    RadiologyReport,
    ReplicationRecord,
    ScheduledProcedureStep,
    Series,
    Study,
    TriageFlag,
)
from app.services import db_admin


def test_default_database_is_mysql():
    """The out-of-the-box configuration targets MySQL 8.

    The test-suite env pins an isolated SQLite DATABASE_URL (conftest), so we
    assert the *code* default via the Settings class rather than the instance.
    """
    from app.core.config import Settings

    default = Settings().model_fields["DATABASE_URL"].default
    assert default.startswith("mysql+aiomysql://")
    assert "charset=utf8mb4" in default


def test_build_mysql_url_includes_utf8mb4():
    url = db_admin.build_mysql_url({
        "host": "127.0.0.1", "port": 3306,
        "user": "paxs_user", "password": "p@ss", "name": "paxs_db",
    })
    assert url.startswith("mysql+aiomysql://")
    assert url.endswith("?charset=utf8mb4")


def test_parse_db_url_handles_charset():
    p = db_admin.parse_db_url(
        "mysql+aiomysql://u:p@127.0.0.1:3306/paxs_db?charset=utf8mb4"
    )
    assert p["backend"] == "mysql"
    assert p["database"] == "paxs_db"


def test_hot_tables_declare_performance_indexes():
    """Fresh create_all must expose the same indexes the migration adds."""
    idx = {m.__tablename__: {i.name for i in m.__table__.indexes}
           for m in (Study, Series, Instance, AuditLog, ScheduledProcedureStep,
                     TriageFlag, CriticalResult, RadiologyReport, ReplicationRecord)}

    assert "ix_studies_tenant_created" in idx["studies"]
    assert "ix_studies_tenant_date" in idx["studies"]
    assert "ix_studies_state_created" in idx["studies"]
    assert "ix_series_study_number" in idx["series"]
    assert "ix_series_study_modality" in idx["series"]
    assert "ix_instances_series_number" in idx["instances"]
    assert "ix_instances_backend_created" in idx["instances"]
    assert "ix_sps_status_station" in idx["scheduled_procedure_steps"]
    assert "ix_sps_status_modality" in idx["scheduled_procedure_steps"]
    assert "ix_triage_reviewed_severity" in idx["triage_flags"]
    assert "ix_critical_status_study" in idx["critical_results"]
    assert "ix_reports_status_oru" in idx["reports"]
    assert "ix_replication_status_attempts" in idx["replication_records"]

    # Single-column statuses used by queue scans must be indexed too.
    assert "ix_audit_log_performed_at" in idx["audit_log"]
    assert "ix_audit_log_action" in idx["audit_log"]
    assert "ix_reports_oru_status" in idx["reports"]
    assert "ix_triage_flags_reviewed" in idx["triage_flags"]
