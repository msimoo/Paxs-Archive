"""C-1 regression tests — split/merge must not clobber the rewritten header.

Issue C-1: ``_rewrite_instance_study_uid`` rewrote the dataset and saved it, but
the caller then re-read the ORIGINAL bytes and overwrote the *same* destination
key. The database re-linked the series while the stored object kept the old
``StudyInstanceUID``/``PatientID`` — a PHI-misattribution defect.

These tests assert the stored object and the database now always agree, and that
a failed write leaves the source archive intact with the transaction rolled back.
"""

from __future__ import annotations

import pytest
from pydicom.uid import generate_uid
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import Instance, Series, Study
from app.db.session import AsyncSessionLocal, sync_engine
from app.dicom.parser import parse_metadata
from app.services import indexing_service, split_merge_service, storage_service
from tests.helpers import make_instance


@pytest.fixture(autouse=True)
def _no_scripts(monkeypatch, tmp_path):
    """Point SCRIPTS_DIR at an empty dir so ingest does not run the real
    ``scripts/`` hooks (which fire on every new study)."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "SCRIPTS_DIR", str(tmp_path / "scripts"))


async def _ingest(ds) -> str:
    """Store + index one synthetic instance; return its storage key."""
    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            await indexing_service.index_new_instance(
                session, parse_metadata(ds), key, sha
            )
    return key


def _stored(study_uid: str, series_uid: str, sop_uid: str):
    """Read a stored instance back *with* pixels (identity + payload check)."""
    key = storage_service.instance_key(study_uid, series_uid, sop_uid)
    return storage_service.read_dataset(key)


# --------------------------------------------------------------------------- #
# Happy path: the on-disk header matches the new identity, source is removed
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_split_rewrites_stored_header_and_removes_source():
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop1 = generate_uid()
    sop2 = generate_uid()

    ds1 = make_instance(patient_id="C1_P1", study_uid=study_uid,
                        series_uid=series_uid, sop_uid=sop1, instance_num=1)
    ds2 = make_instance(patient_id="C1_P1", study_uid=study_uid,
                        series_uid=series_uid, sop_uid=sop2, instance_num=2)

    old_keys = [await _ingest(ds1), await _ingest(ds2)]

    async with AsyncSessionLocal() as session:
        async with session.begin():
            new_study = await split_merge_service.split_series_to_new_study(
                session,
                source_study_uid=study_uid,
                series_uids=[series_uid],
                target_patient_id="C1_P2",
                target_patient_name="Split^PatientTwo",
            )
            new_study_uid = new_study.study_instance_uid

    assert new_study_uid != study_uid

    # On disk: every relocated object carries the NEW study + patient identity.
    for sop in (sop1, sop2):
        stored = _stored(new_study_uid, series_uid, sop)
        assert str(stored.StudyInstanceUID) == str(new_study_uid)
        assert str(stored.PatientID) == "C1_P2"
        # The rewrite preserved the pixel payload (still a servable instance).
        assert stored.PixelData is not None and len(stored.PixelData) > 0

    # The superseded source objects are gone.
    for old_key in old_keys:
        assert not storage_service.exists(old_key), (
            f"source object {old_key} should have been unlinked after the split"
        )

    # The DB agrees with the header on disk.
    with Session(sync_engine) as session:
        inst = session.execute(
            select(Instance).where(Instance.sop_instance_uid == sop1)
        ).scalar_one()
        assert inst.file_path == storage_service.instance_key(
            new_study_uid, series_uid, sop1
        )


@pytest.mark.anyio
async def test_merge_rewrites_stored_header():
    """The same guarantee holds for the merge direction."""
    study1_uid = generate_uid()
    study2_uid = generate_uid()
    series1_uid = generate_uid()
    series2_uid = generate_uid()
    sop1 = generate_uid()
    sop2 = generate_uid()

    ds1 = make_instance(patient_id="C1_M1", study_uid=study1_uid,
                        series_uid=series1_uid, sop_uid=sop1)
    ds2 = make_instance(patient_id="C1_M2", study_uid=study2_uid,
                        series_uid=series2_uid, sop_uid=sop2)
    await _ingest(ds1)
    await _ingest(ds2)

    async with AsyncSessionLocal() as session:
        async with session.begin():
            await split_merge_service.merge_studies(
                session,
                source_study_uids=[study1_uid],
                target_study_uid=study2_uid,
            )

    # The merged object now reports the TARGET study + its patient identity.
    stored = _stored(study2_uid, series1_uid, sop1)
    assert str(stored.StudyInstanceUID) == str(study2_uid)
    assert str(stored.PatientID) == "C1_M2"

    with Session(sync_engine) as session:
        inst = session.execute(
            select(Instance).where(Instance.sop_instance_uid == sop1)
        ).scalar_one()
        assert inst.file_path == storage_service.instance_key(
            study2_uid, series1_uid, sop1
        )


# --------------------------------------------------------------------------- #
# Failure handling: a disk-write error leaves the source intact and rolls back
#
# NOTE: this test deliberately does not pass ``target_patient_id``, so the split
# does not create a new Patient. Patient creation goes through
# ``indexing_service.resolve_patient_identity``, which wraps the INSERT in
# ``session.begin_nested()`` (a SAVEPOINT); on SQLite/aiosqlite a released
# SAVEPOINT is *not* undone by an outer rollback (verified), so a patient row
# would survive — a pre-existing defect in indexing_service, not in the split
# contract exercised here. The split's own rows (the new Study, the series
# re-parent, the instance file_path) all live in the outer transaction and do
# roll back, which is what this test asserts.
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_split_write_failure_leaves_source_intact_and_rolls_back(monkeypatch):
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop1 = generate_uid()
    sop2 = generate_uid()

    ds1 = make_instance(patient_id="C1_P1", study_uid=study_uid,
                        series_uid=series_uid, sop_uid=sop1, instance_num=1)
    ds2 = make_instance(patient_id="C1_P1", study_uid=study_uid,
                        series_uid=series_uid, sop_uid=sop2, instance_num=2)
    old_keys = [await _ingest(ds1), await _ingest(ds2)]

    # Snapshot the study set so we can prove no split study survived the rollback.
    with Session(sync_engine) as session:
        studies_before = {
            row[0] for row in session.execute(select(Study.study_instance_uid)).all()
        }

    real_save = storage_service.save_bytes
    attempted: list[str] = []

    def flaky_save(key, data, backend_name=None):
        """Let the first relocation land, then fail the second one."""
        attempted.append(key)
        if len(attempted) >= 2:
            raise storage_service.StorageError("simulated disk write failure")
        return real_save(key, data, backend_name=backend_name)

    monkeypatch.setattr(storage_service, "save_bytes", flaky_save)

    with pytest.raises(storage_service.StorageError):
        async with AsyncSessionLocal() as session:
            async with session.begin():
                await split_merge_service.split_series_to_new_study(
                    session,
                    source_study_uid=study_uid,
                    series_uids=[series_uid],
                    # No target_patient_id: the source patient is reused, so the
                    # transaction contains no SAVEPOINT-protected insert.
                )

    # 1. Source objects are intact and still carry the ORIGINAL identity.
    for old_key in old_keys:
        assert storage_service.exists(old_key)
        src = storage_service.read_dataset(old_key, stop_before_pixels=True)
        assert str(src.StudyInstanceUID) == str(study_uid)
        assert str(src.PatientID) == "C1_P1"

    # 2. Any object this call managed to write before failing was cleaned up.
    assert attempted, "expected the relocation to have attempted a write"
    for key in attempted:
        assert not storage_service.exists(key), (
            f"partially written object {key} should have been removed on failure"
        )

    # 3. The database rolled back cleanly: no new study row, the series still
    #    belongs to the source study, and the instance still points at the
    #    original key.
    with Session(sync_engine) as session:
        studies_after = {
            row[0] for row in session.execute(select(Study.study_instance_uid)).all()
        }
        assert studies_after == studies_before, (
            "the split's new study row must not survive the rollback"
        )

        src_study = session.execute(
            select(Study).where(Study.study_instance_uid == study_uid)
        ).scalar_one_or_none()
        assert src_study is not None

        series_row = session.execute(
            select(Series).where(Series.series_instance_uid == series_uid)
        ).scalar_one()
        assert series_row.study_id == src_study.id

        inst = session.execute(
            select(Instance).where(Instance.sop_instance_uid == sop1)
        ).scalar_one()
        assert inst.file_path == old_keys[0]


# --------------------------------------------------------------------------- #
# Unit: the rewrite helper returns bytes and propagates errors (no blind except)
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_rewrite_helper_returns_bytes_and_propagates_errors(monkeypatch):
    study_uid = generate_uid()
    series_uid = generate_uid()
    sop_uid = generate_uid()
    ds = make_instance(patient_id="C1_R1", study_uid=study_uid,
                       series_uid=series_uid, sop_uid=sop_uid)
    key = await _ingest(ds)

    import io
    import pydicom

    data = split_merge_service._rewrite_instance_study_uid(
        key, new_study_uid="1.2.3.4.5", new_patient_id="C1_R2",
        new_patient_name="Rewritten^Name",
    )
    assert isinstance(data, bytes) and data
    parsed = pydicom.dcmread(io.BytesIO(data))
    assert str(parsed.StudyInstanceUID) == "1.2.3.4.5"
    assert str(parsed.PatientID) == "C1_R2"
    assert str(parsed.PatientName) == "Rewritten^Name"

    # A read failure must propagate (the old code swallowed it silently).
    def boom(*_a, **_k):
        raise storage_service.StorageError("read failed")

    monkeypatch.setattr(storage_service, "read_dataset", boom)
    with pytest.raises(storage_service.StorageError):
        split_merge_service._rewrite_instance_study_uid(
            key, new_study_uid="1.2.3.4.6", new_patient_id="C1_R3",
        )
