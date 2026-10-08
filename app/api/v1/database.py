"""Database administration API — Settings → Database tab.

GET    /api/v1/db/status            -> backend, masked URL, migration state,
                                      saved config + backup summary (auth).
POST   /api/v1/db/test              -> probe a MySQL server (admin).
POST   /api/v1/db/configure         -> persist a backend (server_settings +
                                      .env) — takes effect after restart (admin).
POST   /api/v1/db/init              -> create DB (MySQL) + run Alembic to head,
                                      optionally seed the first ADMIN (admin).
POST   /api/v1/db/backup            -> snapshot the running DB to a dump file.
GET    /api/v1/db/backups           -> list backup files.
GET    /api/v1/db/backups/{name}    -> download a backup.
DELETE /api/v1/db/backups/{name}    -> remove a backup.
"""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.api.v1.deps import get_current_user, require_admin
from app.db.models import User
from app.services import db_admin

router = APIRouter(prefix="/api/v1/db", tags=["database"])

MYSQL_FIELDS = ("host", "port", "user", "password", "name")


class MysqlCfg(BaseModel):
    backend: str = "mysql"
    host: str = ""
    port: int = 3306
    user: str = ""
    password: str = ""
    name: str = ""


class ConfigureBody(MysqlCfg):
    persist_env: bool = True


class TestBody(MysqlCfg):
    pass


class InitBody(BaseModel):
    url: str | None = None            # advanced: explicit target DATABASE_URL
    backend: str | None = None
    host: str | None = None
    port: int | None = None
    user: str | None = None
    password: str | None = None
    name: str | None = None
    use_saved: bool = True
    seed_admin: bool = True
    admin_username: str = Field(default="admin", min_length=3, max_length=64)
    admin_password: str = Field(default="admin123", min_length=6, max_length=128)


def _validate_mysql(cfg: dict, required: bool = True) -> None:
    if cfg.get("backend", "mysql") not in ("mysql", "sqlite"):
        raise HTTPException(422, "backend must be 'mysql' or 'sqlite'")
    if cfg.get("backend", "mysql") != "mysql":
        return
    if not (1 <= int(cfg.get("port") or 3306) <= 65535):
        raise HTTPException(422, "port must be 1-65535")
    if required:
        for field in ("host", "user", "name"):
            if not cfg.get(field):
                raise HTTPException(422, f"{field} is required for MySQL")


@router.get("/status")
async def db_status(user: User = Depends(get_current_user)):
    status = await asyncio.to_thread(db_admin.db_status)
    status["admin"] = user.role == "ADMIN"
    return status


@router.post("/test")
async def db_test(body: TestBody, _user: User = Depends(require_admin)):
    cfg = body.model_dump()
    _validate_mysql(cfg)
    if cfg["backend"] != "mysql":
        return {"ok": False, "error": "Connection tests apply to MySQL only"}
    if not cfg.get("password"):
        cfg["password"] = db_admin.saved_password()
    return await asyncio.to_thread(db_admin.test_mysql_connection, cfg)


@router.post("/configure")
async def db_configure(body: ConfigureBody, _user: User = Depends(require_admin)):
    cfg = body.model_dump()
    _validate_mysql(cfg)

    if cfg["backend"] == "sqlite":
        url = "sqlite+aiosqlite:///./paxs.db"
        await asyncio.to_thread(db_admin.clear_saved_config)
    else:
        url = db_admin.build_mysql_url(cfg)
        await asyncio.to_thread(db_admin.save_config, cfg)

    env_path = None
    if body.persist_env:
        env_path = str(await asyncio.to_thread(db_admin.write_env_url, url))

    return {
        "ok": True,
        "backend": cfg["backend"],
        "url_masked": db_admin.masked_url(url),
        "restart": True,
        "env_path": env_path,
        "note": "Saved. The new connection takes effect after the server restarts — "
                "the receiver, worker and API read DATABASE_URL at boot.",
    }


