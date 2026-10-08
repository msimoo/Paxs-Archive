"""Tests for roadmap #26 — 2D MPR backend prep (volume streaming).

Covers the volume geometry metadata endpoint (/info), the raw voxel streaming
endpoint (/data) including sub-range, server-side rescale and .npy output,
slice z-ordering, and the 404 / 503 / bounds guards.
"""

from __future__ import annotations

import io

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.services import volume_service
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

AXIAL_ORIENTATION = [1.0, 0.0, 0.0, 0.0, 1.0, 0.0]
SAGITTAL_ORIENTATION = [0.0, 1.0, 0.0, 0.0, 0.0, -1.0]


def _ingest_series(client, n_slices: int = 4, z0: float = 0.0, step: float = 5.0,
                   orientation=AXIAL_ORIENTATION, values=None,
                   patient_id: str = "VOL_PAT", study_uid=None, series_uid=None):
    """STOW a synthetic single-frame CT stack; returns (study, series, sops)."""
    study_uid = study_uid or generate_uid()
    series_uid = series_uid or generate_uid()
    sops: list[str] = []
    datasets = []
    for i in range(n_slices):
        z = z0 + i * step
        pixel = np.full((32, 32), 100 + i * 10, dtype=np.uint16)  # distinct per slice
        pixel[8:24, 8:24] = 1500 + i * 100  # bright core
        ds = make_instance(
            patient_id=patient_id,
            study_uid=study_uid,
            series_uid=series_uid,
            sop_uid=generate_uid(),
            instance_num=i + 1,
            rows=32,
            cols=32,
            pixel=pixel,
            position=[10.0, 20.0, z],
            orientation=list(orientation),
            rescale_slope="1",
            rescale_intercept="-1024",
            slice_thickness="5",
        )
        sops.append(ds.SOPInstanceUID)
        datasets.append(ds_bytes(ds))
    resp = client.post(
        f"/dicom-web/studies/{study_uid}", content=build_stow_body(datasets),
        headers=stow_headers(),
    )
    assert resp.status_code == 200
    return study_uid, series_uid, sops


def test_info_metadata_and_z_ordering(client):
    study, series, sops = _ingest_series(client)
    resp = client.get(f"/api/v1/volume/{series}/info")
    assert resp.status_code == 200
    info = resp.json()

    assert info["series_instance_uid"] == series
    assert info["study_instance_uid"] == study
    assert info["modality"] == "CT"
    assert info["rows"] == 32
    assert info["columns"] == 32
    assert info["slices"] == 4
    assert info["dtype"] == "uint16"
    assert info["itemsize"] == 2
    assert info["bits_allocated"] == 16
    assert info["pixel_representation"] == 0
    assert info["orientation"] == "AXIAL"
    assert info["pixel_spacing"] is None  # make_instance leaves PixelSpacing unset
    assert info["slice_spacing"] == pytest.approx(5.0)
    assert info["slice_thickness"] == 5.0
    assert info["rescale_slope"] == 1.0
    assert info["rescale_intercept"] == -1024.0
    assert info["image_position_patient"] == [10.0, 20.0, 0.0]
    assert info["image_orientation_patient"] == AXIAL_ORIENTATION
    assert info["isotropic"] is False  # pixel spacing absent → not isotropic
    # z-order matches STOW order here (z0..z0+3*step)
    assert info["instance_order"] == sops
    assert info["multi_frame"] is False


