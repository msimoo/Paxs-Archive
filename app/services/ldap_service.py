"""LDAP / Active Directory integration service.

Directory authentication is **fail-closed** (``plans/legacy/archive-notes/plan_9_2026.md`` C02):

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
import ssl
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.roles import ROLES, normalize_role
from app.db.models import AuthSource, Tenant, User

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


class LDAPIdentityConflictError(LDAPConfigurationError):
    """Raised when LDAP would claim an account owned by another auth source."""


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


def _escape_dn_value(value: str) -> str:
    """Escape an untrusted value inserted into an LDAP distinguished name."""
    escaped: list[str] = []
    last_index = len(value) - 1
    for index, char in enumerate(value):
        if char == "\0":
            escaped.append("\\00")
        elif ord(char) < 0x20 or ord(char) == 0x7F:
            escaped.extend(f"\\{byte:02x}" for byte in char.encode("utf-8"))
        elif char in ',+"\\<>;=/' or (char == "#" and index == 0):
            escaped.append("\\" + char)
        elif char == " " and (index == 0 or index == last_index):
            escaped.append("\\ ")
        else:
            escaped.append(char)
    return "".join(escaped)


def _escape_filter_value(value: str) -> str:
    """Escape LDAP filter metacharacters per RFC 4515."""
    substitutions = {"\\": r"\5c", "*": r"\2a", "(": r"\28", ")": r"\29", "\0": r"\00"}
    return "".join(substitutions.get(char, char) for char in value)


def _user_dn(username: str) -> str:
    template = str(settings.LDAP_USER_DN_TEMPLATE or "uid={username},{base_dn}")
    try:
        return template.format(
            username=_escape_dn_value(username), base_dn=settings.LDAP_BASE_DN
        )
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
        safe_dn = _escape_filter_value(_user_dn(username))
        safe_username = _escape_filter_value(username)
        conn.search(
            search_base=base,
            search_filter=f"(|(member={safe_dn})(uid={safe_username}))",
            attributes=["memberOf", "cn"],
        )
        groups: list[str] = []
        for result in getattr(conn, "entries", []) or []:
            member_of = getattr(result, "memberOf", None)
            if member_of:
                groups.extend(str(v) for v in member_of)
        return groups
    except Exception as exc:  # noqa: BLE001 - boundary guard: group lookup is best effort
        log.warning("ldap_group_lookup_failed error_type=%s", type(exc).__name__)
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
        log.info("ldap_login_refused reason=disabled")
        return None

    if not is_ldap_configured():
        log.warning(
            "ldap_login_refused reason=directory_configuration_missing"
        )
        return None

    try:
        import ldap3
    except ImportError:
        log.warning(
            "ldap_login_refused reason=ldap_dependency_missing"
        )
        return None

    try:
        server_url = str(settings.LDAP_SERVER or "").strip()
        parts = urlsplit(server_url)
        scheme = parts.scheme.lower()
        if (
            scheme not in {"ldap", "ldaps"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or parts.path not in ("", "/")
            or parts.query
            or parts.fragment
        ):
            raise LDAPConfigurationError(
                "LDAP_SERVER must be an ldap:// or ldaps:// server URL without credentials or a path"
            )

        use_ssl = scheme == "ldaps"
        if not use_ssl and not settings.LDAP_USE_TLS:
            raise LDAPConfigurationError(
                "LDAP simple binds require LDAPS or LDAP_USE_TLS=true for StartTLS"
            )

        environment = str(getattr(settings, "ENVIRONMENT", "") or "").strip().lower()
        validation = ssl.CERT_NONE if environment in {"test", "testing"} else ssl.CERT_REQUIRED
        ca_file = str(getattr(settings, "LDAP_CA_CERT_FILE", "") or "").strip() or None
        tls = ldap3.Tls(validate=validation, ca_certs_file=ca_file)
        server = ldap3.Server(
            parts.hostname,
            port=parts.port or (636 if use_ssl else 389),
            get_info=ldap3.NONE,
            use_ssl=use_ssl,
            tls=tls,
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
        # LDAPS negotiates TLS when the socket is opened. Plain ldap:// is only
        # permitted with StartTLS, and that negotiation must succeed before the
        # connection is ever given credentials to bind.
        if not use_ssl and not conn.start_tls():
            log.warning("LDAP StartTLS negotiation failed; refusing bind")
            return None
        if not conn.bind():
            log.info("ldap_bind_rejected")
            return None
    except LDAPConfigurationError as exc:
        log.error("LDAP configuration error: %s", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - boundary guard: any failure must deny the login, never 500
        log.warning("LDAP transport or bind error: %s", type(exc).__name__)
        return None

    groups = _search_groups(conn, username)
    role = resolve_role(groups)
    if role is None:
        log.warning(
            "ldap_login_denied reason=no_configured_role_mapping"
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
    except (KeyError, AttributeError, TypeError) as exc:  # cosmetic only
        # The username is a usable display name; never fail the page over a label.
        log.debug("ldap_display_name_failed error_type=%s", type(exc).__name__)
    return username


def _email_domain() -> str:
    base = str(settings.LDAP_BASE_DN or "")
    parts = [p.split("=", 1)[-1] for p in base.split(",") if p.strip().upper().startswith("DC=")]
    return ".".join(parts) or "localhost"


# --------------------------------------------------------------------------- #
# provisioning
# --------------------------------------------------------------------------- #
async def _assigned_directory_tenant_id(session: AsyncSession) -> int | None:
    """Resolve the operator-assigned tenant for this LDAP directory.

    A legacy single-scope installation with no VNA tenant rows may continue to
    provision NULL-scoped accounts. Once tenant isolation is configured, LDAP
    provisioning requires an explicit active ``LDAP_TENANT_ID`` and fails closed
    rather than creating an unscoped account.
    """
    configured_id = getattr(settings, "LDAP_TENANT_ID", None)
    if configured_id is not None:
        try:
            tenant_id = int(configured_id)
        except (TypeError, ValueError) as exc:
            raise LDAPConfigurationError("LDAP_TENANT_ID must be a valid tenant id") from exc
        tenant = await session.get(Tenant, tenant_id)
        if tenant is None or not tenant.is_active:
            raise LDAPConfigurationError("LDAP_TENANT_ID must name an active tenant")
        return tenant_id

    has_tenants = (
        await session.execute(select(Tenant.id).limit(1))
    ).scalar_one_or_none()
    if has_tenants is not None:
        raise LDAPConfigurationError(
            "LDAP_TENANT_ID must be configured when VNA tenants exist"
        )
    return None


async def sync_ldap_user_to_db(session: AsyncSession, identity: LDAPUserIdentity) -> User:
    """Create or synchronize only an LDAP-owned record in its assigned tenant.

    Role and display name come from the bound directory identity. The local
    password, active flag, and tenant assignment are never overwritten. Existing
    non-LDAP accounts are a hard namespace collision, not candidates for adoption.
    """
    role = normalize_role(identity.mapped_role)
    if role is None:
        raise LDAPConfigurationError(
            f"refusing to provision {identity.username!r}: {identity.mapped_role!r} is not a known role"
        )

    tenant_id = await _assigned_directory_tenant_id(session)
    normalized_username = str(identity.username or "").strip()
    if not normalized_username:
        raise LDAPConfigurationError("refusing to provision an empty LDAP username")

    user = (
        await session.execute(
            select(User).where(func.lower(User.username) == normalized_username.lower())
        )
    ).scalar_one_or_none()

    if user is not None:
        if user.auth_source != AuthSource.LDAP:
            log.error("SECURITY ALERT: refused LDAP identity collision with a non-LDAP account")
            raise LDAPIdentityConflictError("username belongs to another authentication source")
        if user.tenant_id != tenant_id:
            log.error("SECURITY ALERT: refused LDAP account sync across directory tenant scope")
            raise LDAPIdentityConflictError("LDAP account belongs to a different tenant")
        user.full_name = identity.full_name or user.full_name
        user.role = role
        await session.flush()
        return user

    user = User(
        username=normalized_username,
        password_hash=NO_LOCAL_PASSWORD_HASH,
        full_name=identity.full_name,
        role=role,
        auth_source=AuthSource.LDAP,
        tenant_id=tenant_id,
    )
    session.add(user)
    await session.flush()
    log.info("Provisioned LDAP-owned account with directory-mapped role %s", user.role)
    return user
