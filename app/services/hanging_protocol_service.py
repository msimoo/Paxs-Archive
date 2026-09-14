"""Hanging Protocol Service.

Manages layout rules for diagnostic image viewports, matching protocols to current study modality,
body part, and user preference.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
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


async def list_protocols(session: AsyncSession) -> list[HangingProtocol]:
    return list(
        (
            await session.execute(
                select(HangingProtocol).order_by(HangingProtocol.priority.asc(), HangingProtocol.id.asc())
            )
        ).scalars().all()
    )


async def match_best_protocol(
    session: AsyncSession,
    modality: str | None = None,
    body_part: str | None = None,
    user_id: int | None = None,
) -> HangingProtocol | None:
    """Find highest priority matching hanging protocol for study + user."""
    stmt = select(HangingProtocol)
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
