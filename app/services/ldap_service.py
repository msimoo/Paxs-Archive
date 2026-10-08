"""LDAP / Active Directory integration service.

Directory authentication is **fail-closed** (``plan_9_2026.md`` C02):

* disabled unless ``LDAP_ENABLED`` is true *and* ``LDAP_SERVER`` + ``LDAP_BASE_DN``
  are configured;
* a bind failure, timeout, or a missing ``ldap3`` dependency denies the login;
* groups are mapped to the canonical PAXS roles through ``LDAP_GROUP_ROLE_MAP``
  (see :mod:`app.core.roles`); a user whose groups match nothing is denied unless
  ``LDAP_DEFAULT_ROLE`` names a known role;
* provisioned directory accounts receive an unusable local password hash, so they
  can never be used against the local ``/api/v1/auth/login`` endpoint.

There is deliberately **no development fallback**: the previous hardcoded
``ldap_*`` / ``domainpass`` credential pattern and the shared
``ldap_managed_password`` hash were removed because they authenticated callers
without contacting any directory at all — and, because ``LDAP_SERVER`` was never
a declared setting, that fallback was in fact the only reachable code path.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.roles import ROLES, normalize_role
from app.db.models import User

log = logging.getLogger("paxs.ldap")

# Stored in users.password_hash for directory-managed accounts. It is not a valid
# PBKDF2 record, so app.core.security.verify_password() always returns False and
# the local password login can never succeed for these accounts.
NO_LOCAL_PASSWORD_HASH = "!ldap-managed-no-local-password"

_GROUP_ROLE_SEPARATOR = ";"
_ROLE_SEPARATOR = "="


@dataclass
class LDAPUserIdentity:
    """A successfully bound directory user."""

    username: str
    full_name: str
    email: str | None
    groups: list[str] = field(default_factory=list)
    mapped_role: str = ""


class LDAPConfigurationError(RuntimeError):
    """Raised when LDAP is enabled but not usable."""


# --------------------------------------------------------------------------- #
# configuration helpers
# --------------------------------------------------------------------------- #
def parse_group_role_map(raw: str | None) -> dict[str, str]:
    """Parse ``"CN=Radiologists=ADMIN;CN=Doctors=PHYSICIAN"`` into a role map.

    Keys are lowercase group identifiers (the full DN *and* the leading CN are
    accepted when matching). Values are canonical uppercase roles; entries whose
    role is unknown are ignored with a warning rather than silently granting a
    role that no other part of the system recognises.
    """
    mapping: dict[str, str] = {}
    for chunk in (raw or "").split(_GROUP_ROLE_SEPARATOR):
        entry = chunk.strip()
        if not entry:
            continue
        group, _, role_text = entry.rpartition(_ROLE_SEPARATOR)
        group = group.strip()
        role = normalize_role(role_text)
        if not group:
            log.warning("LDAP group-role map entry %r has no group; ignored", entry)
            continue
        if role is None:
            log.warning(
                "LDAP group-role map entry %r maps to unknown role %r (expected one of %s); ignored",
                entry, role_text.strip(), ", ".join(ROLES),
            )
            continue
        mapping[group.lower()] = role
        # Also index the bare CN so a directory that returns only "Radiologists"
        # still matches "CN=Radiologists,...".
        if group.upper().startswith("CN="):
            cn = group.split(",", 1)[0].split("=", 1)[-1].strip().lower()
            if cn:
                mapping.setdefault(cn, role)
    return mapping


def default_role() -> str | None:
    """Configured fallback role for unmapped groups (None = deny)."""
    return normalize_role(settings.LDAP_DEFAULT_ROLE)


def is_ldap_configured() -> bool:
    """True when directory authentication is enabled and usable."""
    if not settings.LDAP_ENABLED:
        return False
    return bool(str(settings.LDAP_SERVER or "").strip()) and bool(
        str(settings.LDAP_BASE_DN or "").strip()
    )


def _user_dn(username: str) -> str:
    template = str(settings.LDAP_USER_DN_TEMPLATE or "uid={username},{base_dn}")
    try:
        return template.format(username=username, base_dn=settings.LDAP_BASE_DN)
    except (KeyError, IndexError, ValueError) as exc:  # malformed template
        raise LDAPConfigurationError(
            f"LDAP_USER_DN_TEMPLATE is invalid ({exc}); expected {{username}} / {{base_dn}} placeholders"
        ) from exc


def resolve_role(groups: list[str]) -> str | None:
    """Canonical role for a set of group identifiers, or None to deny."""
    mapping = parse_group_role_map(settings.LDAP_GROUP_ROLE_MAP)
    for group in groups:
        candidate = str(group or "").strip().lower()
        if not candidate:
            continue
        if candidate in mapping:
            return mapping[candidate]
        # Match a full DN against the leading CN of a mapped group.
        cn = candidate.split(",", 1)[0].split("=", 1)[-1].strip()
        if cn and cn in mapping:
            return mapping[cn]
    return default_role()


# --------------------------------------------------------------------------- #
# authentication
# --------------------------------------------------------------------------- #
def _search_groups(conn, username: str) -> list[str]:
    """Group identifiers for a bound user (best effort; [] when unsupported)."""
    try:
        base = settings.LDAP_BASE_DN
        entry = conn.entries[0] if getattr(conn, "entries", None) else None
        if entry is not None:
            for attribute in ("memberOf", "memberOf;range=0-*", "groups"):
                value = getattr(entry, attribute, None)
                if value:
                    return [str(v) for v in value]
        conn.search(
            search_base=base,
            search_filter=f"(|(member={_user_dn(username)})(uid={username}))",
            attributes=["memberOf", "cn"],
        )
        groups: list[str] = []
        for result in getattr(conn, "entries", []) or []:
            member_of = getattr(result, "memberOf", None)
            if member_of:
                groups.extend(str(v) for v in member_of)
        return groups
    except Exception as exc:  # noqa: BLE001 - group lookup is best effort
        log.warning("LDAP group lookup failed for %s: %s", username, exc)
        return []


def authenticate_ldap_user(username: str, password: str) -> LDAPUserIdentity | None:
    """Bind ``username``/``password`` against the configured directory.

    Returns the identity on a successful bind, or ``None`` for every failure
    mode (disabled, misconfigured, unreachable, bad credentials, unmapped
    groups, inactive account). Never authenticates without a real bind.
    """
    username = (username or "").strip()
    if not username or not password:
        return None

    if not settings.LDAP_ENABLED:
        log.info("LDAP login refused for %r: directory authentication is disabled", username)
        return None

    if not is_ldap_configured():
        log.warning(
            "LDAP login refused for %r: LDAP_SERVER and LDAP_BASE_DN must both be configured",
            username,
        )
        return None

    try:
        import ldap3
    except ImportError:
        log.warning(
            "LDAP login refused for %r: the optional 'ldap3' package is not installed", username
        )
        return None

    try:
        server = ldap3.Server(
            settings.LDAP_SERVER,
            get_info=ldap3.NONE,
            use_ssl=str(settings.LDAP_SERVER).lower().startswith("ldaps://"),
            connect_timeout=float(settings.LDAP_TIMEOUT_SECONDS),
        )
        conn = ldap3.Connection(
            server,
            user=_user_dn(username),
            password=password,
            auto_bind=False,
            receive_timeout=float(settings.LDAP_TIMEOUT_SECONDS),
            raise_exceptions=False,
        )
        if not conn.bind():
            log.info(
                "LDAP bind rejected for %r: %s",
                username,
                getattr(conn, "result", "") or "invalid credentials",
            )
            return None
    except LDAPConfigurationError as exc:
        log.error("LDAP configuration error: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - any failure denies the login
        log.warning("LDAP bind error for %s: %s", username, exc)
        return None

    groups = _search_groups(conn, username)
    role = resolve_role(groups)
    if role is None:
        log.warning(
            "LDAP login denied for %r: no configured group mapped to a PAXS role "
            "(set LDAP_GROUP_ROLE_MAP or LDAP_DEFAULT_ROLE)",
            username,
        )
        return None

    display = _display_name(conn, username)
    return LDAPUserIdentity(
        username=username,
        full_name=display,
        email=f"{username}@{_email_domain()}",
        groups=groups,
        mapped_role=role,
    )


def _display_name(conn, username: str) -> str:
    try:
        entries = getattr(conn, "entries", None)
        if entries:
            for attribute in ("displayName", "cn"):
                value = getattr(entries[0], attribute, None)
                if value:
                    return str(value)
    except Exception:  # noqa: BLE001 - cosmetic only
        pass
    return username


def _email_domain() -> str:
    base = str(settings.LDAP_BASE_DN or "")
    parts = [p.split("=", 1)[-1] for p in base.split(",") if p.strip().upper().startswith("DC=")]
    return ".".join(parts) or "localhost"


# --------------------------------------------------------------------------- #
# provisioning
# --------------------------------------------------------------------------- #
async def sync_ldap_user_to_db(session: AsyncSession, identity: LDAPUserIdentity) -> User:
    """Create or update the local record for a bound directory user.

    * the role is always the canonical uppercase role resolved from the directory
      groups (never a value supplied by the caller);
    * ``is_active`` is never modified here, so an account disabled by an
      administrator stays disabled;
    * directory-managed accounts receive :data:`NO_LOCAL_PASSWORD_HASH` on
      creation, which makes the local password login impossible.
    """
    role = normalize_role(identity.mapped_role)
    if role is None:
        raise LDAPConfigurationError(
            f"refusing to provision {identity.username!r}: {identity.mapped_role!r} is not a known role"
        )

    user = (
        await session.execute(select(User).where(User.username == identity.username))
    ).scalar_one_or_none()

    if user is not None:
        user.full_name = identity.full_name or user.full_name
        user.role = role
        await session.flush()
        return user

    user = User(
        username=identity.username,
        password_hash=NO_LOCAL_PASSWORD_HASH,
        full_name=identity.full_name,
        role=role,
    )
    session.add(user)
    await session.flush()
    log.info("Provisioned directory user %s with role %s", user.username, user.role)
    return user
