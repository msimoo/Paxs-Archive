"""Tests for the disposable-database guard (``plan_9_2026.md`` C08).

The suite's fixtures drop, recreate and truncate tables, so an inherited
``DATABASE_URL`` must never be able to point them at a real archive. These tests
cover the guard logic directly and then prove the guard actually fires when
``tests/conftest.py`` is imported with a production-shaped URL.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests._db_guard import (
    EXTERNAL_OPT_IN_ENV,
    TEST_DB_SENTINEL,
    DatabaseGuardError,
    backend_of,
    database_name,
    guard_database_url,
    is_disposable,
    isolated_sqlite_url,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:///./paxs_test.db", "paxs_test.db"),
        ("sqlite+aiosqlite:////tmp/paxs_test_abc/paxs_test.db", "paxs_test.db"),
        ("mysql+aiomysql://u:p@127.0.0.1:3306/paxs_test_db?charset=utf8mb4", "paxs_test_db"),
        ("postgresql+asyncpg://u:p@h:5432/paxs_test", "paxs_test"),
        ("sqlite+aiosqlite://", ""),
        ("not-a-url", ""),
        ("", ""),
    ],
)
def test_database_name_parsing(url: str, expected: str):
    assert database_name(url) == expected


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("sqlite+aiosqlite:///x.db", "sqlite"),
        ("mysql+aiomysql://u@h/db", "mysql"),
        ("postgresql+psycopg2://u@h/db", "postgresql"),
        ("", ""),
    ],
)
def test_backend_of(url: str, expected: str):
    assert backend_of(url) == expected


# --------------------------------------------------------------------------- #
# the guard
# --------------------------------------------------------------------------- #
def test_blank_url_is_allowed_and_means_use_the_isolated_default():
    assert guard_database_url("") == ""
    assert guard_database_url("   ") == ""


def test_isolated_default_is_itself_sentinel_bearing():
    url = isolated_sqlite_url("/tmp/paxs_test_abc")
    assert url.endswith("/paxs_test.db")
    assert is_disposable(url), "the default target must satisfy the sentinel policy"


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///./paxs_test.db",
        "sqlite+aiosqlite:////tmp/paxs_test_x/paxs_test.db",
    ],
)
def test_disposable_sqlite_urls_are_accepted(url: str):
    assert guard_database_url(url) == url


@pytest.mark.parametrize(
    "url",
    [
        # the real project database (no sentinel)
        "sqlite+aiosqlite:///./paxs.db",
        "sqlite+aiosqlite:///D:/Paxs Archive/paxs.db",
        # production-shaped MySQL / PostgreSQL targets
        "mysql+aiomysql://paxs_user:paxs@127.0.0.1:3306/paxs_db?charset=utf8mb4",
        "mysql+pymysql://root:@prod-db.internal:3306/paxs_db",
        "postgresql+asyncpg://paxs:paxs@10.0.0.9:5432/paxs",
    ],
)
def test_non_test_databases_are_refused(url: str):
    with pytest.raises(DatabaseGuardError) as exc:
        guard_database_url(url)
    assert "Refusing to run the test suite" in str(exc.value)


def test_refusal_message_is_actionable():
    with pytest.raises(DatabaseGuardError) as exc:
        guard_database_url("mysql+aiomysql://u:p@h:3306/paxs_db")
    message = str(exc.value)
    # The sentinel refusal names the requirement and the safe alternative.
    assert TEST_DB_SENTINEL in message
    assert "unset DATABASE_URL" in message


def test_opt_in_requirement_is_explained_separately():
    with pytest.raises(DatabaseGuardError) as exc:
        guard_database_url("mysql+aiomysql://u:p@h:3306/paxs_test_db")
    assert EXTERNAL_OPT_IN_ENV in str(exc.value)


def test_external_backend_requires_sentinel_plus_explicit_opt_in():
    url = "mysql+aiomysql://u:p@127.0.0.1:3307/paxs_test_db?charset=utf8mb4"
    # Sentinel present but no acknowledgement -> still refused.
    with pytest.raises(DatabaseGuardError) as exc:
        guard_database_url(url, opt_in=False)
    assert EXTERNAL_OPT_IN_ENV in str(exc.value)
    # Sentinel + acknowledgement -> allowed.
    assert guard_database_url(url, opt_in=True) == url


def test_opt_in_alone_cannot_bypass_the_sentinel():
    """The acknowledgement must never be a substitute for the sentinel."""
    with pytest.raises(DatabaseGuardError):
        guard_database_url("mysql+aiomysql://u:p@h:3306/paxs_db", opt_in=True)


# --------------------------------------------------------------------------- #
# end-to-end: conftest must abort before importing the application
# --------------------------------------------------------------------------- #
def _run_conftest_import(database_url: str | None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop("DATABASE_URL", None)
    env.pop(EXTERNAL_OPT_IN_ENV, None)
    if database_url is not None:
        env["DATABASE_URL"] = database_url
    return subprocess.run(
        [sys.executable, "-c", "import tests.conftest"],
        cwd=str(PROJECT_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )


def test_conftest_aborts_on_production_shaped_url_before_app_import():
    proc = _run_conftest_import(
        "mysql+aiomysql://paxs_user:paxs@prod-db.internal:3306/paxs_db"
    )
    output = proc.stdout + proc.stderr
    assert proc.returncode != 0, "an inherited production URL must abort the run"
    assert "Refusing to run the test suite" in output
    # The guard must fire *before* the app (and its engines) are imported.
    assert "app.db.session" not in output


def test_conftest_still_imports_with_no_inherited_url():
    """Positive control: the default isolated path is unaffected."""
    proc = _run_conftest_import(None)
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"default import path broken:\n{output}"
