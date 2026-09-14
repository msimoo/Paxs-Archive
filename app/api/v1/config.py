"""Admin Settings API — read and edit the server configuration.

GET  /api/v1/config  -> every editable key (registry metadata + live value,
                        "changed" flag when a DB override exists) grouped by
                        Settings tab, plus the live health payload as readonly.
PATCH /api/v1/config  -> validate a batch of {key: value} updates against the
                        editable registry, persist them to `server_settings`
                        and apply them to the runtime settings object
                        immediately. Admin role required.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.core.config import settings
from app.api.v1.deps import get_current_user, require_admin
from app.db.models import User
from app.services import settings_service

router = APIRouter(prefix="/api/v1/config", tags=["config"])


class ConfigPatch(BaseModel):
    updates: dict[str, object] = Field(default_factory=dict)


def _field(key: str, overrides: dict[str, Any]) -> dict:
    """One editable field: registry metadata + live value + changed flag."""
    spec = settings_service.EDITABLE_KEYS[key]
    return {
        "key": key,
        "label": spec["label"],
        "type": spec["type"],
        "options": spec.get("options"),
        "description": spec.get("description", ""),
        "restart": bool(spec.get("restart", False)),
        "value": overrides.get(key, getattr(settings, key)),
        "changed": key in overrides,
    }


@router.get("")
async def get_config(user: User = Depends(get_current_user)):
    # One batched read of every stored override (single query), then render
    # each field as: override ?? env default.
    overrides = settings_service.overrides_map()

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
    from app.api.v1.dashboard import health  # local import avoids a cycle

    readonly = await health()
    return {
        "admin": user.role == "ADMIN",
        "groups": list(groups.values()),
        "readonly": readonly,
    }


@router.patch("")
async def patch_config(body: ConfigPatch, user: User = Depends(require_admin)):
    if not body.updates:
        raise HTTPException(422, "no settings supplied")

    applied: list[dict] = []
    errors: list[dict] = []
    for key, value in body.updates.items():
        if key not in settings_service.EDITABLE_KEYS:
            errors.append({"key": key, "error": "unknown setting"})
            continue
        try:
            parsed = settings_service.set_override(key, value)
        except ValueError as exc:
            errors.append({"key": key, "error": str(exc)})
            continue
        applied.append({**_field(key, {key: parsed}), "changed": True})

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
