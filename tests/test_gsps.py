"""Tests for GSPS (Grayscale Softcopy Presentation State) feature."""

from __future__ import annotations

import pytest

import numpy as np
from pydicom.dataset import Dataset, FileDataset, FileMetaDataset
from pydicom.sequence import Sequence
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

# GSPS SOP Class UID
GSPS_SOP_CLASS = "1.2.840.10008.5.1.4.1.1.11.1"


def _make_gsps(
    *,
    study_uid: str,
    series_uid: str | None = None,
    sop_uid: str | None = None,
    ref_sop_uids: list[str] | None = None,
    ref_series_uid: str | None = None,
    window_center: float = 40.0,
    window_width: float = 400.0,
    rotation: int = 0,
    flip: str = "N",
    label: str = "DEFAULT",
    with_annotations: bool = False,
    with_shutter: bool = False,
) -> Dataset:
    """Build a synthetic GSPS dataset."""
    file_meta = FileMetaDataset()
    file_meta.MediaStorageSOPClassUID = GSPS_SOP_CLASS
    sop = sop_uid or generate_uid()
    file_meta.MediaStorageSOPInstanceUID = sop
    file_meta.TransferSyntaxUID = ExplicitVRLittleEndian

    ds = FileDataset("gsps.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
    ds.SOPClassUID = GSPS_SOP_CLASS
    ds.SOPInstanceUID = sop
    ds.Modality = "PR"
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid or generate_uid()
    # pydicom >= 3.x dictionary names these ContentLabel / ContentDescription /
    # ContentCreatorName (the DICOM GSPS IOD calls them Presentation Label &c,
    # but the official pydicom keyword is the registry name). Using the legacy
    # Presentation* keywords silently drops the tags on serialization.
    ds.ContentLabel = label
    ds.ContentDescription = "Test presentation state"
    ds.PresentationCreationDate = "20260805"
    ds.PresentationCreationTime = "120000"
    ds.ContentCreatorName = "TestCreator"

    # Softcopy VOI LUT Sequence
    voi_item = Dataset()
    voi_item.WindowCenter = window_center
    voi_item.WindowWidth = window_width
    ds.SoftcopyVOILUTSequence = Sequence([voi_item])

    # Spatial Transformation
    if rotation or flip == "Y":
        spatial_item = Dataset()
        spatial_item.ImageRotation = rotation
        spatial_item.ImageHorizontalFlip = flip
        ds.SpatialTransformationSequence = Sequence([spatial_item])

    # Referenced Series Sequence
    if ref_sop_uids:
        ref_series = Dataset()
        ref_series.SeriesInstanceUID = ref_series_uid or generate_uid()
        ref_images = []
        for uid in ref_sop_uids:
            ref_img = Dataset()
            ref_img.ReferencedSOPClassUID = "1.2.840.10008.5.1.4.1.1.2"  # CT
            ref_img.ReferencedSOPInstanceUID = uid
            ref_images.append(ref_img)
        ref_series.ReferencedImageSequence = Sequence(ref_images)
        ds.ReferencedSeriesSequence = Sequence([ref_series])

    # Graphic Annotations (optional)
    if with_annotations:
        ga_item = Dataset()
        # Text annotation
        text_obj = Dataset()
        text_obj.UnformattedTextValue = "Finding: Nodule"
        text_obj.AnchorPoint = [100.0, 100.0]
        text_obj.AnchorPointVisibility = "Y"
        text_obj.AnchorPointAnnotationUnits = "PIXEL"
        ga_item.TextObjectSequence = Sequence([text_obj])
        # Graphic annotation (circle)
        graphic_obj = Dataset()
        graphic_obj.GraphicType = "CIRCLE"
        graphic_obj.GraphicData = [150.0, 150.0, 170.0, 150.0]  # center + edge point
        graphic_obj.GraphicAnnotationUnits = "PIXEL"
        graphic_obj.NumberOfGraphicPoints = 2
        ga_item.GraphicObjectSequence = Sequence([graphic_obj])
        ds.GraphicAnnotationSequence = Sequence([ga_item])

    # Display Shutter (optional)
    if with_shutter:
        ds.ShutterShape = "RECTANGULAR"
        ds.ShutterUpperHorizontalEdge = 10
        ds.ShutterLowerHorizontalEdge = 54
        ds.ShutterLeftVerticalEdge = 10
        ds.ShutterRightVerticalEdge = 54

    return ds


# --------------------------------------------------------------------------- #
# Parser tests
# --------------------------------------------------------------------------- #
class TestGSPSParser:
    def test_parse_basic_gsps(self):
        from app.services.gsps_service import parse_gsps
        study_uid = generate_uid()
        sop = generate_uid()
        ref_sop = generate_uid()
        ds = _make_gsps(study_uid=study_uid, sop_uid=sop,
                        ref_sop_uids=[ref_sop], window_center=50, window_width=350)
        content = parse_gsps(ds)
        assert content.window_center == 50.0
        assert content.window_width == 350.0
        assert content.rotation == 0
        assert content.horizontal_flip is False
        assert ref_sop in content.referenced_sop_uids
        assert content.presentation_label == "DEFAULT"

    def test_parse_rotation_and_flip(self):
        from app.services.gsps_service import parse_gsps
        ds = _make_gsps(study_uid=generate_uid(), rotation=90, flip="Y")
        content = parse_gsps(ds)
        assert content.rotation == 90
        assert content.horizontal_flip is True

    def test_parse_annotations(self):
        from app.services.gsps_service import parse_gsps
        ds = _make_gsps(study_uid=generate_uid(), with_annotations=True)
        content = parse_gsps(ds)
        assert len(content.text_annotations) == 1
        assert content.text_annotations[0].text == "Finding: Nodule"
        assert len(content.graphic_annotations) == 1
        assert content.graphic_annotations[0].graphic_type == "CIRCLE"

    def test_parse_shutter(self):
        from app.services.gsps_service import parse_gsps
        ds = _make_gsps(study_uid=generate_uid(), with_shutter=True)
        content = parse_gsps(ds)
        assert len(content.shutters) == 1
        assert content.shutters[0].shape == "RECTANGULAR"
        assert content.shutters[0].top == 10

    def test_parse_multiple_referenced_images(self):
        from app.services.gsps_service import parse_gsps
        refs = [generate_uid() for _ in range(5)]
        ds = _make_gsps(study_uid=generate_uid(), ref_sop_uids=refs)
        content = parse_gsps(ds)
        assert len(content.referenced_sop_uids) == 5
        assert set(content.referenced_sop_uids) == set(refs)

    def test_parse_empty_gsps(self):
        """GSPS with no sequences should parse without errors."""
        from app.services.gsps_service import parse_gsps
        file_meta = FileMetaDataset()
        file_meta.MediaStorageSOPClassUID = GSPS_SOP_CLASS
        file_meta.MediaStorageSOPInstanceUID = generate_uid()
        file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds = FileDataset("empty.dcm", {}, file_meta=file_meta, preamble=b"\0" * 128)
        ds.SOPClassUID = GSPS_SOP_CLASS
        ds.SOPInstanceUID = file_meta.MediaStorageSOPInstanceUID
        ds.Modality = "PR"
        content = parse_gsps(ds)
        assert content.window_center is None
        assert content.referenced_sop_uids == []


# --------------------------------------------------------------------------- #
# STOW-RS + API integration tests
# --------------------------------------------------------------------------- #
class TestGSPSIntegration:
    def test_stow_gsps_and_query(self, client):
        """Store a CT image + GSPS via STOW-RS, then query presentations."""
        study_uid = generate_uid()
        series_uid = generate_uid()
        img = make_instance(study_uid=study_uid, series_uid=series_uid)
        img_sop = img.SOPInstanceUID

        gsps = _make_gsps(
            study_uid=study_uid,
            ref_sop_uids=[img_sop],
            ref_series_uid=series_uid,
            label="BONE_WL",
        )

        # STOW both
        body = build_stow_body([ds_bytes(img), ds_bytes(gsps)])
        resp = client.post(
            f"/dicom-web/studies/{study_uid}",
            content=body,
            headers=stow_headers(),
        )
        assert resp.status_code == 200

        # Query presentations for the image
        resp = client.get(
            f"/api/v1/gsps/instance/{img_sop}/presentations",
            params={"study_uid": study_uid},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) >= 1
        assert data[0]["presentation_label"] == "BONE_WL"
        assert img_sop in data[0]["referenced_sop_instance_uids"]

    def test_stow_gsps_study_list(self, client):
        """All GSPS in a study are returned by the study endpoint."""
        study_uid = generate_uid()
        img = make_instance(study_uid=study_uid)

        gsps1 = _make_gsps(study_uid=study_uid, ref_sop_uids=[img.SOPInstanceUID], label="SOFT_TISSUE")
        gsps2 = _make_gsps(study_uid=study_uid, ref_sop_uids=[img.SOPInstanceUID], label="LUNG")

        body = build_stow_body([ds_bytes(img), ds_bytes(gsps1), ds_bytes(gsps2)])
        client.post(f"/dicom-web/studies/{study_uid}", content=body, headers=stow_headers())

        resp = client.get(f"/api/v1/gsps/study/{study_uid}")
        assert resp.status_code == 200
        labels = {ps["presentation_label"] for ps in resp.json()}
        assert "SOFT_TISSUE" in labels
        assert "LUNG" in labels

    def test_get_gsps_by_uid(self, client):
        """Retrieve a specific GSPS by its SOP UID."""
        study_uid = generate_uid()
        img = make_instance(study_uid=study_uid)
        gsps = _make_gsps(study_uid=study_uid, ref_sop_uids=[img.SOPInstanceUID])
        gsps_sop = gsps.SOPInstanceUID

        body = build_stow_body([ds_bytes(img), ds_bytes(gsps)])
        client.post(f"/dicom-web/studies/{study_uid}", content=body, headers=stow_headers())

        resp = client.get(f"/api/v1/gsps/{gsps_sop}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["sop_instance_uid"] == gsps_sop

    def test_gsps_not_found(self, client):
        resp = client.get("/api/v1/gsps/1.2.3.999")
        assert resp.status_code == 404

    def test_rendered_with_gsps(self, client):
        """WADO-RS /rendered with presentationUID applies GSPS window/level."""
        study_uid = generate_uid()
        series_uid = generate_uid()
        img = make_instance(study_uid=study_uid, series_uid=series_uid)
        img_sop = img.SOPInstanceUID

        gsps = _make_gsps(
            study_uid=study_uid,
            ref_sop_uids=[img_sop],
            ref_series_uid=series_uid,
            window_center=1800,
            window_width=10,
        )
        gsps_sop = gsps.SOPInstanceUID

        body = build_stow_body([ds_bytes(img), ds_bytes(gsps)])
        client.post(f"/dicom-web/studies/{study_uid}", content=body, headers=stow_headers())

        # Rendered without GSPS
        resp1 = client.get(
            f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{img_sop}/rendered"
        )
        assert resp1.status_code == 200

        # Rendered with GSPS
        resp2 = client.get(
            f"/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{img_sop}/rendered",
            params={"presentationUID": gsps_sop},
        )
        assert resp2.status_code == 200
        # The two renders should differ because the GSPS applies a different window
        assert resp1.content != resp2.content


# --------------------------------------------------------------------------- #
# Rendering tests
# --------------------------------------------------------------------------- #
class TestGSPSRendering:
    def test_apply_rotation(self):
        from app.services.gsps_service import GSPSContent, apply_gsps_to_image
        img = Image.fromarray(np.zeros((64, 32), dtype=np.uint8), mode="L")
        content = GSPSContent(rotation=90)
        result = apply_gsps_to_image(img, content)
        # 90° rotation swaps dimensions
        assert result.size == (64, 32) or result.size[0] != img.size[0]

    def test_apply_flip(self):
        from app.services.gsps_service import GSPSContent, apply_gsps_to_image
        arr = np.arange(64 * 64, dtype=np.uint8).reshape(64, 64)
        img = Image.fromarray(arr, mode="L")
        content = GSPSContent(horizontal_flip=True)
        result = apply_gsps_to_image(img, content)
        result_arr = np.array(result)
        # Flipped: first row should be reversed
        np.testing.assert_array_equal(result_arr[0], arr[0][::-1])

    def test_apply_annotations(self):
        from app.services.gsps_service import (
            GSPSContent, GraphicObject, TextObject, apply_gsps_to_image,
        )
        img = Image.fromarray(np.full((100, 100), 128, dtype=np.uint8), mode="L")
        content = GSPSContent(
            graphic_annotations=[
                GraphicObject(graphic_type="CIRCLE", data=[50, 50, 60, 50]),
            ],
            text_annotations=[
                TextObject(text="Test", anchor_point=(10, 10)),
            ],
        )
        result = apply_gsps_to_image(img, content)
        # Should be RGB now (converted for colored annotations)
        assert result.mode == "RGB"

    def test_apply_rectangular_shutter(self):
        from app.services.gsps_service import (
            DisplayShutter, GSPSContent, apply_gsps_to_image,
        )
        arr = np.full((64, 64), 200, dtype=np.uint8)
        img = Image.fromarray(arr, mode="L")
        content = GSPSContent(
            shutters=[DisplayShutter(shape="RECTANGULAR", top=10, bottom=54, left=10, right=54)],
        )
        result = apply_gsps_to_image(img, content)
        result_arr = np.array(result)
        # Corners outside the shutter should be black (0)
        assert result_arr[0, 0] == 0
        assert result_arr[63, 63] == 0


from PIL import Image  # noqa: E402  — import at bottom for rendering tests


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
