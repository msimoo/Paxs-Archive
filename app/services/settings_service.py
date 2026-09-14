"""Runtime-editable server configuration (admin Settings UI).

The dashboard's Settings view is a form over a curated registry of editable
keys (EDITABLE_KEYS below). Each key's *live* value is

    DB override (server_settings table)  ??  env default (app.core.config.settings)

so nothing is hard-coded in the UI: the form always shows what the server is
actually using. PATCHes are validated against the registry, persisted, and
immediately applied to the in-process ``settings`` object (attribute
assignment — pydantic-settings instances are not frozen, and no
``validate_assignment`` is configured). At boot, ``apply_db_overrides()``
replays every stored override so persisted changes survive restarts.

Keys whose consumers bind at import/startup time (DICOM receiver, HL7
listener, router inclusion) carry ``restart=True`` in the registry: the UI
shows an explicit "restart required" hint, and the value still lands in the
DB + settings object so the *next* boot honors it.
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select

from app.core.config import settings
from app.db.models import ServerSetting
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.settings")

# ---------------------------------------------------------------- registry --
# key -> {label, group, type, options?, description, restart?}
# group values match the dashboard's Settings sub-tabs.
EDITABLE_KEYS: dict[str, dict[str, Any]] = {
    # ---- System ----------------------------------------------------------
    "APP_NAME": {
        "label": "Application name",
        "group": "sys",
        "type": "str",
        "description": "Server display name shown in the header and login panel.",
    },
    "PUBLIC_BASE_URL": {
        "label": "Public base URL",
        "group": "sys",
        "type": "str",
        "description": "External URL used in patient share links and QR codes (behind a reverse proxy).",
    },
    "SHARE_EXPIRY_DAYS": {
        "label": "Share link expiry (days)",
        "group": "sys",
        "type": "int",
        "description": "Default lifetime of patient share links.",
    },
    "DICOMWEB_PUBLIC": {
        "label": "DICOMweb public access",
        "group": "sys",
        "type": "bool",
        "description": "Allow QIDO/WADO/STOW without a login token.",
    },
    # ---- DICOM receiver ----------------------------------------------------
    "DICOM_AE_TITLE": {
        "label": "AE title",
        "group": "dicom",
        "type": "str",
        "restart": True,
        "description": "Application Entity title the DIMSE receiver advertises.",
    },
    "DICOM_PORT": {
        "label": "DIMSE port",
        "group": "dicom",
        "type": "int",
        "restart": True,
        "description": "TCP port for C-STORE / C-FIND / C-GET / C-MOVE / MPPS / Storage Commitment.",
    },
    "DICOM_RECEIVER_ENABLED": {
        "label": "DICOM receiver",
        "group": "dicom",
        "type": "bool",
        "restart": True,
        "description": "Start the DIMSE listener on boot.",
    },
    "DICOM_MAX_ASSOCIATIONS": {
        "label": "Max associations",
        "group": "dicom",
        "type": "int",
        "restart": True,
        "description": "Concurrent DIMSE association cap.",
    },
    "DICOM_TLS_ENABLED": {
        "label": "DICOM TLS (AES)",
        "group": "dicom",
        "type": "bool",
        "restart": True,
        "description": "Wrap DIMSE in TLS 1.2/1.3 with a self-signed certificate.",
    },
    "MWL_DEFAULT_STATION_AET": {
        "label": "MWL default station AE",
        "group": "dicom",
        "type": "str",
        "description": "Station AE stamped on ADT-created worklist steps (empty = all stations).",
    },
    # ---- Interop (HL7 / FHIR) ---------------------------------------------
    "HL7_ENABLED": {
        "label": "HL7 MLLP listener",
        "group": "interop",
        "type": "bool",
        "restart": True,
        "description": "Receive ADT admissions and ORM imaging orders from the HIS.",
    },
    "HL7_HOST": {
        "label": "HL7 bind host",
        "group": "interop",
        "type": "str",
        "restart": True,
    },
    "HL7_PORT": {
        "label": "HL7 MLLP port",
        "group": "interop",
        "type": "int",
        "restart": True,
    },
    "HL7_EMR_ENABLED": {
        "label": "ORU report write-back",
        "group": "interop",
        "type": "bool",
        "description": "Send ORU^R01 to the EMR when a report is finalized.",
    },
    "HL7_EMR_HOST": {
        "label": "EMR host",
        "group": "interop",
        "type": "str",
    },
    "HL7_EMR_PORT": {
        "label": "EMR MLLP port",
        "group": "interop",
        "type": "int",
    },
    "HL7_EMR_APPLICATION": {
        "label": "EMR application (MSH-5)",
        "group": "interop",
        "type": "str",
    },
    "HL7_EMR_FACILITY": {
        "label": "EMR facility (MSH-6)",
        "group": "interop",
        "type": "str",
    },
    "HL7_SENDING_APPLICATION": {
        "label": "Sending application (MSH-3)",
        "group": "interop",
        "type": "str",
    },
    "HL7_SENDING_FACILITY": {
        "label": "Sending facility (MSH-4)",
        "group": "interop",
        "type": "str",
    },
    "FHIR_ENABLED": {
        "label": "FHIR R4 endpoint",
        "group": "interop",
        "type": "bool",
        "restart": True,
        "description": "Serve the /fhir REST API.",
    },
    # ---- Storage -----------------------------------------------------------
    "STORAGE_BACKEND": {
        "label": "Storage backend",
        "group": "storage",
        "type": "select",
        "options": ["local", "s3"],
        "restart": True,
        "description": "Filesystem under STORAGE_ROOT, or an S3-compatible object store.",
    },
    "S3_ENDPOINT_URL": {
        "label": "S3 endpoint URL",
        "group": "storage",
        "type": "str",
        "description": "MinIO / S3-compatible endpoint (empty = AWS).",
    },
    "S3_REGION": {
        "label": "S3 region",
        "group": "storage",
        "type": "str",
    },
    "S3_BUCKET": {
        "label": "S3 bucket",
        "group": "storage",
        "type": "str",
        "restart": True,
    },
    "S3_PRESIGN_EXPIRES": {
        "label": "S3 presign expiry (s)",
        "group": "storage",
        "type": "int",
    },
    "DEFAULT_COMPRESSION": {
        "label": "Default compression",
        "group": "storage",
        "type": "select",
        "options": ["RLE", "JPEG-LS", "JPEG2000", "HTJ2K"],
        "description": "Transfer syntax used by the background compression worker.",
    },
    "COMPRESSION_ENABLED": {
        "label": "Background compression",
        "group": "storage",
        "type": "bool",
    },
    "THUMBNAIL_ENABLED": {
        "label": "Thumbnails",
        "group": "storage",
        "type": "bool",
    },
    "WARM_ON_OPEN_ENABLED": {
        "label": "Warm on viewer open",
        "group": "storage",
        "type": "bool",
        "description": "Pre-generate thumbnails + warm frame caches for a study the moment it is opened in the viewer.",
    },
    # ---- Security ----------------------------------------------------------
    "TOKEN_EXPIRE_MINUTES": {
        "label": "Access token lifetime (min)",
        "group": "security",
        "type": "int",
        "description": "Short-lived bearer JWT; silently refreshed via the session cookie.",
    },
    "REFRESH_TOKEN_EXPIRE_DAYS": {
        "label": "Session lifetime (days)",
        "group": "security",
        "type": "int",
        "description": "Rotating refresh-token expiry — how long a login stays alive.",
    },
    "ALLOW_REGISTER": {
        "label": "Allow self-registration",
        "group": "security",
        "type": "bool",
        "description": "Let new users create accounts from the login panel.",
    },
    "RATE_LIMIT_ENABLED": {
        "label": "Rate limiting",
        "group": "security",
        "type": "bool",
        "description": "Sliding-window per-IP abuse protection.",
    },
    "RATE_LIMIT_LOGIN_PER_MINUTE": {
        "label": "Login attempts / min",
        "group": "security",
        "type": "int",
    },
    "RATE_LIMIT_GLOBAL_PER_MINUTE": {
        "label": "Global requests / min",
        "group": "security",
        "type": "int",
    },
    "DICOMWEB_CLIENT_ALLOW_PRIVATE": {
        "label": "Allow private web peers",
        "group": "security",
        "type": "bool",
        "description": "Permit node QIDO/WADO/STOW connections to loopback/private IPs (SSRF guard off).",
    },
    "ANONYMIZER_STRIP_PRIVATE_TAGS": {
        "label": "Anonymizer: strip private tags",
        "group": "security",
        "type": "bool",
    },
    "ANONYMIZER_STRIP_DATES": {
        "label": "Anonymizer: strip dates",
        "group": "security",
        "type": "bool",
    },
    # ---- Workflow ----------------------------------------------------------
    "TRIAGE_ENABLED": {
        "label": "AI emergency triage",
        "group": "workflow",
        "type": "bool",
        "description": "Run the critical-finding detector pipeline in the worker.",
    },
    "TRIAGE_MAX_SLICES_PER_SERIES": {
        "label": "Triage max slices / series",
        "group": "workflow",
        "type": "int",
    },
    "LIFECYCLE_RUN_IN_WORKER": {
        "label": "Lifecycle auto-archive sweep",
        "group": "workflow",
        "type": "bool",
        "description": "Worker evaluates archive/restore/purge rules on schedule.",
    },
    "LIFECYCLE_AUTO_ARCHIVE_WADO_PULL": {
        "label": "Auto-tier WADO pulls to cold",
        "group": "workflow",
        "type": "bool",
        "description": "Tier studies pulled via Web Retrieve straight to the cold tier.",
    },
    "ROUTING_QUEUE_ENABLED": {
        "label": "Smart Router send queue",
        "group": "workflow",
        "type": "bool",
    },
    "ROUTING_MAX_ATTEMPTS": {
        "label": "Router max attempts",
        "group": "workflow",
        "type": "int",
    },
    "ROUTING_RETRY_BACKOFF_SECONDS": {
        "label": "Router retry backoff (s)",
        "group": "workflow",
        "type": "int",
    },
    "TRANSFER_ACCELERATOR_ENABLED": {
        "label": "Transfer Accelerator",
        "group": "workflow",
        "type": "bool",
        "description": "Parallel chunked STOW-RS fast uploads to web peers.",
    },
    "TRANSFER_ACCELERATOR_WORKERS": {
        "label": "Accelerator workers",
        "group": "workflow",
        "type": "int",
    },
    "TRANSFER_ACCELERATOR_BATCH_SIZE": {
        "label": "Accelerator batch size",
        "group": "workflow",
        "type": "int",
    },
}

GROUPS: dict[str, dict[str, str]] = {
    "sys": {"label": "System", "description": "Identity, URLs and sharing defaults."},
    "dicom": {"label": "DICOM", "description": "DIMSE receiver, AE title, TLS and worklist defaults."},
    "interop": {"label": "Interop", "description": "HL7 MLLP (ADT · ORM · ORU) and FHIR R4 gateways."},
    "storage": {"label": "Storage", "description": "Backend (local / S3) and compression policy."},
    "security": {"label": "Security", "description": "Sessions, registration, rate limits and anonymizer."},
    "workflow": {"label": "Workflow", "description": "Triage, lifecycle, routing and transfer acceleration."},
}

# ------------------------------------------------------------- coercion ----
def _parse(key: str, raw: str) -> Any:
    """Convert a stored string back to the key's native Python type."""
    t = EDITABLE_KEYS[key]["type"]
    if t == "bool":
        return str(raw).strip().lower() in ("1", "true", "yes", "on")
    if t == "int":
        return int(float(raw))
    if t == "float":
        return float(raw)
    return raw


