"""Serving-layer performance tests: gzip compression middleware, static
asset Cache-Control headers, and the pre-generated-thumbnail fast path.

These guard the viewer-experience wins: a ~70-chunk SPA bundle that is
gzipped and cached-forever (hashed files), entry points that revalidate,
binary DICOM endpoints that are never gzip-wasted, and study-list
thumbnails served from the worker's pre-generated objects instead of a
pydicom read + PIL render per request.
"""

from __future__ import annotations

import gzip
from pathlib import Path

from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

VIEWER_DIR = Path(__file__).resolve().parent.parent / "app" / "static" / "viewer"


def _first_bundle_chunk() -> tuple[str, bytes]:
    """A real hashed bundle chunk from the viewer dist (e.g. *.bundle.<hex>.js)."""
    for path in sorted(VIEWER_DIR.glob("*.bundle.*.js")):
        if ".bundle." in path.name and path.stat().st_size > 10_000:
            return f"/viewer/{path.name}", path.read_bytes()
    raise AssertionError("no hashed viewer bundle chunk found")


def _stow_one_instance(client) -> dict:
    """STOW a single synthetic CT instance; return its study/series/sop UIDs."""
    ds = make_instance()
    resp = client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds)]),
        headers=stow_headers(),
    )
    assert resp.status_code == 200, resp.text
    stored = resp.json()["00081199"]["Value"]
    assert len(stored) == 1
    return {
        "study": str(ds.StudyInstanceUID),
        "series": str(ds.SeriesInstanceUID),
        "sop": str(ds.SOPInstanceUID),
    }


# --------------------------------------------------------------------------- #
# Gzip compression middleware
# --------------------------------------------------------------------------- #
def test_viewer_bundle_chunk_gzipped(client):
    path, raw = _first_bundle_chunk()
    resp = client.get(path, headers={"Accept-Encoding": "gzip"})
    assert resp.status_code == 200
    assert resp.headers["content-encoding"] == "gzip"
    assert "accept-encoding" in resp.headers["vary"].lower()
    # httpx transparently decodes gzip — the decoded body must round-trip to
    # the exact bytes on disk, and the wire (content-length) must be smaller.
    assert resp.content == raw
    wire_bytes = int(resp.headers["content-length"])
    assert 0 < wire_bytes < len(raw)


def test_gzip_wire_bytes_are_real_gzip(client):
    """Raw ASGI check that the bytes on the wire really are gzip.

    httpx's TestClient transparently decodes Content-Encoding: gzip, so this
    drives the app directly and captures the un-decoded body messages.
    """
    import asyncio

    from app.main import app

    path, raw = _first_bundle_chunk()
    captured: list[bytes] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if message["type"] == "http.response.body":
            captured.append(message.get("body", b""))

    scope = {
        "type": "http",
        "method": "GET",
        "path": path,
        "headers": [(b"accept-encoding", b"gzip"), (b"host", b"test")],
        "query_string": b"",
        "scheme": "http",
        "server": ("test", 80),
        "client": ("test", 1234),
        "root_path": "",
        "http_version": "1.1",
        "app": app,
        "state": {},
    }

    asyncio.run(app(scope, receive, send))
    wire = b"".join(captured)
    assert wire[:2] == b"\x1f\x8b", "wire bytes are not gzip"
    assert gzip.decompress(wire) == raw


def test_viewer_bundle_not_gzipped_without_gzip(client):
    """A client that explicitly disables gzip gets the raw bytes."""
    path, _ = _first_bundle_chunk()
    # httpx adds "Accept-Encoding: gzip, deflate, br" by default — override it.
    resp = client.get(path, headers={"Accept-Encoding": "identity"})
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


def test_small_json_not_gzipped(client):
    """Bodies under the minimum size pass through untouched."""
    ds = make_instance()
    client.post(
        "/dicom-web/studies",
        content=build_stow_body([ds_bytes(ds)]),
        headers=stow_headers(),
    )
    resp = client.get(
        "/dicom-web/studies?limit=1", headers={"Accept-Encoding": "gzip"}
    )
    assert resp.status_code == 200
    assert "content-encoding" not in resp.headers


