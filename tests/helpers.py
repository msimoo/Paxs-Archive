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


# --------------------------------------------------------------------------- #
# Test authentication helpers (H02)
#
# Self-registration is off by default and can never mint a destructive role, so
# tests that need a user with a specific role create it the way an administrator
# would: bootstrap the first account, then use the admin API.
# --------------------------------------------------------------------------- #
BOOTSTRAP_USER = "root_admin"
BOOTSTRAP_PASSWORD = "secret123"


def admin_headers(client, username: str = BOOTSTRAP_USER) -> dict[str, str]:
    """Bearer headers for an ADMIN, bootstrapping the first account if needed."""
    client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": BOOTSTRAP_PASSWORD},
    )
    res = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": BOOTSTRAP_PASSWORD},
    )
    assert res.status_code == 200, f"could not log in as {username}: {res.text}"
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def auth_for_role(
    client,
    username: str,
    role: str | None = None,
    password: str = BOOTSTRAP_PASSWORD,
    admin: dict[str, str] | None = None,
) -> dict[str, str]:
    """Create ``username`` with ``role`` via the admin API and return its headers.

    ``role=None`` means "whatever the server decides" (the first account is the
    bootstrap ADMIN, later ones are least-privilege). A specific role is created
    through ``POST /api/v1/users`` with an admin token, because H02 forbids
    self-registering privileged accounts.
    """
    admin = admin or admin_headers(client)
    if role:
        created = client.post(
            "/api/v1/users",
            headers=admin,
            json={"username": username, "password": password, "role": role},
        )
        assert created.status_code in (200, 201, 409), (
            f"could not create {username}/{role}: {created.status_code} {created.text}"
        )
    else:
        client.post(
            "/api/v1/auth/register",
            json={"username": username, "password": password},
        )
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert res.status_code == 200, f"could not log in as {username}: {res.text}"
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def create_user(
    client,
    admin: dict[str, str],
    username: str,
    role: str,
    password: str = BOOTSTRAP_PASSWORD,
    tenant_id: int | None = None,
) -> dict[str, str]:
    """Create an account with ``role`` the way an administrator would (H02).

    Self-registration cannot mint privileged roles, so tests that need a
    RADIOLOGIST/ADMIN/etc. use the admin API with an existing admin's headers.
    """
    payload = {"username": username, "password": password, "role": role}
    if tenant_id is not None:
        payload["tenant_id"] = tenant_id
    res = client.post("/api/v1/users", headers=admin, json=payload)
    assert res.status_code in (200, 201), (
        f"admin could not create {username}/{role}: {res.status_code} {res.text}"
    )
    # Return the new user's bearer headers, which is what most callers want.
    login = client.post(
        "/api/v1/auth/login", json={"username": username, "password": password}
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}
