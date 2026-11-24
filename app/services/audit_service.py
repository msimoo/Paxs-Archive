"""Audit trail + SHA-256 tamper detection.

Every meaningful event (STORED / RETRIEVED / COMPRESSED / DELETED / SHARED)
is recorded with the file fingerprint so any tampering can be proven later.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models import AuditLog
from app.services.storage.backend import StorageError
from app.services.storage_service import sha256_of

log = logging.getLogger("paxs.audit")


def add_audit(
    session: Session | AsyncSession,
    sop_instance_uid: str | None,
    action: str,
    performed_by: str | None = None,
    study_instance_uid: str | None = None,
    file_path: str | None = None,
    sha256: str | None = None,
    backend_name: str | None = None,
) -> None:
    """Append an audit row, fingerprinting ``file_path`` when it is readable.

    The fingerprint is **best effort and must never fail the caller's
    operation**. Two cases make it unreadable, and both are normal:

    * ``backend_name`` selects the store that actually holds the object. An
      archive can keep instances on several backends (``instances.storage_backend``
      is per row), so hashing against the *default* backend finds nothing and
      used to turn a legitimate delete into a 500.
    * the object may already be gone. Deleting a study is idempotent: if the
      file is absent the goal state is already reached, so the audit row is
      still written — just without a hash.

    ``StorageError`` is not an ``OSError`` subclass, which is why catching only
    ``OSError`` left every missing-object delete answering 500 with nothing
    deleted.
    """
    if sha256 is None and file_path:
        try:
            sha256 = sha256_of(file_path, backend_name=backend_name)
        except (OSError, StorageError) as exc:
            # Expected when the object is already absent (or lives on another
            # backend). Worth a line so storage/DB drift is visible, but never
            # worth failing a delete for.
            #
            # N-2 / Finding 7: name the SOP Instance UID and the exception TYPE,
            # never `file_path`. A pre-B1 archive stored absolute paths, and an
            # absolute path can carry a patient name or MRN in a directory
            # component — which is PHI, and logs are shipped off-box.
            log.warning(
                "audit fingerprint unavailable for %s (%s)",
                sop_instance_uid or "<unknown>", type(exc).__name__,
            )
            sha256 = None
    session.add(
        AuditLog(
            sop_instance_uid=sop_instance_uid,
            study_instance_uid=study_instance_uid,
            sha256_hash=sha256,
            action=action.upper(),
            performed_by=performed_by,
        )
    )


# --------------------------------------------------------------------------- #
# H-05: study-view access audit, coalesced per (actor, study, window)
# --------------------------------------------------------------------------- #
#: One STUDY_VIEWED row per actor and study inside this window, never one per frame.
STUDY_VIEW_WINDOW_SECONDS = 300.0
#: N-5 bound on the in-memory debounce; the oldest key is evicted when full.
STUDY_VIEW_DEBOUNCE_MAX_ENTRIES = 10_000

_study_view_lock = threading.Lock()
_study_view_seen: OrderedDict[tuple[str, str], float] = OrderedDict()


def should_record_study_view(actor_key: str, study_uid: str, *,
                             now: float | None = None) -> bool:
    """True the first time ``(actor_key, study_uid)`` is seen inside the window.

    In-memory and per process, so each worker may write one row per window —
    still bounded, and a viewer's hundreds of frame fetches cost no DB writes.
    """
    moment = time.monotonic() if now is None else now
    key = (actor_key, study_uid)
    with _study_view_lock:
        last = _study_view_seen.get(key)
        if last is not None and moment - last < STUDY_VIEW_WINDOW_SECONDS:
            return False
        _study_view_seen[key] = moment
        _study_view_seen.move_to_end(key)
        while len(_study_view_seen) > STUDY_VIEW_DEBOUNCE_MAX_ENTRIES:
            _study_view_seen.popitem(last=False)
        return True


def reset_study_view_debounce() -> None:
    """Forget every recorded view (tests)."""
    with _study_view_lock:
        _study_view_seen.clear()


async def record_study_view(session: AsyncSession, *, actor_key: str, study_uid: str,
                            user_id: int | None = None,
                            client_ip: str | None = None) -> bool:
    """Write one STUDY_VIEWED row unless this actor viewed this study recently.

    ``actor_key`` names the identity (``user:<id>``, ``share``, ``anonymous``)
    and is stored with the client IP in ``performed_by`` — no PHI, only
    identifiers. Commits its own session; returns whether a row was written.
    """
    if not should_record_study_view(actor_key, study_uid):
        return False
    performed_by = actor_key if not client_ip else f"{actor_key} ip={client_ip}"
    session.add(
        AuditLog(
            user_id=user_id,
            study_instance_uid=study_uid,
            action="STUDY_VIEWED",
            performed_by=performed_by[:128],
        )
    )
    await session.commit()
    return True
