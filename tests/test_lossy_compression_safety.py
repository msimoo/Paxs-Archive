"""Safety regression tests for the lossy compression engine (issues C-2/C-3/C-4).

C-2: the old engine rendered pixels through ``imaging.dataset_to_image`` (VOI LUT
     + 1-99% stretch + 8-bit RGB), irreversibly destroying 16-bit diagnostic data.
C-3: it emitted baseline-JPEG bytes under the JPEG-LS transfer-syntax UID.
C-4: it re-encoded already-lossy data (only baseline JPEG was excluded).

These tests pin the corrected contract: native bit depth preserved, encoder
matched to the declared syntax, lossy sources excluded, and a fail-closed
``LOSSY_COMPRESSION_ENABLED`` guard that modifies nothing.
"""

from __future__ import annotations

import numpy as np
import pytest
from pydicom.uid import UID, generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import AuditLog, Instance, LossyCompressionRule
from app.db.session import AsyncSessionLocal, sync_engine
from app.dicom.parser import parse_metadata
from app.services import indexing_service, lossy_compression_service, storage_service
from tests.helpers import make_instance

EXPLICIT_VR_LE = "1.2.840.10008.1.2.1"
IMPLICIT_VR_LE = "1.2.840.10008.1.2"
JPEG_LS_LOSSY = "1.2.840.10008.1.2.4.81"
JPEG2000_LOSSY = "1.2.840.10008.1.2.4.91"
JPEG_BASELINE = "1.2.840.10008.1.2.4.50"
JPEG_LS_LOSSLESS = "1.2.840.10008.1.2.4.80"


@pytest.fixture(autouse=True)
def _no_scripts(monkeypatch, tmp_path):
    """Point SCRIPTS_DIR at an empty dir so ingest does not run the real scripts."""
    monkeypatch.setattr(settings, "SCRIPTS_DIR", str(tmp_path / "scripts"))


@pytest.fixture(autouse=True)
def _clean_rules():
    """Start every test with no lossy rules, so a run only evaluates the rule the
    test itself creates (the DB is session-scoped and shared across test files)."""
    with sync_engine.begin() as conn:
        conn.execute(LossyCompressionRule.__table__.delete())
    yield


async def _ingest(ds) -> str:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            await indexing_service.index_new_instance(
                session, parse_metadata(ds), key, sha
            )
    return key


async def _make_rule(name: str, *, target: str = JPEG_LS_LOSSY) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await lossy_compression_service.create_rule(
                session,
                {
                    "name": name,
                    "enabled": True,
                    "priority": 1,
                    "min_age_days": 0,
                    "modality": "CT",
                    "transfer_syntax_uid": target,
                    "quality_factor": 85,
                },
            )


# --------------------------------------------------------------------------- #
# C-2: 16-bit CT data survives — no display-pipeline down-conversion
# --------------------------------------------------------------------------- #
def test_16bit_slice_preserves_bit_depth_and_voxel_distribution():
    """A 16-bit CT slice stays 16-bit with its value distribution intact."""
    rng = np.random.default_rng(1234)
    pixel = (rng.random((64, 64)) * 4000).astype(np.uint16)  # values far above 255
    ds = make_instance(modality="CT", bits=16, pixel=pixel)
    original = ds.pixel_array.copy()
    assert original.max() > 255  # would be clipped by any 8-bit conversion

    out, n_bytes = lossy_compression_service.transcode_dataset_lossy(
        ds, target_ts_uid=JPEG_LS_LOSSY, quality_factor=100
    )

    # Bit depth + geometry preserved (the C-2 failure produced 8-bit RGB).
    assert out.BitsAllocated == 16
    assert out.BitsStored == 16
    assert out.PhotometricInterpretation == "MONOCHROME2"
    assert out.SamplesPerPixel == 1
    assert str(out.file_meta.TransferSyntaxUID) == JPEG_LS_LOSSY
    assert n_bytes > 0

    decoded = out.pixel_array
    assert decoded.dtype == np.uint16
    # No 8-bit clipping: high sample values survive the round trip.
    assert decoded.max() > 255
    assert decoded.max() == original.max()

    # Near-lossless (q=100 -> jls_error=1): the distribution is intact.
    diff = np.abs(decoded.astype(np.int64) - original.astype(np.int64))
    assert diff.max() <= 1
    assert abs(float(decoded.mean()) - float(original.mean())) < 1.0


def test_jpeg2000_lossy_also_preserves_16bit():
    ds = make_instance(modality="CT", bits=16)
    out, _ = lossy_compression_service.transcode_dataset_lossy(
        ds, target_ts_uid=JPEG2000_LOSSY, quality_factor=95
    )
    assert out.BitsAllocated == 16
    assert out.PhotometricInterpretation == "MONOCHROME2"
    assert str(out.file_meta.TransferSyntaxUID) == JPEG2000_LOSSY


# --------------------------------------------------------------------------- #
# C-3: encoder is matched to the declared transfer syntax; unencodable targets abort
# --------------------------------------------------------------------------- #
def test_unencodable_target_aborts_instead_of_mislabelling():
    ds = make_instance(modality="CT", bits=16)
    # JPEG Baseline has no pydicom encoder — must raise, never store a
    # baseline-JPEG codestream under another UID.
    with pytest.raises(lossy_compression_service.LossyCompressionError):
        lossy_compression_service.transcode_dataset_lossy(
            ds, target_ts_uid=JPEG_BASELINE
        )
    # An unknown / arbitrary UID is likewise refused.
    with pytest.raises(lossy_compression_service.LossyCompressionError):
        lossy_compression_service.transcode_dataset_lossy(
            ds, target_ts_uid="1.2.840.10008.1.2.4.999"
        )


