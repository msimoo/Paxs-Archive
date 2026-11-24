"""Bounded read-only DICOM association and C-ECHO monitoring."""

from __future__ import annotations

from uuid import uuid4

from pydicom.dataset import Dataset
from pynetdicom import AE
from pynetdicom.sop_class import (
    CTImageStorage, ModalityPerformedProcedureStep, ModalityWorklistInformationFind,
    StorageCommitmentPushModel, StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelGet, StudyRootQueryRetrieveInformationModelMove,
    Verification,
)

from paxs_control.settings import ControlSettings

CONTEXTS = {
    "C-STORE (CT)": CTImageStorage,
    "C-FIND": StudyRootQueryRetrieveInformationModelFind,
    "C-GET": StudyRootQueryRetrieveInformationModelGet,
    "C-MOVE": StudyRootQueryRetrieveInformationModelMove,
    "MWL": ModalityWorklistInformationFind,
    "MPPS": ModalityPerformedProcedureStep,
    "Storage Commitment": StorageCommitmentPushModel,
    "C-ECHO": Verification,
}
REQUIRED_PROTOCOLS = ("C-ECHO", "C-STORE", "C-FIND", "C-GET", "C-MOVE", "MWL", "MPPS", "Storage Commitment")


def overall_dicom_status(results: dict[str, str]) -> str:
    """Report complete success only after each required operation has passed."""
    if any(value.startswith(("FAILED", "ASSOCIATION FAILED", "PROBE ERROR", "NOT NEGOTIATED"))
           for value in results.values()):
        return "FAILED"
    if all(results.get(name) == "OK" for name in REQUIRED_PROTOCOLS):
        return "ALL OK"
    return "INCOMPLETE"


def verify_empty_query(association, *, worklist: bool) -> str:
    """Send a unique synthetic query and require a successful empty response."""
    query = Dataset()
    if worklist:
        query.PatientID = f"PAXS-{uuid4().hex[:12]}"
        syntax = ModalityWorklistInformationFind
    else:
        query.QueryRetrieveLevel = "STUDY"
        query.StudyInstanceUID = f"2.25.{uuid4().int}"
        syntax = StudyRootQueryRetrieveInformationModelFind
    for status, identifier in association.send_c_find(query, syntax):
        if not status or not hasattr(status, "Status"):
            return "FAILED (NO RESPONSE)"
        if status.Status in (0xFF00, 0xFF01) or identifier is not None:
            association.abort()
            return "FAILED (UNEXPECTED MATCH)"
        return "OK (EMPTY QUERY)" if status.Status == 0 else "FAILED"
    return "FAILED (NO FINAL RESPONSE)"


def probe_dicom(settings: ControlSettings) -> dict[str, str]:
    """Negotiate service contexts and verify C-ECHO; never issue clinical writes."""
    result = {name: "NOT TESTED" for name in CONTEXTS}
    # C-STORE is a family of SOP classes; CT negotiation cannot establish all storage support.
    result["C-STORE"] = "NOT TESTED"
    association = None
    try:
        ae = AE(ae_title="PAXS_CONTROL")
        ae.acse_timeout = ae.network_timeout = ae.dimse_timeout = 3
        for context in CONTEXTS.values():
            ae.add_requested_context(context)
        association = ae.associate(settings.dicom_host, settings.dicom_port, ae_title=settings.ae_title)
        if not association.is_established:
            result["C-ECHO"] = "ASSOCIATION FAILED"
            return result
        accepted = {str(context.abstract_syntax) for context in association.accepted_contexts}
        for name, syntax in CONTEXTS.items():
            result[name] = "NEGOTIATED (NOT TESTED)" if str(syntax) in accepted else "NOT NEGOTIATED"
        if str(Verification) in accepted:
            status = association.send_c_echo()
            result["C-ECHO"] = "OK" if status and status.Status == 0 else "FAILED"
        if str(StudyRootQueryRetrieveInformationModelFind) in accepted:
            result["C-FIND"] = verify_empty_query(association, worklist=False)
        if association.is_established and str(ModalityWorklistInformationFind) in accepted:
            result["MWL"] = verify_empty_query(association, worklist=True)
    except (OSError, RuntimeError, ValueError):
        result["C-ECHO"] = "PROBE ERROR"
    finally:
        if association is not None and association.is_established:
            association.release()
    return result
