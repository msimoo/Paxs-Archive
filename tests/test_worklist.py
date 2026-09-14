"""Tests for the Modality Worklist (MWL SCP) milestone — Phase A1.

Covers: the worklist information model over real DIMSE, HL7 ADT
auto-scheduling, C-FIND matching keys/wildcards, auto-completion on image
arrival, the management REST API + role gating.
"""

import socket
import time

from pynetdicom import AE
from pynetdicom.sop_class import ModalityWorklistInformationFind
from pydicom.dataset import Dataset
from pydicom.uid import ExplicitVRLittleEndian
from sqlalchemy import select

from app.db.models import Patient, ScheduledProcedureStep
from app.db.session import SyncSessionLocal
from tests.helpers import ds_bytes, make_instance


def _new_sps(patient_id="MRN-WL-001", sps_id="SPS-TEST-1", patient_name=None, **kw):
    with SyncSessionLocal() as s:
        patient = s.query(Patient).filter(Patient.patient_id == patient_id).one_or_none()
        if patient is None:
            patient = Patient(patient_id=patient_id,
                              patient_name=patient_name or "Worklist^Patient",
                              patient_birth_date="19750505", patient_sex="F",
                              source="his")
            s.add(patient)
            s.flush()
        elif patient_name:
            patient.patient_name = patient_name
        step = ScheduledProcedureStep(
            patient_id=patient.id,
            sps_id=sps_id,
            accession_number=kw.get("accession_number", "ACC-WL-1"),
            modality=kw.get("modality", "CT"),
            scheduled_station_aet=kw.get("station", "CT1"),
            start_date=kw.get("start_date", "20260805"),
            start_time=kw.get("start_time", "090000"),
            status=kw.get("status", "SCHEDULED"),
            requested_procedure_id=kw.get("rp_id", "RP-WL-1"),
            requested_procedure_description=kw.get("rp_desc", "CT CHEST W CONTRAST"),
            scheduled_performing_physician=kw.get("physician", "Radiologist^Anna"),
        )
        s.add(step)
        s.commit()
        return step.id


def _wl_query(**kw):
    """Build a worklist C-FIND identifier like a real modality console sends."""
    query = Dataset()
    seq = Dataset()
    seq.ScheduledStationAETitle = kw.get("station", "CT1")
    seq.ScheduledProcedureStepStartDate = kw.get("date", "20260805")
    seq.Modality = kw.get("modality", "CT")
    # empty values = return these fields in every match
    seq.ScheduledProcedureStepID = ""
    seq.ScheduledProcedureStepStartTime = ""
    seq.ScheduledProcedureStepStatus = ""
    query.ScheduledProcedureStepSequence = [seq]
    query.PatientName = ""
    query.PatientID = ""
    query.PatientBirthDate = ""
    query.PatientSex = ""
    query.AccessionNumber = ""
    query.RequestedProcedureID = ""
    query.RequestedProcedureDescription = ""
    if kw.get("patient_name_filter"):
        query.PatientName = kw["patient_name_filter"]
    return query


def _mwl_find(port, query, ae_title="PAXS_TEST_SCP"):
    """Run a real DIMSE C-FIND against the receiver using the worklist model."""
    ae = AE(ae_title="PAXS_WL_SCU")
    ae.add_requested_context(ModalityWorklistInformationFind, ExplicitVRLittleEndian)
    assoc = ae.associate("127.0.0.1", port, ae_title=ae_title)
    assert assoc.is_established
    results = []
    statuses = []
    for status, identifier in assoc.send_c_find(query, ModalityWorklistInformationFind):
        if identifier is not None:
            results.append(identifier)
        if status is not None and status.Status is not None:
            statuses.append(int(status.Status))
    assoc.release()
    return statuses, results


# --------------------------------------------------------------------------- #
# Real DIMSE MWL C-FIND
# --------------------------------------------------------------------------- #
def test_mwl_c_find_returns_scheduled_steps(receiver):
    port, _ = receiver
    _new_sps()

    statuses, results = _mwl_find(port, _wl_query())
    assert statuses and statuses[-1] == 0x0000
    assert len(results) == 1

    rsp = results[0]
    assert rsp.ScheduledProcedureStepSequence[0].ScheduledProcedureStepID == "SPS-TEST-1"
    # Patient demographics come live from the patients table (HIS data).
    assert rsp.PatientID == "MRN-WL-001"
    assert rsp.PatientName == "Worklist^Patient"
    assert rsp.PatientBirthDate == "19750505"
    assert rsp.PatientSex == "F"
    assert rsp.AccessionNumber == "ACC-WL-1"


