import pytest
import numpy as np
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydicom.dataset import Dataset

from app.api.v1 import volume
from app.api.v1.volume import _mode_endpoints
from app.services.volume.assembly import bundle_cache_key
from app.services.volume.geometry_validation import (
    GeometryErrorCode,
    GeometryTolerances,
    GeometryValidationError,
)
from app.services.volume.slices import _header_from_metadata_json, select_volume_geometry
from app.services.volume.stream import _cache_keys
from app.services.volume.tiles import tile_cache_key


def _image(index, position, *, source=None):
    image = Dataset()
    image.SOPInstanceUID = f"2.25.{index}"
    image.SeriesInstanceUID = "2.25.80"
    image.FrameOfReferenceUID = "2.25.81"
    image.ImageType = ["DERIVED" if source else "ORIGINAL", "PRIMARY"]
    image.LossyImageCompression = "01" if source else "00"
    image.ImagePositionPatient = position
    image.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    image.PixelSpacing = [1, 1]
    image.Rows = 8
    image.Columns = 8
    if source:
        reference = Dataset()
        reference.ReferencedSOPInstanceUID = f"2.25.{source}"
        image.SourceImageSequence = [reference]
    return image


@pytest.mark.parametrize("indexed_metadata", [False, True])
def test_verified_copy_groups_keep_their_original_source_indices(indexed_metadata):
    images = [
        _image(10, [0, 0, 0], source=1),
        _image(3, [0, 0, 2]),
        _image(1, [0, 0, 0]),
        _image(20, [0, 0, 1], source=2),
        _image(2, [0, 0, 1]),
    ]
    if indexed_metadata:
        images = [_header_from_metadata_json(image.to_json()) for image in images]
    tolerances = GeometryTolerances()
    for variant, expected in (("auto", [2, 4, 1]), ("original", [2, 4, 1]), ("lossy", [0, 3])):
        result = select_volume_geometry(images, tolerances, variant)
        assert [frame.source_index for frame in result.slices] == expected


@pytest.mark.parametrize("source,copy_position", [
    (99, [0, 0, 0]),  # Source is not in the series.
    (1, [2, 0, 0]),  # Same projected position, different patient-space origin.
    (2, [0, 0, 0]),  # Reference points to a different slice.
])
def test_unverified_duplicate_positions_are_rejected(source, copy_position):
    images = [
        _image(1, [0, 0, 0]),
        _image(2, [0, 0, 1]),
        _image(11, copy_position, source=source),
    ]
    with pytest.raises(GeometryValidationError) as error:
        select_volume_geometry(images, GeometryTolerances())
    assert error.value.code == GeometryErrorCode.DUPLICATE_POSITION
    with pytest.raises(GeometryValidationError) as error:
        select_volume_geometry(images, GeometryTolerances(), "lossy")
    assert error.value.code == GeometryErrorCode.VARIANT_UNAVAILABLE


def test_regular_series_still_uses_all_images_by_default():
    images = [_image(1, [0, 0, 0]), _image(2, [0, 0, 1])]
    result = select_volume_geometry(images, GeometryTolerances())
    assert [frame.source_index for frame in result.slices] == [0, 1]


def test_original_group_must_pass_geometry_validation():
    images = [
        _image(1, [0, 0, 0]),
        _image(2, [0, 0, 2]),
        _image(3, [0, 0, 3]),
        _image(11, [0, 0, 0], source=1),
    ]
    with pytest.raises(GeometryValidationError) as error:
        select_volume_geometry(images, GeometryTolerances())
    assert error.value.code == GeometryErrorCode.IRREGULAR_SPACING


def test_variant_urls_and_cache_identities_keep_groups_separate():
    endpoints = _mode_endpoints("server", "2.25.80", "lossy")
    assert endpoints["rendered"].endswith("?variant=lossy&format=png")
    assert all("variant=lossy" in url for url in endpoints.values())
    assert bundle_cache_key(1, False, "2.25.80", "original") != bundle_cache_key(
        1, False, "2.25.80", "lossy"
    )
    fields = {"start": 0, "end": 2, "downsample": 1, "rescale": False, "fmt": "raw", "total": 2}
    original = _cache_keys("2.25.80", 1, {**fields, "fingerprint": "a"})
    lossy = _cache_keys("2.25.80", 1, {**fields, "fingerprint": "b"})
    assert set(original).isdisjoint(lossy)
    assert tile_cache_key("2.25.80", "axial", 0, frame_hash="a") != tile_cache_key(
        "2.25.80", "axial", 0, frame_hash="b"
    )


def test_verified_axial_request_rejects_stale_stack_and_reads_one_plane(monkeypatch):
    from app.services.volume.mpr import ResliceResult

    app = FastAPI()
    app.include_router(volume.router)
    app.dependency_overrides[volume.get_db] = lambda: None
    app.dependency_overrides[volume.require_series_read] = lambda: None
    monkeypatch.setattr(volume.settings, "VIEWER_PROCESSING_MODE", "hybrid")
    monkeypatch.setattr(volume.settings, "VOLUME_STREAMING_ENABLED", True)
    fingerprint = "1" * 16
    calls = []

    async def authorized(*args, **kwargs):
        return None

    async def bundle(*args, variant, **kwargs):
        assert variant == "lossy"
        return {"rows": 8, "columns": 8, "slices": 2, "stack_fingerprint": fingerprint}, []

    def streamed(_slices, _info, plane, index, *, max_pixels, downsample):
        calls.append((plane, index, downsample))
        return ResliceResult(
            data=np.arange(64, dtype=np.float32).reshape(8, 8),
            rows=8, cols=8, spacing=[1, 1], plane=plane, index=index,
            origin=None, row_vector=None, col_vector=None,
        )

    def unexpected_volume_assembly(*_args, **_kwargs):
        pytest.fail("Reading one axial slice must not materialize the full volume")

    monkeypatch.setattr(volume, "require_series_access", authorized)
    monkeypatch.setattr(volume.volume_service, "volume_bundle", bundle)
    monkeypatch.setattr(volume.volume_service, "reslice_native_plane_streamed", streamed)
    monkeypatch.setattr(volume.volume_service, "reslice_plane", unexpected_volume_assembly)
    with TestClient(app) as client:
        route = "/api/v1/volume/2.25.80/mpr/slice?variant=lossy&plane=axial&index=1"
        assert client.get(route + "&stack_fingerprint=" + "0" * 16).status_code == 409
        response = client.get(route + "&stack_fingerprint=" + fingerprint)
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/png"
    assert calls == [("axial", 1, 1)]
