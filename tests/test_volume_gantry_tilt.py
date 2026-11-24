"""Tests for gantry-tilt detection, pure-NumPy deskewing, and derived-volume metadata."""

from __future__ import annotations

import math
from types import SimpleNamespace

import numpy as np
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset

from app.api.v1 import volume
from app.services import volume_service
from app.services.volume.assembly import iter_volume_bytes, stack_fingerprint, volume_byte_length
from app.services.volume.cache import _VOL_CACHE, load_volume_array
from app.services.volume.calibration import FrameCalibration
from app.services.volume.deskew import build_deskew_plan, deskew_slice_2d
from app.services.volume.geometry_validation import (
    GeometryErrorCode,
    GeometryTolerances,
    GeometryValidationError,
    assess_reformat_safety,
    validate_series_geometry,
)
from app.services.volume.mpr import reslice_native_plane_streamed, reslice_plane
from app.services.volume.slices import VolumeSlice, _header_from_metadata_json


def _tilted_header(
    index: int,
    *,
    tilt_deg: float = 15.0,
    table_step_mm: float = 2.0,
    rows: int = 32,
    cols: int = 32,
    pixel_spacing: tuple[float, float] = (1.0, 1.0),
    declare_tilt_tag: bool = True,
    extra_offset_xy: tuple[float, float] = (0.0, 0.0),
) -> Dataset:
    rad = math.radians(tilt_deg)
    ds = Dataset()
    ds.SOPInstanceUID = f"2.25.900.{index}"
    ds.SeriesInstanceUID = "2.25.900"
    ds.FrameOfReferenceUID = "2.25.901"
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]
    ds.ImageOrientationPatient = [1.0, 0.0, 0.0, 0.0, math.cos(rad), math.sin(rad)]
    # Table moves purely along patient Z while gantry is tilted around X.
    ds.ImagePositionPatient = [
        extra_offset_xy[0],
        extra_offset_xy[1],
        index * table_step_mm,
    ]
    ds.PixelSpacing = [pixel_spacing[0], pixel_spacing[1]]
    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.PixelRepresentation = 1
    ds.SamplesPerPixel = 1
    ds.RescaleSlope = 1.0
    ds.RescaleIntercept = 0.0
    if declare_tilt_tag:
        ds.GantryDetectorTilt = tilt_deg
    return ds


