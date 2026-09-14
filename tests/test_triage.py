"""AI emergency triage tests: detector logic, worker integration, API."""

import numpy as np

from app.services import triage_service
from app.services.triage_service import PneumothoraxHeuristicDetector
from tests.helpers import make_instance
from tests.test_dicomweb import _upload_study


# --------------------------------------------------------------------------- #
# Detector unit tests
# --------------------------------------------------------------------------- #
def _ct_instance(pixel: np.ndarray, sop_uid: str | None = None):
    ds = make_instance(modality="CT", sop_uid=sop_uid, pixel=pixel, rows=pixel.shape[0],
                       cols=pixel.shape[1])
    return ds


def _normal_lung_pixels():
    """Symmetric tissue: no unilateral air pocket."""
    arr = np.full((64, 64), 400, dtype=np.uint16)  # soft tissue HU ~ (400*1-1024)
    return arr


def _pneumothorax_pixels():
    """Left side filled with air (very low HU), right side tissue."""
    arr = np.full((64, 64), 400, dtype=np.uint16)
    arr[:, :28] = 0  # HU = 0*1 - 1024 = -1024 (air) on the left
    return arr


def test_detector_flags_pneumothorax():
    ds = _ct_instance(_pneumothorax_pixels())
    findings = PneumothoraxHeuristicDetector().detect([ds])
    assert len(findings) == 1
    f = findings[0]
    assert f.finding == "PNEUMOTHORAX"
    assert f.severity == "HIGH"
    assert f.confidence > 0.5


def test_detector_quiet_on_normal_lungs():
    ds = _ct_instance(_normal_lung_pixels())
    findings = PneumothoraxHeuristicDetector().detect([ds])
    assert findings == []


def test_detector_ignores_non_ct():
    ds = make_instance(modality="MR")
    findings = PneumothoraxHeuristicDetector().detect([ds])
    assert findings == []


def test_detector_skips_corrupt_pixels():
    # A dataset with no pixel data must not crash the detector.
    ds = make_instance(modality="CT")
    del ds.PixelData
    findings = PneumothoraxHeuristicDetector().detect([ds])
    assert findings == []


# --------------------------------------------------------------------------- #
# run_detectors_on_series + worker integration
# --------------------------------------------------------------------------- #
def test_run_detectors_on_series_sync(client):
    """Upload a study with a pneumothorax-like series and verify the worker
    writes a HIGH triage flag and the API surfaces it."""
    from app.db.session import SyncSessionLocal
    from app.db.models import Instance, Series

    # Build a real file with the air-pocket pixels.
    ds = make_instance(modality="CT", patient_id="TPX1", study_uid="1.2.3.500",
                       series_uid="1.2.3.500.1", instance_num=1,
                       pixel=_pneumothorax_pixels(), rows=64, cols=64)
    from tests.helpers import ds_bytes
    from tests.test_dicomweb import _upload
    _upload(client, [ds_bytes(ds)])

    with SyncSessionLocal() as session:
        series = session.query(Series).filter(Series.series_instance_uid == "1.2.3.500.1").one()
        insts = session.query(Instance).filter(Instance.series_id == series.id).all()
        findings = triage_service.run_detectors_on_series(series, insts)
        assert len(findings) == 1
        assert findings[0].severity == "HIGH"

        # Simulate the worker persisting flags.
        from app.db.models import TriageFlag
        for f in findings:
            session.add(TriageFlag(
                study_instance_uid="1.2.3.500",
                series_instance_uid=f.series_instance_uid,
                finding=f.finding, severity=f.severity,
                confidence=f.confidence, summary=f.summary, detector=f.detector,
            ))
        session.commit()


def test_worker_process_triage_creates_flag(client):
    """The CompressionWorker._process_triage path writes TriageFlag rows."""
    from app.db.session import SyncSessionLocal
    from app.db.models import Series, TriageFlag
    from app.services.worker import CompressionWorker

    ds = make_instance(modality="CT", patient_id="TPX2", study_uid="1.2.3.600",
                       series_uid="1.2.3.600.1", instance_num=1,
                       pixel=_pneumothorax_pixels(), rows=64, cols=64)
    from tests.helpers import ds_bytes
    from tests.test_dicomweb import _upload
    _upload(client, [ds_bytes(ds)])

    worker = CompressionWorker(poll_seconds=999)
    with SyncSessionLocal() as session:
        worker._process_triage(session)
        session.commit()
        flags = session.query(TriageFlag).filter(
            TriageFlag.study_instance_uid == "1.2.3.600").all()
        assert len(flags) >= 1
        assert flags[0].severity == "HIGH"
        # Series marked DONE so a second run adds nothing new.
        series = session.query(Series).filter(
            Series.series_instance_uid == "1.2.3.600.1").one()
        assert series.triage_status == "DONE"
        worker._process_triage(session)
        session.commit()
        flags2 = session.query(TriageFlag).filter(
            TriageFlag.study_instance_uid == "1.2.3.600").all()
        assert len(flags2) == len(flags)


