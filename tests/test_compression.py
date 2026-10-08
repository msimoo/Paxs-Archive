import asyncio
import io

import imagecodecs
import numpy as np
import pydicom
import pytest
from pydicom.encaps import generate_frames
from pydicom.uid import HTJ2KLossless, JPEGLSLossless
from sqlalchemy import select

from app.core.config import settings
from app.db.models import CompressionJob, Instance
from app.db.session import AsyncSessionLocal, SyncSessionLocal
from app.services import indexing_service, storage_service
from app.services.compression_service import encoder_available
from tests.helpers import make_instance

JPEG_LS_UID = str(JPEGLSLossless)  # 1.2.840.10008.1.2.4.80
HTJ2K_UID = str(HTJ2KLossless)     # 1.2.840.10008.1.2.4.201


def test_rle_compression_reduces_size():
    ds = make_instance(rows=128, cols=128)
    key, _ = storage_service.save_dataset(ds)
    original = storage_service.read_bytes(key)

    result = storage_service.compress(key, "RLE")
    assert result["transfer_syntax_uid"] == "1.2.840.10008.1.2.5"  # RLE Lossless
    assert result["saved_percent"] > 0
    assert result["compressed_bytes"] < result["original_bytes"]

    after = storage_service.read_bytes(key)
    assert after != original
    assert len(after) == result["compressed_bytes"]


def test_compress_is_idempotent():
    ds = make_instance()
    key, _ = storage_service.save_dataset(ds)
    first = storage_service.compress(key, "RLE")
    second = storage_service.compress(key, "RLE")
    assert second["already_compressed"] is True
    assert second["compressed_bytes"] == first["compressed_bytes"]


@pytest.mark.skipif(
    encoder_available(JPEG_LS_UID),
    reason="JPEG-LS encoder (pyjpegls) is installed — fallback path not exercised",
)
def test_fallback_when_encoder_missing():
    # JPEG-LS encoder is not installed in this environment — the archive must
    # gracefully fall back to RLE so compression still works.
    ds = make_instance()
    key, _ = storage_service.save_dataset(ds)
    result = storage_service.compress(key, "JPEG-LS")
    assert result["transfer_syntax_uid"] == "1.2.840.10008.1.2.5"  # fell back to RLE
    assert result["method"] == "RLE"
    assert result["compressed_bytes"] > 0


@pytest.mark.skipif(
    not encoder_available(HTJ2K_UID),
    reason="HTJ2K encoder (imagecodecs) not installed",
)
def test_htj2k_compression_roundtrip():
    """With imagecodecs installed, HTJ2K actually compresses: the stored file
    carries the HTJ2K Lossless transfer syntax, shrinks, and the codestream
    decodes back to identical pixels (lossless)."""
    ds = make_instance(rows=128, cols=128)
    key, _ = storage_service.save_dataset(ds)
    original = storage_service.read_bytes(key)

    result = storage_service.compress(key, "HTJ2K")
    assert result["transfer_syntax_uid"] == HTJ2K_UID
    assert result["method"] == "HTJ2K"
    assert result["saved_percent"] > 0
    assert result["compressed_bytes"] < result["original_bytes"]

    after = storage_service.read_bytes(key)
    assert after != original
    assert len(after) == result["compressed_bytes"]

    # The file is valid DICOM with the HTJ2K transfer syntax…
    stored = pydicom.dcmread(io.BytesIO(after), force=True)
    assert str(stored.file_meta.TransferSyntaxUID) == HTJ2K_UID

    # …and the codestream is genuinely lossless (imagecodecs decode is the
    # ground truth — pydicom 3.0.x registers no HTJ2K pixel handler).
    frames = list(generate_frames(stored.PixelData))
    assert len(frames) == 1
    decoded = imagecodecs.htj2k_decode(frames[0])
    assert np.array_equal(decoded, ds.pixel_array)


def test_check_codecs_cli_exit_code(monkeypatch, capsys):
    """The worker CLI's --check-codecs matrix prints all four methods and
    returns 0 when DEFAULT_COMPRESSION is usable, 1 when it would silently
    fall back to RLE (deploy gate)."""
    from app.services import worker

    monkeypatch.setattr(settings, "DEFAULT_COMPRESSION", "RLE")
    assert worker._check_codecs_cli() == 0
    out = capsys.readouterr().out
    assert "PAXS lossless codec availability" in out
    assert "RLE" in out and "JPEG-LS" in out
    assert "JPEG2000" in out and "HTJ2K" in out
    assert "usable" in out

    monkeypatch.setattr(settings, "DEFAULT_COMPRESSION", "BOGUS-CODEC")
    assert worker._check_codecs_cli() == 1
    out = capsys.readouterr().out
    assert "UNAVAILABLE" in out and "fall back to RLE" in out


