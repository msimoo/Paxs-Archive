from pathlib import Path

import numpy as np
import pydicom
import pytest
from pydicom.dataset import FileDataset, FileMetaDataset
from pydicom.uid import CTImageStorage, ExplicitVRLittleEndian, HTJ2KLossless, RLELossless, generate_uid
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import CompressionJob
from app.services import routing_service
from app.services.compression_service import compress_file, compression_window_open, encoder_available
from app.services.settings_service import _validate_new_setting
from app.services.worker import CompressionWorker


def make_scan(path: Path, stored_bits: int):
    pixels = (np.arange(4096, dtype=np.int16) % 1000 - 500).reshape(64, 64)
    meta = FileMetaDataset()
    meta.TransferSyntaxUID = ExplicitVRLittleEndian
    meta.MediaStorageSOPClassUID = CTImageStorage
    meta.MediaStorageSOPInstanceUID = generate_uid()
    ds = FileDataset(path, {}, file_meta=meta, preamble=b"\0" * 128)
    ds.SOPClassUID = CTImageStorage
    ds.SOPInstanceUID = meta.MediaStorageSOPInstanceUID
    ds.Rows, ds.Columns = pixels.shape
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelRepresentation = 1
    ds.BitsAllocated, ds.BitsStored, ds.HighBit = 16, stored_bits, stored_bits - 1
    ds.PixelData = pixels.tobytes()
    ds.save_as(path, enforce_file_format=True)
    return ds.SOPInstanceUID, pixels


@pytest.mark.parametrize(("bits", "expected"), [(16, HTJ2KLossless), (12, RLELossless)])
def test_lossless_profile_preserves_pixels_and_sop_uid(tmp_path, bits, expected):
    if not encoder_available(str(HTJ2KLossless)):
        pytest.skip("HTJ2K codec is not installed")
    path = tmp_path / "synthetic.dcm"
    uid, pixels = make_scan(path, bits)
    assert compress_file(path, "HTJ2K")["transfer_syntax_uid"] == expected
    converted = pydicom.dcmread(path)
    assert converted.SOPInstanceUID == converted.file_meta.MediaStorageSOPInstanceUID == uid
    np.testing.assert_array_equal(converted.pixel_array, pixels)
    assert compress_file(path, "UNCOMPRESSED")["transfer_syntax_uid"] == ExplicitVRLittleEndian
    native = pydicom.dcmread(path)
    np.testing.assert_array_equal(native.pixel_array, pixels)
    assert native.SOPInstanceUID == uid
    before = path.read_bytes()
    assert compress_file(path, "AS_SOURCE")["already_compressed"]
    assert path.read_bytes() == before


@pytest.mark.parametrize(("start", "end", "clock", "expected"), [
    ("09:00", "17:00", "09:00", True),
    ("09:00", "17:00", "17:00", False),
    ("22:00", "06:00", "00:00", True),
    ("22:00", "06:00", "12:00", False),
    ("07:00", "07:00", "12:00", True),
])
def test_schedule_respects_boundaries(start, end, clock, expected):
    assert compression_window_open("scheduled", start, end, clock) == expected


def test_thumbnail_claims_continue_outside_compression_window(monkeypatch):
    engine = create_engine("sqlite://")
    CompressionJob.__table__.create(engine)
    with Session(engine) as db:
        db.add_all([
            CompressionJob(id=1, instance_id=1, method="HTJ2K", status="PENDING"),
            CompressionJob(id=2, instance_id=1, method="THUMBNAIL", status="PENDING"),
        ])
        db.commit()
        monkeypatch.setattr("app.services.worker.compression_window_open", lambda *args: False)
        assert [job.method for job in CompressionWorker()._claim_jobs(db)] == ["THUMBNAIL"]
        monkeypatch.setattr("app.services.worker.compression_window_open", lambda *args: True)
        assert [job.method for job in CompressionWorker()._claim_jobs(db)] == ["HTJ2K"]
    engine.dispose()


def test_invalid_time_keeps_live_setting_intact():
    before = settings.COMPRESSION_START_TIME
    with pytest.raises(ValueError):
        _validate_new_setting("COMPRESSION_START_TIME", "24:05")
    assert settings.COMPRESSION_START_TIME == before


def test_routing_queue_waits_without_claiming_outside_its_window(monkeypatch):
    monkeypatch.setattr(settings, "ROUTING_QUEUE_ENABLED", True)
    monkeypatch.setattr(settings, "ROUTING_TIMING", "scheduled")
    monkeypatch.setattr(settings, "ROUTING_START_TIME", "23:00")
    monkeypatch.setattr(settings, "ROUTING_END_TIME", "04:00")
    observed = []

    def check_window(mode, start, end, local_time):
        assert (mode, start, end) == ("scheduled", "23:00", "04:00")
        observed.append(local_time)
        return False

    def unexpected_claim():
        pytest.fail("Routing touched the queue outside its dispatch window")

    monkeypatch.setattr(routing_service, "compression_window_open", check_window)
    monkeypatch.setattr(routing_service, "SyncSessionLocal", unexpected_claim)
    assert routing_service.process_queue_batch() == {
        "claimed": 0, "completed": 0, "failed": 0, "requeued": 0,
    }
    assert len(observed) == 1
