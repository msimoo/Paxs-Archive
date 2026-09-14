"""Tests for multi-frame / Enhanced CT volume streaming.

A multi-frame instance (NumberOfFrames > 1, e.g. Enhanced CT) is expanded by
the volume service into per-frame z-ordered slices instead of being rejected:
/info reports one slice per frame, /data streams frames in patient z-order
(per-frame positions from PerFrameFunctionalGroupsSequence, fallback offset
along the plane normal), and /mpr/slice reslices the expanded stack.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from tests.helpers import build_stow_body, ds_bytes, stow_headers

ENHANCED_CT_SOP = "1.2.840.10008.5.1.4.1.1.130.1"


def _make_enhanced_ct(*, study_uid, series_uid, sop_uid, n_frames: int = 3,
                      per_frame_positions: list[list[float]] | None = None,
                      base_position: list[float] | None = None,
                      dimension_steps: list[int] | None = None) -> bytes:
    """A synthetic Enhanced CT instance: frames with distinct values and
    optional per-frame positions (scrambled order supported).

    ``dimension_steps`` builds a Position dimension (DimensionIndexSequence
    pointing at ImagePositionPatient) with per-frame DimensionIndexValues and
    *no* PlanePositionSequence — the case the ordering fallback must handle.
    """
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = ENHANCED_CT_SOP
    file_meta.MediaStorageSOPInstanceUID = sop_uid
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset("enh.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = ENHANCED_CT_SOP
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.PatientID = "MF_PAT"
    ds.Modality = "CT"
    ds.SeriesNumber = 1
    ds.InstanceNumber = 1
    ds.Rows = 16
    ds.Columns = 16
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.NumberOfFrames = n_frames
    ds.RescaleSlope = "1"
    ds.RescaleIntercept = "-1024"
    if base_position is not None:
        ds.ImagePositionPatient = [str(v) for v in base_position]
        ds.SliceThickness = "5"

    frames = []
    for f in range(n_frames):
        px = np.full((16, 16), 100 + f * 10, dtype=np.uint16)
        px[4:12, 4:12] = 1500 + f * 100  # bright core
        frames.append(px)
    ds.PixelData = np.stack(frames).tobytes()

    # Shared functional groups: orientation + pixel measures (as in real
    # Enhanced CT — these live in sequences, not at the top level).
    shared = Dataset()
    orient = Dataset()
    orient.ImageOrientationPatient = [str(v) for v in (1.0, 0.0, 0.0, 0.0, 1.0, 0.0)]
    shared.PlaneOrientationSequence = Sequence([orient])
    measures = Dataset()
    measures.PixelSpacing = ["0.5", "0.5"]
    measures.SliceThickness = "5"
    shared.PixelMeasuresSequence = Sequence([measures])
    ds.SharedFunctionalGroupsSequence = Sequence([shared])

    if per_frame_positions is not None:
        pffg = Sequence()
        for pos in per_frame_positions:
            item = Dataset()
            plane = Dataset()
            plane.ImagePositionPatient = [str(v) for v in pos]
            item.PlanePositionSequence = Sequence([plane])
            pffg.append(item)
        ds.PerFrameFunctionalGroupsSequence = pffg
    elif dimension_steps is not None:
        # Position dimension declared via DimensionIndexSequence; per-frame
        # DimensionIndexValues in FrameContentSequence, no PlanePosition.
        dim = Dataset()
        dim.DimensionIndexPointer = 0x00200032  # ImagePositionPatient
        dim.FunctionalGroupPointer = 0x00209111  # FrameContentSequence
        dim.DimensionOrganizationUID = generate_uid()
        ds.DimensionIndexSequence = Sequence([dim])
        pffg = Sequence()
        for step in dimension_steps:
            item = Dataset()
            fcs = Dataset()
            fcs.DimensionIndexValues = [step]
            item.FrameContentSequence = Sequence([fcs])
            pffg.append(item)
        ds.PerFrameFunctionalGroupsSequence = pffg

    return ds_bytes(ds)


def _stow(client, study_uid: str, data: bytes) -> None:
    resp = client.post(
        f"/dicom-web/studies/{study_uid}", content=build_stow_body([data]),
        headers=stow_headers(),
    )
    assert resp.status_code == 200, resp.text


def test_enhanced_ct_expands_to_frames(client):
    """/info reports one slice per frame, z-ordered by per-frame positions."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    # frame 0 at z=10, frame 1 at z=0, frame 2 at z=5 → sorted 1, 2, 0
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        per_frame_positions=[[1.0, 2.0, 10.0], [1.0, 2.0, 0.0], [1.0, 2.0, 5.0]],
    ))

    resp = client.get(f"/api/v1/volume/{series}/info")
    assert resp.status_code == 200
    info = resp.json()
    assert info["slices"] == 3
    assert info["rows"] == 16 and info["columns"] == 16
    assert info["multi_frame"] is False      # expanded per-frame stream
    assert info["source_multi_frame"] is True
    assert info["dtype"] == "uint16"
    assert info["orientation"] == "AXIAL"
    assert info["slice_spacing"] == 5.0
    assert info["instance_order"] == [f"{sop}#1", f"{sop}#2", f"{sop}#0"]