def _ingest_one(patient_id="HTJ2KP1", sop_uid="1.2.3.5.800.1"):
    """Save + index one instance through the normal pipeline; returns the
    (instance key, source dataset). Ingest queues a PENDING compression job
    with ``settings.DEFAULT_COMPRESSION``."""
    from app.dicom.parser import parse_metadata

    ds = make_instance(study_uid="1.2.840.113619.2.408.800",
                       series_uid="1.2.3.4.800.1", sop_uid=sop_uid,
                       instance_num=1, patient_id=patient_id)
    key, sha = storage_service.save_dataset(ds)

    async def _index():
        async with AsyncSessionLocal() as s:
            async with s.begin():
                await indexing_service.index_new_instance(
                    s, parse_metadata(ds), key, sha, performed_by="test"
                )

    asyncio.run(_index())
    return key, ds


@pytest.mark.skipif(
    not encoder_available(HTJ2K_UID),
    reason="HTJ2K encoder (imagecodecs) not installed",
)
def test_default_compression_htj2k_flows_through_worker(monkeypatch):
    """End-to-end with DEFAULT_COMPRESSION=HTJ2K: ingest queues the job with
    method HTJ2K, the worker's _process_job compresses the stored instance,
    and the instance row + file both carry the HTJ2K transfer syntax."""
    monkeypatch.setattr(settings, "DEFAULT_COMPRESSION", "HTJ2K")
    key, ds = _ingest_one()

    with SyncSessionLocal() as s:
        # Select *this* test's job. Querying "any PENDING job" made the test
        # order-dependent: a leftover row from an earlier test (queued with the
        # default JPEG-LS) could win and fail the HTJ2K assertion below.
        inst = s.execute(
            select(Instance).where(Instance.sop_instance_uid == str(ds.SOPInstanceUID))
        ).scalar_one()
        job = s.execute(
            select(CompressionJob).where(CompressionJob.instance_id == inst.id)
        ).scalars().first()
        assert job is not None
        assert job.method == "HTJ2K"  # job inherits DEFAULT_COMPRESSION

        from app.services.worker import CompressionWorker

        CompressionWorker()._process_job(s, job)
        s.commit()

        assert job.status == "DONE"
        assert job.compressed_size and job.compressed_size < job.original_size
        inst = s.get(Instance, job.instance_id)
        assert inst.transfer_syntax_uid == HTJ2K_UID
        assert inst.compressed_at is not None

    # The stored file on the backend is the compressed HTJ2K instance.
    after = storage_service.read_bytes(key)
    stored = pydicom.dcmread(io.BytesIO(after), force=True)
    assert str(stored.file_meta.TransferSyntaxUID) == HTJ2K_UID


@pytest.mark.skipif(
    not encoder_available(JPEG_LS_UID),
    reason="JPEG-LS encoder (pyjpegls) not installed",
)
def test_jpeg_ls_compression_roundtrip():
    """With pyjpegls installed, JPEG-LS actually compresses: the stored file
    carries the JPEG-LS Lossless transfer syntax, shrinks, and round-trips
    pixel values exactly (lossless)."""
    ds = make_instance(rows=128, cols=128)
    key, _ = storage_service.save_dataset(ds)
    original = storage_service.read_bytes(key)

    result = storage_service.compress(key, "JPEG-LS")
    assert result["transfer_syntax_uid"] == JPEG_LS_UID
    assert result["method"] == "JPEG-LS"
    assert result["saved_percent"] > 0
    assert result["compressed_bytes"] < result["original_bytes"]

    after = storage_service.read_bytes(key)
    assert after != original
    assert len(after) == result["compressed_bytes"]

    # Lossless round-trip: the compressed file decodes back to identical pixels.
    stored = pydicom.dcmread(io.BytesIO(after), force=True)
    assert str(stored.file_meta.TransferSyntaxUID) == JPEG_LS_UID
    assert stored.pixel_array.shape == ds.pixel_array.shape
    assert (stored.pixel_array == ds.pixel_array).all()
