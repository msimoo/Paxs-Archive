"""Custom server scripts API (roadmap #43) — manage runtime Python scripts.

Admin-only. Scripts live in ``SCRIPTS_DIR`` (default ``scripts/``) and are
loaded at runtime via importlib (hot reload — no restart).

    GET    /api/v1/scripts                 list scripts + status
    POST   /api/v1/scripts/reload          drop the module cache (re-import next dispatch)
    POST   /api/v1/scripts/{name}/toggle   enable/disable via marker file
    POST   /api/v1/scripts/run             explicit synchronous test-run with a sample payload
    POST   /api/v1/scripts/{name}/run      deprecated compatibility alias
    GET    /api/v1/scripts/{name}/source   view the script source (admin)
"""

from __future__ import annotations

import asyncio
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.auth import write_audit
from app.api.v1.deps import require_admin
from app.db.models import User
from app.db.session import get_db
from app.services import scripting_service

_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")

router = APIRouter(prefix="/api/v1/scripts", tags=["scripts"])


class ScriptRun(BaseModel):
    event: str = Field(default="StudyReceived", max_length=64)
    payload: dict = Field(default_factory=dict)


class ScriptRunRequest(ScriptRun):
    name: str = Field(min_length=1, max_length=64)


@router.get("")
async def list_scripts(user: User = Depends(require_admin)):
    # Filesystem listing (iterdir + stat + read) off the event loop.
    scripts = await asyncio.to_thread(scripting_service.list_scripts)
    max_count, max_source_bytes = scripting_service.script_limits()
    return {"scripts": scripts,
            "dir": str(scripting_service.scripts_dir()),
            "enabled": bool(max_count and max_source_bytes),
            "limits": {"max_scripts": max_count, "max_source_bytes": max_source_bytes}}


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


async def _run_script(name: str, body: ScriptRun, request: Request,
                      db: AsyncSession, user: User) -> dict:
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(422, "Name must be 1-64 chars of A-Za-z0-9 _")

    # Record the explicit admin action before execution so even a failing script
    # leaves a durable, PHI-free audit event. Never log the arbitrary payload.
    await write_audit(
        db, user.id, "script_run", request=request, details={"script": name}
    )
    await db.commit()

    result = await asyncio.to_thread(
        scripting_service.run_script_now, name, body.event, body.payload
    )
    if not result.get("ok"):
        raise HTTPException(422, result.get("error", "script run failed"))
    return result


@router.post("/run")
async def run_script(body: ScriptRunRequest, request: Request,
                     db: AsyncSession = Depends(get_db),
                     user: User = Depends(require_admin)):
    """Explicitly execute one named script; ADMIN only and audit logged."""
    return await _run_script(body.name, body, request, db, user)


@router.post("/{name}/run", deprecated=True, include_in_schema=False)
async def run_script_legacy(name: str, body: ScriptRun, request: Request,
                            db: AsyncSession = Depends(get_db),
                            user: User = Depends(require_admin)):
    """Compatibility alias for older admin clients; new clients use /run."""
    return await _run_script(name, body, request, db, user)


@router.get("/{name}/source")
async def script_source(name: str, user: User = Depends(require_admin)):
    path = scripting_service.scripts_dir() / f"{name}.py"
    if not await asyncio.to_thread(path.is_file):
        raise HTTPException(404, f"Script {name!r} not found")
    _max_count, max_source_bytes = scripting_service.script_limits()
    size = await asyncio.to_thread(lambda: path.stat().st_size)
    if not max_source_bytes or size > max_source_bytes:
        raise HTTPException(413, "Script exceeds the configured source size limit")
    source = await asyncio.to_thread(path.read_text, encoding="utf-8")
    return {"name": name, "source": source}


@router.put("/{name}", status_code=201)
async def create_script(name: str, request: Request,
                       user: User = Depends(require_admin)):
    """Create a script file from the request body (the UI sends a template)."""
    if not _NAME_RE.match(name):
        raise HTTPException(422, "Name must be 1-64 chars of A-Za-z0-9 _")
    _max_count, max_source_bytes = scripting_service.script_limits()
    if not max_source_bytes:
        raise HTTPException(413, "Script registration is disabled by configuration")
    chunks = bytearray()
    async for chunk in request.stream():
        if len(chunks) + len(chunk) > max_source_bytes:
            raise HTTPException(413, "Script exceeds the configured source size limit")
        chunks.extend(chunk)
    try:
        source = chunks.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise HTTPException(422, "Script source must be UTF-8") from exc
    if not source:
        raise HTTPException(422, "Empty script body")
    outcome = await asyncio.to_thread(scripting_service.create_script, name, source)
    if outcome == "exists":
        raise HTTPException(409, f"Script {name!r} already exists")
    if outcome == "registry_full":
        raise HTTPException(409, "Script registry is at its configured capacity")
    if outcome == "source_limit":
        raise HTTPException(413, "Script exceeds the configured source size limit")
    return {"name": name, "created": True}
