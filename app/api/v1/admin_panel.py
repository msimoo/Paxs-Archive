"""PaxsViewer Control Panel web bridge — REST API.

Backs the control-panel renderer served at ``/admin`` (see
``app/static/admin/admin-bridge.js``). Status/logs/config are available to any
authenticated user; destructive actions (start/stop/restart, config save)
require an ADMIN.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends

from app.api.v1.deps import get_current_user, require_admin
from app.db.models import User
from app.services.admin_panel_service import get_admin_panel_service

router = APIRouter(prefix="/api/v1/admin-panel", tags=["admin-panel"])

_SERVICES = ("orthanc", "proxy", "api", "adminFront")
_LAUNCH_TARGETS = {"launch-viewer": "viewer", "launch-admin": "admin",
                   "open-viewer": "viewer", "open-admin": "admin"}


@router.get("/status")
async def panel_status(_user: User = Depends(get_current_user)) -> dict[str, Any]:
    svc = get_admin_panel_service()
    return await asyncio.to_thread(svc.snapshot)


@router.get("/logs")
async def panel_logs(_user: User = Depends(get_current_user)) -> list[str]:
    return get_admin_panel_service().logs


@router.get("/config")
async def panel_get_config(_user: User = Depends(get_current_user)) -> dict[str, Any]:
    return get_admin_panel_service().config


@router.post("/config")
async def panel_save_config(
    body: dict[str, Any] | None = None,
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    svc = get_admin_panel_service()
    return await asyncio.to_thread(svc.save_config, body or {})


@router.post("/actions/{action}")
async def panel_action(
    action: str,
    body: dict[str, Any] | None = None,
    _user: User = Depends(require_admin),
) -> dict[str, Any]:
    svc = get_admin_panel_service()
    if action in ("start-all", "stop-all"):
        fn = svc.start_all if action == "start-all" else svc.stop_all
        return await asyncio.to_thread(fn)
    if action == "restart":
        service = (body or {}).get("service")
        if service not in _SERVICES:
            return {"ok": False, "error": f"unknown service {service!r}"}
        result = await asyncio.to_thread(svc.restart_service, service)
        return {"ok": result["status"] == "running", "changes": [result]}
    if action in _LAUNCH_TARGETS:
        return await asyncio.to_thread(svc.launch, _LAUNCH_TARGETS[action])
    return {"ok": False, "error": f"unknown action {action!r}"}