def test_enhanced_ct_data_streams_frames_in_z_order(client):
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        per_frame_positions=[[1.0, 2.0, 10.0], [1.0, 2.0, 0.0], [1.0, 2.0, 5.0]],
    ))

    resp = client.get(f"/api/v1/volume/{series}/data")
    assert resp.status_code == 200
    assert resp.headers["x-volume-slices"] == "3"
    assert resp.headers["content-length"] == str(3 * 16 * 16 * 2)
    vol = np.frombuffer(resp.content, dtype="<u2").reshape(3, 16, 16)
    # z-order: frame1 (z=0), frame2 (z=5), frame0 (z=10)
    assert vol[0, 0, 0] == 110 and vol[0, 8, 8] == 1600
    assert vol[1, 0, 0] == 120 and vol[1, 8, 8] == 1700
    assert vol[2, 0, 0] == 100 and vol[2, 8, 8] == 1500


def test_enhanced_ct_rescale_to_hu(client):
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=2,
        per_frame_positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 5.0]],
    ))
    resp = client.get(f"/api/v1/volume/{series}/data?rescale=1")
    assert resp.status_code == 200
    vol = np.frombuffer(resp.content, dtype="<f4").reshape(2, 16, 16)
    assert vol[0, 0, 0] == pytest.approx(100 - 1024)
    assert vol[1, 8, 8] == pytest.approx(1600 - 1024)


def test_enhanced_ct_slice_range_and_mpr(client):
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=4,
        per_frame_positions=[[0.0, 0.0, float(5 * f)] for f in range(4)],
    ))

    # Sub-range streams the right frames
    resp = client.get(f"/api/v1/volume/{series}/data?slices=1-2")
    assert resp.status_code == 200
    vol = np.frombuffer(resp.content, dtype="<u2").reshape(2, 16, 16)
    assert vol[0, 0, 0] == 110  # frame 1
    assert vol[1, 0, 0] == 120  # frame 2

    # MPR slice now works on the expanded stack (no 422)
    resp = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=2&format=raw")
    assert resp.status_code == 200, resp.text
    plane = np.frombuffer(resp.content, dtype="<f4").reshape(16, 16)
    assert plane[0, 0] == pytest.approx(120 - 1024)
    assert plane[8, 8] == pytest.approx(1700 - 1024)

    # Out-of-range frame index
    assert client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=99").status_code == 416


