"""Shared FastAPI dependencies."""

from __future__ import annotations

import logging
from contextvars import ContextVar
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, Response
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select

from app.core.config import settings
from app.core.csrf import csrf_violation
from app.core.roles import DESTRUCTIVE_ROLES, may_delete_studies
from app.core.security import decode_token, hash_refresh_token
from app.db.models import Instance, Series, Session, Study, User
from app.db.session import AsyncSessionLocal
from app.services import query_service, tenant_service

log = logging.getLogger("paxs.api.deps")

bearer_scheme = HTTPBearer(auto_error=False)


def _utcnow() -> datetime:
    """Naive UTC, matching how session timestamps are stored."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def require_csrf(request: Request) -> None:
    """Reject a cookie-authenticated state change without a valid CSRF token.

    Defence-in-depth companion to ``CsrfProtectionMiddleware`` (B-19 / FS-10):
    routers that want the check to be locally explicit — and to keep it even if
    the global middleware is switched off — attach this dependency. Safe methods
    and bearer-token API clients pass through unchanged; a missing or mismatched
    token is a **403**.
    """
    if csrf_violation(request.scope):
        raise HTTPException(status_code=403, detail="CSRF token missing or invalid")


async def get_current_user_from_cookie(request: Request) -> User | None:
    """Resolve the user from the HttpOnly refresh-token cookie, or None.

    Used by the /viewer auth gate and the /login page (already-signed-in
    redirect). The cookie carries the opaque refresh token; we look up the
    session row by its SHA-256 hash and reject revoked **or expired** sessions
    (H04: expiry is checked here, not left to the background sweep, so a session
    can never outlive its TTL between sweeps). Returns None ("redirect to
    /login") instead of raising.
    """
    token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    if not token:
        return None
    async with AsyncSessionLocal() as session:
        sess = (
            await session.execute(
                select(Session).where(
                    Session.refresh_token_hash == hash_refresh_token(token)
                )
            )
        ).scalar_one_or_none()
        if sess is None or sess.is_revoked:
            return None
        if sess.expires_at is not None and sess.expires_at <= _utcnow():
            # Expired: refuse the session on every request (H04).
            return None
        user = (
            await session.execute(select(User).where(User.id == sess.user_id))
        ).scalar_one_or_none()
    if user is None or not user.is_active:
        return None
    return user


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    payload = decode_token(credentials.credentials, settings.SECRET_KEY)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    async with AsyncSessionLocal() as session:
        user = (
            await session.execute(select(User).where(User.username == payload["sub"]))
        ).scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    if not user.is_active:
        # Disabled accounts are rejected even with a valid token.
        raise HTTPException(status_code=401, detail="Account disabled")
    return user


async def get_optional_user(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User | None:
    """Current user when a valid bearer token or session cookie is present, else None.

    Used by browse endpoints (studies list, QIDO-RS, status endpoints) that stay readable
    for guests / unauthenticated users while providing user context when authenticated.
    """
    if credentials is not None:
        payload = decode_token(credentials.credentials, settings.SECRET_KEY)
        if payload is not None:
            async with AsyncSessionLocal() as session:
                user = (
                    await session.execute(
                        select(User).where(User.username == payload["sub"])
                    )
                ).scalar_one_or_none()
            if user is not None and user.is_active:
                return user
    return await get_current_user_from_cookie(request)


async def get_current_user_any(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User:
    """Current user via bearer token (preferred) or the HttpOnly session cookie.

    Used by /auth/me and admin actions so browser sessions with HttpOnly refresh cookies
    can authenticate without re-logging in. Raises 401 when neither authenticates.
    """
    user = await get_optional_user(request, credentials)
    if user is None:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return user


# DESTRUCTIVE_ROLES (roles allowed to destroy archive data) is imported from
# app.core.roles so every role check shares one vocabulary.


async def require_destructive_role(user: User = Depends(get_current_user_any)) -> User:
    if user.role not in DESTRUCTIVE_ROLES:
        raise HTTPException(
            status_code=403,
            detail=f"Requires one of {DESTRUCTIVE_ROLES} roles",
        )
    return user


async def require_admin(user: User = Depends(get_current_user_any)) -> User:
    if user.role != "ADMIN":
        raise HTTPException(status_code=403, detail="Requires ADMIN role")
    return user


def assert_can_delete_studies(user: User) -> None:
    """403 unless the caller may permanently delete a study.

    **Call this after the study has been resolved inside the caller's tenant
    scope**, not as a route dependency. The ordering is load-bearing (N-1): a
    dependency runs before the handler, so it would answer 403 for a study the
    caller cannot see, and a 403 on an existing foreign UID discloses that the
    UID exists. Resolving scope first keeps every cross-tenant probe at 404 —
    which is exactly the ordering ``test_tenant_a_cannot_delete_tenant_b_study``
    pins for a RADIOLOGIST caller.

    ``DESTRUCTIVE_ROLES`` still gates the route (it admits ADMIN and
    RADIOLOGIST), so a PHYSICIAN is refused at the dependency before any lookup
    happens; this check narrows the surviving RADIOLOGIST to ADMIN-only.
    """
    if not may_delete_studies(user.role):
        raise HTTPException(
            status_code=403,
            detail="Only an administrator can delete studies",
        )


# --------------------------------------------------------------------------- #
# C03 — PHI access control and patient share tokens
# --------------------------------------------------------------------------- #
# Patient share links are delivered to people with no archive account, so a
# share token has to be accepted by the *read* paths the viewer uses. It is
# carried by:
#   * the paxs_share cookie, set when the share viewer page is served, so the
#     bundled (third-party) viewer needs no changes;
#   * the X-PAXS-Share-Token header for API clients. Query tokens are accepted
#     only by the one-time link exchange endpoint.
# A share token only ever grants READ access to the single study it names.
SHARE_TOKEN_QUERY_PARAM = "share"
SHARE_TOKEN_HEADER = "X-PAXS-Share-Token"
SHARE_COOKIE_NAME = "paxs_share"


def share_cookie_max_age(expires_at) -> int:
    """Remaining lifetime of a share, capped at one day, in seconds."""
    from datetime import datetime, timezone

    try:
        remaining = int((expires_at - datetime.now(timezone.utc).replace(tzinfo=None)).total_seconds())
    except (TypeError, AttributeError, ValueError):  # naive/aware mismatch or missing value
        return 3600
    return max(60, min(remaining, 86400))


async def get_share_scope(request: Request) -> str | None:
    """Study UID authorised by the caller's share token, or None.

    Validates the token against the shares table (existence + expiry). Never
    raises: an invalid token simply yields no scope, so the caller falls through
    to the normal authentication requirement.
    """
    from app.services.sharing_service import get_share

    token = request.headers.get(SHARE_TOKEN_HEADER) or request.cookies.get(SHARE_COOKIE_NAME)
    if not token:
        return None
    async with AsyncSessionLocal() as session:
        share = await get_share(session, token)
    if share is None:
        return None
    return share.study_instance_uid


def set_share_cookie(response: Response, token: str, expires_at) -> None:
    """Set the study-scoped share credential without permitting HTTP transport."""
    response.set_cookie(
        SHARE_COOKIE_NAME,
        token,
        max_age=share_cookie_max_age(expires_at),
        httponly=True,
        secure=True,
        samesite="lax",
        path="/",
    )


async def _study_uid_for_series(series_uid: str, study_scope: str | None = None) -> str | None:
    """Owning study of a series (used to scope share access to volume data)."""
    from app.db.models import Series, Study

    async with AsyncSessionLocal() as session:
        stmt = (
            select(Study.study_instance_uid)
            .join(Series, Series.study_id == Study.id)
            .where(Series.series_instance_uid == series_uid)
        )
        if study_scope is not None:
            stmt = stmt.where(Study.study_instance_uid == study_scope)
        return (await session.execute(stmt)).scalar_one_or_none()


async def require_dicomweb_read(
    request: Request,
    study_uid: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User | None:
    """Authorise a DICOMweb READ (QIDO-RS / WADO-RS / WADO-URI).

    Two questions are answered here, for every DICOMweb route at once:

    * **Authentication (C03)** — ``DICOMWEB_PUBLIC_READ=true`` allows
      anonymous QIDO/WADO access; ``false`` requires a bearer token or session
      cookie, unless a patient share token authorises exactly this study.
    * **Tenant isolation (C04)** — when the route names a study, an
      authenticated caller must be able to see it, and a share-scoped caller
      must be holding the token for it. A cross-tenant study is a 404.

    Anonymous callers in the explicitly-public mode keep the documented
    archive-wide behaviour; authenticated callers are always tenant-scoped.

    Returns the authenticated user, or ``None`` when access came from a share
    token (callers must then keep the response scoped to that study).
    """
    from app.services import dicomweb_auth_cache as auth_memo

    # S2: a frame burst presents the same token hundreds of times; reuse a
    # recent successful lookup (see dicomweb_auth_cache for the safety rules).
    cookie_token = request.cookies.get(settings.AUTH_COOKIE_NAME)
    identity_key = None
    if credentials is not None or cookie_token:
        identity_key = auth_memo.token_key(
            "id", f"{credentials.credentials if credentials else ''}\n{cookie_token or ''}"
        )
    user = auth_memo.get_identity(identity_key) if identity_key else None
    if user is None:
        user = await get_optional_user(request, credentials)
        if user is not None and identity_key:
            auth_memo.put_identity(identity_key, user)
    share_scope: str | None = None

    if user is None and not settings.DICOMWEB_PUBLIC_READ:
        scope = await get_share_scope(request)
        if scope is not None and (study_uid is None or study_uid == scope):
            # Study-scoped routes must match exactly; collection/search routes
            # carry no study in the path and force the share's study into their
            # own query (see qido_* and the check in wado_uri).
            share_scope = scope
        else:
            raise HTTPException(
                status_code=401,
                detail="Authentication required: DICOMweb is not public on this server",
            )

    if study_uid and not (user is None and settings.DICOMWEB_PUBLIC_READ):
        access_key = (user.id, tenant_scope(user), study_uid) if user is not None else None
        if access_key is None or not auth_memo.has_study_access(access_key):
            async with AsyncSessionLocal() as session:
                await require_study_access(session, study_uid, user, share_scope=share_scope)
            if access_key is not None:
                auth_memo.put_study_access(access_key)

    # Publish the resolved scope for the shared resolution helpers.
    _request_scope.set(read_scope(user, share_scope=share_scope))
    # C-02: and the single study a share token authorizes, so routes that name
    # objects without a study path segment (WADO-URI) can pin their lookup to
    # it instead of running archive-wide.
    _request_share_study.set(share_scope)
    if study_uid:
        await _audit_study_view(request, study_uid, user, share_scope)
    return user


async def _audit_study_view(request: Request, study_uid: str, user: User | None,
                            share_scope: str | None) -> None:
    """H-05: record who opened a study — once per actor/study/5 min, never per frame.

    Runs in the router-level read guard so every study-scoped DICOMweb read
    (metadata, frames, rendered, WADO-RS) is covered by construction. An audit
    failure is logged and never fails the read.
    """
    from app.api.ratelimit import _client_key
    from app.services.audit_service import record_study_view

    if user is not None:
        actor_key = f"user:{user.id}"
    elif share_scope is not None:
        actor_key = "share"
    else:
        actor_key = "anonymous"
    client_ip = _client_key(request)
    try:
        async with AsyncSessionLocal() as session:
            await record_study_view(
                session, actor_key=f"{actor_key}@{client_ip}" if user is None else actor_key,
                study_uid=study_uid,
                user_id=getattr(user, "id", None), client_ip=client_ip,
            )
    except Exception as exc:  # noqa: BLE001 - audit is best effort; the read must still answer
        log.error("study_view_audit_failed error_type=%s", type(exc).__name__)


async def require_dicomweb_write(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User | None:
    """Authorise a DICOMweb WRITE (STOW-RS).

    Share tokens never grant write access. Anonymous writes are allowed only
    when the separate ``DICOMWEB_PUBLIC_WRITE`` switch is explicitly enabled.
    """
    user = await get_optional_user(request, credentials)
    if user is None and not settings.DICOMWEB_PUBLIC_WRITE:
        raise HTTPException(
            status_code=401,
            detail="Authentication required: public DICOMweb writes are disabled",
        )
    _request_scope.set(read_scope(user))
    _request_share_study.set(None)
    return user


async def require_series_read(
    request: Request,
    series_uid: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> User | None:
    """Authorise a series-scoped read (volume streaming / MIP / MPR reslice).

    Mirrors :func:`require_dicomweb_read` but resolves the owning study from the
    series UID, so a share link can still render its study's volume while
    everything else requires authentication.
    """
    user = await get_optional_user(request, credentials)
    if user is not None:
        return user
    if series_uid:
        share_scope = await get_share_scope(request)
        study_uid = await _study_uid_for_series(series_uid, study_scope=share_scope)
        if share_scope is not None and study_uid == share_scope:
            return None
        if share_scope is not None:
            raise HTTPException(status_code=404, detail="Series not found")
    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_share_or_user(request: Request) -> None:
    """Allow a request that carries *either* a session or a valid share token.

    Used by the viewer page handler so a share link keeps working while the
    archive requires logins.
    """
    if await get_current_user_from_cookie(request) is not None:
        return
    if await get_share_scope(request) is not None:
        return
    raise HTTPException(status_code=401, detail="Not authenticated")


async def require_dicomweb_access(
    request: Request,
    study_uid: str | None = None,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
) -> None:
    """Router-level guard for the entire DICOMweb surface.

    Reads follow :func:`require_dicomweb_read` (share tokens allowed for their
    own study); writes follow :func:`require_dicomweb_write` (share tokens never
    authorise a write). Applied on the router so every current and future
    DICOMweb route is covered by construction.
    """
    if request.method.upper() in ("POST", "PUT", "PATCH", "DELETE"):
        await require_dicomweb_write(request, credentials)
        return
    await require_dicomweb_read(request, study_uid, credentials)


# --------------------------------------------------------------------------- #
# C04 — tenant-scoped study / series / instance access
# --------------------------------------------------------------------------- #
# Authentication (C03) answers "who are you"; these guards answer "may you see
# *this* object". A cross-tenant object is reported as 404, not 403, so an
# institution cannot probe for the existence of another institution's studies.
def tenant_scope(user: User | None) -> tuple[int | None, bool]:
    """The caller's tenant **scope** — ``(tenant_id, include_all)``.

    This is the *resolver*, not a predicate: it answers "which tenant is this
    caller allowed to see", and its result is fed to
    :func:`app.services.query_service.tenant_predicate` /
    :func:`~app.services.query_service.apply_study_tenant_scope`, which is the
    single place the predicate itself is implemented (FA-1). Do not build a
    tenant comparison by hand from this tuple — call the predicate.

    Delegates to :func:`app.services.tenant_service.scope_from_user`, the one
    definition of the role -> scope mapping.
    """
    return tenant_service.scope_from_user(user)


def read_scope(user: User | None, *, share_scope: str | None = None) -> tuple[int | None, bool]:
    """(tenant_id, include_all) for a read that may be anonymous or share-scoped.

    * authenticated caller        -> their tenant (ADMIN: everything)
    * share-scoped caller         -> archive-wide, because the token itself is
      the authorization for exactly one study (the caller still has to name it)
    * anonymous in public mode    -> archive-wide (the documented legacy mode)
    * anonymous otherwise         -> the legacy unscoped data only
    """
    if user is None:
        if share_scope is not None or settings.DICOMWEB_PUBLIC_READ:
            return None, True
    return tenant_scope(user)


# The DICOMweb router-level guard resolves the caller's scope once per request
# and publishes it here, so the shared resolution helpers (_instances_of /
# _get_instance) can scope their queries without threading `user` through every
# handler signature. ContextVar is safe because every DICOMweb endpoint is
# `async def`, so dependencies and the endpoint run in the same task context.
_request_scope: ContextVar[tuple[int | None, bool]] = ContextVar(
    "paxs_request_scope", default=(None, False)
)
# C-02: the study UID a share-token caller is confined to (None = not a share
# caller). Published by the same router-level guard as `_request_scope`.
_request_share_study: ContextVar[str | None] = ContextVar(
    "paxs_request_share_study", default=None
)


def current_share_study() -> str | None:
    """Study UID the current request's share token is confined to, or None."""
    return _request_share_study.get()


def current_scope() -> tuple[int | None, bool]:
    """(tenant_id, include_all) for the request being handled."""
    return _request_scope.get()


async def require_study_access(db: AsyncSession, study_uid: str, user: User | None,
                               *, share_scope: str | None = None) -> Study:
    """The study when the caller may see it, else 404 (C04)."""
    if user is None and share_scope is not None:
        # A share token is a *fallback identity* for someone with no account,
        # never a widening of an authenticated user's access: it authorises
        # exactly this study, so the tenant filter does not apply.
        #
        # C-02: a mismatch is a 404 *here*. It used to fall through to the
        # anonymous legacy scope below, i.e. every NULL-tenant study, so a link
        # for one patient could read another patient's instance by SOP UID.
        if share_scope != study_uid:
            raise HTTPException(404, "Study not found")
        study = (
            await db.execute(select(Study).where(Study.study_instance_uid == study_uid))
        ).scalar_one_or_none()
        if study is None:
            raise HTTPException(404, "Study not found")
        return study

    tenant_id, include_all = tenant_scope(user)
    stmt = query_service.apply_study_tenant_scope(
        select(Study).where(Study.study_instance_uid == study_uid), tenant_id, include_all
    )
    study = (await db.execute(stmt)).scalar_one_or_none()
    if study is None:
        raise HTTPException(404, "Study not found")
    return study


async def require_series_access(db: AsyncSession, series_uid: str, user: User | None,
                                *, share_scope: str | None = None) -> Series:
    """The series when the caller may see its study, else 404 (C04).

    Used by the volume endpoints, which address data by series UID and therefore
    never consulted the study's tenant.
    """
    if (
        user is None
        and share_scope is not None
        and await _series_belongs_to(db, series_uid, share_scope)
    ):
        tenant_id, include_all = None, True
    else:
        tenant_id, include_all = tenant_scope(user)
    stmt = query_service.apply_study_tenant_scope(
        select(Series)
        .join(Study, Series.study_id == Study.id)
        .where(Series.series_instance_uid == series_uid),
        tenant_id,
        include_all,
    )
    series = (await db.execute(stmt)).scalar_one_or_none()
    if series is None:
        raise HTTPException(404, "Series not found")
    return series


async def _series_belongs_to(db: AsyncSession, series_uid: str, study_uid: str) -> bool:
    """True when ``series_uid`` is part of ``study_uid`` (share-scope check)."""
    row = (
        await db.execute(
            select(Series.id)
            .join(Study, Series.study_id == Study.id)
            .where(
                Series.series_instance_uid == series_uid,
                Study.study_instance_uid == study_uid,
            )
        )
    ).scalar_one_or_none()
    return row is not None


async def require_instance_access(db: AsyncSession, study_uid: str, series_uid: str,
                                  sop_uid: str, user: User | None,
                                  *, share_scope: str | None = None) -> Instance:
    """The instance when the caller may see its study, else 404 (C04)."""
    if user is None and share_scope is not None and share_scope == study_uid:
        tenant_id, include_all = None, True
    else:
        tenant_id, include_all = tenant_scope(user)
    inst = (
        await db.execute(
            query_service.instance_query(
                study_uid,
                series_uid,
                {"SOPInstanceUID": sop_uid},
                tenant_id=tenant_id,
                include_all=include_all,
            )
        )
    ).scalar_one_or_none()
    if inst is None:
        raise HTTPException(404, "Instance not found")
    return inst
