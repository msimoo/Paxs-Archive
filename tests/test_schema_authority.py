"""H11/H12/H18 — one schema authority, isolated migration target, decoupled fusion.

H11: Alembic is the single source of truth. There is no automatic stamping, and a
database whose schema is incomplete can never be declared "at head".
H12: a migration never mutates the process-global `settings.DATABASE_URL`.
H18: fusion is registered independently of the FHIR flag.
"""

from __future__ import annotations

import pathlib

import pytest
import sqlalchemy as sa

from app.core.config import settings
from app.db.models import Base
from app.services import db_admin

REPO = pathlib.Path(__file__).resolve().parent.parent


def _url(path: pathlib.Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _create_all_db(tmp_path) -> tuple[str, pathlib.Path]:
    """A database built by `create_all` with no alembic history.

    This is precisely the real-world drift scenario: every table exists but
    `alembic_version` is empty, so a pending migration collides immediately.
    """
    db_path = tmp_path / "drift.db"
    url = _url(db_path)
    engine = sa.create_engine(db_admin.to_sync_url(url))
    try:
        Base.metadata.create_all(engine)
    finally:
        engine.dispose()
    return url, db_path


# --------------------------------------------------------------------------- #
# H11 — drift detection refuses to auto-stamp
# --------------------------------------------------------------------------- #
def test_migration_on_drifted_database_does_not_stamp(tmp_path):
    """A create_all-built database must fail the migration, not be stamped."""
    url, _path = _create_all_db(tmp_path)
    before = db_admin.current_revision(url)
    assert before is None, "a create_all database has no recorded revision"

    with pytest.raises(RuntimeError) as excinfo:
        db_admin.run_migrations(url)

    message = str(excinfo.value)
    assert "already contains objects" in message
    assert "never done automatically" in message

    # The critical assertion: nothing was stamped to head.
    assert db_admin.current_revision(url) == before, (
        "the drifted database was stamped instead of reported"
    )


def test_stamp_head_refuses_an_incomplete_schema(tmp_path):
    """Stamping requires a clean parity check (H11)."""
    url, db_path = _create_all_db(tmp_path)

    # Make the schema genuinely incomplete.
    engine = sa.create_engine(db_admin.to_sync_url(url))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE report_addenda")
    finally:
        engine.dispose()

    result = db_admin.stamp_head(url)
    assert result["ok"] is False
    assert result["missing"], "the drift report must list what is missing"
    assert any("report_addenda" in item for item in result["missing"])
    assert db_admin.current_revision(url) is None, "a refused stamp must not write"


def test_stamp_head_succeeds_on_a_complete_schema(tmp_path):
    """The explicit procedure still works when parity is clean."""
    url, _path = _create_all_db(tmp_path)
    assert db_admin.schema_drift(url) == []

    result = db_admin.stamp_head(url)
    assert result["ok"] is True
    assert result["revision"], "stamping a complete schema should record a revision"


def test_schema_drift_reports_missing_objects(tmp_path):
    url, _path = _create_all_db(tmp_path)
    engine = sa.create_engine(db_admin.to_sync_url(url))
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP TABLE report_addenda")
            conn.exec_driver_sql("ALTER TABLE patients DROP COLUMN patient_phone")
    finally:
        engine.dispose()

    drift = db_admin.schema_drift(url)
    assert any("table report_addenda" in item for item in drift)
    assert any("patients.patient_phone" in item for item in drift)


# --------------------------------------------------------------------------- #
# H12 — the migration target is explicit; global settings are untouched
# --------------------------------------------------------------------------- #
def test_migration_does_not_mutate_global_database_url(tmp_path, monkeypatch):
    """H12: settings.DATABASE_URL must be identical before and after."""
    url, _path = _create_all_db(tmp_path)
    original = settings.DATABASE_URL

    with pytest.raises(RuntimeError):
        db_admin.run_migrations(url)
    assert settings.DATABASE_URL == original

    db_admin.schema_drift(url)
    assert settings.DATABASE_URL == original

    db_admin.stamp_head(url)
    assert settings.DATABASE_URL == original


def test_fresh_database_is_migrated_to_head(tmp_path):
    """A brand-new database is brought up by the chain (migrations authority)."""
    db_path = tmp_path / "fresh.db"
    url = _url(db_path)

    steps, stamped = db_admin.run_migrations(url)
    assert stamped is False
    assert db_admin.current_revision(url), "a fresh database must end at a revision"

    # The chain created the tables, and parity is clean.
    assert db_admin.schema_drift(url) == []


def test_env_py_honours_the_explicit_url():
    """env.py must prefer the caller's URL over the global settings (H12)."""
    source = (REPO / "migrations" / "env.py").read_text(encoding="utf-8")
    assert "paxs_migration_url" in source, (
        "env.py must accept an explicit migration URL instead of reading the "
        "process-global settings"
    )
    assert "_override or settings.DATABASE_URL" in source


# --------------------------------------------------------------------------- #
# H18 — fusion is independent of the FHIR flag
# --------------------------------------------------------------------------- #
def test_fusion_registration_is_not_inside_the_fhir_branch():
    """H18: turning FHIR off must not remove the fusion endpoints."""
    source = (REPO / "app" / "main.py").read_text(encoding="utf-8")
    assert "if settings.FUSION_ENABLED:" in source, "fusion needs its own flag"

    fhir_branch = source.index("if settings.FHIR_ENABLED:")
    fusion_branch = source.index("if settings.FUSION_ENABLED:")
    assert fusion_branch > fhir_branch
    # `fusion.router` must sit under the fusion flag, not the FHIR one.
    fusion_include = source.index("fusion.router")
    assert fusion_include > fusion_branch
    fhir_include = source.index("fhir.router")
    assert fhir_include < fusion_branch


def test_fusion_routes_are_registered(client):
    """With the defaults, the fusion endpoints are present on the app."""
    resp = client.get("/api/v1/fusion/1.2.3.4.5")
    assert resp.status_code != 404, "fusion routes are missing from the app"


def test_fusion_flag_exists_and_is_independent():
    assert settings.FUSION_ENABLED is True
    from app.services.settings_service import EDITABLE_KEYS

    assert "FUSION_ENABLED" in EDITABLE_KEYS