def test_multiframe_without_positions_falls_back(client):
    """Legacy multi-frame without PerFrameFunctionalGroupsSequence uses the
    base ImagePositionPatient + SliceThickness along the plane normal."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        per_frame_positions=None, base_position=[10.0, 20.0, 30.0],
    ))

    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["slices"] == 3
    assert info["source_multi_frame"] is True
    assert info["image_position_patient"] == [10.0, 20.0, 30.0]

    resp = client.get(f"/api/v1/volume/{series}/data")
    vol = np.frombuffer(resp.content, dtype="<u2").reshape(3, 16, 16)
    # frame order preserved (positions are sequential z=30, 35, 40)
    assert vol[0, 0, 0] == 100
    assert vol[1, 0, 0] == 110
    assert vol[2, 0, 0] == 120


def test_multiframe_guards_and_npy(client):
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        per_frame_positions=[[0.0, 0.0, 0.0], [0.0, 0.0, 5.0], [0.0, 0.0, 10.0]],
    ))
    # Range bounds still enforced on the expanded stack
    assert client.get(f"/api/v1/volume/{series}/data?slices=9-12").status_code == 416
    # npy container of the expanded volume
    import io

    resp = client.get(f"/api/v1/volume/{series}/data?format=npy&rescale=1")
    assert resp.status_code == 200
    arr = np.load(io.BytesIO(resp.content))
    assert arr.shape == (3, 16, 16)
    assert arr.dtype == np.float32
    assert arr[2, 8, 8] == pytest.approx(1700 - 1024)


def test_enhanced_ct_dimension_index_orders_scrambled_frames(client):
    """No PlanePositionSequence, but DimensionIndexValues referencing the
    Position dimension: the stream must follow the dimension steps, not the
    file order."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    # File order carries frames 0,1,2; the Position dimension says their
    # stack order is 3,1,2 → frames reorder to 1 (step 1), 2 (step 2),
    # 0 (step 3) along the +z normal.
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        dimension_steps=[3, 1, 2], base_position=[10.0, 20.0, 30.0],
    ))

    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["slices"] == 3
    assert info["source_multi_frame"] is True
    assert info["instance_order"] == [f"{sop}#1", f"{sop}#2", f"{sop}#0"]

    resp = client.get(f"/api/v1/volume/{series}/data")
    vol = np.frombuffer(resp.content, dtype="<u2").reshape(3, 16, 16)
    # z-order from dimension steps: frame1 (step 1), frame2 (step 2), frame0 (step 3)
    assert vol[0, 0, 0] == 110 and vol[0, 8, 8] == 1600
    assert vol[1, 0, 0] == 120 and vol[1, 8, 8] == 1700
    assert vol[2, 0, 0] == 100 and vol[2, 8, 8] == 1500


def test_enhanced_ct_dimension_index_without_base_position(client):
    """Dimension steps alone (no ImagePositionPatient at all) still produce
    a correctly ordered stack from synthesized positions."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        dimension_steps=[2, 3, 1], base_position=None,
    ))

    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["slices"] == 3
    assert info["instance_order"] == [f"{sop}#2", f"{sop}#0", f"{sop}#1"]

    resp = client.get(f"/api/v1/volume/{series}/data")
    vol = np.frombuffer(resp.content, dtype="<u2").reshape(3, 16, 16)
    assert vol[0, 0, 0] == 120 and vol[0, 8, 8] == 1700  # frame2 (step 1)
    assert vol[1, 0, 0] == 100 and vol[1, 8, 8] == 1500  # frame0 (step 2)
    assert vol[2, 0, 0] == 110 and vol[2, 8, 8] == 1600  # frame1 (step 3)


def test_enhanced_ct_dimension_index_ignored_with_plane_positions(client):
    """Real per-frame positions win over the dimension-index fallback."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        per_frame_positions=[[1.0, 2.0, 10.0], [1.0, 2.0, 0.0], [1.0, 2.0, 5.0]],
        dimension_steps=[3, 1, 2],
    ))

    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["instance_order"] == [f"{sop}#1", f"{sop}#2", f"{sop}#0"]

