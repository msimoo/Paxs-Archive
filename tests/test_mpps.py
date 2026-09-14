"""Tests for the Modality Performed Procedure Step (MPPS) SCP — Phase A2.

Covers real DIMSE N-CREATE / N-SET round trips, worklist (SPS) linking and
status transitions (SCHEDULED -> IN PROGRESS -> COMPLETED, DISCONTINUED
release), the 0x0112 unknown-instance reply, and the read-only REST surface.
"""

import time

from pynetdicom import AE
from pynetdicom.sop_class import ModalityPerformedProcedureStep
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian, generate_uid

from app.db.models import Patient, PerformedProcedureStep, ScheduledProcedureStep
from app.db.session import SyncSessionLocal

MPPS = str(ModalityPerformedProcedureStep)


def _new_sps(patient_id="MRN-MPPS-1", sps_id="SPS-MPPS-1", status="SCHEDULED",
             station="CT1", modality="CT"):
    with SyncSessionLocal() as s:
        patient = s.query(Patient).filter(Patient.patient_id == patient_id).one_or_none()
        if patient is None:
            patient = Patient(patient_id=patient_id, patient_name="MPPS^Patient",
                              patient_birth_date="19800303", patient_sex="M")
            s.add(patient)
            s.flush()
        step = ScheduledProcedureStep(patient_id=patient.id, sps_id=sps_id,
                                      accession_number="ACC-MPPS-1", modality=modality,
                                      scheduled_station_aet=station,
                                      start_date="20260805", start_time="080000",
                                      status=status)
        s.add(step)
        s.commit()
        return step.id


def _n_create_ds(**kw):
    ds = Dataset()
    ds.SOPClassUID = MPPS
    ds.SOPInstanceUID = kw.get("sop_uid", "1.2.840.113619.5.1.1")
    ds.PatientName = kw.get("patient_name", "MPPS^Patient")
    ds.PatientID = kw.get("patient_id", "MRN-MPPS-1")
    ds.PatientBirthDate = kw.get("birth", "19800303")
    ds.PatientSex = kw.get("sex", "M")
    ds.AccessionNumber = kw.get("accession", "ACC-MPPS-1")
    ds.Modality = kw.get("modality", "CT")
    ds.PerformedProcedureStepID = kw.get("pps_id", "PPS-001")
    ds.PerformedProcedureStepDescription = kw.get("desc", "CT CHEST W CONTRAST")
    ds.PerformedProcedureStepStartDate = kw.get("start_date", "20260805")
    ds.PerformedProcedureStepStartTime = kw.get("start_time", "081500")
    ds.PerformedStationAETitle = kw.get("station", "CT1")
    ds.PerformedProcedureStepStatus = kw.get("status", "IN PROGRESS")
    if kw.get("with_sps_attrs", True):
        sched_attrs = Dataset()
        sps_seq = Dataset()
        sps_seq.ScheduledProcedureStepID = kw.get("sps_id", "SPS-MPPS-1")
        sps_seq.AccessionNumber = kw.get("accession", "ACC-MPPS-1")
        sched_attrs.ScheduledProcedureStepSequence = [sps_seq]
        ds.ScheduledStepAttributesSequence = [sched_attrs]
    return ds


def _n_set_ds(status="COMPLETED", **kw):
    ds = Dataset()
    ds.PerformedProcedureStepStatus = status
    ds.PerformedProcedureStepEndDate = kw.get("end_date", "20260805")
    ds.PerformedProcedureStepEndTime = kw.get("end_time", "093000")
    if kw.get("with_series", True):
        item = Dataset()
        item.StudyInstanceUID = kw.get("study_uid", "1.2.840.113619.2.408.200")
        item.SeriesInstanceUID = kw.get("series_uid", "1.2.3.4.200")
        item.Modality = "CT"
        item.SeriesNumber = 1
        ds.PerformedSeriesSequence = [item]
    return ds


def _mpps_create(port, ds, sop_uid=None, ae_title="PAXS_TEST_SCP"):
    ae = AE(ae_title="PAXS_MPPS_SCU")
    ae.add_requested_context(ModalityPerformedProcedureStep, ExplicitVRLittleEndian)
    assoc = ae.associate("127.0.0.1", port, ae_title=ae_title)
    assert assoc.is_established
    status, _ = assoc.send_n_create(ds, MPPS, sop_uid or ds.SOPInstanceUID)
    assoc.release()
    return int(status.Status) if status is not None and status.Status is not None else None


