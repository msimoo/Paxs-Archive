"""Admin Settings API — read and edit the server configuration.

GET  /api/v1/config  -> every editable key (registry metadata + live value,
                        "changed" flag when a DB override exists) grouped by
                        Settings tab, plus the live health payload as readonly.
PATCH /api/v1/config  -> validate a batch of {key: value} updates against the
                        editable registry, persist them to `server_settings`
                        and apply them to the runtime settings object
                        immediately. Admin role required.

Authentication (B-19): both endpoints accept a bearer token **or** the HttpOnly
session cookie (a browser on the Settings page has only the cookie). Cookie
callers additionally present the CSRF token on the PATCH (FS-10); see
`app/core/csrf.py`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.api.v1.deps import require_admin, require_csrf
from app.core.config import InsecureConfigurationError, settings
from app.db.models import User
from app.services import settings_service

log = logging.getLogger("paxs.api.config")

# B-19/FS-10: the Settings page is a browser page, so every route here accepts
# the HttpOnly session cookie as well as a bearer token (get_current_user_any),
# and every state change is CSRF-guarded (require_csrf). The router-level guard
# is inert for GETs and for bearer callers, so it never widens the contract.
router = APIRouter(
    prefix="/api/v1/config",
    tags=["config"],
    dependencies=[Depends(require_csrf)],
)


class ConfigPatch(BaseModel):
    updates: dict[str, object] = Field(default_factory=dict)


def _field(key: str, overrides: dict[str, Any]) -> dict:
    """One editable field: registry metadata + live value + changed flag."""
    spec = settings_service.EDITABLE_KEYS[key]
    field = {
        "key": key,
        "label": spec["label"],
        "type": spec["type"],
        "options": spec.get("options"),
        "option_labels": spec.get("option_labels"),
        "input": spec.get("input"),
        "description": spec.get("description", ""),
        "restart": bool(spec.get("restart", False)),
        "value": overrides.get(key, getattr(settings, key)),
        "changed": key in overrides,
    }
    # Optional numeric bounds so the settings UI can constrain input instead of
    # letting an operator submit a value the server will reject (the cache
    # memory budgets declare min/max; other int keys do not).
    if spec.get("min") is not None:
        field["min"] = spec["min"]
    if spec.get("max") is not None:
        field["max"] = spec["max"]
    return field


@router.get("")
async def get_config(user: User = Depends(require_admin)):
    # One batched read of every stored override (single query), then render
    # each field as: override ?? env default. Offloaded — the helper runs a
    # synchronous DB query.
    overrides = await asyncio.to_thread(settings_service.overrides_map)

    groups: dict[str, dict] = {}
    for key, spec in settings_service.EDITABLE_KEYS.items():
        group = groups.setdefault(
            spec["group"],
            {
                "key": spec["group"],
                "label": settings_service.GROUPS[spec["group"]]["label"],
                "description": settings_service.GROUPS[spec["group"]]["description"],
                "fields": [],
            },
        )
        group["fields"].append(_field(key, overrides))

    # Live runtime facts (read-only) — same shape as /api/v1/health.
    # `health_payload` takes an optional request: this endpoint has no need for
    # one, and passing none simply means public_base_url resolves from
    # PUBLIC_BASE_URL rather than the caller's Host header.
    from app.api.v1.dashboard import health_payload  # local import avoids a cycle

    readonly = await health_payload()
    return {
        "admin": user.role == "ADMIN",
        "groups": list(groups.values()),
        "readonly": readonly,
    }


@router.patch("")
async def patch_config(body: ConfigPatch, user: User = Depends(require_admin)):
    if not body.updates:
        raise HTTPException(422, "no settings supplied")

    immutable_keys = sorted(
        key for key in body.updates if key in settings_service.IMMUTABLE_SECURITY_KEYS
    )
    if immutable_keys:
        raise HTTPException(
            400,
            {"error": "immutable security setting", "keys": immutable_keys},
        )

    applied: list[dict] = []
    errors: list[dict] = []
    insecure: list[dict] = []
    for key, value in body.updates.items():
        if key not in settings_service.EDITABLE_KEYS:
            errors.append({"key": key, "error": "unknown setting"})
            continue
        try:
            # Offloaded — set_override persists to the DB (sync) and mutates the
            # runtime settings object.
            parsed = await asyncio.to_thread(
                settings_service.set_override,
                key,
                value,
                actor_user_id=user.id,
                performed_by=user.username,
            )
        except InsecureConfigurationError as exc:
            # B-22: a *policy* refusal, not a malformed value. The request is
            # well-formed and the server is saying "not here, not now" — 400 is
            # the honest status. 422 stays for a value that cannot be parsed or
            # is out of range.
            # The reason is returned to the caller; the log carries only the
            # event and the type (a setting value may be a credential).
            log.warning("config_override_refused error_type=%s", type(exc).__name__)
            insecure.append({"key": key, "error": str(exc)})
            continue
        except ValueError as exc:
            log.debug("config_override_invalid error_type=%s", type(exc).__name__)
            errors.append({"key": key, "error": str(exc)})
            continue
        applied.append({**_field(key, {key: parsed}), "changed": True})

    if insecure:
        # Nothing insecure was persisted (set_override gates before writing), so
        # the refusal is complete for those keys. `applied` is reported because a
        # batch is not transactional and the caller must know what did land.
        raise HTTPException(
            400,
            {
                "error": "insecure configuration",
                "errors": insecure,
                "applied": [entry["key"] for entry in applied],
            },
        )
    if errors and not applied:
        raise HTTPException(422, {"errors": errors})
    if errors:
        return {
            "ok": True,
            "partial": True,
            "applied": applied,
            "errors": errors,
        }
    return {"ok": True, "applied": applied, "errors": []}
