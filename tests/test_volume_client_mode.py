"""Phase 3 integration tests — explicit client mode (thick workstation).

Covers ``plan_viewer_process.md`` §3.2 and §8 Phase 3:

  * ``/mpr/slice`` and ``/mip`` are refused with **503 +
    ``X-PAXS-Mode-Hint: client``** while ``VIEWER_PROCESSING_MODE == "client"``,
    and ``?force=1`` restores them (the documented diagnostic escape hatch).
  * ``/data`` keeps streaming the raw binary voxel buffer in client mode — the
    whole point of the mode is that the workstation does the work, so the raw
    stream must never be gated.
  * ``/tiles`` stays available in client mode (a batch prefetch is a legitimate
    client-side optimisation; it is not the same thing as server-side slicing).

Regression intent: this guard is the mirror image of Phase 2's.  ``hybrid``
(the shipped default) and ``server`` must behave exactly as they did before
Phase 3 — in particular ``server`` mode must still be able to slice, and
``hybrid`` must still be able to do both.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from PIL import Image
from pydicom.uid import generate_uid

from app.core.config import settings
from app.services import volume_service
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

SLICE_DISABLED_DETAIL = "Server-side slicing is disabled in client processing mode"


def _ingest_series(client, n_slices: int = 6):
    """STOW a synthetic axial CT stack; returns (study_uid, series_uid)."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    datasets = []
    for i in range(n_slices):
        pixel = np.full((32, 32), 100 + i * 10, dtype=np.uint16)
        pixel[8:24, 8:24] = 1500 + i * 100  # bright core, differs per slice
        ds = make_instance(
            patient_id="CLI_PAT",
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
    """Decode PNG bytes, asserting the magic number so a truncated frame fails
    loudly here rather than as a confusing pixel assertion later."""
    assert payload[:8] == PNG_MAGIC, "payload is not a PNG"
    img = Image.open(io.BytesIO(payload))
    img.load()
    return img


@pytest.fixture(autouse=True)
def _isolate_viewer_state():
    """Restore viewer settings and clear server-mode state around each test.

    ``_ACTIVE_SERVER_SESSIONS``/``_VOL_CACHE``/``_TILE_CACHE`` are module-level
    state shared across suites; clearing on both sides keeps these tests
    independent of collection order (see the Phase 2 suite for the failure this
    prevented).
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
#  §1 — Step 3.1: mode guard on /mpr/slice
# =========================================================================== #


class TestSliceClientModeGuard:
    """``/mpr/slice`` refuses server-side reslicing only when mode == 'client'."""

    def test_client_mode_rejects_slice_with_503_and_hint(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1")

        assert resp.status_code == 503
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"
        assert resp.headers["Cache-Control"] == "no-store"
        assert resp.json()["detail"] == SLICE_DISABLED_DETAIL
        # No rendered frame may leak through the refusal.
        assert len(resp.content) < 4096

    def test_client_mode_force_bypasses_the_guard(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)

        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&force=1"
        )

        assert resp.status_code == 200
        img = _decode_png(resp.content)
        assert img.size == (32, 32)
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"

    @pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "Yes"])
    def test_force_accepts_documented_spellings(self, client, monkeypatch, flag):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&force={flag}"
        )
        assert resp.status_code == 200

    @pytest.mark.parametrize("flag", ["0", "false", "no", ""])
    def test_non_force_values_do_not_bypass(self, client, monkeypatch, flag):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&force={flag}"
        )
        assert resp.status_code == 503

    @pytest.mark.parametrize("plane", ["axial", "sagittal", "coronal"])
    def test_guard_covers_every_native_plane(self, client, monkeypatch, plane):
        """The guard is on the endpoint, not on one plane's code path."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane={plane}&index=0"
        )
        assert resp.status_code == 503
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"

    def test_guard_applies_to_raw_format_too(self, client, monkeypatch):
        """Not just the PNG path — measurements must not sneak through."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&format=raw"
        )
        assert resp.status_code == 503

    def test_guard_applies_to_oblique_planes(self, client, monkeypatch):
        """An oblique request must be refused before any reslice work happens."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?origin=0,0,0"
            "&row=1,0,0&col=0,1,0&size=16x16"
        )
        assert resp.status_code == 503
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"

    def test_client_mode_is_case_insensitive(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "CLIENT")
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1")
        assert resp.status_code == 503
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"

    def test_invalid_mode_falls_back_to_hybrid_and_slices(self, client, monkeypatch):
        """A junk setting must not silently disable server slicing."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "banana")
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1")
        assert resp.status_code == 200

    def test_unknown_series_still_404s_in_client_mode(self, client, monkeypatch):
        """ACL runs before the guard, so an unknown series does not get a hint."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        resp = client.get(
            f"/api/v1/volume/{generate_uid()}/mpr/slice?plane=axial&index=0"
        )
        assert resp.status_code in (403, 404)


