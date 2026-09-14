"""Helpers to build realistic synthetic DICOM instances for tests."""

from __future__ import annotations

import io

import numpy as np
import pydicom
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, generate_uid

CT = str(CTImageStorage)


def make_instance(
    *,
    patient_id: str = "P0001",
    patient_name: str = "Doe^John",
    birth: str = "19800115",
    sex: str = "M",
    study_uid: str | None = None,
    series_uid: str | None = None,
    sop_uid: str | None = None,
    study_date: str = "20250115",
    study_time: str = "093000",
    accession: str = "ACC0001",
    study_desc: str = "CT CHEST W CONTRAST",
    study_id: str = "S100",
    referring: str = "Smith^Jane",
    modality: str = "CT",
    series_num: int = 1,
    series_desc: str = "AXIAL 1.0mm",
    body_part: str = "CHEST",
    sop_class_uid: str = CT,
    instance_num: int = 1,
    rows: int = 64,
    cols: int = 64,
    bits: int = 16,
    photometric: str = "MONOCHROME2",
    window_center: str = "40",
    window_width: str = "400",
    pixel: np.ndarray | None = None,
    position: list[float] | None = None,
    orientation: list[float] | None = None,
    rescale_slope: str = "1",
    rescale_intercept: str = "-1024",
    slice_thickness: str | None = None,
    frame_of_reference: str | None = None,
):
    """Build a synthetic single-frame grayscale DICOM dataset."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = sop_class_uid
    sop = sop_uid or generate_uid()
    file_meta.MediaStorageSOPInstanceUID = sop
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset("test.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = birth
    ds.PatientSex = sex
    ds.StudyInstanceUID = study_uid or generate_uid()
    ds.StudyDate = study_date
    ds.StudyTime = study_time
    ds.AccessionNumber = accession
    ds.StudyDescription = study_desc
    ds.StudyID = study_id
    ds.ReferringPhysicianName = referring
    ds.SeriesInstanceUID = series_uid or generate_uid()
    ds.Modality = modality
    ds.SeriesNumber = series_num
    ds.SeriesDescription = series_desc
    ds.BodyPartExamined = body_part
    ds.SOPClassUID = sop_class_uid
    ds.SOPInstanceUID = sop
    ds.InstanceNumber = instance_num
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = photometric
    ds.Rows = rows
    ds.Columns = cols
    ds.BitsAllocated = bits
    ds.BitsStored = bits
    ds.HighBit = bits - 1
    ds.PixelRepresentation = 0
    ds.WindowCenter = window_center
    ds.WindowWidth = window_width
    ds.RescaleSlope = rescale_slope
    ds.RescaleIntercept = rescale_intercept

    if position is not None:
        ds.ImagePositionPatient = [str(v) for v in position]
    if orientation is not None:
        ds.ImageOrientationPatient = [str(v) for v in orientation]
    if slice_thickness is not None:
        ds.SliceThickness = slice_thickness
    if frame_of_reference is not None:
        ds.FrameOfReferenceUID = frame_of_reference

    if pixel is None:
        # A bright disk on black — compressible + visually meaningful.
        yy, xx = np.mgrid[:rows, :cols]
        center, radius = rows / 2, rows / 4
        disk = (yy - center) ** 2 + (xx - center) ** 2 <= radius**2
        pixel = (disk * 1800).astype(np.uint16)
    ds.PixelData = np.ascontiguousarray(pixel).tobytes()
    return ds


def ds_bytes(ds: pydicom.Dataset) -> bytes:
    buf = io.BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


def build_stow_body(datasets: list[bytes], boundary: str = "test-boundary-xyz") -> bytes:
    body = bytearray()
    for data in datasets:
        body += f"--{boundary}\r\n".encode("ascii")
        body += b"Content-Type: application/dicom\r\n\r\n"
        body += data
        body += b"\r\n"
    body += f"--{boundary}--\r\n".encode("ascii")
    return bytes(body)


def stow_headers(boundary: str = "test-boundary-xyz") -> dict[str, str]:
    return {
        "Content-Type": f'multipart/related; type="application/dicom"; boundary={boundary}'
    }