def _build_synthetic_tilted_stack(
    *,
    tilt_deg: float = 15.0,
    num_slices: int = 16,
    rows: int = 32,
    cols: int = 32,
    table_step_mm: float = 2.0,
    rod_mode: str = "normal",
):
    """Build a synthetic tilted CT stack with a bright landmark rod.

    - ``rod_mode == "normal"``: rod runs along the slice plane normal through
      patient point ``P0 = (16, 16*cos(tilt), 16*sin(tilt))``.
    - ``rod_mode == "patient_z"``: vertical rod along patient Z at ``(X=16, Y=16)``.
    """
    rad = math.radians(tilt_deg)
    row_dir = np.array([1.0, 0.0, 0.0], dtype=np.float64)
    col_dir = np.array([0.0, math.cos(rad), math.sin(rad)], dtype=np.float64)
    normal = np.cross(row_dir, col_dir)

    headers = [
        _tilted_header(
            k,
            tilt_deg=tilt_deg,
            table_step_mm=table_step_mm,
            rows=rows,
            cols=cols,
        )
        for k in range(num_slices)
    ]
    geometry = validate_series_geometry(headers)
    plan = build_deskew_plan(geometry, rows, cols, enabled=True)

    datasets: dict[str, SimpleNamespace] = {}
    slices: list[VolumeSlice] = []
    ii, jj = np.meshgrid(np.arange(cols, dtype=np.float64), np.arange(rows, dtype=np.float64))

    for k, sg in enumerate(geometry.slices):
        ipp = np.array(sg.image_position_patient, dtype=np.float64)
        # True patient coordinate of every pixel (j, i) on raw slice k:
        px = ipp[0] + ii * row_dir[0] + jj * col_dir[0]
        py = ipp[1] + ii * row_dir[1] + jj * col_dir[1]
        pz = ipp[2] + ii * row_dir[2] + jj * col_dir[2]

        if rod_mode == "normal":
            # Line P(t) = P0 + t * normal with P0 at (i=16, j=16) of slice 0.
            p0 = np.array(geometry.slices[0].image_position_patient) + 16.0 * row_dir + 16.0 * col_dir
            diff_x, diff_y, diff_z = px - p0[0], py - p0[1], pz - p0[2]
            proj = diff_x * normal[0] + diff_y * normal[1] + diff_z * normal[2]
            perp_sq = (
                (diff_x - proj * normal[0]) ** 2
                + (diff_y - proj * normal[1]) ** 2
                + (diff_z - proj * normal[2]) ** 2
            )
            arr = np.rint(1000.0 * np.exp(-perp_sq / (2.0 * 1.25**2))).astype(np.int16)
        else:
            # Vertical rod along patient Z at X=16.0, Y=16.0.
            dist_sq = (px - 16.0) ** 2 + (py - 16.0) ** 2
            arr = np.rint(1000.0 * np.exp(-dist_sq / (2.0 * 1.25**2))).astype(np.int16)

        path = f"mem://tilted/{rod_mode}/{k}.dcm"
        datasets[path] = SimpleNamespace(
            Rows=rows,
            Columns=cols,
            SamplesPerPixel=1,
            BitsAllocated=16,
            PixelRepresentation=1,
            RescaleSlope=1.0,
            RescaleIntercept=0.0,
            pixel_array=arr,
        )
        slices.append(
            VolumeSlice(
                sop_uid=f"2.25.900.{k}",
                file_path=path,
                backend=None,
                position=list(sg.image_position_patient),
                iop=list(sg.image_orientation_patient),
                z=sg.projected_position_mm,
                instance_number=k + 1,
                geometry=sg,
                series_geometry=geometry,
                calibration=FrameCalibration(
                    slope=1.0,
                    intercept=0.0,
                    valid=True,
                    padding_low=None,
                    padding_high=None,
                    rescale_type="HU",
                ),
            )
        )

    info = {
        "series_instance_uid": f"2.25.900.{rod_mode}",
        "study_instance_uid": "2.25.900.1",
        "rows": plan.out_rows,
        "columns": plan.out_cols,
        "source_rows": rows,
        "source_columns": cols,
        "slices": len(slices),
        "samples_per_pixel": 1,
        "dtype": "int16",
        "pixel_spacing": [1.0, 1.0],
        "slice_spacing": geometry.slice_spacing_mm,
        "image_position_patient": list(plan.orthogonal_ipp),
        "image_orientation_patient": list(geometry.slices[0].image_orientation_patient),
        "derived_resampled": plan.active,
        "gantry_tilt_corrected": plan.active,
        "gantry_tilt_deg": geometry.gantry_tilt_deg,
        "stack_fingerprint": stack_fingerprint(slices),
    }
    return geometry, plan, slices, info, datasets


@pytest.mark.parametrize("via_metadata_json", [False, True])
def test_validate_series_geometry_detects_linear_gantry_tilt(via_metadata_json):
    headers = [_tilted_header(k, tilt_deg=15.0) for k in range(10)]
    if via_metadata_json:
        headers = [_header_from_metadata_json(ds.to_json()) for ds in headers]

    geometry = validate_series_geometry(headers)
    assert geometry.has_in_plane_displacement is True
    assert geometry.has_linear_gantry_tilt is True
    assert geometry.gantry_tilt_deg == pytest.approx(15.0, abs=0.05)
    assert geometry.max_in_plane_displacement_mm == pytest.approx(
        9 * 2.0 * math.sin(math.radians(15.0)), abs=1e-3
    )

    report = assess_reformat_safety(headers, gantry_tilt_resample_enabled=True)
    assert report["reformat_safe"] is True
    assert report["requires_deskew"] is True
    assert report["gantry_tilt_deg"] == pytest.approx(15.0, abs=0.05)


def test_geometric_tilt_inferred_even_when_gantry_tag_absent():
    headers = [_tilted_header(k, tilt_deg=18.0, declare_tilt_tag=False) for k in range(8)]
    geometry = validate_series_geometry(headers)
    assert geometry.has_linear_gantry_tilt is True
    assert geometry.gantry_tilt_deg == pytest.approx(18.0, abs=0.05)


