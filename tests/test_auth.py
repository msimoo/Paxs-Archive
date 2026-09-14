def _register(client, username="dr.ali", password="secret123", full_name="Ali Hassan"):
    return client.post("/api/v1/auth/register", json={
        "username": username, "password": password, "full_name": full_name,
    })


def test_register_first_user_is_admin(client):
    res = _register(client)
    assert res.status_code == 201
    data = res.json()
    assert data["user"]["role"] == "ADMIN"
    assert data["access_token"]


def test_duplicate_username_conflict(client):
    _register(client)
    res = _register(client)
    assert res.status_code == 409


def test_login_success_and_failure(client):
    _register(client, username="user1", password="secret123")

    ok = client.post("/api/v1/auth/login", json={"username": "user1", "password": "secret123"})
    assert ok.status_code == 200
    assert ok.json()["access_token"]

    bad = client.post("/api/v1/auth/login", json={"username": "user1", "password": "wrong"})
    assert bad.status_code == 401


def test_me_requires_token(client):
    _register(client, username="user2", password="secret123")
    token = client.post("/api/v1/auth/login", json={"username": "user2", "password": "secret123"}).json()["access_token"]

    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "user2"

    # No cookie, no bearer → 401. (The login above left a session cookie in
    # the jar, so clear it to truly simulate an anonymous visitor — the
    # cookie itself is now a valid way to authenticate /me.)
    client.cookies.clear()
    anon = client.get("/api/v1/auth/me")
    assert anon.status_code == 401

    tampered = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token[:-2]}xx"})
    assert tampered.status_code == 401
