"""Tests for the 3D VRT / MIP endpoint (roadmap #27) — server-side projection.

Covers maximum/minimum/average projections and the alpha-composited vrt mode
along all three axes, slab (offset + thickness) semantics, PNG/raw/npy
formats, window override, the expanded multi-frame stack, and the
404/400/416/503 guards.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from pydicom.uid import generate_uid

from tests.test_volume import _ingest_series
from tests.test_multiframe_volume import _make_enhanced_ct, _stow

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _mip_raw(client, series: str, **params) -> np.ndarray:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    url = f"/api/v1/volume/{series}/mip?format=raw" + (f"&{qs}" if qs else "")
    resp = client.get(url)
    assert resp.status_code == 200, resp.text
    rows = int(resp.headers["x-mip-rows"])
    cols = int(resp.headers["x-mip-cols"])
    return np.frombuffer(resp.content, dtype="<f4").reshape(rows, cols)


def test_mip_axial_full_stack_is_per_pixel_max(client):
    """MIP along z over the whole stack = per-pixel max of the 4 slices."""
    study, series, _ = _ingest_series(client, n_slices=4)
    data = _mip_raw(client, series)
    assert data.shape == (32, 32)
    # base = 100 + i*10, core = 1500 + i*100; HU = value - 1024
    assert data[0, 0] == pytest.approx(130 - 1024)      # max base
    assert data[16, 16] == pytest.approx(1800 - 1024)   # max core
    assert data[16, 3] == pytest.approx(130 - 1024)     # outside core
    assert data[7, 7] == pytest.approx(130 - 1024)      # just outside core


def test_mip_slab_offset_thickness(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    data = _mip_raw(client, series, offset=1, thickness=2)
    # slab = slices 1..2: base 110..120, core 1600..1700
    assert data[0, 0] == pytest.approx(120 - 1024)
    assert data[16, 16] == pytest.approx(1700 - 1024)
    assert "1:3" == _mip_slab(client, series, offset=1, thickness=2)


def _mip_slab(client, series: str, **params) -> str:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    resp = client.get(f"/api/v1/volume/{series}/mip?format=raw&{qs}")
    assert resp.status_code == 200
    return resp.headers["x-mip-slab"]


def test_mip_axis_x_projects_over_columns(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    data = _mip_raw(client, series, axis="x")
    # output is (depth, rows) = (4, 32); data[k,j] = max_i vol[k,j,i]
    assert data.shape == (4, 32)
    assert data[0, 0] == pytest.approx(100 - 1024)   # slice 0 base
    assert data[3, 0] == pytest.approx(130 - 1024)   # slice 3 base
    assert data[3, 16] == pytest.approx(1800 - 1024)  # core, last slice
    assert resp_axis(client, series, axis="x") == "x"


def resp_axis(client, series: str, **params) -> str:
    qs = "&".join(f"{k}={v}" for k, v in params.items())
    resp = client.get(f"/api/v1/volume/{series}/mip?format=raw&{qs}")
    assert resp.status_code == 200
    return resp.headers["x-mip-axis"]


def test_mip_axis_y_projects_over_rows(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    data = _mip_raw(client, series, axis="y")
    # output is (depth, cols) = (4, 32); data[k,i] = max_j vol[k,j,i]
    assert data.shape == (4, 32)
    assert data[0, 0] == pytest.approx(100 - 1024)
    assert data[3, 0] == pytest.approx(130 - 1024)
    assert data[3, 16] == pytest.approx(1800 - 1024)


def test_mip_minip_and_average(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    mn = _mip_raw(client, series, mode="minip")
    assert mn[0, 0] == pytest.approx(100 - 1024)     # min base
    assert mn[16, 16] == pytest.approx(1500 - 1024)  # min core
    avg = _mip_raw(client, series, mode="average")
    assert avg[0, 0] == pytest.approx(115 - 1024)    # mean base
    assert avg[16, 16] == pytest.approx(1650 - 1024)  # mean core


def test_mip_vrt_renders_composite(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    data = _mip_raw(client, series, mode="vrt")
    assert data.shape == (32, 32)
    assert np.isfinite(data).all()
    # composite intensity stays inside the volume's HU range
    assert data.min() >= (100 - 1024) - 1
    assert data.max() <= (1800 - 1024) + 1
    # the bright core (visible in the window) renders brighter than air
    assert data[16, 16] > data[0, 0]
    # window override is reflected in the header
    resp = client.get(f"/api/v1/volume/{series}/mip?mode=vrt"
                      "&window_center=0&window_width=2000")
    assert resp.status_code == 200
    assert resp.headers["x-mip-window"].startswith("0.0,2000.0")


def test_mip_png_and_npy_formats(client):
    study, series, _ = _ingest_series(client, n_slices=3)
    resp = client.get(f"/api/v1/volume/{series}/mip")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content[:8] == PNG_MAGIC
    assert resp.headers["x-mip-mode"] == "mip"
    assert resp.headers["x-mip-window"]

    resp = client.get(f"/api/v1/volume/{series}/mip?format=npy")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-npy")
    arr = np.load(io.BytesIO(resp.content))
    assert arr.shape == (32, 32)
    assert arr.dtype == np.float32
    assert arr[16, 16] == pytest.approx(1700 - 1024)


def test_mip_on_multiframe_expanded_stack(client):
    """MIP works on an Enhanced CT whose frames are z-ordered by the service."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    # frame z: 10, 0, 5 → z-order 1, 2, 0 → base 120, core 1700
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        per_frame_positions=[[1.0, 2.0, 10.0], [1.0, 2.0, 0.0], [1.0, 2.0, 5.0]],
    ))
    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["slices"] == 3
    data = _mip_raw(client, series)
    assert data.shape == (16, 16)
    assert data[0, 0] == pytest.approx(120 - 1024)
    assert data[8, 8] == pytest.approx(1700 - 1024)


def test_mip_guards(client):
    assert client.get(f"/api/v1/volume/{generate_uid()}/mip").status_code == 404

    study, series, _ = _ingest_series(client, n_slices=3)

    # Bad axis / mode / format
    assert client.get(f"/api/v1/volume/{series}/mip?axis=w").status_code == 400
    assert client.get(f"/api/v1/volume/{series}/mip?mode=bogus").status_code == 400
    assert client.get(f"/api/v1/volume/{series}/mip?format=wav").status_code == 400

    # Bad numeric params
    assert client.get(f"/api/v1/volume/{series}/mip?offset=abc").status_code == 400
    assert client.get(f"/api/v1/volume/{series}/mip?thickness=abc").status_code == 400
    assert client.get(
        f"/api/v1/volume/{series}/mip?window_center=nan&window_width=10"
    ).status_code in (400, 200)  # NaN window falls back to the series window

    # Out-of-range slab
    assert client.get(f"/api/v1/volume/{series}/mip?offset=99").status_code == 416
    assert client.get(f"/api/v1/volume/{series}/mip?offset=-1").status_code == 416
    assert client.get(f"/api/v1/volume/{series}/mip?thickness=0").status_code == 416

    # Disabled → 503
    from app.core.config import settings
    prev = settings.VOLUME_STREAMING_ENABLED
    settings.VOLUME_STREAMING_ENABLED = False
    try:
        assert client.get(f"/api/v1/volume/{series}/mip").status_code == 503
    finally:
        settings.VOLUME_STREAMING_ENABLED = prev
