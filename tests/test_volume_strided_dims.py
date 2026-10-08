"""H13 — strided-dimension contract for ``GET /api/v1/volume/{series}/data``.

The volume streaming route has three geometry statements that must agree:

1. the ``X-Volume-Rows`` / ``X-Volume-Cols`` / ``X-Volume-Slices`` headers,
2. the ``.npy`` header shape,
3. the bytes actually produced by the sampler.

The sampler strides with numpy slicing (``arr[::step, ::step]`` and
``slices[start:end:step]``), which yields ``ceil(n / step)`` samples per axis —
never ``floor(n / step)``.  ``volume_service.strided_dim`` is the single
source of truth for that arithmetic.

Regression guard for ``plan_9_2026.md`` H13: the route previously declared
``rows // downsample`` (floor) while the sampler produced ``ceil`` samples, so
odd dimensions returned wrong row/col headers, a wrong NPY header shape and a
``Content-Length`` that did not match the body.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.services import volume_service
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]

# Odd / prime dimensions: the only shapes where floor(n/step) != ceil(n/step).
ODD_SHAPES = [(5, 7), (7, 5), (3, 5), (9, 11)]
STEPS = [1, 2, 3, 4, 5]


def _ingest_stack(
    client,
    *,
    rows: int,
    cols: int,
    n_slices: int = 4,
    z_step: float = 5.0,
    row_spacing: float = 1.0,
    col_spacing: float = 1.0,
    patient_id: str = "STRIDE_PAT",
):
    """STOW a synthetic axial stack with deterministic per-voxel values.

    Returns ``(series_uid, volume)`` where ``volume`` is the float32 source
    array shaped ``(n_slices, rows, cols)`` in the same order the server
    stacks and streams it.
    """
    study_uid = generate_uid()
    series_uid = generate_uid()
    datasets = []
    volume = np.zeros((n_slices, rows, cols), dtype=np.float32)
    for k in range(n_slices):
        # value = 1000*k + 10*row + col -> every voxel is uniquely identifiable
        pixel = (1000 * k + 10 * np.arange(rows)[:, None] + np.arange(cols)[None, :]).astype(np.uint16)
        volume[k] = pixel.astype(np.float32)
        ds = make_instance(
            patient_id=patient_id,
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=generate_uid(),
            instance_num=k + 1,
            rows=rows,
            cols=cols,
            pixel=pixel,
            position=[10.0, 20.0, k * z_step],
            orientation=list(AXIAL_ORIENTATION),
            rescale_slope="1",
            rescale_intercept="0",
            slice_thickness=str(z_step),
        )
        ds.PixelSpacing = [str(row_spacing), str(col_spacing)]
        datasets.append(ds_bytes(ds))

    resp = client.post(
        f"/dicom-web/studies/{study_uid}",
        content=build_stow_body(datasets),
        headers=stow_headers(),
    )
    assert resp.status_code == 200
    return series_uid, volume


def _expected_strided(volume: np.ndarray, step: int) -> np.ndarray:
    """The exact array the sampler must produce for ``downsample=step``."""
    return volume[::step, ::step, ::step]


def _npy_shape_and_array(body: bytes) -> tuple[tuple[int, ...], np.ndarray]:
    """Decode a ``.npy`` payload exactly as a client would (np.load)."""
    arr = np.load(io.BytesIO(body))
    return tuple(arr.shape), arr


# --------------------------------------------------------------------------- #
# The helper itself
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("n", list(range(1, 41)))
@pytest.mark.parametrize("step", list(range(1, 9)))
def test_strided_dim_matches_numpy_slicing(n: int, step: int):
    """H13: ``strided_dim`` must equal the sample count of ``range(n)[::step]``."""
    expected = len(np.arange(n)[::step])
    assert volume_service.strided_dim(n, step) == expected


def test_strided_dim_edge_cases():
    """H13: the helper mirrors slicing exactly — empty input yields 0 samples."""
    assert volume_service.strided_dim(0, 3) == 0
    assert volume_service.strided_dim(-4, 3) == 0
    # step < 1 is an un-downsampled read, never a ZeroDivisionError.
    assert volume_service.strided_dim(5, 0) == 5
    assert volume_service.strided_dim(5, -2) == 5


# --------------------------------------------------------------------------- #
# The endpoint contract
# --------------------------------------------------------------------------- #
def test_5x7_step2_returns_3x4(client):
    """H13: 5x7 at step 2 samples 3x4 (ceil), not 2x3 (floor)."""
    series, volume = _ingest_stack(client, rows=5, cols=7, n_slices=4)

    resp = client.get(f"/api/v1/volume/{series}/data?downsample=2")
    assert resp.status_code == 200

    expected = _expected_strided(volume, 2)
    nz_e, ny_e, nx_e = expected.shape
    assert (ny_e, nx_e) == (3, 4), "sanity: numpy striding on 5x7 at step 2"

    assert resp.headers["x-volume-rows"] == str(ny_e)
    assert resp.headers["x-volume-cols"] == str(nx_e)
    assert resp.headers["x-volume-slices"] == str(nz_e)
    assert int(resp.headers["content-length"]) == len(resp.content)

    actual = np.frombuffer(resp.content, dtype="<u2").reshape(nz_e, ny_e, nx_e)
    assert np.array_equal(actual.astype(np.float32), expected)


def test_content_length_matches_shape_all_formats(client):
    """H13: Content-Length must equal the body for raw, rescaled and .npy output."""
    series, volume = _ingest_stack(client, rows=5, cols=7, n_slices=5)
    expected = _expected_strided(volume, 2)
    nz_e, ny_e, nx_e = expected.shape

    for query in (
        "?downsample=2",
        "?downsample=2&rescale=1",
        "?downsample=2&format=npy",
        "?downsample=2&format=npy&rescale=1",
    ):
        resp = client.get(f"/api/v1/volume/{series}/data{query}")
        assert resp.status_code == 200, query
        assert int(resp.headers["content-length"]) == len(resp.content), query

        if "format=npy" in query:
            shape, arr = _npy_shape_and_array(resp.content)
            assert shape == (nz_e, ny_e, nx_e), query
            assert arr.shape == (nz_e, ny_e, nx_e), query
        else:
            dtype = "<f4" if "rescale=1" in query else "<u2"
            arr = np.frombuffer(resp.content, dtype=dtype)
            assert arr.size == nz_e * ny_e * nx_e, query


def test_npy_header_shape_matches_sampled_array(client):
    """H13: the declared NPY shape must equal the actual sampled array shape."""
    series, volume = _ingest_stack(client, rows=5, cols=7, n_slices=5)

    for query in ("?format=npy", "?format=npy&rescale=1", "?format=npy&downsample=3"):
        resp = client.get(f"/api/v1/volume/{series}/data{query}")
        assert resp.status_code == 200, query
        shape, arr = _npy_shape_and_array(resp.content)
        assert shape == arr.shape, query

        step = 3 if "downsample=3" in query else 1
        expected = _expected_strided(volume, step)
        assert shape == expected.shape, query


@pytest.mark.parametrize(("rows", "cols"), ODD_SHAPES)
@pytest.mark.parametrize("step", STEPS)
def test_every_step_on_odd_dimensions(client, rows: int, cols: int, step: int):
    """H13: headers, body length and voxel data agree for every step 1..5."""
    series, volume = _ingest_stack(client, rows=rows, cols=cols, n_slices=5)
    resp = client.get(f"/api/v1/volume/{series}/data?downsample={step}")
    assert resp.status_code == 200

    expected = _expected_strided(volume, step)
    nz_e, ny_e, nx_e = expected.shape

    assert resp.headers["x-volume-rows"] == str(ny_e)
    assert resp.headers["x-volume-cols"] == str(nx_e)
    assert resp.headers["x-volume-slices"] == str(nz_e)
    assert int(resp.headers["content-length"]) == len(resp.content)

    actual = np.frombuffer(resp.content, dtype="<u2").reshape(nz_e, ny_e, nx_e)
    assert np.array_equal(actual.astype(np.float32), expected)


def test_slice_subrange_and_downsample_agree(client):
    """H13: ``out_count`` (slice selection) must match the sampled stack too."""
    series, volume = _ingest_stack(client, rows=5, cols=7, n_slices=7)
    resp = client.get(f"/api/v1/volume/{series}/data?slices=1-5&downsample=2")
    assert resp.status_code == 200

    expected = volume[1:6][::2][:, ::2, ::2]
    nz_e, ny_e, nx_e = expected.shape
    assert resp.headers["x-volume-slices"] == str(nz_e)
    assert resp.headers["x-volume-rows"] == str(ny_e)
    assert resp.headers["x-volume-cols"] == str(nx_e)
    assert int(resp.headers["content-length"]) == len(resp.content)

    actual = np.frombuffer(resp.content, dtype="<u2").reshape(nz_e, ny_e, nx_e)
    assert np.array_equal(actual.astype(np.float32), expected)


def test_byte_length_helper_matches_streamed_body(client):
    """H13: ``volume_byte_length`` must equal the bytes the sampler yields."""
    series, volume = _ingest_stack(client, rows=5, cols=7, n_slices=5)
    resp = client.get(f"/api/v1/volume/{series}/data?downsample=2")
    assert resp.status_code == 200

    bundle = client.get(f"/api/v1/volume/{series}/info").json()
    declared = volume_service.volume_byte_length(
        [object()] * bundle["slices"], bundle["dtype"],
        bundle["rows"], bundle["columns"], bundle["samples_per_pixel"],
        start=0, end=bundle["slices"], downsample=2,
    )
    assert declared == len(resp.content)


# --------------------------------------------------------------------------- #
# C03: the volume streaming / MIP / MPR endpoints require authentication.
# Override the anonymous ``client`` for this module so its requests carry a
# valid ADMIN bearer token. (No test here asserts anonymous behaviour.)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client
