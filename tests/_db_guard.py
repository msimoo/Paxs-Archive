"""Disposable-database guard for the test suite (``plan_9_2026.md`` C08).

The fixtures in ``tests/conftest.py`` drop and recreate every table and
truncate every row, so they must never be pointed at a real archive. This
module is deliberately side-effect free — it can be imported without touching
the environment — so the guard itself is unit-testable.

Policy
------
* No inherited ``DATABASE_URL`` at all  -> the caller uses an isolated temp
  SQLite database (the safe default).
* ``sqlite`` URL                        -> accepted only when the database file
  name contains the sentinel ``test`` (``:memory:`` is always accepted, it is
  inherently disposable).
* Any other backend (MySQL, PostgreSQL, ...) -> the sentinel is required *and*
  the operator must set ``PAXS_TEST_ALLOW_EXTERNAL_DB=1``, because a URL that
  merely looks like a test target can still be a live server.
"""

from __future__ import annotations

# A database is only accepted as a test target when its name carries this
# marker, e.g. "paxs_test", "test_paxs", "paxs_test_db".
TEST_DB_SENTINEL = "test"

# Second, explicit acknowledgement required for non-SQLite backends.
EXTERNAL_OPT_IN_ENV = "PAXS_TEST_ALLOW_EXTERNAL_DB"

# In-memory SQLite is disposable by construction.
_SQLITE_MEMORY_MARKERS = (":memory:", "mode=memory")


class DatabaseGuardError(RuntimeError):
    """Raised when an inherited DATABASE_URL is not demonstrably disposable."""


def database_name(url: str) -> str:
    """Database name component of a SQLAlchemy URL (``""`` when absent)."""
    text = (url or "").strip()
    if "://" not in text:
        return ""
    tail = text.split("://", 1)[1]
    for sep in ("?", "#"):
        tail = tail.split(sep, 1)[0]
    if "/" not in tail:
        return ""
    return tail.rsplit("/", 1)[1].strip()


def backend_of(url: str) -> str:
    """Driver-agnostic backend name, e.g. ``sqlite`` / ``mysql``."""
    text = (url or "").strip().lower()
    if "://" not in text:
        return ""
    return text.split("://", 1)[0].split("+", 1)[0]


def is_disposable(url: str) -> bool:
    """True when ``url`` carries the test sentinel (or is in-memory SQLite)."""
    text = (url or "").strip().lower()
    if not text:
        return False
    if backend_of(text) == "sqlite" and any(m in text for m in _SQLITE_MEMORY_MARKERS):
        return True
    return TEST_DB_SENTINEL in database_name(text).lower()


def guard_database_url(url: str, *, opt_in: bool = False) -> str:
    """Validate an inherited ``DATABASE_URL``; return it unchanged when safe.

    Raises :class:`DatabaseGuardError` when the URL is not demonstrably a
    disposable test database. An empty/blank value returns ``""`` so the caller
    can substitute its own isolated default.
    """
    text = (url or "").strip()
    if not text:
        return ""

    backend = backend_of(text)
    name = database_name(text)

    if not is_disposable(text):
        raise DatabaseGuardError(
            "Refusing to run the test suite: the inherited DATABASE_URL is not a "
            "disposable test database.\n"
            f"  backend       : {backend or 'unknown'}\n"
            f"  database name : {name or '(none)'}\n"
            f"  required      : the database name must contain {TEST_DB_SENTINEL!r} "
            "(e.g. paxs_test)\n"
            "The fixtures drop, recreate and truncate tables, so this guard exists "
            "to protect a real archive.\n"
            "Fix: unset DATABASE_URL to use the isolated temporary SQLite database, "
            f"or point it at a throwaway database whose name contains {TEST_DB_SENTINEL!r}."
        )

    if backend != "sqlite" and not opt_in:
        raise DatabaseGuardError(
            "Refusing to run the test suite against a non-SQLite backend without an "
            "explicit acknowledgement.\n"
            f"  backend       : {backend}\n"
            f"  database name : {name}\n"
            f"  required      : {EXTERNAL_OPT_IN_ENV}=1 in the environment\n"
            "The fixtures DROP and TRUNCATE tables on the configured database."
        )

    return text


def isolated_sqlite_url(test_root: str) -> str:
    """The safe default URL: a SQLite file inside the per-run temp directory.

    The file name carries the test sentinel so the default target is itself
    self-evidently disposable.
    """
    root = str(test_root).replace("\\", "/")
    return f"sqlite+aiosqlite:///{root}/paxs_test.db"
