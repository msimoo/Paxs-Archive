"""Tests for the Admin → DB Pool panel (read-replica / pool / slow-query / EXPLAIN)."""

from __future__ import annotations

from sqlalchemy import text


def _auth(client, username="dbp_admin"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": "pw123456", "role": "ADMIN"},
    )
    token = (reg.json().get("access_token") if reg.status_code == 201
             else client.post("/api/v1/auth/login",
                              json={"username": username, "password": "pw123456"})
             .json()["access_token"])
    return {"Authorization": f"Bearer {token}"}


def test_perf_panel_requires_destructive_role(client):
    assert client.get("/api/v1/db/perf").status_code in (401, 403)


def test_perf_panel_shape(client):
    h = _auth(client)
    r = client.get("/api/v1/db/perf", headers=h)
    assert r.status_code == 200
    p = r.json()
    assert p["backend"] in ("sqlite", "mysql")
    assert "sync" in p["pool"]
    assert "checkedout" in p["pool"]["sync"]
    assert "size" in p["pool"]["sync"]
    assert "overflow" in p["pool"]["sync"]
    assert p["replica"]["configured"] is False  # no DB_READ_REPLICA_URL in tests
    assert "slow_query" in p
    assert "hot_queries" in p and len(p["hot_queries"]) >= 4


def test_slow_query_toggle_persists_and_captures(client):
    from app.db import perf as perf_mod

    perf_mod.set_slow_query(False, 500)  # reset module state
    try:
        h = _auth(client)
        r = client.post("/api/v1/db/perf/slow-query", headers=h,
                        json={"enabled": True, "threshold_ms": 1})
        assert r.status_code == 200
        body = r.json()
        assert body["enabled"] is True and body["threshold_ms"] == 1

        # Deterministic capture: run a deliberately slow statement through the
        # sync engine (its hooks are installed at import). Dialect-aware:
        # MySQL has no recursive-CTE depth allowance by default and no need —
        # SELECT SLEEP() is the canonical slow query there.
        from app.core.config import settings
        from app.db.session import SyncSessionLocal

        slow_sql = (
            "SELECT SLEEP(0.05)"
            if settings.DATABASE_URL.startswith("mysql")
            else ("WITH RECURSIVE cnt(x) AS (SELECT 1 UNION ALL "
                  "SELECT x + 1 FROM cnt WHERE x < 120000) SELECT count(*) FROM cnt")
        )
        with SyncSessionLocal() as s:
            s.execute(text(slow_sql))

        # And a normal API read (exercises the async engine hooks too).
        assert client.get("/api/v1/stats", headers=h).status_code == 200

        rows = client.get("/api/v1/db/perf/slow-queries", headers=h).json()
        assert rows, "expected at least one captured slow query"
        assert "ms" in rows[0] and "sql" in rows[0] and "engine" in rows[0]

        # Toggle off — persisted value is visible in the panel payload.
        r = client.post("/api/v1/db/perf/slow-query", headers=h,
                        json={"enabled": False})
        assert r.status_code == 200 and r.json()["enabled"] is False
        p = client.get("/api/v1/db/perf", headers=h).json()
        assert p["slow_query"]["enabled"] is False
    finally:
        perf_mod.set_slow_query(False, 500)  # never leak module state into other tests


def test_explain_named_and_custom(client):
    h = _auth(client)
    r = client.get("/api/v1/db/perf/explain?name=dashboard_studies", headers=h)
    assert r.status_code == 200
    d = r.json()
    assert d["name"] == "dashboard_studies"
    assert d["rows"] and d["columns"]

    r = client.get("/api/v1/db/perf/explain", headers=h,
                   params={"sql": "SELECT * FROM studies LIMIT 5"})
    assert r.status_code == 200
    assert r.json()["name"] == "custom"

    # Non-SELECT statements are rejected.
    assert client.get("/api/v1/db/perf/explain", headers=h,
                      params={"sql": "DROP TABLE studies"}).status_code == 422
    # CTE-backed writes (WITH … DELETE) must not slip through the read-only guard.
    assert client.get("/api/v1/db/perf/explain", headers=h,
                      params={"sql": "WITH x AS (SELECT 1) DELETE FROM studies"}).status_code == 422
    # Unknown hot-query name is rejected.
    assert client.get("/api/v1/db/perf/explain?name=nope", headers=h).status_code == 422


def test_replica_toggle_unconfigured(client):
    """No DB_READ_REPLICA_URL in the test env -> the toggle refuses to enable."""
    h = _auth(client)
    r = client.post("/api/v1/db/perf/replica", headers=h, json={"enabled": True})
    assert r.status_code == 422
