"""Hanging Protocol Service.

Manages layout rules for diagnostic image viewports, matching protocols to current study modality,
body part, and user preference.
"""

from __future__ import annotations

import logging
import json
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import HangingProtocol

log = logging.getLogger("paxs.hp")


async def create_protocol(session: AsyncSession, data: dict[str, Any]) -> HangingProtocol:
    hp = HangingProtocol(**data)
    session.add(hp)
    await session.flush()
    return hp


async def get_protocol(session: AsyncSession, protocol_id: int) -> HangingProtocol | None:
    return (
        await session.execute(
            select(HangingProtocol).where(HangingProtocol.id == protocol_id)
        )
    ).scalar_one_or_none()


async def list_protocols(
    session: AsyncSession,
    *,
    user_id: int,
    modality: str | None = None,
    body_part: str | None = None,
) -> list[HangingProtocol]:
    """List global and caller-owned protocols, optionally narrowed to study metadata."""
    stmt = select(HangingProtocol).where(
        or_(HangingProtocol.user_id.is_(None), HangingProtocol.user_id == user_id)
    )
    if modality:
        stmt = stmt.where(
            or_(HangingProtocol.modality.is_(None), func.upper(HangingProtocol.modality) == modality.upper())
        )
    if body_part:
        stmt = stmt.where(
            or_(HangingProtocol.body_part_examined.is_(None),
                func.upper(HangingProtocol.body_part_examined) == body_part.upper())
        )
    stmt = stmt.order_by(HangingProtocol.priority.asc(), HangingProtocol.id.asc())
    return list((await session.execute(stmt)).scalars().all())


async def save_user_preference(
    session: AsyncSession,
    *,
    user_id: int,
    modality: str,
    layout_rows: int,
    layout_columns: int,
) -> HangingProtocol:
    """Upsert a user's modality-specific default in the existing protocol table."""
    normalized_modality = modality.strip().upper()
    name = f"PAXS_USER_LAYOUT_{user_id}_{normalized_modality}"
    stmt = select(HangingProtocol).where(
        HangingProtocol.name == name,
        HangingProtocol.user_id == user_id,
    )
    protocol = (await session.execute(stmt)).scalar_one_or_none()
    configuration = json.dumps({
        "kind": "user-default",
        "modality": normalized_modality,
        "layout_rows": layout_rows,
        "layout_columns": layout_columns,
    }, separators=(",", ":"))
    if protocol is None:
        protocol = HangingProtocol(
            name=name,
            description=f"Personal default layout for {normalized_modality}",
            modality=normalized_modality,
            user_id=user_id,
            priority=0,
            layout_rows=layout_rows,
            layout_columns=layout_columns,
            configuration_json=configuration,
        )
        session.add(protocol)
    else:
        protocol.layout_rows = layout_rows
        protocol.layout_columns = layout_columns
        protocol.configuration_json = configuration
    await session.flush()
    return protocol


async def match_best_protocol(
    session: AsyncSession,
    modality: str | None = None,
    body_part: str | None = None,
    user_id: int | None = None,
) -> HangingProtocol | None:
    """Find highest priority matching hanging protocol for study + user."""
    stmt = select(HangingProtocol).where(
        or_(HangingProtocol.user_id.is_(None), HangingProtocol.user_id == user_id)
    )
    protocols = list((await session.execute(stmt)).scalars().all())

    best_match = None
    best_score = -1

    for hp in protocols:
        score = 0
        if hp.user_id and user_id and hp.user_id == user_id:
            score += 100
        elif hp.user_id and (not user_id or hp.user_id != user_id):
            continue

        if hp.modality and modality:
            if hp.modality.upper() == modality.upper():
                score += 50
            else:
                continue

        if hp.body_part_examined and body_part:
            if hp.body_part_examined.upper() == body_part.upper():
                score += 30
            else:
                continue

        # Adjust for defined priority (lower number = higher priority)
        score += max(0, 50 - hp.priority)

        if score > best_score:
            best_score = score
            best_match = hp

    return best_match


async def delete_protocol(session: AsyncSession, protocol_id: int) -> bool:
    hp = await get_protocol(session, protocol_id)
    if not hp:
        return False
    await session.delete(hp)
    await session.flush()
    return True