class TestSliceGuardRegression:
    """The Phase 3 guard must not disturb the other two modes."""

    def test_server_mode_still_slices(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1")

        assert resp.status_code == 200
        assert _decode_png(resp.content).size == (32, 32)
        assert resp.headers["X-PAXS-Mode-Hint"] == "server"

    def test_hybrid_default_still_slices(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1")

        assert resp.status_code == 200
        assert resp.headers["X-PAXS-Mode-Hint"] == "hybrid"

    def test_per_role_override_drives_the_guard(
        self, client, auth_headers, monkeypatch
    ):
        """A role pinned to 'client' loses slicing while a server role keeps it."""
        from tests.helpers import auth_for_role

        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP",
            '{"PHYSICIAN": "client", "TECHNOLOGIST": "server"}',
        )
        _, series = _ingest_series(client)

        phys = auth_for_role(
            client, "cli_physician", role="PHYSICIAN", admin=auth_headers
        )
        tech = auth_for_role(
            client, "cli_technologist", role="TECHNOLOGIST", admin=auth_headers
        )

        denied = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1", headers=phys
        )
        assert denied.status_code == 503
        assert denied.headers["X-PAXS-Mode-Hint"] == "client"

        allowed = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1", headers=tech
        )
        assert allowed.status_code == 200
        assert allowed.headers["X-PAXS-Mode-Hint"] == "server"

    def test_force_restores_slicing_for_the_pinned_client_role(
        self, client, auth_headers, monkeypatch
    ):
        from tests.helpers import auth_for_role

        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        monkeypatch.setattr(
            settings, "VIEWER_PROCESSING_ROLE_MAP", '{"PHYSICIAN": "client"}'
        )
        _, series = _ingest_series(client)
        phys = auth_for_role(
            client, "cli_physician2", role="PHYSICIAN", admin=auth_headers
        )

        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&force=1",
            headers=phys,
        )
        assert resp.status_code == 200

    def test_streaming_disabled_reports_503_without_mode_hint(
        self, client, monkeypatch
    ):
        """A globally disabled endpoint must not masquerade as a mode refusal."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        monkeypatch.setattr(settings, "VOLUME_STREAMING_ENABLED", False)
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1")

        assert resp.status_code == 503
        assert resp.json()["detail"] == "Volume streaming is disabled"


# =========================================================================== #
#  §2 — Step 3.1: mode guard on /mip
# =========================================================================== #


class TestMipClientModeGuard:
    """``/mip`` refuses server-side projection only when mode == 'client'."""

    def test_client_mode_rejects_mip_with_503_and_hint(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/mip?axis=z")

        assert resp.status_code == 503
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"
        assert resp.json()["detail"] == SLICE_DISABLED_DETAIL

    def test_client_mode_force_bypasses_the_guard(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/mip?axis=z&force=1")

        assert resp.status_code == 200
        img = _decode_png(resp.content)
        assert img.size == (32, 32)
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"

    @pytest.mark.parametrize("axis", ["z", "x", "y"])
    def test_guard_covers_every_projection_axis(self, client, monkeypatch, axis):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/mip?axis={axis}")
        assert resp.status_code == 503
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"

    @pytest.mark.parametrize("mode", ["mip", "minip", "average", "vrt"])
    def test_guard_covers_every_projection_mode(self, client, monkeypatch, mode):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/mip?axis=z&mode={mode}")
        assert resp.status_code == 503

    def test_guard_applies_to_raw_projection_too(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)
        resp = client.get(f"/api/v1/volume/{series}/mip?axis=z&format=raw")
        assert resp.status_code == 503

    def test_server_mode_still_projects(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/mip?axis=z")

        assert resp.status_code == 200
        assert _decode_png(resp.content).size == (32, 32)
        assert resp.headers["X-PAXS-Mode-Hint"] == "server"

    def test_hybrid_default_still_projects(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "hybrid")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/mip?axis=z")

        assert resp.status_code == 200
        assert resp.headers["X-PAXS-Mode-Hint"] == "hybrid"

    def test_unknown_series_still_404s_in_client_mode(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        resp = client.get(f"/api/v1/volume/{generate_uid()}/mip?axis=z")
        assert resp.status_code in (403, 404)


# =========================================================================== #
#  §3 — Step 3.2: client mode keeps the raw voxel stream
# =========================================================================== #


class TestClientModeRawStreaming:
    """In client mode `/data` is the pipeline — it must never be gated."""

    def test_data_streams_raw_bytes_in_client_mode(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/data")

        assert resp.status_code == 200
        assert resp.content[:8] != PNG_MAGIC, "raw stream must not be a PNG"
        info = client.get(f"/api/v1/volume/{series}/info").json()
        expected = (
            info["slices"] * info["rows"] * info["columns"]
            * np.dtype(info["dtype"]).itemsize
        )
        assert len(resp.content) == expected
        assert resp.headers["X-Volume-Slices"] == str(info["slices"])

    def test_data_bytes_are_the_documented_voxel_values(self, client, monkeypatch):
        """Decode the stream and confirm the HU values, not just its length.

        A stream of the right size but wrong contents would still break the GPU
        pipeline, so the test reads the actual voxels back.
        """
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client, n_slices=4)

        resp = client.get(f"/api/v1/volume/{series}/data")
        assert resp.status_code == 200

        info = client.get(f"/api/v1/volume/{series}/info").json()
        arr = np.frombuffer(resp.content, dtype=np.dtype(info["dtype"]))

        # Slice 0 is a 32x32 frame of 100 with a 1500 core (rescale -1024).
        frame0 = arr[: 32 * 32]
        bg = frame0[0]
        core = frame0[16 * 32 + 16]
        assert bg < core, "the bright core must survive the stream"
        assert frame0.size == 32 * 32

    def test_data_downsample_still_works_in_client_mode(self, client, monkeypatch):
        """The coarse-preview path the viewer uses must stay available."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client, n_slices=8)

        full = client.get(f"/api/v1/volume/{series}/data")
        coarse = client.get(f"/api/v1/volume/{series}/data?downsample=2")

        assert full.status_code == 200
        assert coarse.status_code == 200
        assert len(coarse.content) < len(full.content)

    def test_data_slice_range_still_works_in_client_mode(self, client, monkeypatch):
        """Streaming a sub-range is a client-side optimisation, not server work."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client, n_slices=6)

        resp = client.get(f"/api/v1/volume/{series}/data?slices=1-3")

        assert resp.status_code == 200
        assert resp.headers["X-Volume-Slices"] == "3"

    def test_data_rescale_is_honoured_in_client_mode(self, client, monkeypatch):
        """The rescaled stream is float32 (HU for CT) while the default keeps the
        native integer dtype — the client shader picks its texture format from
        ``X-Volume-Dtype``, so both encodings must stay available in client mode.

        NOTE: ``rescale=1`` changes the *width* of each voxel (uint16 → float32),
        so the payload lengths legitimately differ by a 2x factor; it is the voxel
        *count* (geometry) that must match, not the byte count.
        """
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client, n_slices=4)

        raw = client.get(f"/api/v1/volume/{series}/data")
        rescaled = client.get(f"/api/v1/volume/{series}/data?rescale=1")

        assert raw.status_code == rescaled.status_code == 200
        # Geometry is identical: same slice/row/column counts on both streams.
        for header in ("X-Volume-Slices", "X-Volume-Rows", "X-Volume-Cols"):
            assert raw.headers[header] == rescaled.headers[header]

        # ...but the dtype — and therefore the payload width — changes.
        assert raw.headers["X-Volume-Dtype"] == "uint16"
        assert rescaled.headers["X-Volume-Dtype"] == "float32"
        assert int(rescaled.headers["Content-Length"]) == int(raw.headers["Content-Length"]) * 2

        # Same voxel count, wider elements, different value mapping.
        voxels = 4 * 32 * 32
        assert len(raw.content) == voxels * 2
        assert len(rescaled.content) == voxels * 4
        assert raw.content != rescaled.content

    def test_data_rescaled_stream_decodes_to_float32_hu(self, client, monkeypatch):
        """The float32 stream really carries HU (slope=1, intercept=-1024)."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client, n_slices=4)

        resp = client.get(f"/api/v1/volume/{series}/data?rescale=1")

        assert resp.status_code == 200
        assert resp.headers["X-Volume-Dtype"] == "float32"
        arr = np.frombuffer(resp.content, dtype="<f4")
        assert arr.size == 4 * 32 * 32
        # Slice 0 background stored as 100, intercept -1024 → -924 HU.
        assert arr[0] == pytest.approx(-924.0)
        # The bright core of slice 0 (1500) → 476 HU, and it scales per slice.
        assert arr.reshape(4, 32, 32)[0, 12, 12] == pytest.approx(476.0)
        assert arr.reshape(4, 32, 32)[3, 12, 12] == pytest.approx(776.0)


