"""Study reading claim (plans/09 C1): who is reporting a study right now.

Without it two radiologists can open and report the same study and nothing
tells either of them. A claim is taken when the diagnostic viewer opens a
study, refreshed by a heartbeat while it stays open, and released when the
viewer closes or the report is finalized.

Rules:

* **Advisory.** A claim never blocks image access — it warns the second reader
  and lets them take over explicitly. Blocking would strand a study behind a
  crashed workstation in the middle of an on-call shift.
* **Atomic.** Taking a claim is one conditional ``UPDATE``; two readers opening
  the same study at the same instant cannot both win.
* **Self-healing.** A claim whose heartbeat is older than
  ``READING_CLAIM_TTL_SECONDS`` is stale and free to take (``0`` = claims never
  expire and must be released or taken over).
* **Audited.** Taking over a live claim writes ``READING_CLAIM_TAKEOVER``.

Every lookup goes through the caller's study tenant boundary.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import Study
from app.services import audit_service, query_service

log = logging.getLogger("paxs.reading_claim")


class ClaimHeld(Exception):
    """Another reader holds a live claim on the study."""

    def __init__(self, claimed_by: str, claimed_at: datetime | None):
        super().__init__(f"Study is being read by {claimed_by}")
        self.claimed_by = claimed_by
        self.claimed_at = claimed_at


@dataclass(frozen=True)
class ClaimState:
    """The claim on one study as seen by one reader."""

    study_uid: str
    claimed_by: str | None
    claimed_at: datetime | None
    active: bool
    mine: bool

    def as_dict(self) -> dict:
        return {
            "study_uid": self.study_uid,
            "claimed_by": self.claimed_by if self.active else None,
            "claimed_at": self.claimed_at.isoformat() if self.active and self.claimed_at else None,
            "active": self.active,
            "mine": self.mine,
            "ttl_seconds": settings.READING_CLAIM_TTL_SECONDS,
        }


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _stale_before(now: datetime) -> datetime | None:
    """Heartbeats older than this are stale; None when claims never expire."""
    ttl = settings.READING_CLAIM_TTL_SECONDS
    return now - timedelta(seconds=ttl) if ttl > 0 else None


def is_active(claimed_by: str | None, claimed_at: datetime | None,
              now: datetime | None = None) -> bool:
    """True while a claim exists and its heartbeat is inside the TTL."""
    if not claimed_by:
        return False
    stale = _stale_before(now or _now())
    if stale is None:
        return True
    return claimed_at is not None and claimed_at >= stale


def state_of(study: Study, username: str | None) -> ClaimState:
    active = is_active(study.claimed_by, study.claimed_at)
    return ClaimState(
        study_uid=study.study_instance_uid,
        claimed_by=study.claimed_by,
        claimed_at=study.claimed_at,
        active=active,
        mine=active and bool(username) and study.claimed_by == username,
    )


async def _study_in_scope(session: AsyncSession, study_uid: str,
                          tenant_id: int | None, include_all: bool) -> Study:
    statement = query_service.apply_study_tenant_scope(
        select(Study).where(Study.study_instance_uid == study_uid),
        tenant_id, include_all,
    )
    study = (await session.execute(statement)).scalar_one_or_none()
    if study is None:
        raise LookupError("Study not found")
    return study


async def get_claim(session: AsyncSession, study_uid: str, username: str | None, *,
                    tenant_id: int | None = None, include_all: bool = False) -> ClaimState:
    """The current claim on a study inside the caller's tenant scope."""
    return state_of(await _study_in_scope(session, study_uid, tenant_id, include_all), username)


async def claim(session: AsyncSession, study_uid: str, username: str, *,
                force: bool = False, tenant_id: int | None = None,
                include_all: bool = False) -> ClaimState:
    """Take or refresh the claim for ``username`` (the heartbeat is the same call).

    Raises :class:`ClaimHeld` when another reader holds a live claim and
    ``force`` is false. ``force`` takes it over and audits the takeover.
    """
    if not username:
        raise ValueError("A reading claim needs an authenticated user")
    study = await _study_in_scope(session, study_uid, tenant_id, include_all)
    now = _now()
    previous = study.claimed_by if is_active(study.claimed_by, study.claimed_at, now) else None

    statement = update(Study).where(Study.id == study.id)
    if not force:
        free = or_(Study.claimed_by.is_(None), Study.claimed_by == username)
        stale = _stale_before(now)
        if stale is not None:
            free = or_(free, Study.claimed_at.is_(None), Study.claimed_at < stale)
        statement = statement.where(free)
    # updated_at is pinned: a heartbeat every minute is not a change to the study.
    result = await session.execute(
        statement.values(claimed_by=username, claimed_at=now, updated_at=Study.updated_at)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(study)
    if result.rowcount == 0:
        raise ClaimHeld(study.claimed_by or "", study.claimed_at)

    if force and previous and previous != username:
        audit_service.add_audit(
            session, sop_instance_uid=None, action="READING_CLAIM_TAKEOVER",
            performed_by=username, study_instance_uid=study_uid,
        )
        log.info("Reading claim on study %s taken over", study_uid)
    return state_of(study, username)


async def release(session: AsyncSession, study_uid: str, username: str, *,
                  force: bool = False, tenant_id: int | None = None,
                  include_all: bool = False) -> bool:
    """Release the caller's claim; ``force`` (admin) clears anyone's. True if cleared."""
    study = await _study_in_scope(session, study_uid, tenant_id, include_all)
    statement = update(Study).where(Study.id == study.id, Study.claimed_by.is_not(None))
    if not force:
        statement = statement.where(Study.claimed_by == username)
    result = await session.execute(
        statement.values(claimed_by=None, claimed_at=None, updated_at=Study.updated_at)
        .execution_options(synchronize_session=False)
    )
    await session.refresh(study)
    return result.rowcount > 0


def release_on_finalize(session: Session, study_id: int) -> None:
    """A signed report ends the read: clear the claim in the finalize transaction."""
    session.execute(
        update(Study).where(Study.id == study_id, Study.claimed_by.is_not(None))
        .values(claimed_by=None, claimed_at=None, updated_at=Study.updated_at)
        .execution_options(synchronize_session=False)
    )
