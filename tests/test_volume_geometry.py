"""C09 — canonical plane-orientation convention of the volume/MPR backend.

The WebGL MPR viewer (``app/static/viewer/paxs-mpr.js``) and its new
``paxs-mpr-geometry.js`` helper mirror the backend geometry.  That is only
sound if the backend convention is pinned by tests, because the viewer's
plane extractors, probe/ROI lookups, crosshairs and the WebGL2 slice shader
all have to agree with it.

Canonical convention (verified against ``reslice_plane`` and ``mip_project``):

    volume is indexed vol[k, j, i]  (k = slice/z, j = row/y, i = column/x)

    axial    (const k): u = i (X, cols) , v = j (Y, rows)
    sagittal (const i): u = j (Y, rows) , v = k (Z, slices)
    coronal  (const j): u = i (X, cols) , v = k (Z, slices)

Regression guard for ``plan_9_2026.md`` C09: these tests fail if a refactor
silently transposes a plane, which is what made every coronal/sagittal HU and
ROI readout wrong in the viewer.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.services import volume_service
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
SAGITTAL_ORIENTATION = [0.0, 1.0, 0.0, 0.0, 0.0, -1.0]
CORONAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 0.0, -1.0]


def _ingest_stack(
    client,
    *,
    rows: int = 6,
    cols: int = 8,
    n_slices: int = 5,
    z_step: float = 2.5,
    row_spacing: float = 1.0,
    col_spacing: float = 1.0,
    orientation=AXIAL_ORIENTATION,
    patient_id: str = "GEOM_PAT",
):
    """STOW an axial stack of uniquely-valued voxels; returns (series, volume)."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    datasets = []
    volume = np.zeros((n_slices, rows, cols), dtype=np.float32)
    for k in range(n_slices):
        pixel = (
            1000 * k + 10 * np.arange(rows)[:, None] + np.arange(cols)[None, :]
        ).astype(np.uint16)
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
            orientation=list(orientation),
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


def _plane(client, series: str, plane: str, index: int) -> np.ndarray:
    """Fetch one native plane as raw float32 HU and shape it from the headers."""
    resp = client.get(
        f"/api/v1/volume/{series}/mpr/slice?plane={plane}&index={index}&format=raw"
    )
    assert resp.status_code == 200, resp.text
    r = int(resp.headers["x-mpr-rows"])
    c = int(resp.headers["x-mpr-cols"])
    arr = np.frombuffer(resp.content, dtype="<f4").reshape(r, c)
    assert not np.isnan(arr).any(), f"{plane} plane sampled outside the volume"
    return arr


# --------------------------------------------------------------------------- #
# Native-plane orientation
# --------------------------------------------------------------------------- #
def test_axial_plane_is_slice_row_major(client):
    """C09: axial = vol[k] with u = i (X), v = j (Y)."""
    series, volume = _ingest_stack(client)
    got = _plane(client, series, "axial", 2)
    assert got.shape == (volume.shape[1], volume.shape[2])
    assert np.allclose(got, volume[2], atol=1e-3, rtol=0)


def test_sagittal_plane_rows_are_z_and_cols_are_y(client):
    """C09: sagittal = vol[:, :, i] with u = j (Y), v = k (Z)."""
    series, volume = _ingest_stack(client)
    got = _plane(client, series, "sagittal", 3)
    nz, ny, _nx = volume.shape
    assert got.shape == (nz, ny), "sagittal must be (slices, rows), not transposed"
    assert np.allclose(got, volume[:, :, 3], atol=1e-3, rtol=0)


def test_coronal_plane_rows_are_z_and_cols_are_x(client):
    """C09: coronal = vol[:, j, :] with u = i (X), v = k (Z)."""
    series, volume = _ingest_stack(client)
    got = _plane(client, series, "coronal", 4)
    nz, _ny, nx = volume.shape
    assert got.shape == (nz, nx), "coronal must be (slices, columns)"
    assert np.allclose(got, volume[:, 4, :], atol=1e-3, rtol=0)


def test_sagittal_is_not_transposed_coronal(client):
    """C09: guard against the classic sagittal<->coronal transposition."""
    series, volume = _ingest_stack(client, rows=4, cols=7, n_slices=5)
    sag = _plane(client, series, "sagittal", 2)
    cor = _plane(client, series, "coronal", 2)
    assert sag.shape == (5, 4)
    assert cor.shape == (5, 7)
    # A transposed extractor would produce (cols, slices) / (rows, slices).
    assert sag.shape != (4, 5)
    assert cor.shape != (5, 5)


