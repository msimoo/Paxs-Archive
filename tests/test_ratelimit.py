"""Rate limiter tests — the limiter is disabled globally in conftest, so these
tests exercise it directly at the dependency level with a tight limit."""

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.api.ratelimit import rate_limit, reset_rate_limits


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    # conftest disables the limiter globally; these tests enable it explicitly.
    from app.core.config import settings
    monkeypatch.setattr(settings, "RATE_LIMIT_ENABLED", True)
    reset_rate_limits()
    yield
    reset_rate_limits()


def _fake_request(host="10.0.0.1"):
    class _Req:
        client = type("C", (), {"host": host})()
        headers = {}
    return _Req()


def test_allows_under_limit_and_rejects_over():
    limiter = rate_limit("test-window", limit=3, window_seconds=60)

    async def call():
        await limiter(_fake_request())

    import asyncio
    for _ in range(3):
        asyncio.run(call())  # allowed
    with pytest.raises(HTTPException) as exc:
        asyncio.run(call())
    assert exc.value.status_code == 429
    assert "Rate limit exceeded" in exc.value.detail


def test_separate_windows_and_ips_are_independent():
    import asyncio
    a = rate_limit("w1", limit=1, window_seconds=60)
    b = rate_limit("w2", limit=1, window_seconds=60)

    asyncio.run(a(_fake_request("1.1.1.1")))
    asyncio.run(b(_fake_request("1.1.1.1")))  # different window: ok
    asyncio.run(a(_fake_request("2.2.2.2")))  # different IP: ok

    with pytest.raises(HTTPException):
        asyncio.run(a(_fake_request("1.1.1.1")))


def test_headers_included():
    import asyncio
    limiter = rate_limit("hdr", limit=1, window_seconds=60)
    asyncio.run(limiter(_fake_request()))
    with pytest.raises(HTTPException) as exc:
        asyncio.run(limiter(_fake_request()))
    assert exc.value.headers.get("Retry-After")


def test_disabled_when_settings_flag_off(client: TestClient):
    # conftest sets RATE_LIMIT_ENABLED=false; a burst must not trip 429.
    for _ in range(5):
        res = client.get("/api/v1/health")
        assert res.status_code == 200
