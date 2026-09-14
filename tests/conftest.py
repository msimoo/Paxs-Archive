"""Pytest configuration — MUST set environment variables before importing app.

Every test uses an isolated temp DB + storage so nothing touches a real archive.
"""

import os
import socket
import tempfile
from pathlib import Path

import pytest

# ---- isolate everything before importing the app ----
# Default to an isolated sqlite DB; allow running the suite against a real
# database (e.g. MySQL) by pre-setting DATABASE_URL, e.g.
#   DATABASE_URL=mysql+aiomysql://user:pass@127.0.0.1:3307/paxs_db pytest tests
TEST_ROOT = Path(tempfile.mkdtemp(prefix="paxs_test_"))
os.environ.setdefault(
    "DATABASE_URL", f"sqlite+aiosqlite:///{TEST_ROOT.as_posix()}/paxs.db"
)
os.environ["STORAGE_ROOT"] = str(TEST_ROOT / "storage")
os.environ["SECRET_KEY"] = "test-secret-key"
os.environ["DICOM_RECEIVER_ENABLED"] = "false"
os.environ["WORKER_ENABLED"] = "false"
os.environ["THUMBNAIL_ENABLED"] = "true"
os.environ["ALLOW_REGISTER"] = "true"
os.environ["PUBLIC_BASE_URL"] = "http://paxs.test"
os.environ["RATE_LIMIT_ENABLED"] = "false"  # rate-limit tests enable it explicitly
os.environ["HL7_ENABLED"] = "false"
# The /viewer auth gate is OFF for the general suite (viewer/perf serving
# tests fetch assets anonymously); the dedicated login-gate tests flip it on
# via monkeypatch to exercise redirects and the session cookie.
os.environ["AUTH_GATE_ENABLED"] = "false"

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