@router.post("/init")
async def db_init(body: InitBody, _user: User = Depends(require_admin)):
    """Initialize a database (new install) or migrate it to head."""
    log: list[str] = []
    try:
        # --- resolve the target URL ---
        if body.url:
            url = body.url
            source = "explicit url"
        else:
            cfg: dict = {}
            provided = {"host": body.host, "user": body.user, "name": body.name}
            if body.backend == "mysql" and not any(provided.values()) and not body.password:
                # Empty MySQL form (fresh default) — fall back to the saved
                # configuration, then the current database, instead of 422ing.
                if body.use_saved and db_admin.saved_config():
                    cfg = dict(db_admin.saved_config())
                    cfg["password"] = db_admin.saved_password()
                    url, source = db_admin.build_mysql_url(cfg), "saved configuration"
                else:
                    url, source = db_admin.resolve_url(), "current database"
            elif body.backend == "mysql" or any(provided.values()):
                cfg = {
                    "backend": "mysql",
                    "host": body.host or "",
                    "port": body.port or 3306,
                    "user": body.user or "",
                    "password": body.password or "",
                    "name": body.name or "",
                }
                if not cfg["password"]:
                    cfg["password"] = db_admin.saved_password()
                _validate_mysql(cfg)
                source = "form"
            elif body.backend == "sqlite":
                url, source = "sqlite+aiosqlite:///./paxs.db", "sqlite default"
            elif body.use_saved and db_admin.saved_config():
                cfg = dict(db_admin.saved_config())
                cfg["password"] = db_admin.saved_password()
                url, source = db_admin.build_mysql_url(cfg), "saved configuration"
            else:
                url, source = db_admin.resolve_url(), "current database"

        log.append(f"Target: {db_admin.masked_url(url)} ({source})")

        # --- MySQL: create the database if missing ---
        created = await asyncio.to_thread(db_admin.ensure_database, url)
        if created:
            log.append("Database created (CREATE DATABASE IF NOT EXISTS, utf8mb4)")

        # --- migrate to head (H11: never auto-stamps) ---
        before = db_admin.current_revision(url)
        steps, _stamped = await asyncio.to_thread(db_admin.run_migrations, url)
        after = db_admin.current_revision(url)
        log.extend(steps or ["Schema is already at head."])
        log.append(f"Revision: {before or 'none'} -> {after or 'none'}")

        # --- seed the first admin (new installs only) ---
        seeded = None
        if body.seed_admin:
            result = await asyncio.to_thread(
                db_admin.seed_admin, url, body.admin_username, body.admin_password
            )
            seeded = result
            log.append(
                f"Admin: {result.get('username', '')} created — login with that account"
                if result.get("created")
                else f"Admin: skipped ({result.get('reason', 'already exists')})"
            )

        return {
            "ok": True,
            "backend": db_admin.parse_db_url(url)["backend"],
            "url_masked": db_admin.masked_url(url),
            "head_revision": after or db_admin.head_revision(),
            "current_revision": after,
            # H11: never automatic — stamping is an explicit, parity-checked
            # operator action (db_admin.stamp_head), so this is always False.
            "stamped": bool(_stamped),
            "seeded": bool(seeded and seeded.get("created")),
            "log": log,
        }
    except HTTPException:
        raise
    except Exception as exc:  # surface migration/connectivity failures cleanly
        log.append(f"ERROR: {exc}")
        raise HTTPException(500, {"error": str(exc), "log": log}) from exc


@router.post("/backup")
async def db_backup(_user: User = Depends(require_admin)):
    result = await asyncio.to_thread(db_admin.create_backup)
    return {"ok": True, **result}


@router.get("/backups")
async def db_backups(_user: User = Depends(require_admin)):
    return await asyncio.to_thread(db_admin.list_backups)


@router.get("/backups/{name}")
async def db_backup_download(name: str, _user: User = Depends(require_admin)):
    try:
        path = db_admin.backup_path(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc
    return FileResponse(
        path, media_type="application/octet-stream", filename=name
    )


@router.delete("/backups/{name}")
async def db_backup_delete(name: str, _user: User = Depends(require_admin)):
    try:
        path = db_admin.backup_path(name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc
    await asyncio.to_thread(path.unlink)
    return {"ok": True, "file": name}
