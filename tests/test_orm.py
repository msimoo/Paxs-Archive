"""HL7 v2 ORM (Order Entry) -> Modality Worklist tests — milestone E1.

Covers the Interop write-path: an ORM^O01 imaging order from the RIS becomes a
SCHEDULED worklist step automatically (NW creates, XO updates, CA/OC/DC/CR
cancel), visible to modalities over real DIMSE MWL C-FIND — including the MLLP
listener path and the dashboard label update.
"""

import socket
import time

from app.dicom.hl7 import (
    HL7Listener,
    _upsert_order,
    parse_orm_message,
)
from app.db.models import Patient, ScheduledProcedureStep
from app.db.session import SyncSessionLocal


# --------------------------------------------------------------------------- #
# Message builder (HL7 v2.3 radiology ORM^O01)
# --------------------------------------------------------------------------- #
def _seg(name: str, *fields: str) -> str:
    return name + "|" + "|".join(fields) + "\r"


def _orm(control="NW", mrn="MRN-ORM-1", name="Salem^Nour", filler="ACC-ORM-1",
         placer="ORD-ORM-1", service="CT CHEST^CT CHEST W CONTRAST",
         modality="CT", dt="20260805103000", provider="1001^Hany^Dr", zds=True):
    msh = _seg("MSH", "^~\\&", "RIS", "HOSPITAL", "PAXS", "ARCHIVE",
               "20260805090000", "", "ORM^O01^ORM_O01", "MSG-ORM-1", "P", "2.3")
    pid = _seg("PID", "1", "", f"{mrn}^^^HOSPITAL^MR", "", f"{name}^^^", "",
               "19770512", "M", "", "2 St^^City^^", "", "0115556677")
    # ORC-1 control, ORC-2 placer, ORC-3 filler, ORC-12 ordering provider.
    orc = _seg("ORC", control, placer, filler, "", "", "", "", "", "", "", "",
               provider)
    # OBR-2 placer, OBR-3 filler, OBR-4 service, OBR-6 requested date-time,
    # OBR-16 ordering provider.
    obr = _seg("OBR", "1", placer, filler, service, "", dt, "", "", "", "", "",
               "", "", "", "", provider)
    zds_seg = _seg("ZDS", modality, "CT CHEST W CONTRAST", "CT1") if zds else "\r"
    return msh + pid + orc + obr + zds_seg


def _send_mllp(port: int, payload: bytes):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(b"\x0b" + payload + b"\x1c\x0d")
        time.sleep(0.3)


def _clean(mrn="MRN-ORM-1"):
    with SyncSessionLocal() as s:
        patient = s.query(Patient).filter(Patient.patient_id == mrn).one_or_none()
        if patient is not None:
            for step in (s.query(ScheduledProcedureStep)
                         .filter(ScheduledProcedureStep.patient_id == patient.id).all()):
                s.delete(step)
            s.delete(patient)
        s.commit()


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #
def test_parse_orm_new_order():
    data = parse_orm_message(_orm())
    assert data is not None
    assert data["family"] == "ORM"
    assert data["control"] == "NW"
    assert data["patient_id"] == "MRN-ORM-1"
    assert data["name"] == "Salem Nour"
    assert data["filler_order"] == "ACC-ORM-1"     # -> Accession Number
    assert data["placer_order"] == "ORD-ORM-1"     # -> Requested Procedure ID
    assert data["procedure_code"] == "CT CHEST"
    assert data["procedure_description"] == "CT CHEST W CONTRAST"
    assert data["modality"] == "CT"                # ZDS-1
    assert data["start_date"] == "20260805"
    assert data["start_time"] == "103000"
    assert data["ordering_provider"] == "Hany^Dr"


def test_parse_orm_ignores_non_orm():
    assert parse_orm_message("ORU^R01|...") is None
    assert parse_orm_message("") is None
    # An ADT message is not an ORM order.
    from tests.test_worklist import ADT_A01_WL
    assert parse_orm_message(ADT_A01_WL) is None


def test_parse_orm_without_pid_returns_none():
    msg = ("MSH|^~\\&|RIS|HOSPITAL|PAXS|ARCHIVE|20260805090000||ORM^O01^ORM_O01|M2|P|2.3\r"
           "ORC|NW|ORD-2|ACC-2\r")
    assert parse_orm_message(msg) is None


