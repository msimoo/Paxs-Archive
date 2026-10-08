"""Pytest configuration — MUST set environment variables before importing app.

Every test uses an isolated temp DB + storage so nothing touches a real archive.

Safety guard (``plan_9_2026.md`` C08): the fixtures below DROP / recreate tables
and DELETE every row, so an inherited ``DATABASE_URL`` is only honoured when it
is *demonstrably disposable* (see ``tests/_db_guard.py``). A production-shaped
URL aborts the run right here — before the application, and therefore before any
database engine, is imported.
"""

import os
import socket
import tempfile
from pathlib import Path

import pytest

from tests._db_guard import (
    EXTERNAL_OPT_IN_ENV,
    guard_database_url,
    isolated_sqlite_url,
)

# ---- isolate everything before importing the app ----
TEST_ROOT = Path(tempfile.mkdtemp(prefix="paxs_test_"))

# Validated before any app import. Raises TestDatabaseGuardError when the
# inherited URL is not a throwaway test database.
_inherited_db_url = guard_database_url(
    os.environ.get("DATABASE_URL", ""),
    opt_in=os.environ.get(EXTERNAL_OPT_IN_ENV) == "1",
)
# Direct assignment (not setdefault): the guarded value is the only one used.
os.environ["DATABASE_URL"] = _inherited_db_url or isolated_sqlite_url(
    TEST_ROOT.as_posix()
)
os.environ["STORAGE_ROOT"] = str(TEST_ROOT / "storage")
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DICOM_RECEIVER_ENABLED"] = "false"
os.environ["WORKER_ENABLED"] = "false"
os.environ["THUMBNAIL_ENABLED"] = "true"
os.environ["PUBLIC_BASE_URL"] = "http://paxs.test"
os.environ["RATE_LIMIT_ENABLED"] = "false"  # rate-limit tests enable it explicitly
os.environ["HL7_ENABLED"] = "false"
# The /viewer auth gate is OFF for the general suite (viewer/perf serving
# tests fetch assets anonymously); the dedicated login-gate tests flip it on
# via monkeypatch to exercise redirects and the session cookie.
os.environ["AUTH_GATE_ENABLED"] = "false"
# H02: registration is OFF by default in production. The suite enables it
# because many tests create their own users through /auth/register; the secure
# default itself is pinned by tests/test_registration_guard.py, which
# monkeypatches the flag off again.
os.environ["ALLOW_REGISTER"] = "true"
# C03: DICOMweb is PRIVATE by default (the shipped baseline). Much of the suite
# predates that flip and drives DICOMweb anonymously, so the test environment
# opts into the legacy public mode here. The private baseline itself is covered
# explicitly by tests/test_route_auth_matrix.py (which monkeypatches the flag
# off) and by the modules already migrated to authenticated clients.
os.environ["DICOMWEB_PUBLIC"] = "true"

from app.db.models import Base  # noqa: E402
from app.db.session import sync_engine  # noqa: E402
from app.main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def schema():
    Base.metadata.drop_all(sync_engine)
    Base.metadata.create_all(sync_engine)
    yield
    Base.metadata.drop_all(sync_engine)


@pytest.fixture()
def client(schema, monkeypatch):
    """FastAPI TestClient (runs lifespan: schema init; receiver/worker disabled).

    Points SCRIPTS_DIR at an empty temp dir so custom-script dispatch is a
    no-op in unrelated tests (the scripting tests override it themselves).
    """
    import tempfile

    from app.core.config import settings

    with tempfile.TemporaryDirectory() as _scripts_dir:
        monkeypatch.setattr(settings, "SCRIPTS_DIR", _scripts_dir)
        with TestClient(app) as c:
            with sync_engine.begin() as conn:
                for table in reversed(Base.metadata.sorted_tables):
                    conn.execute(table.delete())
            yield c


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# Authenticated clients (plan_9_2026.md C03, Option A)
#
# The PHI and admin routes require authentication, so tests that exercise them
# must present real credentials. ``client`` stays anonymous on purpose — tests
# that assert anonymous behaviour (401/redirects) keep using it, and the
# authenticated fixtures below add a valid bearer token.
# --------------------------------------------------------------------------- #
TEST_USER_PASSWORD = "TestPassw0rd!2026"


def _create_test_user(username: str, role: str) -> None:
    """Insert an active user with the given role (idempotent per test)."""
    from app.core.security import hash_password
    from app.db.models import User

    with sync_engine.begin() as conn:
        existing = conn.execute(
            User.__table__.select().where(User.__table__.c.username == username)
        ).first()
        if existing is None:
            conn.execute(
                User.__table__.insert().values(
                    username=username,
                    password_hash=hash_password(TEST_USER_PASSWORD),
                    full_name=f"Test {role.title()}",
                    role=role,
                    is_active=True,
                )
            )


def _bearer_token(username: str, role: str) -> str:
    from app.core.config import settings
    from app.core.security import create_access_token

    return create_access_token(username, role, settings.SECRET_KEY, 60)


class _AuthedClient:
    """TestClient proxy that adds an Authorization header to every request.

    A proxy (rather than mutating ``client.headers``) so that ``client`` and an
    authenticated client can be used side by side in one test without the
    "anonymous" caller silently inheriting the token.
    """

    def __init__(self, client, headers: dict[str, str]):
        self._client = client
        self._headers = dict(headers)

    def request(self, method: str, url: str, **kwargs):
        supplied = dict(kwargs.pop("headers", None) or {})
        merged = {**self._headers, **supplied}
        return self._client.request(method, url, headers=merged, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def put(self, url, **kwargs):
        return self.request("PUT", url, **kwargs)

    def patch(self, url, **kwargs):
        return self.request("PATCH", url, **kwargs)

    def delete(self, url, **kwargs):
        return self.request("DELETE", url, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)


@pytest.fixture()
def auth_headers(client) -> dict[str, str]:
    """Bearer headers for a valid ADMIN user in the current database."""
    _create_test_user("test_admin", "ADMIN")
    return {"Authorization": f"Bearer {_bearer_token('test_admin', 'ADMIN')}"}


@pytest.fixture()
def auth_client(client, auth_headers):
    """`client` with an ADMIN bearer token (for PHI / admin routes)."""
    return _AuthedClient(client, auth_headers)


@pytest.fixture()
def radiologist_client(client):
    """`client` with a RADIOLOGIST bearer token (destructive-role routes)."""
    _create_test_user("test_radiologist", "RADIOLOGIST")
    return _AuthedClient(
        client,
        {"Authorization": f"Bearer {_bearer_token('test_radiologist', 'RADIOLOGIST')}"},
    )


@pytest.fixture()
def anonymous_client(client):
    """Explicit alias for the anonymous client, to make intent obvious.

    Use this in tests that assert a request is rejected without credentials.
    """
    return client


@pytest.fixture()
def receiver(schema):
    """Start the DICOM SCP on a free port; yields (port, AE object).
    Truncates the DB first so audit/count assertions are deterministic."""
    from app.dicom.receiver import shutdown_receiver, start_receiver

    with sync_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            conn.execute(table.delete())

    port = free_port()
    ae = start_receiver("127.0.0.1", port, ae_title="PAXS_TEST_SCP")
    yield port, ae
    shutdown_receiver()
