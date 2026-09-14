"""Redis administration and cache control API — Settings → Redis tab.

GET    /api/v1/redis/status         -> live Redis & L1 RAM cache metrics, connection info
POST   /api/v1/redis/test           -> probe Redis server without persisting changes (admin)
POST   /api/v1/redis/configure      -> persist configuration (server_settings + .env) (admin)
GET    /api/v1/redis/keys           -> search & list keys matching pattern with TTL & size
GET    /api/v1/redis/key            -> view value preview and details for a specific key
DELETE /api/v1/redis/key            -> delete a single key (admin)
POST   /api/v1/redis/flush          -> flush PAXS cache, custom pattern, L1 RAM or full DB (admin)
POST   /api/v1/redis/ping           -> execute real-time PING latency measurement
GET    /api/v1/redis/info           -> raw INFO sections from Redis server
POST   /api/v1/redis/server/start   -> start Redis server / Windows service (admin)
POST   /api/v1/redis/server/stop    -> stop Redis server / Windows service (admin)
POST   /api/v1/redis/server/restart -> restart Redis server / Windows service (admin)
POST   /api/v1/redis/server/install -> download & set up portable Redis for Windows (admin)
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.api.v1.deps import get_optional_user, require_admin
from app.db.models import User
from app.services import redis_service

router = APIRouter(prefix="/api/v1/redis", tags=["redis"])


class RedisCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0, le=15)
    password: str = ""
    enabled: bool = True
    ssl: bool = False


class ConfigureBody(RedisCfg):
    persist_env: bool = True


class TestBody(BaseModel):
    host: str = "127.0.0.1"
    port: int = Field(default=6379, ge=1, le=65535)
    db: int = Field(default=0, ge=0, le=15)
    password: str = ""
    ssl: bool = False


class FlushBody(BaseModel):
    target: str = Field(default="paxs", description="'paxs', 'pattern', 'l1', or 'all'")
    pattern: str = Field(default="paxs:*", description="Custom wildcard pattern when target is 'pattern'")


class DeleteKeyBody(BaseModel):
    key: str


@router.get("/status")
async def get_status(user: User | None = Depends(get_optional_user)):
    """Retrieve full Redis metrics, memory usage, key stats, install state and L1 RAM status."""
    data = await asyncio.to_thread(redis_service.get_redis_status)
    data["admin"] = bool(user and user.role == "ADMIN")
    data["authenticated"] = bool(user is not None)
    return data


@router.post("/test")
async def test_connection(body: TestBody, _user: User = Depends(require_admin)):
    """Probe Redis server at specified host/port/auth without modifying state."""
    cfg = body.model_dump()
    if not cfg.get("password"):
        cfg["password"] = redis_service.saved_redis_password()
    return await asyncio.to_thread(redis_service.test_redis_connection, cfg)


@router.post("/configure")
async def configure_redis(body: ConfigureBody, _user: User = Depends(require_admin)):
    """Persist new Redis settings and immediately re-connect active pool."""
    cfg = body.model_dump()
    result = await asyncio.to_thread(redis_service.save_redis_config, cfg, body.persist_env)
    return {
        "ok": True,
        "url_masked": result.get("url_masked"),
        "enabled": result.get("enabled"),
        "env_path": result.get("env_path"),
        "note": "Redis configuration saved and connection reloaded successfully.",
    }


@router.get("/keys")
async def list_keys(
    pattern: str = Query(default="*", description="Glob pattern to search"),
    limit: int = Query(default=100, ge=1, le=500),
    _user: User | None = Depends(get_optional_user),
):
    """Search and list keys from Redis and Tier-1 RAM cache."""
    return await asyncio.to_thread(redis_service.list_redis_keys, pattern, limit)


@router.get("/key")
async def get_key(
    key: str = Query(..., description="Exact key name"),
    _user: User | None = Depends(get_optional_user),
):
    """View details and content preview of a specific key."""
    result = await asyncio.to_thread(redis_service.get_key_detail, key)
    if not result.get("ok"):
        raise HTTPException(404, result.get("error", "Key not found"))
    return result


@router.delete("/key")
async def delete_key(body: DeleteKeyBody, _user: User = Depends(require_admin)):
    """Delete a specific key from RAM and Redis."""
    deleted = await asyncio.to_thread(redis_service.delete_redis_key, body.key)
    return {"ok": True, "key": body.key, "deleted": deleted}


@router.post("/flush")
async def flush_cache(body: FlushBody, _user: User = Depends(require_admin)):
    """Flush cache keys according to selected target."""
    result = await asyncio.to_thread(
        redis_service.flush_redis_cache, body.target, body.pattern
    )
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Flush operation failed"))
    return result


@router.post("/ping")
async def ping(_user: User | None = Depends(get_optional_user)):
    """Measure real-time PING round-trip latency against Redis."""
    return await asyncio.to_thread(redis_service.ping_redis)


@router.get("/info")
async def get_raw_info(
    section: str | None = Query(default=None, description="Optional Redis INFO section"),
    _user: User = Depends(require_admin),
):
    """Retrieve raw Redis server INFO sections."""
    result = await asyncio.to_thread(redis_service.get_redis_raw_info, section)
    if not result.get("ok"):
        raise HTTPException(502, result.get("error", "Failed to retrieve Redis INFO"))
    return result


# ------------------------------------------------------------- Server Lifecycle --
@router.post("/server/start")
async def start_server(_user: User = Depends(require_admin)):
    """Start Redis / Memurai server or Windows service."""
    result = await asyncio.to_thread(redis_service.start_redis_server)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Failed to start Redis"))
    return result


@router.post("/server/stop")
async def stop_server(_user: User = Depends(require_admin)):
    """Stop Redis / Memurai server or Windows service."""
    result = await asyncio.to_thread(redis_service.stop_redis_server)
    return result


@router.post("/server/restart")
async def restart_server(_user: User = Depends(require_admin)):
    """Restart Redis server."""
    result = await asyncio.to_thread(redis_service.restart_redis_server)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Failed to restart Redis"))
    return result


@router.post("/server/install")
async def install_server(_user: User = Depends(require_admin)):
    """Download & set up portable Redis for Windows in .tools/redis/."""
    result = await asyncio.to_thread(redis_service.install_redis_portable)
    if not result.get("ok"):
        raise HTTPException(500, result.get("error", "Installation failed"))
    return result