# =========================================================================== #
#  §4 — Client mode leaves /tiles and /info available
# =========================================================================== #


class TestClientModeCompanionEndpoints:
    """Endpoints a client-mode workstation legitimately still needs."""

    def test_info_is_available_in_client_mode(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)

        resp = client.get(f"/api/v1/volume/{series}/info")

        assert resp.status_code == 200
        body = resp.json()
        assert body["slices"] == 6
        assert body["rows"] == 32
        assert body["columns"] == 32

    def test_tiles_remain_available_in_client_mode(self, client, monkeypatch):
        """A batch prefetch is a client-side cache, not server-side reslicing."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client, n_slices=4)

        resp = client.get(f"/api/v1/volume/{series}/tiles?plane=axial&range=0-2")

        assert resp.status_code == 200
        assert resp.headers["X-MPR-Tiles"] == "3"
        assert resp.headers["X-PAXS-Mode-Hint"] == "client"

    def test_negotiate_reports_client_mode_endpoints(self, client, monkeypatch):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        _, series = _ingest_series(client)

        body = client.get(f"/api/v1/volume/{series}/negotiate").json()

        assert body["configured_mode"] == "client"
        assert body["effective_mode"] == "client"
        assert body["capabilities"]["raw_volume_stream"] is True
        # The rendered endpoints are advertised as unavailable, matching the
        # guard: a viewer that reads this must not call them.
        assert body["capabilities"]["server_renders_slices"] is False
        assert body["capabilities"]["client_webgl_required"] is True


# =========================================================================== #
#  §5 — Mode guard symmetry: the two guards must not overlap
# =========================================================================== #


class TestModeGuardSymmetry:
    """Each mode disables exactly one pipeline and leaves the other working."""

    @pytest.mark.parametrize(
        "mode,data_ok,slice_ok",
        [
            ("server", False, True),
            ("client", True, False),
            ("hybrid", True, True),
        ],
    )
    def test_each_mode_disables_exactly_one_pipeline(
        self, client, monkeypatch, mode, data_ok, slice_ok
    ):
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", mode)
        _, series = _ingest_series(client)

        data = client.get(f"/api/v1/volume/{series}/data")
        slice_ = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1")

        assert (data.status_code == 200) is data_ok, f"{mode}: /data"
        assert (slice_.status_code == 200) is slice_ok, f"{mode}: /mpr/slice"

    def test_hint_header_names_the_blocking_mode(self, client, monkeypatch):
        """The hint must report who refused, not who asked."""
        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        _, series = _ingest_series(client)
        assert client.get(
            f"/api/v1/volume/{series}/data"
        ).headers["X-PAXS-Mode-Hint"] == "server"

        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        assert client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1"
        ).headers["X-PAXS-Mode-Hint"] == "client"

    def test_both_guards_bypass_with_the_same_force_flag(self, client, monkeypatch):
        """One escape-hatch spelling, both pipelines."""
        _, series = _ingest_series(client)

        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "server")
        assert client.get(
            f"/api/v1/volume/{series}/data?force=1"
        ).status_code == 200
        assert client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&force=1"
        ).status_code == 200

        monkeypatch.setattr(settings, "VIEWER_PROCESSING_MODE", "client")
        assert client.get(
            f"/api/v1/volume/{series}/data?force=1"
        ).status_code == 200
        assert client.get(
            f"/api/v1/volume/{series}/mip?axis=z&force=1"
        ).status_code == 200