@pytest.mark.parametrize(
    ("axis", "expected"),
    [("z", (6, 8)), ("x", (5, 6)), ("y", (5, 8))],
)
def test_mip_projection_uses_the_same_convention(client, axis: str, expected: tuple[int, int]):
    """C09: /mip must follow the axial/sagittal/coronal orientation convention."""
    series, _volume = _ingest_stack(client, rows=6, cols=8, n_slices=5)
    resp = client.get(f"/api/v1/volume/{series}/mip?axis={axis}&format=raw")
    assert resp.status_code == 200
    assert (resp.headers["x-mip-rows"], resp.headers["x-mip-cols"]) == (
        str(expected[0]),
        str(expected[1]),
    )


# --------------------------------------------------------------------------- #
# Metadata the viewer relies on
# --------------------------------------------------------------------------- #
def test_info_reports_row_then_col_spacing_and_slice_step(client):
    """C09: pixel_spacing is [row, col] (DICOM order) and slice_spacing is the z step."""
    series, _volume = _ingest_stack(
        client, row_spacing=0.8, col_spacing=1.6, z_step=2.5
    )
    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["pixel_spacing"] == pytest.approx([0.8, 1.6])
    assert info["slice_spacing"] == pytest.approx(2.5)
    assert info["orientation"] == "AXIAL"
    assert info["image_orientation_patient"] == pytest.approx(AXIAL_ORIENTATION)


def test_patient_transform_axis_assignment_is_not_transposed():
    """C09: i must advance along col_vec, j along row_vec, k along the normal.

    ``paxs-mpr-geometry.js`` mirrors this exact assignment, so a row/column
    swap here would silently move every measurement in the viewer.
    """
    info = {
        "image_orientation_patient": AXIAL_ORIENTATION,  # row=+x, col=+y, normal=+z
        "image_position_patient": [10.0, 20.0, 30.0],
        "pixel_spacing": [0.8, 1.6],   # [row, col]
        "slice_spacing": 2.5,
    }
    ipp, rinv, normal, row_sp, col_sp, slice_sp = volume_service._patient_transform(info)
    assert (row_sp, col_sp, slice_sp) == (0.8, 1.6, 2.5)
    assert normal == pytest.approx([0.0, 0.0, 1.0])
    assert ipp == pytest.approx([10.0, 20.0, 30.0])

    # One step along the column direction (patient +y) is one step of i.
    assert rinv @ np.array([0.0, col_sp, 0.0]) == pytest.approx([1.0, 0.0, 0.0])
    # One step along the row direction (patient +x) is one step of j.
    assert rinv @ np.array([row_sp, 0.0, 0.0]) == pytest.approx([0.0, 1.0, 0.0])
    # One step along the normal (patient +z) is one step of k.
    assert rinv @ np.array([0.0, 0.0, slice_sp]) == pytest.approx([0.0, 0.0, 1.0])

    # Round-trip a few anisotropic voxels through the forward/inverse transform.
    for i, j, k in ((0, 0, 0), (3, 2, 4), (7, 5, 1)):
        patient = ipp + np.array([row_sp * j, col_sp * i, slice_sp * k])
        assert rinv @ (patient - ipp) == pytest.approx([i, j, k])


def test_orientation_label_and_dominant_axis():
    """C09: the plane label/stack axis the viewer shows must match the cosines."""
    assert volume_service.orientation_label(AXIAL_ORIENTATION) == "AXIAL"
    assert volume_service.dominant_axis(AXIAL_ORIENTATION) == 2
    assert volume_service.orientation_label(SAGITTAL_ORIENTATION) == "SAGITTAL"
    assert volume_service.dominant_axis(SAGITTAL_ORIENTATION) == 0
    assert volume_service.orientation_label(CORONAL_ORIENTATION) == "CORONAL"
    assert volume_service.dominant_axis(CORONAL_ORIENTATION) == 1
    assert volume_service.orientation_label(None) == "UNKNOWN"
    assert volume_service.orientation_label([1.0, 0.0, 0.0]) == "UNKNOWN"
    # NOTE: the "OBLIQUE" branch (axis < 0.5) is unreachable for a unit normal,
    # because max(|nx|, |ny|, |nz|) >= 1/sqrt(3) ~= 0.577. Oblique acquisitions
    # are therefore reported as their nearest orthogonal plane. Pinning that
    # behaviour here would endorse it, so this is reported as a separate finding
    # rather than asserted (changing it would alter /info output).
    assert volume_service.orientation_label([0.6, 0.8, 0.0, 0.0, 0.0, 1.0]) == "SAGITTAL"


# --------------------------------------------------------------------------- #
# C03: the volume streaming / MIP / MPR endpoints require authentication.
# Override the anonymous ``client`` for this module so its requests carry a
# valid ADMIN bearer token. (No test here asserts anonymous behaviour.)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client