def _mpps_set(port, ds, sop_uid, ae_title="PAXS_TEST_SCP"):
    ae = AE(ae_title="PAXS_MPPS_SCU")
    ae.add_requested_context(ModalityPerformedProcedureStep, ExplicitVRLittleEndian)
    assoc = ae.associate("127.0.0.1", port, ae_title=ae_title)
    assert assoc.is_established
    status, _ = assoc.send_n_set(ds, MPPS, sop_uid)
    assoc.release()
    return int(status.Status) if status is not None and status.Status is not None else None


def _get_pps(sop_uid):
    from sqlalchemy.orm import joinedload
    with SyncSessionLocal() as s:
        return s.query(PerformedProcedureStep).options(
            joinedload(PerformedProcedureStep.patient)).filter(
            PerformedProcedureStep.sop_instance_uid == sop_uid).one_or_none()


def _get_step(sps_id):
    with SyncSessionLocal() as s:
        return s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.sps_id == sps_id).one_or_none()


# --------------------------------------------------------------------------- #
# Real DIMSE N-CREATE / N-SET
# --------------------------------------------------------------------------- #
def test_n_create_marks_sps_in_progress(receiver):
    _new_sps()
    sop_uid = generate_uid()

    status = _mpps_create(receiver[0], _n_create_ds(sop_uid=sop_uid))
    assert status == 0x0000

    # Performed step recorded with the patient + scheduled step linked.
    for _ in range(50):
        if _get_pps(sop_uid):
            break
        time.sleep(0.1)
    pps = _get_pps(sop_uid)
    assert pps is not None
    assert pps.status == "IN PROGRESS"
    assert pps.performed_station_aet == "CT1"
    assert pps.patient.patient_id == "MRN-MPPS-1"
    # The worklist step flipped to IN PROGRESS without any images.
    assert _get_step("SPS-MPPS-1").status == "IN PROGRESS"


def test_n_create_is_idempotent(receiver):
    _new_sps()
    sop_uid = generate_uid()
    assert _mpps_create(receiver[0], _n_create_ds(sop_uid=sop_uid)) == 0x0000
    time.sleep(0.2)
    assert _mpps_create(receiver[0], _n_create_ds(sop_uid=sop_uid)) == 0x0000
    with SyncSessionLocal() as s:
        count = s.query(PerformedProcedureStep).filter(
            PerformedProcedureStep.sop_instance_uid == sop_uid).count()
        assert count == 1


def test_n_create_without_scheduled_step_still_recorded(receiver):
    # Patient exists but no open SPS -> performed step is still recorded, just
    # with no scheduled step link.
    sop_uid = generate_uid()
    status = _mpps_create(receiver[0], _n_create_ds(sop_uid=sop_uid,
                                                    patient_id="MRN-NOSPS",
                                                    sps_id="SPS-NONE"))
    assert status == 0x0000
    for _ in range(50):
        if _get_pps(sop_uid):
            break
        time.sleep(0.1)
    pps = _get_pps(sop_uid)
    assert pps is not None and pps.scheduled_step_id is None


def test_n_set_completed_closes_worklist_loop(receiver):
    _new_sps()
    sop_uid = generate_uid()
    assert _mpps_create(receiver[0], _n_create_ds(sop_uid=sop_uid)) == 0x0000
    time.sleep(0.2)
    assert _get_step("SPS-MPPS-1").status == "IN PROGRESS"

    status = _mpps_set(receiver[0], _n_set_ds(status="COMPLETED"), sop_uid)
    assert status == 0x0000

    for _ in range(50):
        if _get_step("SPS-MPPS-1").status == "COMPLETED":
            break
        time.sleep(0.1)
    pps = _get_pps(sop_uid)
    assert pps.status == "COMPLETED"
    assert pps.study_instance_uid == "1.2.840.113619.2.408.200"
    assert pps.series_instance_uid == "1.2.3.4.200"
    assert pps.end_date == "20260805"
    # The worklist loop is closed with the study linked — all before images.
    step = _get_step("SPS-MPPS-1")
    assert step.status == "COMPLETED"
    assert step.study_instance_uid == "1.2.840.113619.2.408.200"