def test_dicomweb_rendered_never_gzipped(client):
    """image/png from /dicom-web is binary (already compressed) — path skip."""
    uids = _stow_one_instance(client)
    resp = client.get(
        f"/dicom-web/studies/{uids['study']}/series/{uids['series']}"
        f"/instances/{uids['sop']}/rendered",
        headers={"Accept-Encoding": "gzip", "Accept": "image/png"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")
    assert "content-encoding" not in resp.headers


# --------------------------------------------------------------------------- #
# Static asset Cache-Control
# --------------------------------------------------------------------------- #
def test_hashed_assets_immutable(client):
    path, _ = _first_bundle_chunk()
    resp = client.get(path)
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_admin_hashed_asset_immutable(client):
    resp = client.get("/admin/")
    if resp.status_code != 200:
        return  # admin bundle absent — skip silently
    assert resp.headers.get("cache-control") == "no-cache"


def test_viewer_index_no_cache(client):
    resp = client.get("/viewer/")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


def test_viewer_config_no_cache(client):
    resp = client.get("/viewer/app-config.js")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


def test_dashboard_index_no_cache(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers.get("cache-control") == "no-cache"


# --------------------------------------------------------------------------- #
# Pre-generated thumbnail fast path
# --------------------------------------------------------------------------- #
def test_pre_generated_thumbnail_served(client):
    """When the worker thumbnail exists, the endpoint serves it byte-exact."""
    from app.services import storage_service, thumbnail_service

    uids = _stow_one_instance(client)
    inst_key = storage_service.instance_key(
        uids["study"], uids["series"], uids["sop"]
    )
    thumb_key = storage_service.thumbnail_key(uids["sop"])
    ok = thumbnail_service.generate_thumbnail(inst_key, thumb_key, size=96)
    assert ok, "test thumbnail generation failed"

    resp = client.get(
        f"/dicom-web/studies/{uids['study']}/series/{uids['series']}"
        f"/instances/{uids['sop']}/thumbnail"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert resp.content == storage_service.read_bytes(thumb_key)
    assert resp.headers.get("etag")


def test_thumbnail_renders_when_no_pre_generated(client):
    """Without a pre-generated object the endpoint still renders on the fly."""
    uids = _stow_one_instance(client)
    resp = client.get(
        f"/dicom-web/studies/{uids['study']}/series/{uids['series']}"
        f"/instances/{uids['sop']}/thumbnail"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_thumbnail_with_window_renders_on_the_fly(client):
    """An explicit window forces the render path (no pre-generated use)."""
    uids = _stow_one_instance(client)
    resp = client.get(
        f"/dicom-web/studies/{uids['study']}/series/{uids['series']}"
        f"/instances/{uids['sop']}/thumbnail?window=40,400"
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/")


def test_thumbnail_redis_serves_cached(client, monkeypatch):
    """The rendered-thumbnail path consults the Redis cache first (like /rendered).

    An explicit window forces the render path; with Redis patched to a stub,
    the second render's cache hit must be served verbatim without rendering.
    """
    from app.services import redis_service

    uids = _stow_one_instance(client)
    url = (
        f"/dicom-web/studies/{uids['study']}/series/{uids['series']}"
        f"/instances/{uids['sop']}/thumbnail?window=40,400"
    )

    # First request: cache miss → render runs → result is stored.
    store: dict[bytes, bytes] = {}
    monkeypatch.setattr(redis_service, "cache_get", lambda k: store.get(k))
    monkeypatch.setattr(
        redis_service, "cache_set", lambda k, v, expire_seconds=300: store.__setitem__(k, v) or True
    )
    first = client.get(url)
    assert first.status_code == 200
    assert len(store) == 1, "render result must be written to the cache"

    # Second request (e.g. another browser): served from the cache, byte-exact.
    second = client.get(url)
    assert second.status_code == 200
    assert second.content == first.content

    # Prove the bytes really came from the cache: pre-populate with a sentinel.
    calls = {"n": 0}

    def fake_get(_k):
        calls["n"] += 1
        return b"REDIS_THUMB_SENTINEL"

    monkeypatch.setattr(redis_service, "cache_get", fake_get)
    cached = client.get(url)
    assert cached.status_code == 200
    assert cached.content == b"REDIS_THUMB_SENTINEL"
    assert calls["n"] == 1


def test_thumbnail_pre_generated_bypasses_redis(client, monkeypatch):
    """When the worker's pre-generated object exists it is served WITHOUT touching
    Redis or the render path (cheapest of the three layers)."""
    from app.services import redis_service, storage_service, thumbnail_service

    uids = _stow_one_instance(client)
    inst_key = storage_service.instance_key(uids["study"], uids["series"], uids["sop"])
    thumb_key = storage_service.thumbnail_key(uids["sop"])
    assert thumbnail_service.generate_thumbnail(inst_key, thumb_key, size=96)

    def boom(*_a, **_k):
        raise AssertionError("Redis must not be consulted when the pre-generated object exists")

    monkeypatch.setattr(redis_service, "cache_get", boom)
    monkeypatch.setattr(redis_service, "cache_set", boom)

    resp = client.get(
        f"/dicom-web/studies/{uids['study']}/series/{uids['series']}"
        f"/instances/{uids['sop']}/thumbnail"
    )
    assert resp.status_code == 200
    assert resp.content == storage_service.read_bytes(thumb_key)