# --------------------------------------------------------------------------- #
# Create / update / cancel
# --------------------------------------------------------------------------- #
def test_orm_new_order_creates_worklist_step():
    _clean()
    _upsert_order(parse_orm_message(_orm()))

    with SyncSessionLocal() as s:
        patient = s.query(Patient).filter(Patient.patient_id == "MRN-ORM-1").one()
        assert patient.source == "his"          # patient registered like ADT
        steps = (s.query(ScheduledProcedureStep)
                 .filter(ScheduledProcedureStep.patient_id == patient.id).all())
        assert len(steps) == 1
        step = steps[0]
        assert step.status == "SCHEDULED"
        assert step.accession_number == "ACC-ORM-1"
        assert step.requested_procedure_id == "ORD-ORM-1"
        assert step.requested_procedure_description == "CT CHEST W CONTRAST"
        assert step.modality == "CT"
        assert step.start_date == "20260805"
        assert step.scheduled_performing_physician == "Hany^Dr"
        assert step.study_instance_uid is None


def test_orm_resend_is_idempotent():
    """Re-sending the same NW order (same accession) must not duplicate."""
    _clean()
    _upsert_order(parse_orm_message(_orm()))
    _upsert_order(parse_orm_message(_orm()))

    with SyncSessionLocal() as s:
        patient = s.query(Patient).filter(Patient.patient_id == "MRN-ORM-1").one()
        count = (s.query(ScheduledProcedureStep)
                 .filter(ScheduledProcedureStep.patient_id == patient.id).count())
        assert count == 1


def test_orm_change_order_updates_step():
    """XO (change order) updates the matching open step instead of creating a
    new one — even when the procedure text changes."""
    _clean()
    _upsert_order(parse_orm_message(_orm()))

    changed = _orm(control="XO", service="CT CHEST^CT CHEST WO CONTRAST")
    _upsert_order(parse_orm_message(changed))

    with SyncSessionLocal() as s:
        patient = s.query(Patient).filter(Patient.patient_id == "MRN-ORM-1").one()
        steps = (s.query(ScheduledProcedureStep)
                 .filter(ScheduledProcedureStep.patient_id == patient.id).all())
        assert len(steps) == 1
        assert steps[0].status == "SCHEDULED"
        assert steps[0].requested_procedure_description == "CT CHEST WO CONTRAST"


def test_orm_cancel_marks_step_canceled():
    _clean()
    _upsert_order(parse_orm_message(_orm()))
    _upsert_order(parse_orm_message(_orm(control="CA")))

    with SyncSessionLocal() as s:
        step = (s.query(ScheduledProcedureStep)
                .filter(ScheduledProcedureStep.accession_number == "ACC-ORM-1").one())
        assert step.status == "CANCELED"


def test_orm_cancel_only_affects_matching_step():
    """Cancel for one accession leaves a different patient's step untouched."""
    _clean()
    _upsert_order(parse_orm_message(_orm(mrn="MRN-ORM-1")))
    _upsert_order(parse_orm_message(_orm(mrn="MRN-ORM-2", filler="ACC-OTHER")))

    _upsert_order(parse_orm_message(_orm(mrn="MRN-ORM-1", control="OC")))

    with SyncSessionLocal() as s:
        p1 = s.query(Patient).filter(Patient.patient_id == "MRN-ORM-1").one()
        assert (s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.patient_id == p1.id).one().status) == "CANCELED"
        p2 = s.query(Patient).filter(Patient.patient_id == "MRN-ORM-2").one()
        assert (s.query(ScheduledProcedureStep).filter(
            ScheduledProcedureStep.patient_id == p2.id).one().status) == "SCHEDULED"


# --------------------------------------------------------------------------- #
# Modality Worklist e2e (real DIMSE)
# --------------------------------------------------------------------------- #
def _mwl_find(port, query):
    from pynetdicom import AE
    from pynetdicom.sop_class import ModalityWorklistInformationFind
    from pydicom.uid import ExplicitVRLittleEndian

    ae = AE(ae_title="PAXS_ORM_SCU")
    ae.add_requested_context(ModalityWorklistInformationFind, ExplicitVRLittleEndian)
    assoc = ae.associate("127.0.0.1", port, ae_title="PAXS_TEST_SCP")
    assert assoc.is_established
    results = []
    for status, identifier in assoc.send_c_find(query, ModalityWorklistInformationFind):
        if identifier is not None:
            results.append(identifier)
    assoc.release()
    return results


