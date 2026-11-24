"""DICOM node registry — resolves AE titles to host/port for C-MOVE
destinations and forwarding targets.

The DICOM receiver runs handlers in threads (sync sessions), while the
management API is async, so this module exposes both access patterns.

Q-3: the registry statements live here, not in ``api/v1/nodes.py``. The router
keeps the HTTP shape (status codes, ``_node_out`` serialisation) and the DIMSE
work; every ``select``/``delete``/``flush`` it used to run is a function below.
The registry is archive-wide configuration rather than tenant-owned data — the
isolation boundary is the ADMIN gate on the mutating routes — so there is no
tenant predicate to apply here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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


async def list_nodes(db: AsyncSession) -> list[DicomNode]:
    """Every registered node, ordered by AE title (the UI's stable order)."""
    return list(
        (await db.execute(select(DicomNode).order_by(DicomNode.ae_title)))
        .scalars()
        .all()
    )


async def get_node(db: AsyncSession, ae_title: str) -> DicomNode | None:
    """Look up a node by AE title regardless of its ``enabled`` flag.

    Callers that must be enabled (ping/send/find/pull) check that themselves, so
    "disabled" stays a distinct 409 from "missing" 404.
    """
    return (
        await db.execute(select(DicomNode).where(DicomNode.ae_title == ae_title))
    ).scalar_one_or_none()


async def node_exists(db: AsyncSession, ae_title: str) -> bool:
    """True when the AE title is already registered (the 409 pre-check)."""
    return (
        await db.execute(select(DicomNode.id).where(DicomNode.ae_title == ae_title))
    ).scalar_one_or_none() is not None


async def create_node(db: AsyncSession, fields: dict[str, Any]) -> DicomNode:
    """Insert a node and reload server-generated columns.

    MySQL has no ``INSERT … RETURNING``: ``created_at``/``updated_at`` are
    server defaults, expired by the flush and unloaded until an explicit
    refresh — touching them lazily would raise ``MissingGreenlet`` in the
    async handler.
    """
    node = DicomNode(**fields)
    db.add(node)
    await db.flush()
    await db.refresh(node)
    return node


async def update_node(
    db: AsyncSession, node: DicomNode, fields: dict[str, Any]
) -> DicomNode:
    """Apply ``fields`` to ``node`` and reload the ``onupdate`` timestamps."""
    for field, value in fields.items():
        setattr(node, field, value)
    await db.flush()
    await db.refresh(node)
    return node


async def delete_node(db: AsyncSession, node: DicomNode) -> None:
    """Remove a node row (the router owns the role gate and the 404)."""
    await db.delete(node)
