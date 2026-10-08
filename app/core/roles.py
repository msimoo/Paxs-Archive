"""Canonical RBAC role vocabulary (``plan_9_2026.md`` C02).

One definition of the role names, shared by the API dependencies, the
user-administration endpoints, the worklist guard and the LDAP group mapping.

Roles are **UPPERCASE** everywhere. The previous LDAP service emitted lowercase
roles ("radiologist"), which fell outside this enum: destructive-role checks
rejected such accounts while generic authentication accepted them, so the same
token was simultaneously "too privileged to delete" and "privileged enough to
read every study".
"""

from __future__ import annotations

ROLES: tuple[str, ...] = ("ADMIN", "RADIOLOGIST", "PHYSICIAN", "TECHNOLOGIST")

# Roles allowed to destroy archive data (delete studies / shares).
DESTRUCTIVE_ROLES: tuple[str, ...] = ("ADMIN", "RADIOLOGIST")

# Roles allowed to write modality worklist entries.
WRITE_ROLES: tuple[str, ...] = ("ADMIN", "TECHNOLOGIST")

# The role that sees every tenant regardless of assignment.
ADMIN_ROLE: str = "ADMIN"


def normalize_role(value: str | None) -> str | None:
    """Canonical uppercase role name, or ``None`` when it is not a known role."""
    if value is None:
        return None
    candidate = str(value).strip().upper()
    return candidate if candidate in ROLES else None


def is_known_role(value: str | None) -> bool:
    """True when ``value`` names one of :data:`ROLES` (case-insensitive)."""
    return normalize_role(value) is not None