def _coerce(key: str, value: Any) -> str:
    """Validate + canonicalize an incoming value for storage (raises ValueError)."""
    spec = EDITABLE_KEYS[key]
    t = spec["type"]
    if value is None:
        raise ValueError("value is required")

    if t == "bool":
        if not isinstance(value, bool):
            raise ValueError("expected a boolean")
        return "1" if value else "0"
    if t == "int":
        try:
            n = int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError("expected an integer") from exc
        return str(n)
    if t == "float":
        try:
            return str(float(value))
        except (TypeError, ValueError) as exc:
            raise ValueError("expected a number") from exc
    if t == "select":
        if value not in spec.get("options", []):
            raise ValueError(f"must be one of {', '.join(spec['options'])}")
        return str(value)
    return str(value)


def _row(session, key: str) -> ServerSetting | None:
    """Fetch a setting row by its key column (NOT the surrogate PK)."""
    return session.execute(
        select(ServerSetting).where(ServerSetting.key == key)
    ).scalar_one_or_none()


def current_value(key: str) -> Any:
    """Live value: DB override (if any) ?? env default."""
    if key not in EDITABLE_KEYS:
        raise KeyError(key)
    with SyncSessionLocal() as session:
        row = _row(session, key)
    if row is not None:
        return _parse(key, row.value)
    return getattr(settings, key)


