"""D4: the per-user 3D view state of a study — one bounded JSON document.

The server does not interpret the document's fields: the viewer owns the shape and
versions it (`v: 1`), so a new 3D setting never needs a migration. What the server
does enforce is the *boundary*: the document must be a JSON object, and it must fit
in ``MAX_VIEW_STATE_BYTES`` after canonical re-serialisation — whitespace must not
be able to smuggle size past the cap.

Tenant scope is the study's, resolved through the same predicate every other study
query uses (FA-1). A study that is absent or belongs to another tenant is a 404,
never a 403: the caller must not learn that the UID exists elsewhere.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import StudyViewState, User
from app.services import query_service

log = logging.getLogger("paxs.view_state")

#: 8 kB is a camera, a crop box and two curves; anything larger is a bug.
MAX_VIEW_STATE_BYTES = 8192


class ViewStateNotFound(LookupError):
    """The study is absent from the caller's tenant scope."""


class ViewStateInvalid(ValueError):
    """The document is not a JSON object, or is too large to store."""


def canonical_state(raw: Any) -> str:
    """Validate a document and return its canonical text (sorted, no whitespace)."""
    if not isinstance(raw, str):
        raise ViewStateInvalid("state must be a JSON string")
    if len(raw) > MAX_VIEW_STATE_BYTES:
        raise ViewStateInvalid(f"state exceeds {MAX_VIEW_STATE_BYTES} bytes")
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ViewStateInvalid("state must be valid JSON") from exc
    if not isinstance(parsed, dict):
        raise ViewStateInvalid("state must be a JSON object")
    text = json.dumps(parsed, separators=(",", ":"), sort_keys=True)
    if len(text) > MAX_VIEW_STATE_BYTES:
        raise ViewStateInvalid(f"state exceeds {MAX_VIEW_STATE_BYTES} bytes")
    return text


async def _study(db: AsyncSession, study_uid: str, tenant_id: int | None, include_all: bool) -> Any:
    """The tenant-scoped study row, or ViewStateNotFound."""
    stmt = query_service.study_query(
        {"StudyInstanceUID": study_uid}, tenant_id=tenant_id, include_all=include_all
    )
    study = (await db.execute(stmt)).scalars().first()
    if study is None:
        raise ViewStateNotFound(study_uid)
    return study


def _payload(study_uid: str, row: StudyViewState) -> dict[str, Any]:
    return {
        "study_instance_uid": study_uid,
        "state": row.state,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
    }


async def get_view_state(
    db: AsyncSession,
    study_uid: str,
    *,
    user: User,
    tenant_id: int | None,
    include_all: bool,
) -> dict[str, Any] | None:
    """The caller's saved view for this study, or None when they never saved one."""
    study = await _study(db, study_uid, tenant_id, include_all)
    stmt = select(StudyViewState).where(
        StudyViewState.study_id == study.id, StudyViewState.user_id == user.id
    )
    row = (await db.execute(stmt)).scalars().first()
    return _payload(study_uid, row) if row is not None else None


async def put_view_state(
    db: AsyncSession,
    study_uid: str,
    *,
    user: User,
    tenant_id: int | None,
    include_all: bool,
    state: Any,
) -> dict[str, Any]:
    """Insert or replace the caller's view for this study."""
    study = await _study(db, study_uid, tenant_id, include_all)
    text = canonical_state(state)
    stmt = select(StudyViewState).where(
        StudyViewState.study_id == study.id, StudyViewState.user_id == user.id
    )
    row = (await db.execute(stmt)).scalars().first()
    if row is None:
        row = StudyViewState(study_id=study.id, user_id=user.id, state=text)
        db.add(row)
    else:
        row.state = text
    try:
        await db.commit()
    except IntegrityError:
        # Two tabs saving at once: the loser of the unique (study, user) race
        # updates the row the winner inserted instead of failing the reader's save.
        await db.rollback()
        row = (await db.execute(stmt)).scalars().first()
        if row is None:
            raise
        row.state = text
        await db.commit()
    await db.refresh(row)
    return _payload(study_uid, row)
