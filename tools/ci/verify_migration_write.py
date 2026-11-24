"""Verify one committed write on an Alembic-migrated CI database.

Plan: PAXS-READINESS-2026-10-24, P1.5. This deliberately bypasses pytest's
``create_all`` fixture so the check proves that the migration chain itself
created the schema used by the application model.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from uuid import uuid4

from tests._db_guard import backend_of, guard_database_url


def migration_target(environ: Mapping[str, str]) -> tuple[str, str]:
    """Return a guarded SQLite/MySQL URL and its backend without importing app."""
    url = guard_database_url(
        environ.get("DATABASE_URL", ""),
        opt_in=environ.get("PAXS_TEST_ALLOW_EXTERNAL_DB") == "1",
    )
    backend = backend_of(url)
    if backend not in {"sqlite", "mysql"}:
        raise RuntimeError("CI migration write verification requires SQLite or MySQL")
    return url, backend


def _require_migration_head(url: str) -> str:
    """Refuse to write unless the disposable target is at the current Alembic head."""
    from app.services.db_admin import current_revision, head_revision

    expected = head_revision()
    current = current_revision(url)
    if not expected or current != expected:
        raise RuntimeError("CI database is not at the repository Alembic head")
    return current


def _verify_mysql_version(engine: object) -> str:
    """Require a MySQL 8 service and return its exact server version."""
    from sqlalchemy.engine import Engine

    if not isinstance(engine, Engine):
        raise TypeError("migration smoke engine was not created")
    with engine.connect() as connection:
        version = str(connection.exec_driver_sql("SELECT VERSION()").scalar_one())
    major = version.split(".", 1)[0]
    if major != "8":
        raise RuntimeError(f"MySQL 8 is required; detected major version {major}")
    return version


def _exercise_first_write(url: str, backend: str) -> str | None:
    """Commit, read back, and remove a probe setting; return MySQL version if used."""
    from sqlalchemy import create_engine, delete, select
    from sqlalchemy.orm import Session
    from sqlalchemy.pool import NullPool

    from app.db.models import ServerSetting
    from app.db.session import to_sync_url

    engine = create_engine(to_sync_url(url), poolclass=NullPool)
    key = f"ci_migration_probe_{uuid4().hex[:32]}"
    inserted = False
    mysql_version = None
    try:
        if backend == "mysql":
            mysql_version = _verify_mysql_version(engine)
        with Session(engine) as session:
            session.add(ServerSetting(key=key, value="migration-write-probe"))
            session.commit()
            inserted = True
            value = session.scalar(
                select(ServerSetting.value).where(ServerSetting.key == key)
            )
            if value != "migration-write-probe":
                raise RuntimeError("migrated setting write did not round-trip")
    finally:
        try:
            if inserted:
                with engine.begin() as connection:
                    connection.execute(
                        delete(ServerSetting).where(ServerSetting.key == key)
                    )
        finally:
            engine.dispose()
    return mysql_version


def main() -> None:
    """Run the safety checks and report backend, revision, and server version."""
    url, backend = migration_target(os.environ)
    revision = _require_migration_head(url)
    mysql_version = _exercise_first_write(url, backend)
    version_detail = f" mysql_version={mysql_version}" if mysql_version else ""
    print(
        "migrated first-write verification passed: "
        f"backend={backend}{version_detail} revision={revision}"
    )


if __name__ == "__main__":
    main()
