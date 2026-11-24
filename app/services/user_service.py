"""Account, session, auth-audit and lockout queries (Q-3 / FA-2).

``users.py`` used to build every statement it ran, which put ``select()``,
``update()`` and ``from app.db.models import …`` in the routing layer. They live
here now; the router keeps only the HTTP shape and the audit calls.

These routes are **admin-only and archive-wide** — user accounts are not
tenant-owned rows, so there is no tenant predicate to apply here. The isolation
boundary for this surface is ``require_admin`` on the router, which is why the
AST guard pins that marker instead of ``tenant_scope``.

Mutations that only touch loaded attributes (``user.role = …``) stay in the
router: they are not statements, and moving them would split one atomic
decision across two modules for no gain. The one unit-of-work *removal* —
``delete_account`` — lives here because a row deletion is a persistence
operation, not request shaping (Q-3/FA-2).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select, update

from app.core.security import hash_password
from app.db.models import AuthAuditLog, AuthSource, Session, User


def _now() -> datetime:
    """Naive UTC 'now', matching the naive columns these tables store."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
async def list_users(db) -> list[User]:
    """Every account, oldest first (the admin console's default order)."""
    return list((await db.execute(
        select(User).order_by(User.created_at.asc(), User.id.asc())
    )).scalars().all())


async def get_user(db, user_id: int) -> User | None:
    return (await db.execute(
        select(User).where(User.id == user_id)
    )).scalar_one_or_none()


async def username_exists(db, username: str) -> bool:
    return (await db.execute(
        select(User.id).where(User.username == username)
    )).scalar_one_or_none() is not None


async def get_user_by_username(db, username: str) -> User | None:
    return (await db.execute(
        select(User).where(User.username == username)
    )).scalar_one_or_none()


async def create_user(db, *, username: str, password: str,
                      full_name: str | None, role: str,
                      tenant_id: int | None) -> User:
    """Insert a LOCAL account and reload server-generated columns.

    MySQL has no RETURNING, so ``id``/``created_at`` are expired by the flush
    and must be reloaded inside the async context before anything reads them.
    """
    user = User(
        username=username,
        password_hash=hash_password(password),
        full_name=full_name,
        role=role,
        auth_source=AuthSource.LOCAL,
        tenant_id=tenant_id,
    )
    db.add(user)
    await db.flush()
    await db.refresh(user)
    return user


async def active_admin_count(db) -> int:
    """Active ADMINs, locked ``FOR UPDATE`` so concurrent demotions serialize.

    Two admins disabling each other at once could otherwise both read ``2`` and
    both proceed, leaving the archive with none. PostgreSQL refuses ``FOR
    UPDATE`` on an aggregate, so the admin rows themselves are locked and
    counted here.
    """
    rows = (await db.execute(
        select(User.id).with_for_update().where(
            User.role == "ADMIN", User.is_active.is_(True)
        )
    )).scalars().all()
    return len(rows)


async def delete_account(db, user: User) -> None:
    """Remove an account row (the router owns the last-admin / self guards).

    Kept here rather than in ``users.py`` so the routing layer performs no
    persistence operation of its own (Q-3/FA-2).
    """
    await db.delete(user)
    await db.flush()


# --------------------------------------------------------------------------- #
# Sessions
# --------------------------------------------------------------------------- #
async def list_active_sessions(db) -> list[tuple[Session, User]]:
    """Every still-valid session with its owning account, newest activity first."""
    now = _now()
    rows = (await db.execute(
        select(Session, User)
        .join(User, User.id == Session.user_id)
        .where(Session.is_revoked.is_(False), Session.expires_at > now)
        .order_by(Session.last_used_at.desc().nulls_last())
    )).all()
    return list(rows)


async def get_session(db, session_id: str) -> Session | None:
    return (await db.execute(
        select(Session).where(Session.id == session_id)
    )).scalar_one_or_none()


async def sessions_of_user(db, user_id: int, *,
                           exclude_user_id: int | None = None) -> list[Session]:
    """A user's live sessions, optionally excluding one account's own rows.

    ``exclude_user_id`` is how an admin revoking their *own* user id keeps the
    device they are using (never lock yourself out).
    """
    clauses = [
        Session.user_id == user_id,
        Session.is_revoked.is_(False),
        Session.expires_at > _now(),
    ]
    if exclude_user_id is not None:
        clauses.append(Session.user_id != exclude_user_id)
    return list((await db.execute(
        select(Session).where(*clauses)
    )).scalars().all())


async def revoke_all_sessions(db, user_id: int) -> int:
    """Mark every live session of ``user_id`` revoked; returns the row count.

    H04: a password reset must invalidate existing sessions, or a stolen cookie
    keeps working after the credential changes.
    """
    result = await db.execute(
        update(Session)
        .where(Session.user_id == user_id, Session.is_revoked.is_(False))
        .values(is_revoked=True)
    )
    return int(result.rowcount or 0)


# --------------------------------------------------------------------------- #
# Auth audit / lockout
# --------------------------------------------------------------------------- #
async def list_auth_audit(db, *, action: str | None = None,
                          user_id: int | None = None,
                          limit: int = 100) -> list[tuple[AuthAuditLog, User | None]]:
    """Auth-audit rows (newest first) with their account, when it still exists."""
    stmt = (
        select(AuthAuditLog, User)
        .outerjoin(User, User.id == AuthAuditLog.user_id)
        .order_by(AuthAuditLog.id.desc())
        .limit(min(max(limit, 1), 500))
    )
    if action:
        stmt = stmt.where(AuthAuditLog.action == action)
    if user_id is not None:
        stmt = stmt.where(AuthAuditLog.user_id == user_id)
    return list((await db.execute(stmt)).all())


async def list_locked(db) -> list[User]:
    """Accounts currently inside the brute-force lock window, soonest first."""
    now = _now()
    return list((await db.execute(
        select(User)
        .where(User.locked_until.is_not(None), User.locked_until > now)
        .order_by(User.locked_until.asc())
    )).scalars().all())


def lockout_remaining_minutes(user: User) -> int:
    """Whole minutes left on a lock, floored at 1 so the UI never shows 0."""
    if not user.locked_until:
        return 0
    return max(int((user.locked_until - _now()).total_seconds() // 60), 1)
