"""HL7 v2 ADT parser + listener tests, incl. HIS auto-attach on the
STOW/C-STORE path (images for an HL7-registered MRN attach to that patient).
"""

import socket
import time

from app.dicom.hl7 import HL7Listener, _upsert_patient, parse_adt_message
from app.db.session import SyncSessionLocal
from app.db.models import Patient


ADT_A01 = (
    "MSH|^~\\&|HIS|HOSPITAL|PAXS|ARCHIVE|20260101120000||ADT^A01^ADT_A01|MSG001|P|2.3\r"
    "PID|1||MRN987654^^^HOSPITAL^MR||Smith^Jane^^^||19800315|F|||1 Main St^^Metro^NY^10001||555-0100|||||||||||||||||||||\r"
    "PV1|1|I|ER^"
)


def test_parse_adt_a01():
    data = parse_adt_message(ADT_A01)
    assert data is not None
    assert data["trigger"] == "A01"
    assert data["patient_id"] == "MRN987654"
    assert data["issuer"] == "HOSPITAL"
    assert data["name"] == "Smith Jane"
    assert data["birth_date"] == "19800315"
    assert data["sex"] == "F"


def test_parse_non_adt_returns_none():
    assert parse_adt_message("ORU^R01|...") is None
    assert parse_adt_message("") is None


def test_parse_adt_without_pid_returns_none():
    msg = "MSH|^~\\&|HIS|H|||20260101120000||ADT^A01^ADT_A01|M2|P|2.3"
    assert parse_adt_message(msg) is None


def _send_mllp(port: int, payload: bytes):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"\x0b" + payload + b"\x1c\x0d")
        time.sleep(0.3)


def test_listener_upserts_patient():
    with SyncSessionLocal() as s:
        s.query(Patient).filter(Patient.patient_id == "MRN987654").delete()
        s.commit()

    # Grab a free port, then start the listener on it.
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    listener = HL7Listener("127.0.0.1", port)
    listener.start()
    try:
        time.sleep(0.5)
        _send_mllp(port, ADT_A01.encode("utf-8"))
        time.sleep(0.8)

        with SyncSessionLocal() as s:
            patient = s.query(Patient).filter(Patient.patient_id == "MRN987654").one()
            assert patient.patient_name == "Smith Jane"
            assert patient.patient_birth_date == "19800315"
            assert patient.patient_sex == "F"
            # HIS provenance: stamped and phone stored.
            assert patient.source == "his"
            assert patient.patient_phone == "555-0100"

        # Idempotent: sending again must not duplicate.
        _send_mllp(port, ADT_A01.encode("utf-8"))
        time.sleep(0.8)
        with SyncSessionLocal() as s:
            count = s.query(Patient).filter(Patient.patient_id == "MRN987654").count()
            assert count == 1
    finally:
        listener.stop()
        listener.join(timeout=3)


def test_hl7_marks_existing_dicom_patient_as_his():
    """ADT arriving AFTER a modality already created the patient: the row is
    enriched with HIS demographics and stamped source=his (no duplicate)."""
    with SyncSessionLocal() as s:
        s.query(Patient).filter(Patient.patient_id == "MRN111222").delete()
        s.add(Patient(patient_id="MRN111222", patient_name="Dicom^Patient"))
        s.commit()

    _upsert_patient(parse_adt_message(ADT_A01.replace("MRN987654", "MRN111222")))

    with SyncSessionLocal() as s:
        patients = s.query(Patient).filter(Patient.patient_id == "MRN111222").all()
        assert len(patients) == 1  # no duplicate row
        p = patients[0]
        assert p.source == "his"
        assert p.patient_name == "Smith Jane"  # HIS name wins
        assert p.patient_phone == "555-0100"


def _upload_dicom_for_patient(client, patient_id: str, patient_name: str, study_uid: str):
    """STOW-RS a single synthetic instance carrying the given PatientID."""
    from tests.helpers import ds_bytes, make_instance
    from tests.test_dicomweb import _upload

    ds = make_instance(patient_id=patient_id, patient_name=patient_name,
                       study_uid=study_uid)
    _upload(client, [ds_bytes(ds)])
    return study_uid


def test_dicom_attaches_to_hl7_patient(client):
    """An image arriving (STOW-RS) for an HL7-registered MRN auto-attaches to
    the HIS patient: same patient row, source stays his, HIS name preserved."""
    # 1. HL7 registers the patient.
    _upsert_patient(parse_adt_message(ADT_A01))

    # 2. A modality pushes an image with the same MRN but a different name.
    _upload_dicom_for_patient(client, "MRN987654", "Modality^Name", "1.2.3.999.1")

    with SyncSessionLocal() as s:
        patients = s.query(Patient).filter(Patient.patient_id == "MRN987654").all()
        assert len(patients) == 1  # attached, not duplicated
        p = patients[0]
        assert p.source == "his"
        assert p.patient_name == "Smith Jane"  # HIS is authoritative
        assert p.studies[0].study_instance_uid == "1.2.3.999.1"

    # 3. The dashboard listing surfaces the HIS badge data.
    listing = client.get("/api/v1/studies?search=MRN987654").json()
    item = next(i for i in listing["items"] if i["patient_id"] == "MRN987654")
    assert item["patient_source"] == "his"

    detail = client.get("/api/v1/studies/1.2.3.999.1").json()
    assert detail["patient"]["source"] == "his"
    assert detail["patient"]["phone"] == "555-0100"


def test_dicom_only_patient_has_dicom_source(client):
    """Patients that never came through HL7 keep source=dicom and no badge."""
    _upload_dicom_for_patient(client, "MRN000001", "Doe^John", "1.2.3.888.1")

    listing = client.get("/api/v1/studies?search=MRN000001").json()
    item = next(i for i in listing["items"] if i["patient_id"] == "MRN000001")
    assert item["patient_source"] == "dicom"

    with SyncSessionLocal() as s:
        p = s.query(Patient).filter(Patient.patient_id == "MRN000001").one()
        assert p.source == "dicom"
        assert p.patient_phone is None


def test_c_store_attaches_to_hl7_patient(receiver):
    """A DIMSE C-STORE carrying the HL7-registered MRN auto-attaches to the HIS
    patient over the real receiver path — no duplicate row, HIS data preserved."""
    from tests.helpers import ds_bytes, make_instance
    from tests.test_receiver import _store_via_scu

    _upsert_patient(parse_adt_message(ADT_A01))  # HIS registers MRN987654
    port, _ = receiver
    ds = make_instance(patient_id="MRN987654", patient_name="Modality^Name",
                       study_uid="1.2.3.777.1", series_uid="1.2.3.777.1.1")
    statuses = _store_via_scu("127.0.0.1", port, [ds_bytes(ds)])
    assert statuses == [0x0000], statuses

    # The receiver indexes asynchronously — wait for it.
    for _ in range(50):
        with SyncSessionLocal() as s:
            patients = s.query(Patient).filter(Patient.patient_id == "MRN987654").all()
            if patients and patients[0].studies:
                break
        time.sleep(0.1)

    with SyncSessionLocal() as s:
        patients = s.query(Patient).filter(Patient.patient_id == "MRN987654").all()
        assert len(patients) == 1  # attached, not duplicated
        p = patients[0]
        assert p.source == "his"
        assert p.patient_name == "Smith Jane"  # HIS name survives the C-STORE
        assert any(st.study_instance_uid == "1.2.3.777.1" for st in p.studies)
