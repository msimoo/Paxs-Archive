"""Canonical RBAC role vocabulary (``plans/legacy/archive-notes/plan_9_2026.md`` C02).

One definition of the role names, shared by the API dependencies, the
user-administration endpoints, the worklist guard and the LDAP group mapping.

Roles are **UPPERCASE** everywhere. The previous LDAP service emitted lowercase
roles ("radiologist"), which fell outside this enum: destructive-role checks
rejected such accounts while generic authentication accepted them, so the same
token was simultaneously "too privileged to delete" and "privileged enough to
read every study".
"""

from __future__ import annotations

ROLES: tuple[str, ...] = ("ADMIN", "RADIOLOGIST", "PHYSICIAN", "TECHNOLOGIST", "REPORT_WRITER")

DICTATION_RECORD_ROLES = ("ADMIN", "RADIOLOGIST")
DICTATION_LISTEN_ROLES = (*DICTATION_RECORD_ROLES, "REPORT_WRITER")

# Roles allowed to destroy archive data (delete studies / shares).
DESTRUCTIVE_ROLES: tuple[str, ...] = ("ADMIN", "RADIOLOGIST")

# Roles allowed to permanently delete a **study** from the browser.
#
# Deliberately narrower than DESTRUCTIVE_ROLES, and the two must not be merged:
# a RADIOLOGIST is destructive in the clinical sense (they may sign a report,
# which is irreversible), but signing a report and erasing the images it
# describes are different acts. Only an ADMIN may erase images. Before this
# constant existed the API admitted RADIOLOGIST, so the dashboard's "delete is
# administrator-only" gate was cosmetic — the button was hidden but the endpoint
# still answered 204 to a RADIOLOGIST.
STUDY_DELETE_ROLES: tuple[str, ...] = ("ADMIN",)

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


def may_delete_studies(role: str | None) -> bool:
    """True when ``role`` may permanently delete a study.

    Fails closed: an unknown, empty or ``None`` role is never allowed, so a
    caller whose role failed to load cannot reach the delete path.
    """
    return (role or "").strip().upper() in STUDY_DELETE_ROLES
