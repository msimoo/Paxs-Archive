"""Admin → Users management API (/api/v1/users).

Covers the admin-only gate, account CRUD, password reset, the self / last-ADMIN
safety guards, and disabled accounts being rejected by authentication.
"""

import pytest


def _register(client, username, password="secret123", role=None):
    body = {"username": username, "password": password}
    if role:
        body["role"] = role
    return client.post("/api/v1/auth/register", json=body)


def _auth(client, username, password="secret123", role=None):
    # H02: a specific role goes through the admin API; no role keeps the
    # original "first account is the bootstrap ADMIN" behaviour.
    from tests.helpers import auth_for_role

    return auth_for_role(client, username, role=role or "ADMIN", password=password)


@pytest.fixture(autouse=True)
def _admin(client):
    """A seeded ADMIN usable from every test."""
    return _auth(client, "root_admin")


# --------------------------------------------------------------------------- #
# Auth gates
# --------------------------------------------------------------------------- #
def test_users_requires_auth(client):
    # The autouse `_admin` fixture logged in on this client, and the API also
    # accepts cookie sessions (C03), so clear the jar to be anonymous.
    client.cookies.clear()
    assert client.get("/api/v1/users").status_code == 401
    assert client.post("/api/v1/users", json={}).status_code == 401


def test_users_requires_admin(client):
    _register(client, "root_admin")
    h = _auth(client, "dr_plain", role="RADIOLOGIST")
    assert client.get("/api/v1/users", headers=h).status_code == 403
    assert client.post("/api/v1/users", json={
        "username": "x", "password": "secret123"}, headers=h).status_code == 403


def test_self_register_cannot_mint_admin(client):
    """Privilege-escalation guard: only the bootstrap admin is auto-ADMIN."""
    _register(client, "root_admin")
    res = client.post("/api/v1/auth/register", json={
        "username": "sneaky", "password": "secret123", "role": "ADMIN"})
    assert res.status_code == 403
    # the account was NOT created
    assert client.post("/api/v1/auth/login", json={
        "username": "sneaky", "password": "secret123"}).status_code == 401


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_users_crud_flow(client, _admin):
    res = client.post("/api/v1/users", headers=_admin, json={
        "username": "dr.mona", "password": "mona123",
        "full_name": "Mona Khalil", "role": "RADIOLOGIST"})
    assert res.status_code == 201
    user = res.json()
    assert user["role"] == "RADIOLOGIST"
    assert user["is_active"] is True
    uid = user["id"]

    names = [u["username"] for u in client.get("/api/v1/users", headers=_admin).json()]
    assert "dr.mona" in names

    # patch role + full name
    res = client.patch(f"/api/v1/users/{uid}", headers=_admin,
                       json={"role": "PHYSICIAN", "full_name": "Mona K."})
    assert res.status_code == 200
    assert res.json()["role"] == "PHYSICIAN"
    assert res.json()["full_name"] == "Mona K."

    # delete
    assert client.delete(f"/api/v1/users/{uid}", headers=_admin).status_code == 200
    assert client.get("/api/v1/users", headers=_admin).status_code == 200


def test_create_user_validation(client, _admin):
    client.post("/api/v1/users", headers=_admin, json={
        "username": "dup", "password": "secret123"})
    # duplicate
    res = client.post("/api/v1/users", headers=_admin, json={
        "username": "dup", "password": "secret123"})
    assert res.status_code == 409
    # invalid role
    res = client.post("/api/v1/users", headers=_admin, json={
        "username": "bob", "password": "secret123", "role": "SUPERUSER"})
    assert res.status_code == 422
    # short password
    res = client.post("/api/v1/users", headers=_admin, json={
        "username": "bob2", "password": "123"})
    assert res.status_code == 422


def test_reset_password(client, _admin):
    from tests.helpers import create_user

    create_user(client, _admin, "dr.sara", "RADIOLOGIST", password="oldpass123")
    uid = next(u["id"] for u in client.get("/api/v1/users", headers=_admin).json()
               if u["username"] == "dr.sara")

    res = client.post(f"/api/v1/users/{uid}/reset-password", headers=_admin,
                      json={"password": "newpass456"})
    assert res.status_code == 200

    assert client.post("/api/v1/auth/login", json={
        "username": "dr.sara", "password": "oldpass123"}).status_code == 401
    assert client.post("/api/v1/auth/login", json={
        "username": "dr.sara", "password": "newpass456"}).status_code == 200


def test_disable_blocks_login(client, _admin):
    _register(client, "dr.lina", password="lina123", role="PHYSICIAN")
    uid = next(u["id"] for u in client.get("/api/v1/users", headers=_admin).json()
               if u["username"] == "dr.lina")
    token = client.post("/api/v1/auth/login", json={
        "username": "dr.lina", "password": "lina123"}).json()["access_token"]
    h = {"Authorization": f"Bearer {token}"}

    assert client.get("/api/v1/auth/me", headers=h).status_code == 200
    assert client.patch(f"/api/v1/users/{uid}", headers=_admin,
                        json={"is_active": False}).status_code == 200
    # even a previously-valid token is rejected now
    assert client.get("/api/v1/auth/me", headers=h).status_code == 401
    assert client.post("/api/v1/auth/login", json={
        "username": "dr.lina", "password": "lina123"}).status_code == 401

    # re-enabling restores access
    assert client.patch(f"/api/v1/users/{uid}", headers=_admin,
                        json={"is_active": True}).status_code == 200
    assert client.post("/api/v1/auth/login", json={
        "username": "dr.lina", "password": "lina123"}).status_code == 200


# --------------------------------------------------------------------------- #
# Safety guards
# --------------------------------------------------------------------------- #
def test_self_protections(client, _admin):
    me = next(u for u in client.get("/api/v1/users", headers=_admin).json()
              if u["username"] == "root_admin")
    uid = me["id"]
    assert client.patch(f"/api/v1/users/{uid}", headers=_admin,
                        json={"role": "PHYSICIAN"}).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", headers=_admin,
                        json={"is_active": False}).status_code == 422
    assert client.delete(f"/api/v1/users/{uid}", headers=_admin).status_code == 422


def test_last_admin_protections(client, _admin):
    # root_admin is the only ADMIN — it cannot be deleted/disabled/demoted
    me = next(u for u in client.get("/api/v1/users", headers=_admin).json()
              if u["username"] == "root_admin")
    uid = me["id"]
    assert client.delete(f"/api/v1/users/{uid}", headers=_admin).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", headers=_admin,
                        json={"is_active": False}).status_code == 422
    assert client.patch(f"/api/v1/users/{uid}", headers=_admin,
                        json={"role": "RADIOLOGIST"}).status_code == 422

    # a second ADMIN (created via the admin API) makes the first deletable again
    res = client.post("/api/v1/users", headers=_admin, json={
        "username": "root_admin2", "password": "secret123", "role": "ADMIN"})
    assert res.status_code == 201
    h2 = _auth(client, "root_admin2")
    res = client.delete(f"/api/v1/users/{uid}", headers=h2)
    assert res.status_code == 200
