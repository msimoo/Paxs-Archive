"""Session housekeeping: worker prune sweep + admin sessions console.

Covers the worker pruning expired / revoked session rows, the admin
GET /api/v1/users/sessions listing, and per-session revocation (which kills
the target device's refresh cookie while leaving other sessions intact).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import settings
from app.core.security import hash_refresh_token
from app.db.models import Session, User
from app.db.session import sync_engine


def _register(client, username, role=None):
    body = {"username": username, "password": "secret123", "full_name": username.title()}
    if role:
        body["role"] = role
    return client.post("/api/v1/auth/register", json=body)


def _login(client, username):
    return client.post("/api/v1/auth/login",
                       json={"username": username, "password": "secret123"})


def _bearer(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _sessions() -> list[dict]:
    with sync_engine.connect() as conn:
        rows = conn.execute(Session.__table__.select()).fetchall()
        return [dict(r._mapping) for r in rows]


def _jar_cookie(client) -> str | None:
    """The refresh cookie currently in the TestClient jar (last set wins —
    every login writes the same cookie name, so capture values explicitly)."""
    for c in client.cookies.jar:
        if c.name == settings.AUTH_COOKIE_NAME:
            return c.value
    return None


def _session_id_for_token(token: str) -> str:
    with sync_engine.connect() as conn:
        row = conn.execute(
            Session.__table__.select().where(
                Session.__table__.c.refresh_token_hash == hash_refresh_token(token)
            )
        ).fetchone()
    assert row is not None, "no session row for the given refresh token"
    return row._mapping["id"]


def _run_sweep() -> None:
    """Call the worker's session sweep directly (same code path as the
    daemon's tick handler — __init__ never starts the thread)."""
    from app.services.worker import CompressionWorker

    CompressionWorker(poll_seconds=0.01)._sweep_sessions()


def _update(row_id: str, **values) -> None:
    with sync_engine.begin() as conn:
        conn.execute(
            Session.__table__.update().where(Session.__table__.c.id == row_id).values(**values)
        )


# --------------------------------------------------------------------------- #
# Worker prune sweep
# --------------------------------------------------------------------------- #

def test_worker_sweep_prunes_expired_and_revoked_rows(client):
    """Expired and revoked rows are deleted; live sessions survive."""
    _register(client, "dr.ali")
    expired_id = _sessions()[0]["id"]
    past = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(minutes=5)
    _update(expired_id, expires_at=past)

    _register(client, "dr.boo")
    _login(client, "dr.boo")
    # Logout revokes the second register session's row.
    client.post("/api/v1/auth/logout")
    revoked_ids = [s["id"] for s in _sessions() if s["is_revoked"]]
    assert revoked_ids, "logout should have revoked a session row"

    _register(client, "dr.cat")  # live session — must survive

    _run_sweep()

    remaining = {s["id"] for s in _sessions()}
    assert expired_id not in remaining
    assert not set(revoked_ids) & remaining
    assert remaining, "the live session must survive the sweep"


def test_worker_sweep_leaves_active_sessions(client):
    _register(client, "dr.ali")
    _register(client, "dr.boo")
    before = {s["id"] for s in _sessions()}
    assert len(before) == 2

    _run_sweep()

    assert {s["id"] for s in _sessions()} == before


# --------------------------------------------------------------------------- #
# Admin console API
# --------------------------------------------------------------------------- #

def test_admin_lists_active_sessions(client):
    admin = _register(client, "root")  # first user bootstraps as ADMIN
    assert admin.json()["user"]["role"] == "ADMIN"
    admin_token = admin.json()["access_token"]

    _register(client, "dr.badr")
    _login(client, "dr.badr")
    _register(client, "dr.cam")

    res = client.get("/api/v1/users/sessions", headers=_bearer(admin_token))
    assert res.status_code == 200
    rows = res.json()
    usernames = {r["username"] for r in rows}
    assert {"root", "dr.badr", "dr.cam"} <= usernames
    badr = next(r for r in rows if r["username"] == "dr.badr")
    assert badr["role"] and badr["expires_at"]
    assert badr["last_used_at"] >= badr["created_at"]


def test_sessions_requires_admin(client):
    _register(client, "root")            # ADMIN
    non_admin = _register(client, "dr.badr", role="RADIOLOGIST")
    token = non_admin.json()["access_token"]

    res = client.get("/api/v1/users/sessions", headers=_bearer(token))
    assert res.status_code == 403