def test_non_linear_in_plane_displacement_fails_closed():
    # Offset along patient X (strictly in-plane: dot(X, normal) == 0) keeps projected
    # slice spacing uniform while introducing a non-linear in-plane jump on slice 4.
    headers = [
        _tilted_header(k, tilt_deg=15.0, extra_offset_xy=(4.0 if k == 4 else 0.0, 0.0))
        for k in range(8)
    ]
    geometry = validate_series_geometry(headers)
    assert geometry.has_in_plane_displacement is True
    assert geometry.has_linear_gantry_tilt is False

    with pytest.raises(GeometryValidationError) as exc:
        build_deskew_plan(geometry, 32, 32, enabled=True)
    assert exc.value.code == GeometryErrorCode.UNSUPPORTED_GANTRY_TILT

    report = assess_reformat_safety(headers, gantry_tilt_resample_enabled=True)
    assert report["reformat_safe"] is False
    assert report["code"] == GeometryErrorCode.UNSUPPORTED_GANTRY_TILT.value


def test_disabled_gantry_tilt_or_excessive_expansion_fails_closed():
    headers = [_tilted_header(k, tilt_deg=15.0) for k in range(10)]
    geometry = validate_series_geometry(headers)

    with pytest.raises(GeometryValidationError) as disabled_exc:
        build_deskew_plan(geometry, 32, 32, enabled=False)
    assert disabled_exc.value.code == GeometryErrorCode.UNSUPPORTED_GANTRY_TILT

    with pytest.raises(GeometryValidationError) as expand_exc:
        build_deskew_plan(geometry, 4, 4, enabled=True, max_expansion_factor=1.1)
    assert expand_exc.value.code == GeometryErrorCode.UNSUPPORTED_GANTRY_TILT


def test_deskew_restores_undistorted_sagittal_and_patient_oblique(monkeypatch):
    _VOL_CACHE.clear()
    _geo, plan, slices, info, datasets = _build_synthetic_tilted_stack(
        tilt_deg=15.0, num_slices=16, rows=32, cols=32, table_step_mm=2.0, rod_mode="normal"
    )
    monkeypatch.setattr(
        volume_service.storage_service,
        "read_dataset",
        lambda path, stop_before_pixels=False, backend_name=None: datasets[path],
    )

    assert plan.active is True
    assert plan.out_rows > 32
    assert plan.out_cols == 32

    # 1. Native sagittal slice through i=16: rod along normal must sit at constant j across all k.
    sag = reslice_plane(slices, info, "sagittal", index=16)
    peak_j_per_slice = [int(np.nanargmax(sag.data[k])) for k in range(sag.rows)]
    expected_j = 16 - plan.origin_offset_j_px
    assert all(j == expected_j for j in peak_j_per_slice), (
        f"Deskewed sagittal rod drifted across slices: {peak_j_per_slice} != {expected_j}"
    )

    # 2. Streamed single-slice fallback must produce the exact same undistorted sagittal plane.
    sag_streamed = reslice_native_plane_streamed(
        slices, info, "sagittal", 16, max_pixels=1_000_000
    )
    np.testing.assert_allclose(sag_streamed.data, sag.data, atol=1e-4, equal_nan=True)

    # 3. OOB corners introduced by expanding the regular grid remain NaN (no fabricated HU).
    vol = load_volume_array(slices, series_uid=info["series_instance_uid"])
    assert np.isnan(vol[0, -1, 16])
    assert np.isnan(vol[-1, 0, 16])

    # 4. True vertical patient-Z rod sampled on a patient-aligned oblique sagittal plane
    #    (row=[0,1,0], col=[0,0,1]) must appear strictly vertical at Y=16.0 mm.
    _VOL_CACHE.clear()
    _geo_z, _plan_z, slices_z, info_z, datasets_z = _build_synthetic_tilted_stack(
        tilt_deg=15.0, num_slices=20, rows=48, cols=32, table_step_mm=2.0, rod_mode="patient_z"
    )
    monkeypatch.setattr(
        volume_service.storage_service,
        "read_dataset",
        lambda path, stop_before_pixels=False, backend_name=None: datasets_z[path],
    )
    oblique = reslice_plane(
        slices_z,
        info_z,
        "oblique",
        origin=[16.0, 16.0, 18.0],
        row_vector=[0.0, 1.0, 0.0],
        col_vector=[0.0, 0.0, 1.0],
        size=(21, 15),
    )
    # Center column u=10 corresponds to Y=16.0 mm across every Z row v.
    finite_rows = [v for v in range(oblique.rows) if np.any(np.isfinite(oblique.data[v]))]
    assert len(finite_rows) >= 10
    for v in finite_rows:
        peak_u = int(np.nanargmax(oblique.data[v]))
        assert abs(peak_u - 10) <= 1, f"Vertical Z rod sheared at row v={v}: peak_u={peak_u}"


