"""Tests for the server-side MPR slice reslicing endpoint (thin-client fallback).

Covers native-plane slices (axial/sagittal/coronal) matching the actual voxel
data, the oblique plane sampler (patient-space geometry, trilinear), PNG/raw/npy
formats, window resolution/override, and the 404/400/416 guards.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from pydicom.uid import generate_uid

from tests.test_volume import _ingest_series

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _hu_volume(client, series: str) -> np.ndarray:
    """Reconstruct the float32 HU volume from /data?rescale=1 (as the client does)."""
    resp = client.get(f"/api/v1/volume/{series}/data?rescale=1")
    assert resp.status_code == 200
    info = client.get(f"/api/v1/volume/{series}/info").json()
    return np.frombuffer(resp.content, dtype="<f4").reshape(
        info["slices"], info["rows"], info["columns"]
    )


def test_mpr_png_axial(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/png")
    assert resp.content[:8] == PNG_MAGIC
    assert resp.headers["x-mpr-rows"] == "32"
    assert resp.headers["x-mpr-cols"] == "32"
    assert resp.headers["x-mpr-plane"] == "axial"
    assert resp.headers["x-mpr-window"]  # auto-resolved series window


def test_mpr_raw_axial_matches_instance_values(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=2&format=raw")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")
    plane = np.frombuffer(resp.content, dtype="<f4").reshape(32, 32)
    # Slice 2 carries base 100 + 20, core 1500 + 200; HU = value - 1024.
    assert plane[0, 0] == pytest.approx(100 + 20 - 1024)
    assert plane[16, 16] == pytest.approx(1500 + 200 - 1024)


def test_mpr_sagittal_matches_volume(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    vol = _hu_volume(client, series)
    for idx in (0, 7, 16, 31):
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=sagittal&index={idx}&format=raw"
        )
        assert resp.status_code == 200
        plane = np.frombuffer(resp.content, dtype="<f4").reshape(vol.shape[0], vol.shape[2])
        # plane[k][j] = vol[k][j][idx]
        assert plane[1, 5] == pytest.approx(vol[1, 5, idx])
        assert plane[3, 20] == pytest.approx(vol[3, 20, idx])


def test_mpr_coronal_matches_volume(client):
    study, series, _ = _ingest_series(client, n_slices=4)
    vol = _hu_volume(client, series)
    for idx in (0, 8, 16, 31):
        resp = client.get(
            f"/api/v1/volume/{series}/mpr/slice?plane=coronal&index={idx}&format=raw"
        )
        assert resp.status_code == 200
        plane = np.frombuffer(resp.content, dtype="<f4").reshape(vol.shape[0], vol.shape[1])
        # plane[k][i] = vol[k][idx][i]
        assert plane[2, 10] == pytest.approx(vol[2, idx, 10])
        assert plane[0, 25] == pytest.approx(vol[0, idx, 25])


def test_mpr_oblique_axial_matches_bilinear_crop(client):
    """An oblique axial plane through the volume centre reproduces the native
    axial slice (bilinear at half-integer positions)."""
    study, series, _ = _ingest_series(client, n_slices=4)
    info = client.get(f"/api/v1/volume/{series}/info").json()
    vol = _hu_volume(client, series)
    k = 2
    row_sp, col_sp, slice_sp = 1.0, 1.0, 5.0  # PixelSpacing unset in fixtures → 1
    normal = np.array([0.0, 0.0, 1.0])
    row_vec = np.array([1.0, 0.0, 0.0])
    col_vec = np.array([0.0, 1.0, 0.0])
    ipp = np.array(info["image_position_patient"])
    ny, nx = info["rows"], info["columns"]
    origin = ipp + k * slice_sp * normal + (ny - 1) / 2 * row_sp * row_vec + (nx - 1) / 2 * col_sp * col_vec
    m = 15
    resp = client.get(
        f"/api/v1/volume/{series}/mpr/slice?plane=oblique&format=raw&size={m}"
        f"&origin={origin[0]:.1f},{origin[1]:.1f},{origin[2]:.1f}"
        f"&row=0,1,0&col=1,0,0"
    )
    assert resp.status_code == 200, resp.text
    plane = np.frombuffer(resp.content, dtype="<f4").reshape(m, m)

    # Expected: bilinear at (j = v' + 8.5, i = u' + 8.5) of the axial slice.
    axis = vol[k].astype(np.float64)
    expected = np.zeros((m, m))
    for vv in range(m):
        for uu in range(m):
            j = vv + 8.5
            i = uu + 8.5
            j0, i0 = int(np.floor(j)), int(np.floor(i))
            fj, fi = j - j0, i - i0
            expected[vv, uu] = (
                axis[j0, i0] * (1 - fj) * (1 - fi) +
                axis[j0, i0 + 1] * (1 - fj) * fi +
                axis[j0 + 1, i0] * fj * (1 - fi) +
                axis[j0 + 1, i0 + 1] * fj * fi
            )
    np.testing.assert_allclose(plane, expected, atol=1e-3)
    # A point strictly inside the bright core (HU 476 + 100k) is exact.
    assert plane[8, 8] == pytest.approx(1500 + 100 * k - 1024)


def test_mpr_npy_format(client):
    study, series, _ = _ingest_series(client, n_slices=3)
    resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=0&format=npy")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-npy")
    arr = np.load(io.BytesIO(resp.content))
    assert arr.shape == (32, 32)
    assert arr.dtype == np.float32
    assert arr[16, 16] == pytest.approx(1500 - 1024)


def test_mpr_window_override(client):
    study, series, _ = _ingest_series(client, n_slices=2)
    resp = client.get(
        f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=0"
        "&window_center=500&window_width=2000"
    )
    assert resp.status_code == 200
    assert resp.headers["x-mpr-window"].startswith("500.0,2000.0")
    assert resp.content[:8] == PNG_MAGIC


def test_mpr_guards(client):
    assert client.get(f"/api/v1/volume/{generate_uid()}/mpr/slice").status_code == 404

    study, series, _ = _ingest_series(client, n_slices=3)

    # Missing / bad index
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial").status_code == 400
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=99").status_code == 416
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=-1").status_code == 416
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=sagittal&index=99").status_code == 416
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=coronal&index=99").status_code == 416

    # Oblique without a full plane definition
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=oblique&origin=0,0,0").status_code == 400
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=oblique&size=10").status_code == 400

    # Bad format / bad size
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?format=wav").status_code == 400
    resp = client.get(
        f"/api/v1/volume/{series}/mpr/slice?plane=oblique&size=9999x9999"
        "&origin=0,0,0&row=1,0,0&col=0,1,0"
    )
    assert resp.status_code == 400  # exceeds VOLUME_MPR_MAX_PIXELS

    # Disabled → 503
    from app.core.config import settings
    prev = settings.VOLUME_STREAMING_ENABLED
    settings.VOLUME_STREAMING_ENABLED = False
    try:
        assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=0").status_code == 503
    finally:
        settings.VOLUME_STREAMING_ENABLED = prev