def test_mwl_c_find_station_and_modality_match(receiver):
    port, _ = receiver
    _new_sps(sps_id="SPS-CT1", station="CT1", modality="CT")
    _new_sps(sps_id="SPS-MR1", patient_id="MRN-WL-002", station="MR1", modality="MR")

    # Only CT1's CT worklist is returned.
    statuses, results = _mwl_find(port, _wl_query(station="CT1", modality="CT"))
    assert len(results) == 1
    assert results[0].ScheduledProcedureStepSequence[0].ScheduledProcedureStepID == "SPS-CT1"

    # A station with nothing scheduled gets an empty (success) result.
    statuses, results = _mwl_find(port, _wl_query(station="US1"))
    assert statuses[-1] == 0x0000
    assert results == []


def test_mwl_c_find_wildcard_patient_name(receiver):
    port, _ = receiver
    _new_sps(sps_id="SPS-ABC", patient_id="MRN-WL-003", patient_name="Adams^Zoe")
    _new_sps(sps_id="SPS-DFG", patient_id="MRN-WL-004", patient_name="Brown^Bill")

    query = _wl_query()
    query.PatientName = "A*"
    statuses, results = _mwl_find(port, query)
    assert len(results) == 1
    assert results[0].PatientName == "Adams^Zoe"


def test_mwl_c_find_hides_canceled(receiver):
    port, _ = receiver
    _new_sps(sps_id="SPS-OK")
    _new_sps(sps_id="SPS-CX", status="CANCELED")

    _, results = _mwl_find(port, _wl_query())
    assert [r.ScheduledProcedureStepSequence[0].ScheduledProcedureStepID for r in results] == ["SPS-OK"]


def test_qr_c_find_still_works_alongside_mwl(receiver):
    """The Q/R C-FIND on the same port is unaffected by the MWL context."""
    from pynetdicom.sop_class import StudyRootQueryRetrieveInformationModelFind

    ds = make_instance(study_uid="1.2.840.113619.2.408.91", series_uid="1.2.3.4.91",
                       sop_uid="1.2.3.5.91.1")
    from tests.test_receiver import _store_via_scu
    assert _store_via_scu("127.0.0.1", receiver[0], [ds_bytes(ds)]) == [0x0000]
    for _ in range(50):
        with SyncSessionLocal() as s:
            if s.execute(select(Patient)).scalars().all():
                break
        time.sleep(0.1)

    ae = AE(ae_title="PAXS_QR_SCU")
    ae.add_requested_context(StudyRootQueryRetrieveInformationModelFind, ExplicitVRLittleEndian)
    assoc = ae.associate("127.0.0.1", receiver[0], ae_title="PAXS_TEST_SCP")
    assert assoc.is_established
    query = Dataset()
    query.QueryRetrieveLevel = "STUDY"
    query.StudyInstanceUID = "1.2.840.113619.2.408.91"
    query.PatientName = ""
    rows = [i for _, i in assoc.send_c_find(query, StudyRootQueryRetrieveInformationModelFind) if i is not None]
    assoc.release()
    assert len(rows) == 1
    assert rows[0].StudyInstanceUID == "1.2.840.113619.2.408.91"


# --------------------------------------------------------------------------- #
# HL7 ADT auto-scheduling
# --------------------------------------------------------------------------- #
ADT_A01_WL = (
    "MSH|^~\\&|HIS|HOSPITAL|PAXS|ARCHIVE|20260101120000||ADT^A01^ADT_A01|MSG-WL|P|2.3\r"
    "PID|1||MRN-WL-ADT^^^HOSPITAL^MR||Hassan^Ali^^^||19820910|M|||1 St^^City^^||0112345678|||||||||||||||||||||\r"
    "PV1|1|I|ER^\r"
)

def _send_mllp(port: int, payload: bytes):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"\x0b" + payload + b"\x1c\x0d")
        time.sleep(0.3)


