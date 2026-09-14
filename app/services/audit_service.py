"""Audit trail + SHA-256 tamper detection.

Every meaningful event (STORED / RETRIEVED / COMPRESSED / DELETED / SHARED)
is recorded with the file fingerprint so any tampering can be proven later.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.db.models import AuditLog
from app.services.storage_service import sha256_of


def add_audit(
    session: Session | AsyncSession,
    sop_instance_uid: str | None,
    action: str,
    performed_by: str | None = None,
    study_instance_uid: str | None = None,
    file_path: str | None = None,
    sha256: str | None = None,
) -> None:
    if sha256 is None and file_path:
        try:
            sha256 = sha256_of(file_path)
        except OSError:
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