def test_revoke_session_kills_only_that_device(client):
    admin = _register(client, "root")
    admin_token = admin.json()["access_token"]

    _register(client, "dr.badr")
    badr_cookie = _jar_cookie(client)
    assert badr_cookie
    badr_id = _session_id_for_token(badr_cookie)
    client.cookies.clear()

    _login(client, "dr.badr")              # second device for dr.badr
    client.cookies.clear()
    _register(client, "dr.cam")
    cam_cookie = _jar_cookie(client)
    assert cam_cookie
    cam_id = _session_id_for_token(cam_cookie)

    rows = client.get("/api/v1/users/sessions", headers=_bearer(admin_token)).json()
    ids = {r["id"] for r in rows}
    assert badr_id in ids and cam_id in ids

    res = client.post(f"/api/v1/users/sessions/{badr_id}/revoke",
                      headers=_bearer(admin_token))
    assert res.status_code == 200

    # dr.badr's revoked device: its refresh cookie no longer rotates.
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, badr_cookie)
    assert client.post("/api/v1/auth/refresh").status_code == 401

    # …but dr.cam's device keeps working.
    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, cam_cookie)
    assert client.post("/api/v1/auth/refresh").status_code == 200

    rows = client.get("/api/v1/users/sessions", headers=_bearer(admin_token)).json()
    assert badr_id not in {r["id"] for r in rows}
    assert cam_id in {r["id"] for r in rows}


def test_revoke_unknown_session_404(client):
    admin = _register(client, "root")
    admin_token = admin.json()["access_token"]
    res = client.post("/api/v1/users/sessions/does-not-exist/revoke",
                      headers=_bearer(admin_token))
    assert res.status_code == 404


def test_revoke_user_kills_every_device(client):
    admin = _register(client, "root")
    admin_token = admin.json()["access_token"]

    _register(client, "dr.badr")
    badr_dev1 = _jar_cookie(client)
    client.cookies.clear()
    _login(client, "dr.badr")
    badr_dev2 = _jar_cookie(client)
    assert badr_dev1 and badr_dev2 and badr_dev1 != badr_dev2
    client.cookies.clear()

    # Admin's own session keeps working throughout.
    assert client.get("/api/v1/auth/me", headers=_bearer(admin_token)).status_code == 200

    rows = client.get("/api/v1/users/sessions", headers=_bearer(admin_token)).json()
    badr_user_id = next(r["user_id"] for r in rows if r["username"] == "dr.badr")

    res = client.post(f"/api/v1/users/sessions/revoke-user/{badr_user_id}",
                      headers=_bearer(admin_token))
    assert res.status_code == 200
    assert res.json()["revoked"] == 2

    # Both dr.badr devices are dead now…
    for cookie in (badr_dev1, badr_dev2):
        client.cookies.clear()
        client.cookies.set(settings.AUTH_COOKIE_NAME, cookie)
        assert client.post("/api/v1/auth/refresh").status_code == 401

    # …the admin still works, and the list no longer shows dr.badr.
    assert client.get("/api/v1/auth/me", headers=_bearer(admin_token)).status_code == 200
    rows = client.get("/api/v1/users/sessions", headers=_bearer(admin_token)).json()
    assert not [r for r in rows if r["username"] == "dr.badr"]


def test_revoke_user_self_is_safe(client):
    admin = _register(client, "root")
    admin_token = admin.json()["access_token"]
    admin_cookie = _jar_cookie(client)
    admin_id = client.get("/api/v1/users/sessions",
                          headers=_bearer(admin_token)).json()[0]["user_id"]

    res = client.post(f"/api/v1/users/sessions/revoke-user/{admin_id}",
                      headers=_bearer(admin_token))
    assert res.status_code == 200
    assert res.json()["revoked"] == 0  # admin can't lock themselves out

    client.cookies.clear()
    client.cookies.set(settings.AUTH_COOKIE_NAME, admin_cookie)
    assert client.post("/api/v1/auth/refresh").status_code == 200


def test_revoke_user_unknown_user_404(client):
    admin = _register(client, "root")
    admin_token = admin.json()["access_token"]
    res = client.post("/api/v1/users/sessions/revoke-user/999999",
                      headers=_bearer(admin_token))
    assert res.status_code == 404


def test_revoke_writes_audit_event(client):
    from app.db.models import AuthAuditLog

    admin = _register(client, "root")
    admin_token = admin.json()["access_token"]
    _register(client, "dr.badr")

    rows = client.get("/api/v1/users/sessions", headers=_bearer(admin_token)).json()
    badr_id = next(r["id"] for r in rows if r["username"] == "dr.badr")
    client.post(f"/api/v1/users/sessions/{badr_id}/revoke", headers=_bearer(admin_token))

    with sync_engine.connect() as conn:
        event = conn.execute(
            AuthAuditLog.__table__.select().where(
                AuthAuditLog.__table__.c.action == "session_revoked"
            )
        ).fetchone()
    assert event is not None
    assert "admin" in event._mapping["details"]