def test_adt_a01_opens_default_worklist_step():
    from app.dicom.hl7 import _upsert_patient, parse_adt_message

    with SyncSessionLocal() as s:
        s.query(ScheduledProcedureStep).delete()
        s.query(Patient).filter(Patient.patient_id == "MRN-WL-ADT").delete()
        s.commit()

    _upsert_patient(parse_adt_message(ADT_A01_WL))

    with SyncSessionLocal() as s:
        patient = s.query(Patient).filter(Patient.patient_id == "MRN-WL-ADT").one()
        assert patient.source == "his"
        steps = (s.query(ScheduledProcedureStep)
                 .filter(ScheduledProcedureStep.patient_id == patient.id).all())
        assert len(steps) == 1
        assert steps[0].status == "SCHEDULED"

        # Re-sending the same admit must NOT open a second step (idempotent).
        _upsert_patient(parse_adt_message(ADT_A01_WL))
        s.expire_all()
        assert s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.patient_id == patient.id).count() == 1


def test_adt_update_trigger_does_not_open_step():
    from app.dicom.hl7 import _upsert_patient, parse_adt_message

    msg = ADT_A01_WL.replace("ADT^A01^ADT_A01", "ADT^A08^ADT_A08")
    with SyncSessionLocal() as s:
        s.query(Patient).filter(Patient.patient_id == "MRN-WL-ADT").delete()
        s.query(ScheduledProcedureStep).delete()
        s.commit()

    _upsert_patient(parse_adt_message(msg))
    with SyncSessionLocal() as s:
        patient = s.query(Patient).filter(Patient.patient_id == "MRN-WL-ADT").one()
        assert s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.patient_id == patient.id).count() == 0


def test_adt_patient_appears_on_modality_worklist(receiver):
    """End-to-end: HL7 admits a patient -> a modality querying MWL sees them
    immediately with HIS demographics."""
    from datetime import datetime

    from app.dicom.hl7 import _upsert_patient, parse_adt_message

    _upsert_patient(parse_adt_message(ADT_A01_WL))

    port, _ = receiver
    # The auto-opened step is dated *today* (the ADT listener uses now()); the
    # query date must follow, not be hardcoded, or this test breaks at midnight.
    _, results = _mwl_find(
        port, _wl_query(station="CT1", date=datetime.now().strftime("%Y%m%d"))
    )
    matches = [r for r in results if r.PatientID == "MRN-WL-ADT"]
    assert len(matches) == 1
    # The HL7 parser normalises PID-5 to a display string (space-joined).
    assert matches[0].PatientName == "Hassan Ali"
    assert matches[0].PatientBirthDate == "19820910"


# --------------------------------------------------------------------------- #
# Auto-completion on image arrival
# --------------------------------------------------------------------------- #
def test_c_store_completes_worklist_step(receiver):
    """Images for the scheduled patient arrive (C-STORE): the open step flips
    to COMPLETED and links the study."""
    from tests.test_receiver import _store_via_scu

    with SyncSessionLocal() as s:
        s.query(ScheduledProcedureStep).delete()
        s.query(Patient).filter(Patient.patient_id == "MRN-WL-999").delete()
        s.commit()
    _new_sps(patient_id="MRN-WL-999", sps_id="SPS-COMPLETE", accession_number=None,
             modality="CT", station="CT1")

    port, _ = receiver
    ds = make_instance(patient_id="MRN-WL-999", patient_name="Worklist^Patient",
                       study_uid="1.2.840.113619.2.408.92", series_uid="1.2.3.4.92",
                       sop_uid="1.2.3.5.92.1", accession="ACC-WL-92")
    assert _store_via_scu("127.0.0.1", port, [ds_bytes(ds)]) == [0x0000]

    for _ in range(50):
        with SyncSessionLocal() as s:
            step = s.query(ScheduledProcedureStep).filter(
                ScheduledProcedureStep.sps_id == "SPS-COMPLETE").one_or_none()
            if step and step.status == "COMPLETED":
                break
            time.sleep(0.1)
    with SyncSessionLocal() as s:
        step = s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.sps_id == "SPS-COMPLETE").one()
        assert step.status == "COMPLETED"
        assert step.study_instance_uid == "1.2.840.113619.2.408.92"
        assert step.performed_at is not None