def test_frame_map_aligns_with_wado_frames(client):
    """info.frame_map maps z-order → (sop, 1-based WADO-RS frame); the same
    physical slice is served by /mpr/slice?index=i and WADO-RS frames/N."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    # steps [3,1,2] → z-order: frame1 (step 1), frame2 (step 2), frame0 (step 3)
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        dimension_steps=[3, 1, 2], base_position=[10.0, 20.0, 30.0],
    ))

    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["instance_order"] == [f"{sop}#1", f"{sop}#2", f"{sop}#0"]
    # WADO-RS frame numbers are 1-based file order: z-slice 0 ↔ frame #2, etc.
    assert info["frame_map"] == [
        {"sop_instance_uid": sop, "frame": 2},
        {"sop_instance_uid": sop, "frame": 3},
        {"sop_instance_uid": sop, "frame": 1},
    ]

    # Toggle parity: the resliced axial slice at z-index i and the raw WADO-RS
    # frame mapped to i must show the same anatomy (per-frame values).
    mpr = client.get(f"/api/v1/volume/{series}/mpr/slice?plane=axial&index=1&format=raw")
    assert mpr.status_code == 200
    plane = np.frombuffer(mpr.content, dtype="<f4").reshape(16, 16)
    assert plane[0, 0] == pytest.approx(120 - 1024)   # frame2 (z-slice 1)

    frame = client.get(
        f"/dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/3"
    )
    assert frame.status_code == 200
    raw = np.frombuffer(frame.content, dtype="<u2").reshape(16, 16)
    assert raw[0, 0] == 120 and raw[8, 8] == 1700


def test_wado_frames_zorder_matches_volume_index(client):
    """?zorder=1 interprets frame numbers as 0-based z-order indices — the same
    geometry as /mpr/slice?index=N and info.frame_map positions."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=3,
        dimension_steps=[3, 1, 2], base_position=[10.0, 20.0, 30.0],
    ))

    def zframe(z: int):
        r = client.get(
            f"/dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/{z}?zorder=1"
        )
        assert r.status_code == 200, r.text
        return np.frombuffer(r.content, dtype="<u2").reshape(16, 16)

    # z-slice 0 → file frame #2 (step 1): value 110
    assert zframe(0)[0, 0] == 110 and zframe(0)[8, 8] == 1600
    # z-slice 1 → file frame #3 (step 2): value 120
    assert zframe(1)[0, 0] == 120 and zframe(1)[8, 8] == 1700
    # z-slice 2 → file frame #1 (step 3): value 100
    assert zframe(2)[0, 0] == 100 and zframe(2)[8, 8] == 1500

    # Without zorder the numbers stay file order: frame #2 == z-slice 0's frame
    plain = client.get(
        f"/dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/2"
    )
    raw = np.frombuffer(plain.content, dtype="<u2").reshape(16, 16)
    assert raw[0, 0] == 110 and raw[8, 8] == 1600

    # Out-of-range z-index
    assert client.get(
        f"/dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/9?zorder=1"
    ).status_code == 404
    assert client.get(
        f"/dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/-1?zorder=1"
    ).status_code == 404


def test_wado_frames_zorder_single_frame_instance(client):
    """zorder on a plain single-frame instance degrades to frame #1."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    _stow(client, study, _make_enhanced_ct(
        study_uid=study, series_uid=series, sop_uid=sop, n_frames=1,
        dimension_steps=[1], base_position=[10.0, 20.0, 30.0],
    ))

    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["frame_map"] == [{"sop_instance_uid": sop, "frame": 1}]

    r = client.get(
        f"/dicom-web/studies/{study}/series/{series}/instances/{sop}/frames/0?zorder=1"
    )
    assert r.status_code == 200
    raw = np.frombuffer(r.content, dtype="<u2").reshape(16, 16)
    assert raw[0, 0] == 100


def test_enhanced_ct_dimension_index_missing_frames_falls_back(client):
    """DimensionIndexSequence present but FrameContentSequence missing on a
    frame: fall back to raw frame order (no crash)."""
    study, series, sop = generate_uid(), generate_uid(), generate_uid()
    dim = Dataset()
    dim.DimensionIndexPointer = 0x00200032
    dim.FunctionalGroupPointer = 0x00209111
    dim.DimensionOrganizationUID = generate_uid()
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = ENHANCED_CT_SOP
    file_meta.MediaStorageSOPInstanceUID = sop
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds = FileDataset("enh.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = ENHANCED_CT_SOP
    ds.SOPInstanceUID = sop
    ds.StudyInstanceUID = study
    ds.SeriesInstanceUID = series
    ds.PatientID = "MF_PAT"
    ds.Modality = "CT"
    ds.NumberOfFrames = 3
    ds.Rows = 16
    ds.Columns = 16
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.RescaleSlope = "1"
    ds.RescaleIntercept = "-1024"
    frames = [np.full((16, 16), 100 + f * 10, dtype=np.uint16) for f in range(3)]
    ds.PixelData = np.stack(frames).tobytes()
    ds.DimensionIndexSequence = Sequence([dim])
    # PerFrameFunctionalGroupsSequence present, but its item lacks
    # FrameContentSequence entirely.
    pffg = Sequence()
    for _ in range(3):
        pffg.append(Dataset())
    ds.PerFrameFunctionalGroupsSequence = pffg
    _stow(client, study, ds_bytes(ds))

    info = client.get(f"/api/v1/volume/{series}/info").json()
    assert info["slices"] == 3
    assert info["instance_order"] == [f"{sop}#0", f"{sop}#1", f"{sop}#2"]

    resp = client.get(f"/api/v1/volume/{series}/data")
    vol = np.frombuffer(resp.content, dtype="<u2").reshape(3, 16, 16)
    # frames built without a bright core: uniform 100/110/120 per frame
    assert vol[0, 0, 0] == 100 and vol[1, 8, 8] == 110 and vol[2, 8, 8] == 120
