"""PHI-free transfer result classification, independent of peer response text."""
from typing import Any

_MESSAGES = {
    'ASSOCIATION_REJECTED': 'Remote DICOM peer rejected the association. Verify called AE and allow the calling AE on the peer.',
    'ASSOCIATION_FAILED': 'DICOM association timed out or could not be established. Check the peer listener, network and TLS settings.',
    'RETRIEVE_UNSUPPORTED': 'Peer did not accept Study Root C-GET. Enable C-GET and Storage SCP role negotiation on the peer; C-ECHO alone does not verify retrieval.',
    'LOCAL_INGEST_FAILED': 'Received images could not be ingested. Check archive storage, quota and ingestion diagnostics.',
    'MOVE_RECEIVER_UNAVAILABLE': 'The dedicated DCMTK C-MOVE receiver is unavailable. Check its configuration and port.',
    'MOVE_INCOMPLETE': 'C-MOVE did not complete or the received files failed validation. Check the remote Move Destination and receiver.',
    'REMOTE_DIMSE_FAILED': 'Remote DIMSE retrieval did not finish successfully. Check the peer retrieval service and sub-operation status.',
    'REMOTE_TRANSFER_FAILED': 'Remote transfer failed before images were received. Check DIMSE association and peer retrieval permissions.',
    'NO_MATCHES': 'The peer completed the request but returned no images. Verify the selected study exists on that peer.',
    'NO_SUCCESSFUL_INSTANCES': 'No images transferred successfully. Check peer and archive ingestion diagnostics.',
}


def classify_outcome(result: dict[str, Any], completed: int, failed: int) -> tuple[str, str | None, str | None]:
    """Keep remote protocol failure distinct from a successful empty match."""
    if completed and not failed and result.get('ok'):
        return 'COMPLETED', None, None
    status = 'PARTIAL' if completed else 'FAILED'
    code = result.get('error_code')
    wire_status = result.get('status')
    if code not in _MESSAGES:
        if not result.get('ok'):
            code = 'REMOTE_DIMSE_FAILED' if isinstance(wire_status, int) else 'REMOTE_TRANSFER_FAILED'
        else:
            code = 'NO_SUCCESSFUL_INSTANCES' if failed else 'NO_MATCHES'
    message = _MESSAGES[code]
    if isinstance(wire_status, int) and 0 <= wire_status <= 65535:
        message += f' DIMSE status 0x{wire_status:04X}.'
    return status, code, message
