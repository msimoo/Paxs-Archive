"""Authentication persistence: accounts, sessions and the auth audit trail (Q-3).

``api/v1/auth.py`` used to build its own ``select``/``update`` statements, which
put SQL in the routing layer and made the login/refresh flows impossible to
review as *policy* — every session read was interleaved with a query. The
statements live here now; the router keeps the HTTP shape (status codes,
cookies, tokens) and the lockout/theft-detection decisions.

Why the decisions stayed in the router: they are one transaction with the
response. ``login`` must commit a failed-attempt audit row *before* raising, and
``refresh`` must commit a family revocation before answering 401. Splitting a
"begin transaction / return a verdict" protocol across two modules would make
that ordering invisible at the call site, which is exactly the class of bug the
audit trail exists to catch.

There is no tenant predicate here on purpose: accounts and sessions are
archive-wide rows gated by authentication itself, not tenant-owned data.
"""

from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AuthAuditLog, AuthSource, Session, User


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
async def username_taken(db: AsyncSession, username: str) -> bool:
    """True when the username is already registered (the 409 pre-check)."""
    return (await db.execute(
        select(User.id).where(User.username == username)
    )).scalar_one_or_none() is not None


async def account_count(db: AsyncSession) -> int:
    """Total accounts — how ``register`` decides whether this is the bootstrap.

    An ``ADMIN … FOR UPDATE`` variant is deliberately *not* used: the bootstrap
    decision is already gated by ``SETUP_TOKEN`` (B-4), so serializing the count
    would buy nothing here.
    """
    return (await db.execute(select(func.count(User.id)))).scalar_one()


async def create_local_account(
    db: AsyncSession,
    *,
    username: str,
    password_hash: str,
    full_name: str | None,
    role: str,
) -> User:
    """Insert a LOCAL account and reload its server-generated columns.

    MySQL has no ``INSERT … RETURNING``: ``created_at`` is expired by the flush
    and accessing it lazily raises ``MissingGreenlet`` in the async handler, so
    the refresh happens here rather than at first use.
    """
    user = User(
        username=username,
        password_hash=password_hash,
        full_name=full_name,
        role=role,
        auth_source=AuthSource.LOCAL,
        must_change_password=False,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def load_account(db: AsyncSession, user_id: int) -> User | None:
    """The account row with this primary key (identity map may answer it)."""
    return await db.get(User, user_id)


# --------------------------------------------------------------------------- #
# Auth audit (plan §3.3)
# --------------------------------------------------------------------------- #
async def record_event(
    db: AsyncSession,
    *,
    user_id: int | None,
    action: str,
    ip_address: str | None = None,
    user_agent: str | None = None,
    details: dict | None = None,
) -> None:
    """Append one ``auth_audit_log`` row.

    ``details`` is small JSON and never PHI — callers pass reason tokens,
    prefixes and counts only. The caller commits: several flows must commit this
    row *before* raising, and ``get_db`` rolls back on exception.
    """
    db.add(AuthAuditLog(
        user_id=user_id,
        action=action,
        ip_address=ip_address,
        user_agent=user_agent,
        details=json.dumps(details) if details else None,
    ))


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
async def find_session(db: AsyncSession, token_hash: str) -> Session | None:
    """The session a refresh token hashes to (``None`` when absent)."""
    return (await db.execute(
        select(Session).where(Session.refresh_token_hash == token_hash)
    )).scalar_one_or_none()


async def sessions_in_family(db: AsyncSession, family_id: str) -> list[Session]:
    """Every session sharing a refresh-token family (theft response)."""
    return list((await db.execute(
        select(Session).where(Session.family_id == family_id)
    )).scalars().all())


async def open_session(
    db: AsyncSession,
    *,
    session_id: str,
    user_id: int,
    refresh_token_hash: str,
    family_id: str,
    ip_address: str | None,
    user_agent: str | None,
    expires_at: datetime,
) -> None:
    """Insert a session row for a freshly issued refresh token."""
    db.add(Session(
        id=session_id,
        user_id=user_id,
        refresh_token_hash=refresh_token_hash,
        family_id=family_id,
        ip_address=ip_address,
        user_agent=user_agent,
        expires_at=expires_at,
    ))


def mark_revoked(session: Session, reason: str) -> None:
    """Flag one session revoked (an attribute write, not a statement).

    Kept beside the session queries so "how a session stops working" has one
    vocabulary: ``logout``, ``expired`` and ``token_reuse`` all come through here.
    """
    session.is_revoked = True
    session.revoke_reason = reason


async def revoke_family(db: AsyncSession, family_id: str, *, reason: str) -> int:
    """Revoke every session in a token family; returns how many were touched.

    Used when a rotated refresh token is replayed (plan §4.3): the replay means
    someone else has one of the family's tokens, so none of them may keep
    working.
    """
    sessions = await sessions_in_family(db, family_id)
    for session in sessions:
        mark_revoked(session, reason)
    return len(sessions)


__all__ = [
    "account_count",
    "create_local_account",
    "find_session",
    "load_account",
    "mark_revoked",
    "open_session",
    "record_event",
    "revoke_family",
    "sessions_in_family",
    "username_taken",
]