def test_source_eligibility_matrix():
    is_eligible = lossy_compression_service.is_eligible_source
    # Lossy sources are never eligible.
    for lossy in (JPEG_BASELINE, JPEG_LS_LOSSY, JPEG2000_LOSSY, "1.2.840.10008.1.2.4.51"):
        assert not is_eligible(lossy), lossy
    # Uncompressed sources are eligible.
    assert is_eligible(EXPLICIT_VR_LE)
    assert is_eligible(IMPLICIT_VR_LE)
    # Verified-lossless compressed sources are eligible.
    assert is_eligible(JPEG_LS_LOSSLESS)
    assert is_eligible("1.2.840.10008.1.2.5")  # RLE


# --------------------------------------------------------------------------- #
# C-4: an already-lossy instance is skipped and never re-encoded
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_already_lossy_instance_is_skipped_and_unchanged(monkeypatch):
    monkeypatch.setattr(settings, "LOSSY_COMPRESSION_ENABLED", True)

    study_uid = generate_uid()
    ds = make_instance(patient_id="LOSSY_S1", study_uid=study_uid, modality="CT")
    # Make it already-lossy (JPEG-LS near-lossless) before ingest.
    ds.compress(UID(JPEG_LS_LOSSY), jls_error=3)
    assert str(ds.file_meta.TransferSyntaxUID) == JPEG_LS_LOSSY

    key = await _ingest(ds)
    sha_before = storage_service.sha256_of(key)

    await _make_rule("C4 skip already-lossy", target=JPEG_LS_LOSSY)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            res = await lossy_compression_service.evaluate_and_run_lossy_compression(session)
    assert res["processed"] == 0

    # The stored object is byte-for-byte identical — it was not re-encoded.
    assert storage_service.sha256_of(key) == sha_before
    stored = storage_service.read_dataset(key)
    assert str(stored.file_meta.TransferSyntaxUID) == JPEG_LS_LOSSY

    with Session(sync_engine) as session:
        inst = session.execute(
            select(Instance).where(Instance.sop_instance_uid == ds.SOPInstanceUID)
        ).scalar_one()
        assert inst.transfer_syntax_uid == JPEG_LS_LOSSY
        audits = session.execute(
            select(AuditLog).where(AuditLog.sop_instance_uid == ds.SOPInstanceUID)
        ).scalars().all()
        assert "LOSSY_COMPRESSED" not in {a.action for a in audits}


# --------------------------------------------------------------------------- #
# Fail-closed: LOSSY_COMPRESSION_ENABLED=false modifies nothing
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_disabled_flag_prevents_any_modification(monkeypatch):
    monkeypatch.setattr(settings, "LOSSY_COMPRESSION_ENABLED", False)

    study_uid = generate_uid()
    ds = make_instance(patient_id="LOSSY_S2", study_uid=study_uid, modality="CT")
    key = await _ingest(ds)
    sha_before = storage_service.sha256_of(key)

    await _make_rule("Disabled guard rule", target=JPEG_LS_LOSSY)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            res = await lossy_compression_service.evaluate_and_run_lossy_compression(session)
    assert res["processed"] == 0
    assert res.get("disabled") is True

    # The per-instance entry point is inert too.
    async with AsyncSessionLocal() as session:
        inst = (
            await session.execute(
                select(Instance).where(Instance.sop_instance_uid == ds.SOPInstanceUID)
            )
        ).scalar_one()
        changed = await lossy_compression_service.transcode_instance_lossy(
            session, inst, JPEG_LS_LOSSY, 85
        )
    assert changed is False

    # Nothing on disk or in the DB changed.
    assert storage_service.sha256_of(key) == sha_before
    with Session(sync_engine) as session:
        inst = session.execute(
            select(Instance).where(Instance.sop_instance_uid == ds.SOPInstanceUID)
        ).scalar_one()
        assert inst.transfer_syntax_uid == EXPLICIT_VR_LE
        audits = session.execute(
            select(AuditLog).where(AuditLog.sop_instance_uid == ds.SOPInstanceUID)
        ).scalars().all()
        assert "LOSSY_COMPRESSED" not in {a.action for a in audits}


@pytest.mark.anyio
async def test_enabled_run_replaces_object_atomically(monkeypatch):
    """Sanity: with the flag on, an uncompressed instance IS transcoded in place."""
    monkeypatch.setattr(settings, "LOSSY_COMPRESSION_ENABLED", True)

    study_uid = generate_uid()
    ds = make_instance(patient_id="LOSSY_S3", study_uid=study_uid, modality="CT")
    key = await _ingest(ds)

    await _make_rule("Enabled run rule", target=JPEG_LS_LOSSY)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            res = await lossy_compression_service.evaluate_and_run_lossy_compression(session)
    assert res["processed"] >= 1

    stored = storage_service.read_dataset(key)
    assert str(stored.file_meta.TransferSyntaxUID) == JPEG_LS_LOSSY
    assert stored.BitsAllocated == 16  # geometry preserved through the real path

    with Session(sync_engine) as session:
        inst = session.execute(
            select(Instance).where(Instance.sop_instance_uid == ds.SOPInstanceUID)
        ).scalar_one()
        assert inst.transfer_syntax_uid == JPEG_LS_LOSSY
        assert inst.bits_allocated == 16