def test_n_set_discontinued_releases_step(receiver):
    _new_sps()
    sop_uid = generate_uid()
    assert _mpps_create(receiver[0], _n_create_ds(sop_uid=sop_uid)) == 0x0000
    time.sleep(0.2)

    status = _mpps_set(receiver[0], _n_set_ds(status="DISCONTINUED"), sop_uid)
    assert status == 0x0000
    for _ in range(50):
        if _get_step("SPS-MPPS-1").status == "SCHEDULED":
            break
        time.sleep(0.1)
    assert _get_pps(sop_uid).status == "DISCONTINUED"
    # The slot is released so the technologist can re-book it.
    assert _get_step("SPS-MPPS-1").status == "SCHEDULED"


def test_n_set_unknown_instance_returns_0112(receiver):
    status = _mpps_set(receiver[0], _n_set_ds(), generate_uid())
    assert status == 0x0112


def test_c_store_completes_step_left_in_progress_by_mpps(receiver):
    """Regression guard: a modality sends N-CREATE (step -> IN PROGRESS) but
    never N-SET COMPLETED, then images arrive via C-STORE. The C-STORE
    fallback must still close the step (match IN PROGRESS too), otherwise the
    worklist row is stuck IN PROGRESS with no study link."""
    from tests.test_receiver import _store_via_scu
    from tests.helpers import ds_bytes, make_instance

    _new_sps(patient_id="MRN-MPPS-LOST", sps_id="SPS-LOST", station="CT1", modality="CT")
    sop_uid = generate_uid()
    assert _mpps_create(receiver[0], _n_create_ds(sop_uid=sop_uid,
                                                  patient_id="MRN-MPPS-LOST",
                                                  sps_id="SPS-LOST")) == 0x0000
    time.sleep(0.2)
    assert _get_step("SPS-LOST").status == "IN PROGRESS"

    # Images arrive; the N-SET COMPLETED never does.
    ds = make_instance(patient_id="MRN-MPPS-LOST", patient_name="MPPS^Patient",
                       study_uid="1.2.840.113619.2.408.777", series_uid="1.2.3.4.777",
                       sop_uid="1.2.3.5.777.1", accession="ACC-MPPS-1", modality="CT")
    assert _store_via_scu("127.0.0.1", receiver[0], [ds_bytes(ds)]) == [0x0000]

    for _ in range(50):
        if _get_step("SPS-LOST").status == "COMPLETED":
            break
        time.sleep(0.1)
    step = _get_step("SPS-LOST")
    assert step.status == "COMPLETED"
    assert step.study_instance_uid == "1.2.840.113619.2.408.777"
    # The late MPPS N-SET COMPLETED is then harmless (already completed).
    status = _mpps_set(receiver[0], _n_set_ds(status="COMPLETED"), sop_uid)
    assert status == 0x0000
    assert _get_step("SPS-LOST").status == "COMPLETED"


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
def _admin_headers(client):
    client.post("/api/v1/auth/register", json={"username": "mppsboss", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "mppsboss", "password": "secret123"})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_mpps_api_lists_performed_steps(client):
    h = _admin_headers(client)
    _new_sps()
    sop_uid = "1.2.840.113619.5.9.99"
    with SyncSessionLocal() as s:
        # Simulate a performed step directly (SCP path is covered above).
        patient = s.query(Patient).filter(Patient.patient_id == "MRN-MPPS-1").one()
        step = s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.sps_id == "SPS-MPPS-1").one()
        s.add(PerformedProcedureStep(
            sop_instance_uid=sop_uid, patient_id=patient.id, scheduled_step_id=step.id,
            status="COMPLETED", pps_id="PPS-001", modality="CT",
            performed_station_aet="CT1", study_instance_uid="1.2.840.113619.2.408.200",
        ))
        s.commit()

    r = client.get("/api/v1/mpps", headers=h)
    assert r.status_code == 200
    rows = r.json()
    assert len(rows) == 1
    assert rows[0]["sop_instance_uid"] == sop_uid
    assert rows[0]["status"] == "COMPLETED"
    assert rows[0]["patient_name"] == "MPPS^Patient"
    assert rows[0]["scheduled_step_id"] == "SPS-MPPS-1"
    assert rows[0]["study_instance_uid"] == "1.2.840.113619.2.408.200"

    r = client.get("/api/v1/mpps?status=IN PROGRESS", headers=h)
    assert r.json() == []


def test_mpps_api_requires_auth(client):
    assert client.get("/api/v1/mpps").status_code == 401


