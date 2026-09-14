"""Tests for WADO-RS Bulk Data & Frame Retrieval (PS 3.18) feature."""

from __future__ import annotations

import numpy as np
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from app.services import wado_rs_bulk_service
from tests.helpers import build_stow_body, ds_bytes, stow_headers


def _make_multiframe_ds(patient_id: str = "WADO_BULK_PAT", num_frames: int = 3) -> Dataset:
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT
    sop = generate_uid()
    file_meta.MediaStorageSOPInstanceUID = sop
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset("mf.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = "1.2.840.10008.5.1.4.1.1.2"
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = generate_uid()
    ds.SeriesInstanceUID = generate_uid()
    ds.PatientID = patient_id
    ds.Modality = "CT"
    ds.Rows = 64
    ds.Columns = 64
    ds.BitsAllocated = 8
    ds.BitsStored = 8
    ds.HighBit = 7
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.NumberOfFrames = num_frames

    # Synthetic pixel frames (3 x 64 x 64)
    arr = np.zeros((num_frames, 64, 64), dtype=np.uint8)
    for f in range(num_frames):
        arr[f, :, :] = (f + 1) * 50
    ds.PixelData = arr.tobytes()
    return ds


def test_extract_frame_bytes_single_and_multiframe():
    """Verify frame byte extraction for single-frame and multi-frame datasets."""
    ds = _make_multiframe_ds(num_frames=3)

    # 1. Extract frame 1
    frames1 = wado_rs_bulk_service.extract_frame_bytes(ds, [1])
    assert len(frames1) == 1
    assert len(frames1[0][0]) == 64 * 64

    # 2. Extract multiple frames (1, 2)
    frames12 = wado_rs_bulk_service.extract_frame_bytes(ds, [1, 2])
    assert len(frames12) == 2

    # 3. Multipart payload formatting
    payload, media_type = wado_rs_bulk_service.build_multipart_related_payload(frames12)
    assert b"--DICOMWEB_BOUNDARY" in payload
    assert 'boundary=DICOMWEB_BOUNDARY' in media_type

    # 4. Bulk data element extraction
    pixel_b, mime = wado_rs_bulk_service.extract_bulk_attribute(ds, "PixelData")
    assert len(pixel_b) == 3 * 64 * 64


def test_wado_frame_and_bulk_api(client):
    """Test WADO-RS frame and bulk data REST API endpoints."""
    ds = _make_multiframe_ds(patient_id="PAT_WADO_API_1", num_frames=2)
    study_uid = ds.StudyInstanceUID
    series_uid = ds.SeriesInstanceUID
    sop_uid = ds.SOPInstanceUID

    # STOW-RS ingest
    body = build_stow_body([ds_bytes(ds)])
    resp_stow = client.post(f"/dicom-web/studies/{study_uid}", content=body, headers=stow_headers())
    assert resp_stow.status_code == 200

    # 1. GET /dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/1 (Single frame)
    resp_f1 = client.get(
        f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/1"
    )
    assert resp_f1.status_code == 200
    assert len(resp_f1.content) == 64 * 64

    # 2. GET /dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/1,2 (Multi-frame stream)
    resp_f12 = client.get(
        f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/1,2"
    )
    assert resp_f12.status_code == 200
    assert b"--DICOMWEB_BOUNDARY" in resp_f12.content

    # 3. GET /dicom-web/studies/{study}/series/{series}/instances/{sop}/bulk/PixelData (Bulk attribute)
    resp_bulk = client.get(
        f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/bulk/PixelData"
    )
    assert resp_bulk.status_code == 200
    assert len(resp_bulk.content) == 2 * 64 * 64
