"""DICOM node registry — resolves AE titles to host/port for C-MOVE
destinations and forwarding targets.

The DICOM receiver runs handlers in threads (sync sessions), while the
management API is async, so this module exposes both access patterns.
"""

from __future__ import annotations

from sqlalchemy import select

from app.db.models import DicomNode
from app.db.session import SyncSessionLocal


def get_node_sync(ae_title: str) -> DicomNode | None:
    """Look up an enabled node by AE title (sync — receiver/worker threads)."""
    with SyncSessionLocal() as session:
        return (
            session.execute(
                select(DicomNode).where(
                    DicomNode.ae_title == ae_title, DicomNode.enabled.is_(True)
                )
            ).scalar_one_or_none()
        )



