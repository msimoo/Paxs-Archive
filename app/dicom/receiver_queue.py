"""Short-lived C-STORE progress shared by the DIMSE and API threads."""

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from threading import Lock

_TERMINAL_TTL_SECONDS = 60
_MAX_TERMINAL_STUDIES = 20
_lock = Lock()


@dataclass
class _Study:
    patient_name: str
    patient_id: str
    modality: str
    calling_ae: str
    updated_at: float
    associations: set[int] = field(default_factory=set)
    pending: int = 0
    received_instances: int = 0
    bytes_received: int = 0
    failed: bool = False


_studies: dict[str, _Study] = {}
_association_studies: dict[int, set[str]] = {}
_terminal_deadlines: dict[str, float] = {}
_ASSOCIATION_CLOSED_ATTR = "_paxs_receiver_queue_closed"


def _expire_terminal_studies(now: float) -> None:
    # Deadlines are inserted chronologically and removed when a study resumes.
    while _terminal_deadlines:
        study_uid = next(iter(_terminal_deadlines))
        if len(_terminal_deadlines) <= _MAX_TERMINAL_STUDIES and _terminal_deadlines[study_uid] > now:
            break
        del _terminal_deadlines[study_uid]
        del _studies[study_uid]


def _dataset_text(dataset, keyword: str) -> str:
    try:
        return str(dataset.get(keyword) or "")
    except Exception:
        # A malformed optional tag must not influence the C-STORE result.
        return ""


def begin_store(study_uid: str, association, calling_ae: str | None, dataset) -> None:
    patient_name = _dataset_text(dataset, "PatientName")
    patient_id = _dataset_text(dataset, "PatientID")
    modality = _dataset_text(dataset, "Modality")
    assoc_id = id(association)
    with _lock:
        _expire_terminal_studies(time.monotonic())
        if _terminal_deadlines.pop(study_uid, None) is not None:
            del _studies[study_uid]
        study = _studies.get(study_uid)
        if study is None:
            study = _Study(patient_name, patient_id, modality, calling_ae or "", time.time())
            _studies[study_uid] = study
        study.pending += 1
        study.updated_at = time.time()
        # The transport thread can close the connection before the store handler reaches us.
        if getattr(association, _ASSOCIATION_CLOSED_ATTR, False):
            study.failed = True
        else:
            study.associations.add(assoc_id)
            _association_studies.setdefault(assoc_id, set()).add(study_uid)


def _update_status(study_uid: str, study: _Study) -> None:
    study.updated_at = time.time()
    if not study.associations and not study.pending:
        now = time.monotonic()
        _terminal_deadlines[study_uid] = now + _TERMINAL_TTL_SECONDS
        _expire_terminal_studies(now)


def finish_store(study_uid: str, bytes_stored: int | None) -> None:
    with _lock:
        study = _studies[study_uid]
        study.pending -= 1
        if bytes_stored is None:
            study.failed = True
        else:
            study.received_instances += 1
            study.bytes_received += bytes_stored
        _update_status(study_uid, study)


def finish_association(association, *, failed: bool) -> None:
    assoc_id = id(association)
    with _lock:
        if failed:
            setattr(association, _ASSOCIATION_CLOSED_ATTR, True)
        _expire_terminal_studies(time.monotonic())
        for study_uid in _association_studies.pop(assoc_id, ()):
            study = _studies[study_uid]
            study.associations.remove(assoc_id)
            study.failed |= failed
            _update_status(study_uid, study)


def receiver_queue_snapshot() -> list[dict]:
    """Return a detached snapshot ordered from most recently updated."""
    with _lock:
        _expire_terminal_studies(time.monotonic())
        rows = []
        for study_uid, study in _studies.items():
            status = "receiving"
            if study_uid in _terminal_deadlines:
                status = "failed" if study.failed else "completed"
            rows.append({
                "study_uid": study_uid,
                "patient_name": study.patient_name,
                "patient_id": study.patient_id,
                "modality": study.modality,
                "calling_ae": study.calling_ae,
                "received_instances": study.received_instances,
                "expected_instances": None,
                "bytes_received": study.bytes_received,
                "status": status,
                "updated_at": study.updated_at,
            })
    rows.sort(key=lambda row: row["updated_at"], reverse=True)
    for row in rows:
        row["updated_at"] = datetime.fromtimestamp(row["updated_at"], UTC).isoformat()
    return rows