def _wl_query(patient_id="", station="", date="20260805", modality=""):
    from pydicom.dataset import Dataset

    query = Dataset()
    seq = Dataset()
    seq.ScheduledStationAETitle = station or ""
    seq.ScheduledProcedureStepStartDate = date or ""
    seq.Modality = modality or ""
    seq.ScheduledProcedureStepID = ""
    seq.ScheduledProcedureStepStatus = ""
    query.ScheduledProcedureStepSequence = [seq]
    query.PatientID = patient_id
    query.PatientName = ""
    query.AccessionNumber = ""
    query.RequestedProcedureID = ""
    query.RequestedProcedureDescription = ""
    return query


def test_orm_order_appears_on_modality_worklist(receiver):
    """End-to-end: an ORM order from the RIS becomes visible to a modality
    querying the worklist — with accession, procedure, modality and HIS
    demographics."""
    _clean()
    _upsert_order(parse_orm_message(_orm()))

    port, _ = receiver
    results = _mwl_find(port, _wl_query(date="20260805"))
    matches = [r for r in results if r.AccessionNumber == "ACC-ORM-1"]
    assert len(matches) == 1
    rsp = matches[0]
    assert rsp.PatientID == "MRN-ORM-1"
    assert rsp.PatientName == "Salem Nour"
    assert rsp.RequestedProcedureID == "ORD-ORM-1"
    assert rsp.RequestedProcedureDescription == "CT CHEST W CONTRAST"
    step = rsp.ScheduledProcedureStepSequence[0]
    assert step.Modality == "CT"
    assert step.ScheduledProcedureStepStatus == "SCHEDULED"
    assert step.ScheduledProcedureStepStartDate == "20260805"


def test_canceled_orm_order_hidden_from_worklist(receiver):
    """A canceled order must NOT appear on the modality worklist."""
    _clean()
    _upsert_order(parse_orm_message(_orm()))
    _upsert_order(parse_orm_message(_orm(control="DC")))

    port, _ = receiver
    results = _mwl_find(port, _wl_query(date="20260805"))
    assert all(r.AccessionNumber != "ACC-ORM-1" for r in results)


def test_changed_orm_order_reflected_on_worklist(receiver):
    _clean()
    _upsert_order(parse_orm_message(_orm()))
    _upsert_order(parse_orm_message(
        _orm(control="XO", service="CT ABDOMEN^CT ABDOMEN W CONTRAST")))

    port, _ = receiver
    results = _mwl_find(port, _wl_query(date="20260805"))
    matches = [r for r in results if r.AccessionNumber == "ACC-ORM-1"]
    assert len(matches) == 1
    assert matches[0].RequestedProcedureDescription == "CT ABDOMEN W CONTRAST"


# --------------------------------------------------------------------------- #
# MLLP listener path
# --------------------------------------------------------------------------- #
def test_orm_via_mllp_listener_creates_step():
    _clean()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    listener = HL7Listener("127.0.0.1", port)
    listener.start()
    try:
        time.sleep(0.5)
        _send_mllp(port, _orm().encode("utf-8"))
        time.sleep(0.8)

        with SyncSessionLocal() as s:
            patient = s.query(Patient).filter(Patient.patient_id == "MRN-ORM-1").one()
            steps = (s.query(ScheduledProcedureStep)
                     .filter(ScheduledProcedureStep.patient_id == patient.id).all())
            assert len(steps) == 1
            assert steps[0].accession_number == "ACC-ORM-1"
            assert steps[0].status == "SCHEDULED"

        # A second identical order over MLLP stays idempotent.
        _send_mllp(port, _orm().encode("utf-8"))
        time.sleep(0.8)
        with SyncSessionLocal() as s:
            patient = s.query(Patient).filter(Patient.patient_id == "MRN-ORM-1").one()
            assert s.query(ScheduledProcedureStep).filter(
                ScheduledProcedureStep.patient_id == patient.id).count() == 1
    finally:
        listener.stop()
        listener.join(timeout=3)


def test_orm_cancel_via_mllp_listener():
    _clean()
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()

    listener = HL7Listener("127.0.0.1", port)
    listener.start()
    try:
        time.sleep(0.5)
        _send_mllp(port, _orm().encode("utf-8"))
        time.sleep(0.8)
        _send_mllp(port, _orm(control="CA").encode("utf-8"))
        time.sleep(0.8)

        with SyncSessionLocal() as s:
            step = (s.query(ScheduledProcedureStep)
                    .filter(ScheduledProcedureStep.accession_number == "ACC-ORM-1").one())
            assert step.status == "CANCELED"
    finally:
        listener.stop()
        listener.join(timeout=3)
