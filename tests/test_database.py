"""Settings → Database tab API (/api/v1/db).

Covers the auth gates, the status payload, MySQL connection probing, saving a
backend configuration (server_settings + .env), initializing a brand-new
database with Alembic + admin seeding, and the backup lifecycle
(create / list / download / delete).

The .env writer and the backup directory are redirected to temp paths so
nothing touches the repository.
"""

import json
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app.core.config import settings as runtime_settings
from app.db.session import to_sync_url


def _auth(client, username="db_admin", password="secret123", role="ADMIN"):
    from tests.helpers import auth_for_role

    return auth_for_role(client, username, role=role or "ADMIN", password=password)


@pytest.fixture(autouse=True)
def _isolate_db_admin():
    """Redirect .env writes + backup dir to temp paths; restore afterwards."""
    from app.db.models import ServerSetting
    from app.db.session import sync_engine

    tmp = Path(tempfile.mkdtemp(prefix="paxs_dbadmin_"))
    snap_env, snap_dir = runtime_settings.ENV_FILE_PATH, runtime_settings.BACKUP_DIR
    runtime_settings.ENV_FILE_PATH = str(tmp / "test.env")
    runtime_settings.BACKUP_DIR = str(tmp / "backups")
    yield tmp
    runtime_settings.ENV_FILE_PATH = snap_env
    runtime_settings.BACKUP_DIR = snap_dir
    with sync_engine.begin() as conn:
        conn.execute(ServerSetting.__table__.delete())


def _saved_config_row():
    from app.db.models import ServerSetting
    from app.db.session import sync_engine

    with sync_engine.connect() as conn:
        row = conn.execute(
            select(ServerSetting).where(ServerSetting.key == "db_config")
        ).first()
    return json.loads(row.value) if row else None


# --------------------------------------------------------------------------- #
# Auth gates
# --------------------------------------------------------------------------- #
def test_db_status_requires_auth(client):
    assert client.get("/api/v1/db/status").status_code == 401


def test_db_actions_require_admin(client):
    h = _auth(client, username="dr_db", role="RADIOLOGIST")
    assert client.post("/api/v1/db/test", json={}, headers=h).status_code == 403
    assert client.post("/api/v1/db/configure", json={}, headers=h).status_code == 403
    assert client.post("/api/v1/db/init", json={}, headers=h).status_code == 403
    assert client.post("/api/v1/db/backup", headers=h).status_code == 403
    assert client.get("/api/v1/db/backups", headers=h).status_code == 403


# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #
def test_db_status_shape(client):
    h = _auth(client)
    res = client.get("/api/v1/db/status", headers=h)
    assert res.status_code == 200
    body = res.json()
    assert body["backend"] in ("sqlite", "mysql")
    assert "url_masked" in body
    assert body["admin"] is True
    assert body["head_revision"]  # migrations directory has a head
    assert body["saved_config_present"] is False
    assert body["backup_count"] == 0

    # non-admins see status but read-only
    h2 = _auth(client, username="dr_view", role="RADIOLOGIST")
    body2 = client.get("/api/v1/db/status", headers=h2).json()
    assert body2["admin"] is False