def test_slices_sorted_by_position_not_instance_number(client):
    """Instances ingested out of z-order are re-sorted by ImagePositionPatient."""
    study_uid = generate_uid()
    series_uid = generate_uid()
    # z positions deliberately scrambled vs. instance numbers 1..3.
    positions = [10.0, 0.0, 5.0]
    sops_by_inst: dict[int, str] = {}
    datasets = []
    for i, z in enumerate(positions):
        pixel = np.full((16, 16), 100 + i, dtype=np.uint16)
        ds = make_instance(
            patient_id="VOL_SORT", study_uid=study_uid, series_uid=series_uid,
            sop_uid=generate_uid(), instance_num=i + 1, rows=16, cols=16,
            pixel=pixel, position=[1.0, 2.0, z],
        )
        sops_by_inst[i + 1] = ds.SOPInstanceUID
        datasets.append(ds_bytes(ds))
    resp = client.post(
        f"/dicom-web/studies/{study_uid}", content=build_stow_body(datasets),
        headers=stow_headers(),
    )
    assert resp.status_code == 200

    info = client.get(f"/api/v1/volume/{series_uid}/info").json()
    # instance_order = [z=0 (instance 2), z=5 (instance 3), z=10 (instance 1)]
    assert info["instance_order"] == [sops_by_inst[2], sops_by_inst[3], sops_by_inst[1]]

    # /data reflects the same ordering: first emitted slice = the z=0 instance.
    data = client.get(f"/api/v1/volume/{series_uid}/data").content
    vol = np.frombuffer(data, dtype="<u2").reshape(3, 16, 16)
    assert vol[0, 0, 0] == 101  # instance 2 (z=0) carries 100 + 1
    assert vol[2, 0, 0] == 100  # instance 1 (z=10) carries 100 + 0


def _sop_uids(client, series_uid: str) -> list[str]:
    resp = client.get(f"/dicom-web/studies/__/series/{series_uid}/instances")
    return [i["00080018"]["Value"][0] for i in resp.json()]


def test_data_streams_raw_little_endian(client):
    study, series, sops = _ingest_series(client, n_slices=4)
    resp = client.get(f"/api/v1/volume/{series}/data")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/octet-stream")
    # 4 slices x 32x32 uint16
    assert resp.headers["content-length"] == str(4 * 32 * 32 * 2)
    assert resp.headers["x-volume-slices"] == "4"
    assert resp.headers["x-volume-dtype"] == "uint16"

    vol = np.frombuffer(resp.content, dtype="<u2").reshape(4, 32, 32)
    # Slice i carries base 100 + 10*i and a bright core 1500 + 100*i.
    for i in range(4):
        assert vol[i, 0, 0] == 100 + 10 * i
        assert vol[i, 16, 16] == 1500 + 100 * i


def test_data_rescale_to_hu(client):
    study, series, _ = _ingest_series(client, n_slices=2)
    resp = client.get(f"/api/v1/volume/{series}/data?rescale=1")
    assert resp.status_code == 200
    assert resp.headers["x-volume-dtype"] == "float32"
    assert resp.headers["content-length"] == str(2 * 32 * 32 * 4)
    vol = np.frombuffer(resp.content, dtype="<f4").reshape(2, 32, 32)
    # stored * 1 + (-1024)
    assert vol[0, 0, 0] == pytest.approx(100 - 1024)
    assert vol[1, 16, 16] == pytest.approx(1600 - 1024)


def test_data_slice_range(client):
    study, series, _ = _ingest_series(client, n_slices=6)
    resp = client.get(f"/api/v1/volume/{series}/data?slices=1-3")
    assert resp.status_code == 200
    assert resp.headers["content-length"] == str(3 * 32 * 32 * 2)
    vol = np.frombuffer(resp.content, dtype="<u2").reshape(3, 32, 32)
    assert vol[0, 0, 0] == 110  # slice 1 (0-indexed) → 100 + 10
    assert vol[2, 0, 0] == 130


def test_data_npy_format(client):
    study, series, _ = _ingest_series(client, n_slices=3)
    resp = client.get(f"/api/v1/volume/{series}/data?format=npy")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/x-npy")
    assert resp.content[:6] == b"\x93NUMPY"
    arr = np.load(io.BytesIO(resp.content))
    assert arr.shape == (3, 32, 32)
    assert arr.dtype == np.uint16
    assert arr[1, 16, 16] == 1600

    # npy + rescale → float32 volume
    resp2 = client.get(f"/api/v1/volume/{series}/data?format=npy&rescale=1")
    arr2 = np.load(io.BytesIO(resp2.content))
    assert arr2.dtype == np.float32
    assert arr2[0, 0, 0] == pytest.approx(100 - 1024)