def is_overridden(key: str) -> bool:
    with SyncSessionLocal() as session:
        return _row(session, key) is not None


# ------------------------------------------------------------ mutations ----
def set_override(key: str, value: Any) -> Any:
    """Persist a validated override and apply it to the runtime settings object.

    Returns the parsed (typed) value that is now live.
    """
    if key not in EDITABLE_KEYS:
        raise KeyError(f"unknown setting {key!r}")
    stored = _coerce(key, value)
    parsed = _parse(key, stored)

    with SyncSessionLocal() as session:
        row = _row(session, key)
        if row is None:
            session.add(ServerSetting(key=key, value=stored))
        else:
            row.value = stored
        session.commit()

    _apply_to_runtime(key, parsed)
    return parsed


def _apply_to_runtime(key: str, parsed: Any) -> None:
    """Push a value onto the in-process pydantic settings object."""
    try:
        setattr(settings, key, parsed)
    except Exception:  # pragma: no cover - defensive
        log.warning("Could not apply runtime override for %s", key, exc_info=True)


def overrides_map() -> dict[str, Any]:
    """All stored overrides as {key: parsed value} (single query).

    Keys stored by subsystems that aren't in the editable registry (e.g. the
    DB panel's own knobs) are skipped — same tolerance as apply_db_overrides,
    so a stray stored key can never 500 the Settings page.
    """
    with SyncSessionLocal() as session:
        rows = session.execute(
            select(ServerSetting.key, ServerSetting.value)
        ).all()
    out: dict[str, Any] = {}
    for row in rows:
        if row.key not in EDITABLE_KEYS:
            continue
        out[row.key] = _parse(row.key, row.value)
    return out


def apply_db_overrides() -> int:
    """Replay all stored overrides onto the runtime settings (call at boot).

    Returns the number of overrides applied.
    """
    with SyncSessionLocal() as session:
        rows = session.execute(
            ServerSetting.__table__.select().order_by(ServerSetting.key)
        ).all()
    applied = 0
    for row in rows:
        key, value = row.key, row.value
        if key not in EDITABLE_KEYS:
            continue
        _apply_to_runtime(key, _parse(key, value))
        applied += 1
    if applied:
        log.info("Applied %d server-settings override(s) from the database", applied)
    return applied
