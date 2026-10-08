"""Custom server scripts API (roadmap #43) — manage runtime Python scripts.

Admin-only. Scripts live in ``SCRIPTS_DIR`` (default ``scripts/``) and are
loaded at runtime via importlib (hot reload — no restart).

    GET    /api/v1/scripts                 list scripts + status
    POST   /api/v1/scripts/reload          drop the module cache (re-import next dispatch)
    POST   /api/v1/scripts/{name}/toggle   enable/disable via marker file
    POST   /api/v1/scripts/{name}/run      synchronous test-run with a sample payload
    GET    /api/v1/scripts/{name}/source   view the script source (admin)
"""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from app.api.v1.deps import require_admin
from app.db.models import User
from app.services import scripting_service

_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

router = APIRouter(prefix="/api/v1/scripts", tags=["scripts"])


class ScriptRun(BaseModel):
    event: str = Field(default="StudyReceived", max_length=64)
    payload: dict = Field(default_factory=dict)


@router.get("")
async def list_scripts(user: User = Depends(require_admin)):
    # Filesystem listing (iterdir + stat + read) off the event loop.
    scripts = await asyncio.to_thread(scripting_service.list_scripts)
    return {"scripts": scripts,
            "dir": str(scripting_service.scripts_dir()),
            "enabled": True}


@router.post("/reload")
async def reload_scripts(user: User = Depends(require_admin)):
    dropped = await asyncio.to_thread(scripting_service.reload_scripts)
    scripts = await asyncio.to_thread(scripting_service.list_scripts)
    return {"dropped": dropped, "scripts": scripts}


@router.post("/{name}/toggle")
async def toggle_script(name: str, user: User = Depends(require_admin)):
    scripts = await asyncio.to_thread(scripting_service.list_scripts)
    current = next((s for s in scripts if s["name"] == name), None)
    if current is None:
        raise HTTPException(404, f"Script {name!r} not found")
    # Writes a marker file — off the event loop.
    await asyncio.to_thread(scripting_service.set_enabled, name, not current["enabled"])
    return {"name": name, "enabled": not current["enabled"]}


@router.post("/{name}/run")
async def run_script(name: str, body: ScriptRun,
                     user: User = Depends(require_admin)):
    # Dynamic import + execution — off the event loop.
    result = await asyncio.to_thread(
        scripting_service.run_script_now, name, body.event, body.payload
    )
    if not result.get("ok"):
        raise HTTPException(422, result.get("error", "script run failed"))
    return result


@router.get("/{name}/source")
async def script_source(name: str, user: User = Depends(require_admin)):
    path = scripting_service.scripts_dir() / f"{name}.py"
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, f"Script {name!r} not found")
    source = await asyncio.to_thread(path.read_text, encoding="utf-8")
    return {"name": name, "source": source}


@router.put("/{name}", status_code=201)
async def create_script(name: str, request: Request,
                       user: User = Depends(require_admin)):
    """Create a script file from the request body (the UI sends a template)."""
    if not _NAME_RE.match(name):
        raise HTTPException(422, "Name must be 1-64 chars of A-Za-z0-9 _")
    path = scripting_service.scripts_dir() / f"{name}.py"
    if await asyncio.to_thread(path.exists):
        raise HTTPException(409, f"Script {name!r} already exists")
    source = (await request.body()).decode("utf-8", "replace").strip()
    if not source:
        raise HTTPException(422, "Empty script body")

    def _write() -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")

    await asyncio.to_thread(_write)
    await asyncio.to_thread(scripting_service.reload_scripts)
    return {"name": name, "created": True}