def test_accession_links_specific_step(receiver):
    """Two open steps for the same patient: the one matching the accession is
    the one completed."""
    from tests.test_receiver import _store_via_scu

    _new_sps(patient_id="MRN-WL-001", sps_id="SPS-ACC", accession_number="ACC-WL-77",
             station="CT1")
    _new_sps(patient_id="MRN-WL-001", sps_id="SPS-NOACC", accession_number=None,
             station="CT2")

    port, _ = receiver
    ds = make_instance(patient_id="MRN-WL-001", patient_name="Worklist^Patient",
                       study_uid="1.2.840.113619.2.408.93", series_uid="1.2.3.4.93",
                       sop_uid="1.2.3.5.93.1", accession="ACC-WL-77")
    assert _store_via_scu("127.0.0.1", port, [ds_bytes(ds)]) == [0x0000]

    for _ in range(50):
        with SyncSessionLocal() as s:
            step = s.query(ScheduledProcedureStep).filter(
                ScheduledProcedureStep.sps_id == "SPS-ACC").one_or_none()
            if step and step.status == "COMPLETED":
                break
            time.sleep(0.1)
    with SyncSessionLocal() as s:
        assert s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.sps_id == "SPS-ACC").one().status == "COMPLETED"
        assert s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.sps_id == "SPS-NOACC").one().status == "SCHEDULED"


# --------------------------------------------------------------------------- #
# REST management API
# --------------------------------------------------------------------------- #
def _admin_headers(client):
    client.post("/api/v1/auth/register", json={"username": "wlboss", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "wlboss", "password": "secret123"})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def _tech_headers(client):
    client.post("/api/v1/auth/register", json={"username": "tech1", "password": "secret123",
                                               "role": "TECHNOLOGIST"})
    res = client.post("/api/v1/auth/login", json={"username": "tech1", "password": "secret123"})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


def test_worklist_api_crud(client):
    h = _admin_headers(client)
    # List is empty
    assert client.get("/api/v1/worklist", headers=h).json() == []

    # Create for a new patient (auto-creates the patient row)
    r = client.post("/api/v1/worklist", headers=h, json={
        "patient_id": "MRN-WL-API", "patient_name": "Api^Patient",
        "modality": "CT", "scheduled_station_aet": "CT1",
        "start_date": "20260805", "start_time": "100000",
        "accession_number": "ACC-WL-API",
        "requested_procedure_description": "CT CHEST",
    })
    assert r.status_code == 201
    body = r.json()
    assert body["status"] == "SCHEDULED"
    assert body["patient_name"] == "Api^Patient"
    step_id = body["id"]

    # Unknown patient without a name -> 404
    r = client.post("/api/v1/worklist", headers=h, json={"patient_id": "GHOST"})
    assert r.status_code == 404

    # Update status -> COMPLETED
    r = client.patch(f"/api/v1/worklist/{step_id}", headers=h, json={"status": "COMPLETED"})
    assert r.status_code == 200 and r.json()["status"] == "COMPLETED"

    # Filter by status
    r = client.get("/api/v1/worklist?status=COMPLETED", headers=h)
    assert len(r.json()) == 1
    r = client.get("/api/v1/worklist?status=SCHEDULED", headers=h)
    assert r.json() == []

    # Delete
    assert client.delete(f"/api/v1/worklist/{step_id}", headers=h).status_code == 204
    assert client.get("/api/v1/worklist", headers=h).json() == []


def test_worklist_api_role_gating(client):
    h = _admin_headers(client)
    tech = _tech_headers(client)

    # A TECHNOLOGIST can create (booking clerk), a PHYSICIAN cannot.
    r = client.post("/api/v1/worklist", headers=tech, json={
        "patient_id": "MRN-WL-TECH", "patient_name": "Tech^Patient", "modality": "MR",
    })
    assert r.status_code == 201
    step_id = r.json()["id"]

    client.post("/api/v1/auth/register", json={"username": "drx", "password": "secret123"})
    res = client.post("/api/v1/auth/login", json={"username": "drx", "password": "secret123"})
    doctor = {"Authorization": f"Bearer {res.json()['access_token']}"}
    r = client.post("/api/v1/worklist", headers=doctor, json={
        "patient_id": "MRN-WL-NOPE", "patient_name": "Nope^Patient",
    })
    assert r.status_code == 403

    # Technologist can update but not delete (admin only)
    r = client.patch(f"/api/v1/worklist/{step_id}", headers=tech, json={"status": "CANCELED"})
    assert r.status_code == 200
    r = client.delete(f"/api/v1/worklist/{step_id}", headers=tech)
    assert r.status_code == 403
    assert client.delete(f"/api/v1/worklist/{step_id}", headers=h).status_code == 204


def test_worklist_api_requires_auth(client):
    assert client.get("/api/v1/worklist").status_code == 401
