"""Format peer association failures without exposing DICOM query values."""

from __future__ import annotations


def association_failure_reason(
    association: object,
    called_ae_title: str,
    calling_ae_title: str,
) -> str:
    """Translate a bounded A-ASSOCIATE-RJ diagnostic into operator guidance."""
    acceptor = getattr(association, "acceptor", None)
    primitive = getattr(acceptor, "primitive", None)
    result = getattr(primitive, "result", None)
    source = getattr(primitive, "result_source", None)
    diagnostic = getattr(primitive, "diagnostic", None)

    if (result, source, diagnostic) == (1, 1, 3):
        return (
            f"Remote DICOM peer does not allow calling AE title '{calling_ae_title}'. "
            "Add it to the peer's allowed calling AE titles."
        )
    if (result, source, diagnostic) == (1, 1, 7):
        return (
            f"Remote DICOM peer does not recognize called AE title '{called_ae_title}'. "
            "Check the AE title configured on the peer."
        )
    if result is not None:
        return (
            "Remote DICOM association was rejected "
            f"(result={result}, source={source}, diagnostic={diagnostic})."
        )
    if getattr(association, "is_aborted", False):
        return (
            "DICOM association timed out or was aborted before the peer returned "
            "an accept/reject response. Verify the remote DICOM listener and "
            "transport/TLS settings; an open TCP port alone does not confirm "
            "that a DICOM association can be completed."
        )
    return "No DICOM association was established; verify peer reachability, port, and TLS settings."