# --------------------------------------------------------------------------- #
# Connection test
# --------------------------------------------------------------------------- #
def test_db_test_bad_credentials(client):
    h = _auth(client)
    res = client.post("/api/v1/db/test", headers=h, json={
        "backend": "mysql", "host": "127.0.0.1", "port": 1,
        "user": "nobody", "password": "x", "name": "nope",
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is False
    assert body["error"]


def test_db_test_rejects_sqlite(client):
    h = _auth(client)
    body = client.post("/api/v1/db/test", headers=h,
                       json={"backend": "sqlite"}).json()
    assert body["ok"] is False


def test_db_test_validates_fields(client):
    h = _auth(client)
    # port out of range
    res = client.post("/api/v1/db/test", headers=h, json={
        "backend": "mysql", "host": "h", "port": 99999, "user": "u", "name": "n"})
    assert res.status_code == 422
    # missing required fields
    res = client.post("/api/v1/db/test", headers=h, json={"backend": "mysql"})
    assert res.status_code == 422


# --------------------------------------------------------------------------- #
# Configure
# --------------------------------------------------------------------------- #
def test_db_configure_mysql_persists(client, _isolate_db_admin):
    h = _auth(client)
    res = client.post("/api/v1/db/configure", headers=h, json={
        "backend": "mysql", "host": "127.0.0.1", "port": 3307,
        "user": "paxs_user", "password": "pw", "name": "paxs_db",
        "persist_env": True,
    })
    assert res.status_code == 200
    body = res.json()
    assert body["ok"] is True
    assert body["backend"] == "mysql"
    assert body["restart"] is True
    assert "pw" not in body["url_masked"]

    # persisted to the server_settings row — non-secret fields only
    saved = _saved_config_row()
    assert saved["backend"] == "mysql"
    assert saved["host"] == "127.0.0.1"
    assert saved["has_password"] is True
    assert "password" not in saved  # credentials never stored in the DB

    # the password lives only in the (redirected) .env file
    env = (_isolate_db_admin / "test.env").read_text(encoding="utf-8")
    assert ('DATABASE_URL="mysql+aiomysql://paxs_user:pw@127.0.0.1:3307/paxs_db?charset=utf8mb4"' in env)

    # status now reports the saved config (without exposing the password)
    status = client.get("/api/v1/db/status", headers=h).json()
    assert status["saved_config_present"] is True
    assert status["saved"]["backend"] == "mysql"
    assert "password" not in status["saved"]


def test_db_configure_sqlite_resets(client, _isolate_db_admin):
    h = _auth(client)
    client.post("/api/v1/db/configure", headers=h, json={
        "backend": "mysql", "host": "h", "user": "u", "name": "n"})
    res = client.post("/api/v1/db/configure", headers=h, json={
        "backend": "sqlite", "persist_env": True})
    assert res.status_code == 200
    assert res.json()["backend"] == "sqlite"
    assert _saved_config_row() is None
    env = (_isolate_db_admin / "test.env").read_text(encoding="utf-8")
    assert "DATABASE_URL=\"sqlite+aiosqlite:///./paxs.db\"" in env


# --------------------------------------------------------------------------- #
# Initialize (new installation)
# --------------------------------------------------------------------------- #
def test_db_init_new_installation(client, _isolate_db_admin):
    from app.db.models import User

    h = _auth(client)
    fresh = _isolate_db_admin / "fresh.db"
    url = f"sqlite+aiosqlite:///{fresh.as_posix()}"

    res = client.post("/api/v1/db/init", headers=h, json={
        "url": url,
        "seed_admin": True,
        "admin_username": "install_admin",
        "admin_password": "install123",
    })
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["backend"] == "sqlite"
    assert body["head_revision"]
    assert body["seeded"] is True
    assert any("install_admin" in line for line in body["log"])

    # the fresh DB really has the schema + the seeded admin
    eng = create_engine(to_sync_url(url), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
            assert rev == body["head_revision"]
        with sessionmaker(eng, expire_on_commit=False)() as session:
            user = session.execute(
                select(User).where(User.username == "install_admin")
            ).scalar_one()
            assert user.role == "ADMIN"
    finally:
        eng.dispose()


def test_db_init_is_idempotent(client, _isolate_db_admin):
    from app.db.models import User
    from sqlalchemy.orm import sessionmaker

    h = _auth(client)
    fresh = _isolate_db_admin / "twice.db"
    url = f"sqlite+aiosqlite:///{fresh.as_posix()}"

    for _ in range(2):
        res = client.post("/api/v1/db/init", headers=h, json={
            "url": url, "seed_admin": True})
        assert res.status_code == 200
        assert res.json()["ok"] is True

    eng = create_engine(to_sync_url(url), poolclass=NullPool)
    try:
        with sessionmaker(eng, expire_on_commit=False)() as session:
            assert len(session.execute(select(User)).scalars().all()) == 1
    finally:
        eng.dispose()


def test_db_init_refuses_a_drifted_schema_instead_of_stamping(client, _isolate_db_admin):
    """H11: a drifted database is reported, never silently stamped to head.

    This replaces an earlier test that asserted the opposite (an automatic
    `stamp head` on an `already exists` error). That behaviour was removed: a
    stamp on a partially-migrated database declares the remaining objects as
    applied and they are never created. Stamping is now an explicit,
    parity-checked operator action — see tests/test_schema_authority.py.
    """
    from app.services.db_admin import head_revision

    h = _auth(client)
    fresh = _isolate_db_admin / "drifted.db"
    url = f"sqlite+aiosqlite:///{fresh.as_posix()}"
    eng = create_engine(to_sync_url(url), poolclass=NullPool)
    with eng.begin() as conn:
        # Drift: a table a pending migration would create already exists, while
        # alembic_version points behind it (the create_all-bootstrap scenario).
        conn.execute(text("CREATE TABLE reports (id INTEGER PRIMARY KEY)"))
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
        conn.execute(text("INSERT INTO alembic_version (version_num) VALUES ('v1234567w8x9')"))
    eng.dispose()

    res = client.post("/api/v1/db/init", headers=h, json={"url": url, "seed_admin": False})
    assert res.status_code == 500, res.text
    detail = res.json()["detail"]
    assert "already contains objects" in str(detail)
    assert "never done automatically" in str(detail)

    # The decisive assertion: the revision was NOT advanced to head.
    eng = create_engine(to_sync_url(url), poolclass=NullPool)
    try:
        with eng.connect() as conn:
            rev = conn.execute(text("SELECT version_num FROM alembic_version")).scalar_one()
        assert rev == "v1234567w8x9", "a drifted database must not be stamped"
        assert rev != head_revision()
    finally:
        eng.dispose()


def test_db_init_uses_saved_config(client, _isolate_db_admin):
    """use_saved picks up the operator's saved MySQL config when no form fields."""
    h = _auth(client)
    client.post("/api/v1/db/configure", headers=h, json={
        "backend": "mysql", "host": "127.0.0.1", "port": 1,
        "user": "paxs_user", "password": "pw", "name": "paxs_db"})
    res = client.post("/api/v1/db/init", headers=h, json={
        "use_saved": True, "seed_admin": False})
    # Resolution targets the saved config (unreachable MySQL on port 1) and
    # fails cleanly — the log proves the saved configuration was picked up.
    assert res.status_code == 500
    log = res.json()["detail"]["log"]
    assert any("saved configuration" in line for line in log)
    assert any("127.0.0.1:1" in line for line in log)


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #
def test_db_backup_lifecycle(client, _isolate_db_admin):
    h = _auth(client)

    res = client.post("/api/v1/db/backup", headers=h)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["file"].startswith("paxs-")
    assert body["size"] > 0

    # listed
    rows = client.get("/api/v1/db/backups", headers=h).json()
    assert len(rows) == 1
    assert rows[0]["file"] == body["file"]

    # downloadable
    dl = client.get(f"/api/v1/db/backups/{body['file']}", headers=h)
    assert dl.status_code == 200
    assert dl.headers["content-type"] == "application/octet-stream"
    assert dl.content

    # deleted
    assert client.delete(f"/api/v1/db/backups/{body['file']}", headers=h).status_code == 200
    assert client.get("/api/v1/db/backups", headers=h).json() == []


def test_db_backup_rejects_bad_names(client, _isolate_db_admin):
    h = _auth(client)
    # traversal attempts never reach a download (404 via path normalization /
    # 422 via the explicit filename guard)
    assert client.get("/api/v1/db/backups/..%2F..%2Fetc%2Fpasswd", headers=h).status_code != 200
    assert client.delete("/api/v1/db/backups/..%2F..%2Fsecret", headers=h).status_code != 200
    # a suspicious-but-routed name hits the explicit guard
    assert client.get("/api/v1/db/backups/..evil.sqlite", headers=h).status_code == 422
    # a valid-looking name that doesn't exist
    assert client.get("/api/v1/db/backups/paxs-sqlite-nope.sqlite", headers=h).status_code == 422