def test_iter_volume_bytes_matches_deskewed_byte_length(monkeypatch):
    _geo, plan, slices, info, datasets = _build_synthetic_tilted_stack(
        tilt_deg=15.0, num_slices=8, rows=24, cols=20, table_step_mm=2.0
    )
    monkeypatch.setattr(
        volume_service.storage_service,
        "read_dataset",
        lambda path, stop_before_pixels=False, backend_name=None: datasets[path],
    )
    for rescale in (False, True):
        for downsample in (1, 2):
            payload = b"".join(
                iter_volume_bytes(slices, rescale=rescale, start=1, end=7, downsample=downsample)
            )
            dtype = "float32" if rescale else info["dtype"]
            expected = volume_byte_length(
                slices, dtype, plan.out_rows, plan.out_cols, 1,
                start=1, end=7, downsample=downsample,
            )
            assert len(payload) == expected


def test_deskew_slice_preserves_nan_padding():
    headers = [_tilted_header(k, tilt_deg=15.0, rows=8, cols=8) for k in range(4)]
    geometry = validate_series_geometry(headers)
    plan = build_deskew_plan(geometry, 8, 8, enabled=True)
    src = np.full((8, 8), 100.0, dtype=np.float32)
    src[:, 0] = np.nan
    out = deskew_slice_2d(src, plan, 1)
    assert out.shape == (plan.out_rows, plan.out_cols)
    # Column 0 was NaN across the whole source slice and shift is pure j-axis (shift_i == 0).
    assert np.all(np.isnan(out[:, 0]))
    assert np.nanmax(out) == pytest.approx(100.0, abs=1e-4)


def test_volume_api_emits_derived_resampled_headers(monkeypatch):
    _VOL_CACHE.clear()
    _geo, _plan, slices, info, datasets = _build_synthetic_tilted_stack(
        tilt_deg=15.0, num_slices=6, rows=16, cols=16, table_step_mm=2.0
    )
    app = FastAPI()
    app.include_router(volume.router)
    app.dependency_overrides[volume.get_db] = lambda: None
    app.dependency_overrides[volume.require_series_read] = lambda: None
    monkeypatch.setattr(volume.settings, "VIEWER_PROCESSING_MODE", "hybrid")
    monkeypatch.setattr(volume.settings, "VOLUME_STREAMING_ENABLED", True)

    async def authorized(*_args, **_kwargs):
        return None

    async def fake_bundle(*_args, **_kwargs):
        return info, slices

    async def fake_info(*_args, **_kwargs):
        return info

    monkeypatch.setattr(volume, "require_series_access", authorized)
    monkeypatch.setattr(volume.volume_service, "volume_bundle", fake_bundle)
    monkeypatch.setattr(volume.volume_service, "volume_info", fake_info)
    monkeypatch.setattr(
        volume_service.storage_service,
        "read_dataset",
        lambda path, stop_before_pixels=False, backend_name=None: datasets[path],
    )

    with TestClient(app) as client:
        info_resp = client.get(f"/api/v1/volume/{info['series_instance_uid']}/info")
        assert info_resp.status_code == 200
        body = info_resp.json()
        assert body["derived_resampled"] is True
        assert body["gantry_tilt_corrected"] is True
        assert body["gantry_tilt_deg"] == pytest.approx(15.0, abs=0.1)
        assert body["source_rows"] == 16
        assert body["rows"] > 16

        slice_resp = client.get(
            f"/api/v1/volume/{info['series_instance_uid']}/mpr/slice?plane=sagittal&index=8&format=png"
        )
        assert slice_resp.status_code == 200
        assert slice_resp.headers["x-mpr-derived-resampled"] == "true"
        assert slice_resp.headers["x-mpr-gantry-tilt-corrected"] == "true"
        assert "X-MPR-Derived-Resampled" in slice_resp.headers["access-control-expose-headers"]