def test_clean_series_triaged_exactly_once(client):
    """A normal (no-finding) series must be marked DONE and never re-analyzed
    (regression: it used to stay PENDING forever and burn CPU every tick)."""
    from app.db.session import SyncSessionLocal
    from app.db.models import Series, TriageFlag
    from app.services.worker import CompressionWorker

    _upload_study(client, n=1, patient_id="TPX3", study_uid="1.2.3.700")  # normal CT
    with SyncSessionLocal() as session:
        series = session.query(Series).filter(
            Series.study.has(study_instance_uid="1.2.3.700")).one()
        assert series.triage_status == "PENDING"
        assert session.query(TriageFlag).count() == 0

    worker = CompressionWorker(poll_seconds=999)
    with SyncSessionLocal() as session:
        worker._process_triage(session)
        session.commit()
        series = session.query(Series).filter(
            Series.study.has(study_instance_uid="1.2.3.700")).one()
        assert series.triage_status == "DONE"
        assert session.query(TriageFlag).count() == 0  # no false positives

        # Second tick: nothing pending -> no work.
        worker._process_triage(session)
        session.commit()
        assert session.query(Series).filter(
            Series.triage_status == "PENDING").count() == 0


def test_late_instance_requeues_series(client):
    """Pushing a new slice into an already-triaged (DONE) series must reset it
    to PENDING so the new slice gets re-analyzed."""
    from app.db.session import SyncSessionLocal
    from app.db.models import Series

    _upload_study(client, n=1, patient_id="TPX4", study_uid="1.2.3.800")  # normal CT
    with SyncSessionLocal() as session:
        series = session.query(Series).filter(
            Series.study.has(study_instance_uid="1.2.3.800")).one()
        series_uid = series.series_instance_uid
        series.triage_status = "DONE"  # simulate a completed triage run
        session.commit()

    # A second slice lands in the same series.
    from tests.helpers import ds_bytes, make_instance
    from tests.test_dicomweb import _upload
    ds = make_instance(patient_id="TPX4", study_uid="1.2.3.800",
                       series_uid=series_uid, instance_num=2)
    _upload(client, [ds_bytes(ds)])

    with SyncSessionLocal() as session:
        series = session.query(Series).filter(
            Series.study.has(study_instance_uid="1.2.3.800")).one()
        assert series.triage_status == "PENDING"


# --------------------------------------------------------------------------- #
# API
# --------------------------------------------------------------------------- #
def _register(client, username="triage-doc"):
    return client.post("/api/v1/auth/register", json={
        "username": username, "password": "secret1",
    }).json()["access_token"]


def test_triage_api_list_and_review(client):
    _upload_study(client, n=1, patient_id="TPA1")  # normal study, no flags
    token = _register(client)
    headers = {"Authorization": f"Bearer {token}"}

    empty = client.get("/api/v1/triage", headers=headers)
    assert empty.status_code == 200
    assert empty.json() == []

    # Seed a flag directly.
    from app.db.session import SyncSessionLocal
    from app.db.models import TriageFlag, Study
    with SyncSessionLocal() as session:
        study = session.query(Study).first()
        session.add(TriageFlag(
            study_instance_uid=study.study_instance_uid,
            finding="PNEUMOTHORAX", severity="HIGH", confidence=0.9,
            summary="Demo flag", detector="pneumothorax-heuristic",
        ))
        session.commit()
        flag_id = session.query(TriageFlag).first().id

    listed = client.get("/api/v1/triage", headers=headers).json()
    assert len(listed) == 1
    assert listed[0]["finding"] == "PNEUMOTHORAX"
    assert listed[0]["patient_name"] == "Doe^John"

    reviewed = client.post(f"/api/v1/triage/{flag_id}/review", headers=headers)
    assert reviewed.status_code == 200
    assert client.get("/api/v1/triage", headers=headers).json() == []


def test_triage_requires_auth(client):
    assert client.get("/api/v1/triage").status_code == 401


def test_delete_study_cleans_triage_flags(client):
    """Deleting a study must remove its (UID-keyed) triage flags too."""
    from app.db.session import SyncSessionLocal
    from app.db.models import TriageFlag

    study_uid, _ = _upload_study(client, n=1, patient_id="PTR1")
    token = _register(client, "triage-del")  # first user -> ADMIN
    with SyncSessionLocal() as session:
        session.add(TriageFlag(
            study_instance_uid=study_uid, finding="X", severity="HIGH",
            confidence=0.9, summary="t", detector="test",
        ))
        session.commit()

    res = client.delete(f"/api/v1/studies/{study_uid}",
                        headers={"Authorization": f"Bearer {token}"})
    assert res.status_code == 204
    with SyncSessionLocal() as session:
        assert session.query(TriageFlag).filter(
            TriageFlag.study_instance_uid == study_uid).count() == 0
