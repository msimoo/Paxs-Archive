"""Custom server scripts (roadmap #43) — runtime Python scripting.

Covers the scripting_service (importlib loading, hot reload, event dispatch,
crash isolation, enable/disable) and the admin API (/api/v1/scripts):
list, reload, create, toggle, run, source — with auth gates.
"""

from __future__ import annotations

import os
import time

import pytest

from app.core.config import settings
from app.services import scripting_service, storage_service


def storage_save(ds):
    """Save a dataset via the storage facade (returns key, sha)."""
    return storage_service.save_dataset(ds)

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
LOGGER = """\
import os

def _log(line):
    with open(os.environ["TEST_SCRIPT_LOG"], "a", encoding="utf-8") as fh:
        fh.write(line + "\\n")

def handle(event, ctx):
    _log("HANDLE:" + event)
"""


def _write(directory, name, source):
    p = directory / f"{name}.py"
    p.write_text(source, encoding="utf-8")
    return p


def _bump(path, seconds=1.0):
    """Force a newer mtime (hot reload uses mtime to detect edits)."""
    os.utime(path, (time.time() + seconds, time.time() + seconds))


def _wait_for_log(log_path, needle, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8")
            if needle in text:
                return text
        time.sleep(0.05)
    return log_path.read_text(encoding="utf-8") if log_path.exists() else ""


@pytest.fixture(autouse=True)
def _scripts_env(client, tmp_path, monkeypatch):
    """Point SCRIPTS_DIR at a per-test dir + reset the module cache.

    Depends on `client` so this fixture's monkeypatch runs *after* the conftest
    client fixture (which points SCRIPTS_DIR at an empty temp dir) — the last
    setattr wins, so the scripts land where these tests expect.
    """
    monkeypatch.setattr(settings, "SCRIPTS_DIR", str(tmp_path))
    monkeypatch.setattr(settings, "SCRIPTS_ENABLED", True)
    scripting_service._cache.clear()
    log_file = tmp_path / "events.log"
    monkeypatch.setenv("TEST_SCRIPT_LOG", str(log_file))
    yield tmp_path, log_file
    scripting_service._cache.clear()


# --------------------------------------------------------------------------- #
# Service behavior
# --------------------------------------------------------------------------- #
def test_empty_dir_lists_nothing(_scripts_env):
    assert scripting_service.list_scripts() == []


def test_fire_invokes_handler(_scripts_env):
    tmp, logf = _scripts_env
    _write(tmp, "hello", LOGGER)
    scripting_service.fire("StudyReceived", study_uid="1.2.3.4.5")
    assert "HANDLE:StudyReceived" in _wait_for_log(logf, "HANDLE:StudyReceived")


def test_event_filter_respected(_scripts_env):
    tmp, logf = _scripts_env
    _write(tmp, "only_study", LOGGER.replace(
        "def handle(event, ctx):", 'EVENTS = ["StudyReceived"]\n\ndef handle(event, ctx):'))
    scripting_service.fire("StudyDeleted", study_uid="x")
    time.sleep(0.4)  # give the (skipped) thread time — must NOT log
    assert not logf.exists() or "HANDLE:" not in logf.read_text()
    scripting_service.fire("StudyReceived", study_uid="y")
    assert "HANDLE:StudyReceived" in _wait_for_log(logf, "HANDLE:StudyReceived")


def test_on_event_handler(_scripts_env):
    tmp, logf = _scripts_env
    _write(tmp, "per_event", """\
import os

def on_study_received(ctx):
    with open(os.environ["TEST_SCRIPT_LOG"], "a", encoding="utf-8") as fh:
        fh.write("ON:" + str(ctx.payload.get("study_uid")) + "\\n")
""")
    scripting_service.fire("StudyReceived", study_uid="1.2.3.9")
    assert "ON:1.2.3.9" in _wait_for_log(logf, "ON:1.2.3.9")


def test_crash_isolation(_scripts_env):
    tmp, logf = _scripts_env
    _write(tmp, "crashes", "def handle(event, ctx):\n    raise RuntimeError(\"boom\")\n")
    _write(tmp, "healthy", LOGGER)
    scripting_service.fire("StudyReceived")
    assert "HANDLE:StudyReceived" in _wait_for_log(logf, "HANDLE:StudyReceived")


def test_hot_reload(_scripts_env):
    tmp, logf = _scripts_env
    path = _write(tmp, "reload_me", """\
import os

def handle(event, ctx):
    with open(os.environ["TEST_SCRIPT_LOG"], "a", encoding="utf-8") as fh:
        fh.write("ONE\\n")
""")
    scripting_service.fire("StudyReceived")
    assert "ONE" in _wait_for_log(logf, "ONE")
    # edit the file (newer mtime) -> next dispatch re-imports
    _write(tmp, "reload_me", """\
import os

def handle(event, ctx):
    with open(os.environ["TEST_SCRIPT_LOG"], "a", encoding="utf-8") as fh:
        fh.write("TWO\\n")
""")
    _bump(path)
    scripting_service.fire("StudyReceived")
    text = _wait_for_log(logf, "TWO")
    assert "TWO" in text


def test_disabled_via_marker(_scripts_env):
    tmp, logf = _scripts_env
    _write(tmp, "off", LOGGER)
    (tmp / "off.py.disabled").touch()
    scripting_service.fire("StudyReceived")
    time.sleep(0.4)
    assert not logf.exists() or "HANDLE:" not in logf.read_text()


def test_ingest_triggers_study_received_hook(_scripts_env):
    """End-to-end: the real ingest path fires StudyReceived into the scripts
    (indexing_service -> scripting_service.fire -> handler)."""
    import asyncio

    from app.dicom.parser import parse_metadata
    from app.db.session import AsyncSessionLocal
    from app.services import indexing_service
    from tests.helpers import make_instance

    tmp, logf = _scripts_env
    _write(tmp, "ingest_hook", LOGGER)
    ds = make_instance(study_uid="1.2.3.9.555.1", series_uid="1.2.3.4.555.1",
                       sop_uid="1.2.3.5.555.1", instance_num=1, patient_id="P-HOOK")
    key, sha = storage_save(ds)

    async def _index():
        async with AsyncSessionLocal() as s:
            async with s.begin():
                await indexing_service.index_new_instance(
                    s, parse_metadata(ds), key, sha, performed_by="test")

    asyncio.run(_index())
    assert "HANDLE:StudyReceived" in _wait_for_log(logf, "HANDLE:StudyReceived")


def test_run_script_now_sync(_scripts_env):
    tmp, logf = _scripts_env
    _write(tmp, "sync", LOGGER)
    res = scripting_service.run_script_now("sync", "StudyReceived", {"study_uid": "x"})
    assert res["ok"] is True
    assert "HANDLE:StudyReceived" in logf.read_text()
    # unknown / disabled / crashing
    assert scripting_service.run_script_now("nope", "StudyReceived", {})["ok"] is False
    (tmp / "sync.py.disabled").touch()
    assert scripting_service.run_script_now("sync", "StudyReceived", {})["ok"] is False


# --------------------------------------------------------------------------- #
# Admin API
# --------------------------------------------------------------------------- #
def _register(client, username, password="secret123"):
    return client.post("/api/v1/auth/register",
                       json={"username": username, "password": password})


def _auth(client, username, password="secret123"):
    reg = _register(client, username, password)
    if reg.status_code == 201:
        return {"Authorization": f"Bearer {reg.json()['access_token']}"}
    return {"Authorization": "Bearer " + client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    ).json()["access_token"]}


