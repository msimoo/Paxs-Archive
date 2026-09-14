"""Clinical Protocols Library (roadmap #31).

Maps study descriptions / modalities / body parts to a standardized clinical
protocol with a local RIS code. Used to:

  * auto-suggest a protocol for an incoming study (dashboards, worklists),
  * give radiologists a canonical protocol name + code instead of free-text
    study descriptions,
  * keep the library curated via the Admin → Protocols panel.

Matching (best-match, higher score wins; priority breaks ties):
  * study-description substring/alternative match      +60
  * modality match                                      +40
  * body part match                                     +30
  * (protocol disabled entries are never matched)
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import ClinicalProtocol

log = logging.getLogger("paxs.clinical_protocols")


async def create_protocol(session: AsyncSession, data: dict[str, Any]) -> ClinicalProtocol:
    """Create a protocol. Raises ValueError on duplicate name."""
    name = (data.get("name") or "").strip()
    if not name:
        raise ValueError("name is required")
    existing = (
        await session.execute(
            select(ClinicalProtocol).where(ClinicalProtocol.name == name)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise ValueError(f"Protocol '{name}' already exists")
    cp = ClinicalProtocol(**data)
    session.add(cp)
    await session.flush()
    return cp


async def get_protocol(session: AsyncSession, protocol_id: int) -> ClinicalProtocol | None:
    return (
        await session.execute(
            select(ClinicalProtocol).where(ClinicalProtocol.id == protocol_id)
        )
    ).scalar_one_or_none()


async def list_protocols(session: AsyncSession) -> list[ClinicalProtocol]:
    return list(
        (
            await session.execute(
                select(ClinicalProtocol).order_by(
                    ClinicalProtocol.priority.asc(), ClinicalProtocol.id.asc()
                )
            )
        ).scalars().all()
    )


async def update_protocol(session: AsyncSession, protocol_id: int,
                          data: dict[str, Any]) -> ClinicalProtocol | None:
    """Update editable fields. Raises ValueError on duplicate name.

    The PATCH body is built with ``exclude_unset`` so only keys the client
    actually sent are present; explicit ``None`` therefore means "clear this
    nullable field" (the frontend sends null to blank an input).
    """
    cp = await get_protocol(session, protocol_id)
    if cp is None:
        return None
    nullable = ("description", "standardized_name", "ris_code",
                "modality", "body_part_examined", "study_description_pattern")
    for field in nullable:
        if field in data:
            setattr(cp, field, data[field] or None)
    for field in ("name", "priority", "enabled"):
        if field in data and data[field] is not None:
            setattr(cp, field, data[field])
    if cp.name:
        dup = (
            await session.execute(
                select(ClinicalProtocol).where(
                    ClinicalProtocol.name == cp.name,
                    ClinicalProtocol.id != cp.id,
                )
            )
        ).scalar_one_or_none()
        if dup is not None:
            raise ValueError(f"Protocol '{cp.name}' already exists")
    return cp


async def delete_protocol(session: AsyncSession, protocol_id: int) -> bool:
    cp = await get_protocol(session, protocol_id)
    if cp is None:
        return False
    await session.delete(cp)
    return True


def _desc_matches(cp: ClinicalProtocol, description: str | None) -> bool:
    """Case-insensitive substring / comma-separated alternative match."""
    if not description or not cp.study_description_pattern:
        return False
    hay = description.casefold()
    for alt in cp.study_description_pattern.split(","):
        if alt.strip().casefold() and alt.strip().casefold() in hay:
            return True
    return False


async def match_best_protocol(session: AsyncSession,
                              study_description: str | None = None,
                              modality: str | None = None,
                              body_part: str | None = None,
                              include_disabled: bool = False) -> ClinicalProtocol | None:
    """Best protocol for the given study context (or None)."""
    stmt = select(ClinicalProtocol)
    if not include_disabled:
        stmt = stmt.where(ClinicalProtocol.enabled.is_(True))
    protocols = list((await session.execute(stmt)).scalars().all())

    best, best_score = None, -1
    for cp in protocols:
        score = 0
        if _desc_matches(cp, study_description):
            score += 60
        if modality and cp.modality and cp.modality.upper() == modality.upper():
            score += 40
        if body_part and cp.body_part_examined and \
                cp.body_part_examined.upper() == body_part.upper():
            score += 30
        if score == 0:
            continue  # no signal — don't match unrelated studies
        # Lower priority number wins ties.
        score -= cp.priority / 100.0
        if score > best_score:
            best, best_score = cp, score
    return best


async def suggest_matches(session: AsyncSession,
                          study_description: str | None = None,
                          modality: str | None = None,
                          body_part: str | None = None,
                          limit: int = 10) -> list[dict]:
    """Ranked candidate list (for the match tester UI / dashboards)."""
    stmt = select(ClinicalProtocol).where(ClinicalProtocol.enabled.is_(True))
    protocols = list((await session.execute(stmt)).scalars().all())

    scored = []
    for cp in protocols:
        score = 0
        if _desc_matches(cp, study_description):
            score += 60
        if modality and cp.modality and cp.modality.upper() == modality.upper():
            score += 40
        if body_part and cp.body_part_examined and \
                cp.body_part_examined.upper() == body_part.upper():
            score += 30
        if score:
            scored.append((score, cp))
    scored.sort(key=lambda t: (-t[0], t[1].priority, t[1].id))
    return [
        {"id": cp.id, "name": cp.name, "standardized_name": cp.standardized_name,
         "ris_code": cp.ris_code, "modality": cp.modality,
         "body_part_examined": cp.body_part_examined, "score": score}
        for score, cp in scored[:limit]
    ]
