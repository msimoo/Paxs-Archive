"""Phase 2 integration tests — server-side processing pipeline (thin client).

Covers ``plan_viewer_process.md`` §5.2 and §5.4:

  * ``/data`` is refused with **503 + ``X-PAXS-Mode-Hint: server``** while
    ``VIEWER_PROCESSING_MODE == "server"``, and ``?force=1`` restores the raw
    stream (the documented diagnostic escape hatch).
  * ``/mpr/slice`` still returns a valid, non-empty PNG in server mode.
  * ``/tiles`` returns a batch of pre-rendered frames for a slice range, in
    ascending index order, with self-describing offset/length lists, and the
    ``inline=1`` multipart framing agrees with the default framing.
  * Server-mode traffic registers an active server session, so the hybrid
    load-balancer can observe real server-mode load.

Regression intent: ``hybrid`` (the shipped default) must behave exactly as it
did before Phase 2 — the guard is a no-op there.  ``client`` mode must leave
``/data`` untouched.
"""

from __future__ import annotations

import asyncio
import io
import re

import numpy as np
import pytest
from PIL import Image
from pydicom.uid import generate_uid

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.services import volume_service
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]


def _ingest_series(client, n_slices: int = 6):
    """STOW a synthetic axial CT stack; returns (study_uid, series_uid)."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    datasets = []
    for i in range(n_slices):
        pixel = np.full((32, 32), 100 + i * 10, dtype=np.uint16)
        pixel[8:24, 8:24] = 1500 + i * 100  # bright core, differs per slice
        ds = make_instance(
            patient_id="SRV_PAT",
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=generate_uid(),
            instance_num=i + 1,
            rows=32,
            cols=32,
            pixel=pixel,
            position=[10.0, 20.0, float(i * 5)],
            orientation=list(AXIAL_ORIENTATION),
            rescale_slope="1",
            rescale_intercept="-1024",
            slice_thickness="5",
        )
        datasets.append(ds_bytes(ds))
    resp = client.post(
        f"/dicom-web/studies/{study_uid}",
        content=build_stow_body(datasets),
        headers=stow_headers(),
    )
    assert resp.status_code == 200
    return study_uid, series_uid


def _decode_png(payload: bytes) -> Image.Image:
    """Decode PNG bytes and assert the magic number, so a truncated frame
    fails loudly here rather than as a confusing pixel assertion later."""
    assert payload[:8] == PNG_MAGIC, "payload is not a PNG"
    img = Image.open(io.BytesIO(payload))
    img.load()
    return img


@pytest.fixture(autouse=True)
def _isolate_viewer_state():
    """Restore the viewer settings and clear server-mode state around each test.

    The session map is module-level mutable state shared with other suites, and
    these tests assert *relative* counts, so it is cleared on BOTH sides: a
    leave-behind from an earlier collector would otherwise look like a
    registration this test caused.
    """
    keys = (
        "VIEWER_PROCESSING_MODE",
        "VIEWER_PROCESSING_ROLE_MAP",
        "VOLUME_STREAMING_ENABLED",
        "VOLUME_MAX_SLICES",
    )
    snap = {k: getattr(settings, k, None) for k in keys}
    with volume_service._VOL_CACHE_LOCK:
        volume_service._ACTIVE_SERVER_SESSIONS.clear()
    yield
    for k, v in snap.items():
        setattr(settings, k, v)
    with volume_service._VOL_CACHE_LOCK:
        volume_service._ACTIVE_SERVER_SESSIONS.clear()
        volume_service._VOL_CACHE.clear()
        volume_service._TILE_CACHE.clear()


# C03: the volume routes require authentication. Override the anonymous client
# fixture for this module so its requests carry a valid ADMIN bearer token.
@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client


# =========================================================================== #
#  §1 — Step 2.1: mode guard on /data
# =========================================================================== #


class TestDataModeGuard:
    """`/data` refuses raw voxel streaming only when mode == 'server'."""

    def test_server_mode_rejects_raw_stream_with_503_and_hint(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/data")

        assert resp.status_code == 503
        assert resp.headers["X-PAXS-Mode-Hint"] == "server"
        detail = resp.json()["detail"]
        assert detail == "Raw volume streaming is disabled in server processing mode"
        # No voxel payload may leak through the refusal.
        assert len(resp.content) < 4096

    def test_server_mode_force_bypasses_the_guard(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/data?force=1")

        assert resp.status_code == 200
        assert resp.content[:8] != PNG_MAGIC
        info = client.get(f"/api/v1/volume/{series}/info").json()
        expected = (
            info["slices"] * info["rows"] * info["columns"]
            * np.dtype(info["dtype"]).itemsize
        )
        assert len(resp.content) == expected
        assert resp.headers["X-Volume-Slices"] == str(info["slices"])

    @pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "Yes"])
    def test_force_accepts_documented_spellings(self, client, monkeypatch, flag):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)
        assert client.get(f"/api/v1/volume/{series}/data?force={flag}").status_code == 200

    @pytest.mark.parametrize("flag", ["0", "false", "no", ""])
    def test_non_force_values_do_not_bypass(self, client, monkeypatch, flag):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)
        assert client.get(f"/api/v1/volume/{series}/data?force={flag}").status_code == 503

    def test_hybrid_default_is_unaffected(self, client, monkeypatch):
        """The shipped default must stream raw voxels exactly as before."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/data")

        assert resp.status_code == 200
        # Hybrid does not assert a hint on the raw stream.
        assert resp.headers.get("X-PAXS-Mode-Hint") in (None, "hybrid")

    def test_client_mode_is_unaffected(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        assert client.get(f"/api/v1/volume/{series}/data").status_code == 200

    def test_server_mode_is_case_insensitive(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "SERVER")
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/data")
        assert resp.status_code == 503
        assert resp.headers["X-PAXS-Mode-Hint"] == "server"

    def test_invalid_mode_falls_back_to_hybrid(self, client, monkeypatch):
        """A junk setting must not silently disable raw streaming."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "banana")
        _, series = _ingest_series(client)
        assert client.get(f"/api/v1/volume/{series}/data").status_code == 200

    def test_per_role_override_drives_the_guard(
        self, client, auth_headers, monkeypatch
    ):
        """A role pinned to 'server' loses the raw stream while hybrid roles keep it."""
        from tests.helpers import auth_for_role

        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP",
            '{"PHYSICIAN": "server", "TECHNOLOGIST": "client"}',
        )
        _, series = _ingest_series(client)

        phys = auth_for_role(
            client, "srv_physician", role="PHYSICIAN", admin=auth_headers
        )
        tech = auth_for_role(
            client, "srv_technologist", role="TECHNOLOGIST", admin=auth_headers
        )

        denied = client.get(f"/api/v1/volume/{series}/data", headers=phys)
        assert denied.status_code == 503
        assert denied.headers["X-PAXS-Mode-Hint"] == "server"

        allowed = client.get(f"/api/v1/volume/{series}/data", headers=tech)
        assert allowed.status_code == 200

    def test_force_restores_the_stream_for_the_pinned_role(
        self, client, auth_headers, monkeypatch
    ):
        from tests.helpers import auth_for_role

        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)
        phys = auth_for_role(
            client, "srv_phys_force", role="PHYSICIAN", admin=auth_headers
        )
        resp = client.get(f"/api/v1/volume/{series}/data?force=1", headers=phys)
        assert resp.status_code == 200

    def test_unknown_series_still_404s(self, client, monkeypatch):
        """ACL/404 resolution precedes the mode guard."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        resp = client.get(f"/api/v1/volume/{generate_uid()}/data")
        assert resp.status_code == 404

    def test_streaming_disabled_reports_503_without_mode_hint(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        monkeypatch.setattr(settings, "VOLUME_STREAMING_ENABLED", False)
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/data")
        assert resp.status_code == 503
        # The feature-disable path is distinct from the mode guard.
        assert resp.headers.get("X-PAXS-Mode-Hint") is None


# =========================================================================== #
#  §2 — Steps 2.2/2.3: server-rendered slices and batch tiles
# =========================================================================== #


class TestServerRenderedSlices:
    """/mpr/slice remains fully available (and PNG-valid) in server mode."""

    def test_slice_returns_valid_png_in_server_mode(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=2&format=png"
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/png")
        img = _decode_png(resp.content)
        assert img.size == (32, 32)
        assert img.mode == "L"
        assert resp.headers["X-MPR-Rows"] == "32"
        assert resp.headers["X-MPR-Cols"] == "32"
        assert resp.headers["X-MPR-Plane"] == "axial"
        assert resp.headers["X-PAXS-Mode-Hint"] == "server"

    def test_slice_honours_window_override_in_server_mode(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=0"
            "&window_center=500&window_width=2000"
        )

        assert resp.status_code == 200
        assert resp.headers["X-MPR-Window"].startswith("500.0,2000.0")
        _decode_png(resp.content)

    def test_raw_slice_is_available_on_demand_for_server_measurements(
        self, client, monkeypatch
    ):
        """Step 2.6 — HU values come from ?format=raw, strictly on demand.

        In server mode there is no client-side volume, so a measurement tool
        must be able to pull one float32 plane.  The payload must match the
        advertised geometry so the client can index it without a header parser.
        """
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&format=raw"
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/octet-stream")
        rows = int(resp.headers["X-MPR-Rows"])
        cols = int(resp.headers["X-MPR-Cols"])
        assert len(resp.content) == rows * cols * 4, "raw plane length must match geometry"

        plane = np.frombuffer(resp.content, dtype="<f4").reshape(rows, cols)
        # Slice 1: base 110, core 1600; HU = value - 1024.
        assert plane[0, 0] == pytest.approx(110 - 1024)
        assert plane[16, 16] == pytest.approx(1600 - 1024)


class TestTileBatchEndpoint:
    """`/tiles` returns a prefetch window of pre-rendered PNG frames."""

    def test_tiles_returns_batch_for_range(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=6)

        resp = client.get(
            f"/api/v1/volume/{series}/tiles?plane=axial&range=1-4"
            "&window_center=40&window_width=400&size=512"
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/octet-stream")
        assert resp.headers["X-MPR-Tiles"] == "4"
        assert resp.headers["X-MPR-Range"] == "1-4"
        assert resp.headers["X-MPR-Plane"] == "axial"
        assert resp.headers["X-PAXS-Mode-Hint"] == "server"

        frames = _split_default_framing(resp)
        assert len(frames) == 4, "one PNG per slice in the range"
        for frame in frames:
            img = _decode_png(frame)
            assert img.size == (32, 32)

    def test_tiles_frames_are_distinct_per_slice(self, client, monkeypatch):
        """Each slice carries different pixel data, so frames must differ."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        resp = client.get(
            f"/api/v1/volume/{series}/tiles?plane=axial&range=0-2"
            "&window_center=1000&window_width=4000"
        )
        frames = _split_default_framing(resp)
        assert len(frames) == 3
        assert len({f for f in frames}) == 3
        # The bright core sits in the middle of every plane.
        for frame in frames:
            arr = np.asarray(_decode_png(frame))
            assert arr[16, 16] > arr[0, 0]

    def test_tiles_single_index_range(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=3")

        assert resp.status_code == 200
        assert resp.headers["X-MPR-Tiles"] == "1"
        assert resp.headers["X-MPR-Range"] == "3-3"
        assert len(_split_default_framing(resp)) == 1

    def test_tiles_agrees_with_single_slice_endpoint(self, client, monkeypatch):
        """A batch frame must be byte-identical to the same /mpr/slice render.

        This is the contract that lets the viewer treat /tiles as a plain
        prefetch accelerator rather than a second rendering pipeline.
        """
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)
        wl = "&window_center=500&window_width=1500"

        batch = client.get(
            f"/api/v1/volume/{series}/tiles?plane=axial&range=2-2{wl}"
        )
        single = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=2{wl}"
        )

        assert batch.status_code == 200 and single.status_code == 200
        frames = _split_default_framing(batch)
        assert len(frames) == 1
        assert frames[0] == single.content

    @pytest.mark.parametrize("plane", ["axial", "sagittal", "coronal"])
    def test_tiles_supports_every_native_plane(self, client, monkeypatch, plane):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        resp = client.get(f"/api/v1/volume/{series}/tiles?plane={plane}&range=0-1")

        assert resp.status_code == 200
        assert resp.headers["X-MPR-Plane"] == plane
        assert len(_split_default_framing(resp)) == 2

    def test_tiles_inline_multipart_is_self_describing(self, client, monkeypatch):
        """The multipart framing carries PNG parts that decode individually."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        resp = client.get(
            f"/api/v1/volume/{series}/tiles?plane=axial&range=0-2&inline=1"
        )

        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("multipart/mixed")
        boundary = re.search(r"boundary=([^;]+)", resp.headers["content-type"]).group(1)
        parts = [
            p for p in resp.content.split(("--" + boundary).encode())
            if p.strip() and not p.strip().startswith(b"--")
        ]
        assert len(parts) == 3
        for part in parts:
            body = part.split(b"\r\n\r\n", 1)[1]
            img = _decode_png(body.rstrip(b"\r\n"))
            assert img.size == (32, 32)

    def test_tiles_clamps_over_long_range_to_the_batch_budget(self, client, monkeypatch):
        """A huge range is capped instead of flattening the node."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        monkeypatch.setattr(settings, "VOLUME_MAX_SLICES", 3)
        _, series = _ingest_series(client, n_slices=6)

        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=0-5")

        assert resp.status_code == 200
        assert resp.headers["X-MPR-Tiles"] == "3"
        assert resp.headers["X-MPR-Range"] == "0-2"

    def test_tiles_range_past_end_is_clamped(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=2-99")

        assert resp.status_code == 200
        assert resp.headers["X-MPR-Range"] == "2-3"
        assert len(_split_default_framing(resp)) == 2

    def test_tiles_reversed_range_collapses_to_start(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=3-1")
        assert resp.status_code == 200
        assert resp.headers["X-MPR-Range"] == "3-3"

    def test_tiles_default_range_covers_the_plane(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial")
        assert resp.status_code == 200
        assert resp.headers["X-MPR-Range"] == "0-3"

    def test_tiles_default_size_is_512(self, client, monkeypatch):
        """`size` defaults to 512 per the plan; the native plane is 32 px, so
        the renderer must not up-sample beyond the real resolution."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=2)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=0-0")
        assert resp.status_code == 200
        img = _decode_png(_split_default_framing(resp)[0])
        assert max(img.size) == 32, "server frames stay at native resolution"

    def test_tiles_is_available_in_hybrid_and_client_modes(self, client, monkeypatch):
        """Phase 2 adds /tiles unconditionally — it is not a server-mode-only
        endpoint in the router, only in intent."""
        for mode in ("hybrid", "client", "server"):
            monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", mode)
            _, series = _ingest_series(client, n_slices=2)
            resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=0-0")
            assert resp.status_code == 200, mode


class TestTileEndpointGuards:
    """Bad input is rejected with a precise status, never a stack trace."""

    def test_unknown_series_404(self, client):
        resp = client.get(f"/api/v1/volume/{generate_uid()}/tiles?plane=axial&range=0-1")
        assert resp.status_code == 404

    def test_bad_plane_400(self, client):
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=oblique&range=0-1")
        assert resp.status_code == 400
        assert "plane" in resp.json()["detail"]

    def test_bad_range_400(self, client):
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=abc")
        assert resp.status_code == 400

    def test_negative_index_416(self, client):
        """A well-formed range that starts before the first slice is a range
        error (416), not a parse error."""
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=-2-3")
        assert resp.status_code == 416

    def test_unparseable_range_400(self, client):
        """A malformed range string is a request error (400)."""
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=abc-def")
        assert resp.status_code == 400

    def test_index_past_end_416(self, client):
        _, series = _ingest_series(client, n_slices=4)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=9-12")
        assert resp.status_code == 416

    def test_bad_size_400(self, client):
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=0-0&size=0")
        assert resp.status_code == 400

    def test_bad_window_400(self, client):
        _, series = _ingest_series(client)
        resp = client.get(
            f"/api/v1/volume/{series}/tiles?plane=axial&range=0-0&window_center=abc"
        )
        assert resp.status_code == 400

    def test_streaming_disabled_503(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VOLUME_STREAMING_ENABLED", False)
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=0-0")
        assert resp.status_code == 503


# =========================================================================== #
#  §3 — Step 2.6 support: session tracking for server-mode activity
# =========================================================================== #


class TestServerSessionTracking:
    """Server-mode operations register sessions (plan §5.3 / task step 2.7)."""

    def _count(self) -> int:
        return volume_service.active_server_session_count()

    def test_slice_request_registers_a_session(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        before = self._count()
        assert client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1"
        ).status_code == 200
        after = self._count()

        assert after == before + 1
        with volume_service._VOL_CACHE_LOCK:
            assert series in volume_service._ACTIVE_SERVER_SESSIONS

    def test_tiles_request_registers_a_session(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        before = self._count()
        assert client.get(
            f"/api/v1/volume/{series}/tiles?plane=axial&range=0-2"
        ).status_code == 200

        assert self._count() == before + 1
        with volume_service._VOL_CACHE_LOCK:
            assert series in volume_service._ACTIVE_SERVER_SESSIONS

    def test_repeated_requests_do_not_inflate_the_count(self, client, monkeypatch):
        """The session map is keyed by series — ten slices are one session."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        for index in range(4):
            client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index={index}")

        assert self._count() == 1

    def test_hybrid_mode_slice_fetch_is_not_a_server_session(self, client, monkeypatch):
        """Off-mode /mpr/slice must not register.

        In client/hybrid mode the endpoint is an occasional on-demand fetch, not
        a live thin-client session. Registering it would inflate the hybrid load
        signal the negotiator reads *and* leak entries that nothing releases,
        which showed up as an ordering failure in test_negotiate's absolute
        session count.
        """
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        _, series = _ingest_series(client, n_slices=4)

        before = self._count()
        assert client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1"
        ).status_code == 200
        assert self._count() == before, "hybrid fetch must not register a session"

    def test_hybrid_mode_tiles_fetch_is_not_a_server_session(self, client, monkeypatch):
        """Off-mode /tiles must not register either (same reasoning)."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        _, series = _ingest_series(client, n_slices=4)

        before = self._count()
        assert client.get(
            f"/api/v1/volume/{series}/tiles?plane=axial&range=0-2"
        ).status_code == 200
        assert self._count() == before, "hybrid prefetch must not register a session"

    def test_failed_request_does_not_register(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        before = self._count()
        assert client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=99"
        ).status_code == 416
        assert self._count() == before

    def test_negotiate_observes_the_registered_session(self, client, monkeypatch):
        """End-to-end: a server render is visible to the hybrid load-balancer."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=0-1")

        # Switch to hybrid with a cap of 1 session and a very weak client:
        # the overloaded server must now refuse to pile on, and the negotiated
        # response reports the load it sees.
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(settings, "HYBRID_MAX_SERVER_MPR_SESSIONS", 1)
        body = client.get(f"/api/v1/volume/{series}/negotiate?webgl_score=10").json()

        assert body["configured_mode"] == "hybrid"
        # score 10 < 20, so even an overloaded server cannot push it to client
        assert body["effective_mode"] == "server"
        # Phase 4 (§7.3) reports load as `<active>/<max>` rather than a bare
        # integer; parse the numerator.
        load = client.get(
            f"/api/v1/volume/{series}/negotiate?webgl_score=10"
        ).headers["X-PAXS-Server-Load"]
        active = int(load.split("/")[0])
        assert active >= 1


# =========================================================================== #
#  §4 — Cache behaviour (steps 2.2/2.3 require L1 + Redis reuse)
# =========================================================================== #


class TestTileCaching:
    """Rendered tiles are cached, so a scroll-back does not re-rasterize."""

    def test_tile_cache_key_groups_windows_and_isolates_series(self):
        a = volume_service.tile_cache_key("1.2.3", "axial", 5, 40.0, 400.0)
        b = volume_service.tile_cache_key("1.2.3", "axial", 5, 40.04, 400.0)
        c = volume_service.tile_cache_key("1.2.3", "axial", 5, 40.0, 800.0)
        d = volume_service.tile_cache_key("1.2.3", "axial", 6, 40.0, 400.0)
        e = volume_service.tile_cache_key("9.9.9", "axial", 5, 40.0, 400.0)
        f = volume_service.tile_cache_key("1.2.3", "axial", 5)
        g = volume_service.tile_cache_key("1.2.3", "sagittal", 5, 40.0, 400.0)

        assert a == b, "sub-tenth window jitter must not mint a new cache key"
        assert len({a, c, d, e, f, g}) == 6, "series/plane/index/window stay distinct"

    def test_repeated_tile_request_is_served_from_cache(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)
        url = f"/api/v1/volume/{series}/tiles?plane=axial&range=0-1&window_center=40&window_width=400"

        first = client.get(url)
        assert first.status_code == 200
        with volume_service._VOL_CACHE_LOCK:
            cached_keys = [k for k in volume_service._TILE_CACHE if series in k]
        assert len(cached_keys) == 2, "both frames should now be cached"

        second = client.get(url)
        assert second.status_code == 200
        assert second.content == first.content

    def test_render_tiles_function_is_directly_callable(self, client, monkeypatch):
        """The service function works against an explicitly resolved bundle,
        which is how the router drives it (one ACL check for the whole batch)."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client, n_slices=4)

        async def _run():
            async with AsyncSessionLocal() as session:
                bundle = await volume_service.volume_bundle(session, series)
                assert bundle is not None
                # volume_bundle returns (info, slices) — same order the router uses.
                info, slices = bundle
                return volume_service.render_tiles(
                    series, "axial", 0, 2, 40.0, 400.0, 512, slices, info
                )

        frames = asyncio.run(_run())
        assert len(frames) == 3
        for png in frames:
            assert png[:8] == PNG_MAGIC

    def test_render_plane_tile_rejects_bad_index(self, client):
        _, series = _ingest_series(client, n_slices=4)
        info = client.get(f"/api/v1/volume/{series}/info").json()
        with pytest.raises(IndexError):
            volume_service.render_plane_tile([], info, "axial", 99)
        with pytest.raises(ValueError):
            volume_service.render_plane_tile([], info, "oblique", 0)


# =========================================================================== #
#  §5 — Regression: Phase 1 surfaces are untouched
# =========================================================================== #


class TestPhaseOneRegression:
    """Negotiate still describes server mode correctly after Phase 2."""

    def test_negotiate_server_mode_advertises_rendered_endpoints_only(
        self, client, monkeypatch
    ):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)
        body = client.get(f"/api/v1/volume/{series}/negotiate").json()

        assert body["configured_mode"] == "server"
        assert body["effective_mode"] == "server"
        assert "mpr_slice" in body["endpoints"]
        assert "data" not in body["endpoints"]
        assert body["capabilities"]["raw_volume_stream"] is False
        assert body["capabilities"]["server_renders_slices"] is True

    def test_hybrid_default_negotiate_is_unchanged(self, client):
        _, series = _ingest_series(client)
        body = client.get(f"/api/v1/volume/{series}/negotiate").json()
        assert body["configured_mode"] == "hybrid"
        assert body["effective_mode"] == "server"
        assert "hybrid_thresholds" in body


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def _split_default_framing(resp) -> list[bytes]:
    """Slice a default-framed /tiles response into its individual PNGs.

    Uses the self-describing ``X-PAXS-Tile-Offset`` / ``X-PAXS-Tile-Length``
    header lists — exactly what the thin client does.
    """
    offsets = [int(v) for v in resp.headers["X-PAXS-Tile-Offset"].split(",") if v != ""]
    lengths = [int(v) for v in resp.headers["X-PAXS-Tile-Length"].split(",") if v != ""]
    assert len(offsets) == len(lengths) == int(resp.headers["X-MPR-Tiles"])
    body = resp.content
    frames = []
    for off, length in zip(offsets, lengths):
        frame = body[off:off + length]
        assert len(frame) == length, "offset/length lists must describe the body"
        frames.append(frame)
    assert offsets[0] == 0
    assert offsets[-1] + lengths[-1] == len(body), "frames must tile the body exactly"
    return frames