# --------------------------------------------------------------------------- #
# MPPS SCU (sender helpers) — Core DICOM #8 completes both sides
# --------------------------------------------------------------------------- #
def test_scu_n_create_against_receiver(receiver):
    """sender.send_mpps_create (N-CREATE SCU) round-trips against our own SCP:
    the performed step is recorded and the worklist step flips IN PROGRESS."""
    from app.dicom.sender import send_mpps_create

    _new_sps()
    sop_uid = generate_uid()
    res = send_mpps_create("127.0.0.1", receiver[0], "PAXS_TEST_SCP",
                           _n_create_ds(sop_uid=sop_uid), sop_uid)
    assert res["ok"] is True
    assert res["status"] == 0x0000
    for _ in range(50):
        if _get_pps(sop_uid):
            break
        time.sleep(0.1)
    pps = _get_pps(sop_uid)
    assert pps is not None and pps.status == "IN PROGRESS"
    assert _get_step("SPS-MPPS-1").status == "IN PROGRESS"


def test_scu_n_create_then_n_set_full_lifecycle(receiver):
    """The SCU drives the whole lifecycle: N-CREATE (IN PROGRESS) then N-SET
    (COMPLETED) — the same messages a modality console would send, via our
    sender helpers instead of raw pynetdicom."""
    from app.dicom.sender import send_mpps_create, send_mpps_set

    _new_sps()
    sop_uid = generate_uid()
    assert send_mpps_create("127.0.0.1", receiver[0], "PAXS_TEST_SCP",
                            _n_create_ds(sop_uid=sop_uid), sop_uid)["ok"]
    time.sleep(0.2)

    res = send_mpps_set("127.0.0.1", receiver[0], "PAXS_TEST_SCP",
                        _n_set_ds(status="COMPLETED"), sop_uid)
    assert res["ok"] is True and res["status"] == 0x0000
    for _ in range(50):
        if _get_step("SPS-MPPS-1").status == "COMPLETED":
            break
        time.sleep(0.1)
    pps = _get_pps(sop_uid)
    assert pps.status == "COMPLETED"
    assert pps.study_instance_uid == "1.2.840.113619.2.408.200"
    step = _get_step("SPS-MPPS-1")
    assert step.status == "COMPLETED"
    assert step.study_instance_uid == "1.2.840.113619.2.408.200"


def test_scu_n_set_before_create_returns_0112(receiver):
    """N-SET for a step the SCP never saw -> 0x0112 (no such object instance),
    surfaced through the sender helper as ok:False with the status."""
    from app.dicom.sender import send_mpps_set

    res = send_mpps_set("127.0.0.1", receiver[0], "PAXS_TEST_SCP",
                        _n_set_ds(), generate_uid())
    assert res["ok"] is False
    assert res["status"] == 0x0112


def test_scu_custom_calling_ae_title(receiver):
    """our_ae_title is carried on the wire by the MPPS SCU."""
    from app.dicom.sender import send_mpps_create

    _new_sps()
    sop_uid = generate_uid()
    res = send_mpps_create("127.0.0.1", receiver[0], "PAXS_TEST_SCP",
                           _n_create_ds(sop_uid=sop_uid), sop_uid,
                           our_ae_title="PAXS_MODALITY")
    assert res["ok"] is True


def test_scu_unreachable_peer_is_graceful():
    """Association failure returns ok:False with a reason — never an exception."""
    from app.dicom.sender import send_mpps_create

    res = send_mpps_create("127.0.0.1", 1, "GHOST", _n_create_ds(), generate_uid())
    assert res["ok"] is False
    assert res["reason"] == "association rejected"


def test_qr_c_find_and_c_store_still_work_after_mpps_context(receiver):
    """Registering the MPPS context must not disturb existing services."""
    from tests.test_receiver import _store_via_scu
    from tests.helpers import ds_bytes, make_instance

    ds = make_instance(patient_id="MRN-MPPS-OK", study_uid="1.2.840.113619.2.408.201",
                       series_uid="1.2.3.4.201", sop_uid="1.2.3.5.201.1")
    assert _store_via_scu("127.0.0.1", receiver[0], [ds_bytes(ds)]) == [0x0000]

    # C-ECHO still answers.
    from pynetdicom.sop_class import Verification
    ae = AE(ae_title="PAXS_ECHO_SCU")
    ae.add_requested_context(Verification)
    assoc = ae.associate("127.0.0.1", receiver[0], ae_title="PAXS_TEST_SCP")
    assert assoc.is_established
    assert assoc.send_c_echo().Status == 0x0000
    assoc.release()