@pytest.fixture(autouse=True)
def admin(client):
    """A seeded ADMIN (also requested by name from API tests)."""
    return _auth(client, "script_admin")


def test_api_requires_auth(client):
    # The autouse `admin` fixture logged in on this client, and the API also
    # accepts cookie sessions (C03), so clear the jar to be anonymous.
    client.cookies.clear()
    assert client.get("/api/v1/scripts").status_code == 401
    assert client.post("/api/v1/scripts/reload").status_code == 401


def test_api_requires_admin(client, admin):
    h = _auth(client, "plain_doc", "secret123")
    assert client.get("/api/v1/scripts", headers=h).status_code == 403


def test_api_list_and_create(client, admin, _scripts_env):
    tmp, _ = _scripts_env
    res = client.get("/api/v1/scripts", headers=admin)
    assert res.status_code == 200
    assert res.json()["scripts"] == []

    tpl = 'EVENTS = ["StudyReceived"]\n\ndef on_study_received(ctx):\n    ctx.log.info("hi")\n'
    res = client.put("/api/v1/scripts/mine", content=tpl, headers={
        "Content-Type": "text/plain", **admin})
    assert res.status_code == 201, res.text
    assert (tmp / "mine.py").exists()

    res = client.get("/api/v1/scripts", headers=admin)
    names = [s["name"] for s in res.json()["scripts"]]
    assert "mine" in names
    mine = next(s for s in res.json()["scripts"] if s["name"] == "mine")
    assert mine["enabled"] is True and mine["loaded"] is True


def test_api_toggle_run_source(client, admin, _scripts_env):
    tmp, _ = _scripts_env
    (tmp / "probe.py").write_text(
        'import os\n\ndef handle(event, ctx):\n    open(os.environ["TEST_SCRIPT_LOG"], "a").write("RAN\\n")\n',
        encoding="utf-8")
    res = client.post("/api/v1/scripts/probe/toggle", headers=admin)
    assert res.status_code == 200 and res.json()["enabled"] is False
    res = client.post("/api/v1/scripts/probe/run", headers=admin, json={
        "event": "StudyReceived", "payload": {"study_uid": "1.2.3"}})
    assert res.status_code == 422  # disabled -> refused
    client.post("/api/v1/scripts/probe/toggle", headers=admin)
    res = client.post("/api/v1/scripts/probe/run", headers=admin, json={
        "event": "StudyReceived", "payload": {"study_uid": "1.2.3"}})
    assert res.status_code == 200
    assert "RAN" in (tmp / "events.log").read_text()

    res = client.get("/api/v1/scripts/probe/source", headers=admin)
    assert res.status_code == 200 and "def handle" in res.json()["source"]
    assert client.get("/api/v1/scripts/nope/source", headers=admin).status_code == 404