def test_sagittal_orientation_and_spacing(client):
    """Orientation label + slice spacing follow the dominant (x) axis."""
    study, series, _ = _ingest_series(
        client, n_slices=3, z0=0.0, step=5.0,
        orientation=SAGITTAL_ORIENTATION,
    )
    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["orientation"] == "SAGITTAL"
    assert info["slice_spacing"] == pytest.approx(5.0)
    assert info["image_orientation_patient"] == SAGITTAL_ORIENTATION


def test_guards(client):
    # Unknown series → 404
    assert client.get(f"/api/v1/volume/{generate_uid()}/info").status_code == 404
    assert client.get(f"/api/v1/volume/{generate_uid()}/data").status_code == 404

    study, series, _ = _ingest_series(client, n_slices=3)

    # Bounds
    assert client.get(f"/api/v1/volume/{series}/data?slices=9-12").status_code == 416
    assert client.get(f"/api/v1/volume/{series}/data?slices=2-1").status_code == 416
    # Bad format
    assert client.get(f"/api/v1/volume/{series}/data?format=wav").status_code == 400
    # Bad range syntax
    assert client.get(f"/api/v1/volume/{series}/data?slices=abc").status_code == 400

    # Disabled → 503 on both endpoints
    from app.core.config import settings
    prev = settings.VOLUME_STREAMING_ENABLED
    settings.VOLUME_STREAMING_ENABLED = False
    try:
        assert client.get(f"/api/v1/volume/{series}/info").status_code == 503
        assert client.get(f"/api/v1/volume/{series}/data").status_code == 503
    finally:
        settings.VOLUME_STREAMING_ENABLED = prev


def test_max_slices_cap(client):
    from app.core.config import settings
    prev = settings.VOLUME_MAX_SLICES
    settings.VOLUME_MAX_SLICES = 2
    try:
        study, series, _ = _ingest_series(client, n_slices=4)
        resp = client.get(f"/api/v1/volume/{series}/data")
        assert resp.status_code == 416
        # A small range still works
        assert client.get(f"/api/v1/volume/{series}/data?slices=0-1").status_code == 200
    finally:
        settings.VOLUME_MAX_SLICES = prev


def test_non_volumetric_series_skipped(client):
    """A presentation-state / SR series has no readable volume → 404."""
    from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    study = generate_uid()
    series = generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.11.1"  # GSPS PR
    sop = generate_uid()
    file_meta.MediaStorageSOPInstanceUID = sop
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("pr.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.11.1"
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = series
    ds.PatientID = "PR_PAT"
    ds.Modality = "PR"
    ds.ReferencedSeriesSequence = [Dataset()]
    ds.ReferencedSeriesSequence[0].SeriesInstanceUID = series

    resp = client.post(
        f"/dicom-web/studies/{study}", content=build_stow_body([ds_bytes(ds)]),
        headers=stow_headers(),
    )
    assert resp.status_code == 200
    assert client.get(f"/api/v1/volume/{series}/info").status_code == 404
    assert client.get(f"/api/v1/volume/{series}/data").status_code == 404


def test_service_helpers():
    assert volume_service.dtype_for(8, 0) == "uint8"
    assert volume_service.dtype_for(16, 0) == "uint16"
    assert volume_service.dtype_for(16, 1) == "int16"
    assert volume_service.dtype_for(32, 1) == "int32"
    assert volume_service.orientation_label([1, 0, 0, 0, 1, 0]) == "AXIAL"
    assert volume_service.orientation_label([0, 1, 0, 0, 0, -1]) == "SAGITTAL"
    assert volume_service.orientation_label([1, 0, 0, 0, 0, 1]) == "CORONAL"
    assert volume_service.orientation_label([0.5, 0.5, 0.7, 0, 0, 0]) == "OBLIQUE"
    assert volume_service.orientation_label(None) == "UNKNOWN"
    header = volume_service.npy_header("uint16", (2, 32, 32))
    assert header[:6] == b"\x93NUMPY"
    assert np.lib.format.read_magic(io.BytesIO(header)) == (1, 0)


# --------------------------------------------------------------------------- #
# C03: the volume / FHIR / GSPS / dashboard / metrics routes require
# authentication. Override the anonymous ``client`` fixture for this module so
# its requests carry a valid ADMIN bearer token. Tests that assert anonymous
# rejection live in test_route_auth_matrix.py and use ``anonymous_client``.
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client
