"""Database administration API — Settings → Database tab.

GET    /api/v1/db/status            -> backend, masked URL, migration state,
                                      saved config + backup summary (auth).
POST   /api/v1/db/test              -> probe a PostgreSQL server (admin).
POST   /api/v1/db/configure         -> persist a backend (server_settings +
                                      .env) — takes effect after restart (admin).
POST   /api/v1/db/init              -> create DB (PostgreSQL) + run Alembic to head,
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
from app.core.config import (
    MIN_ADMIN_PASSWORD_LENGTH,
    bootstrap_enabled,
    is_weak_admin_password,
    settings,
    setup_token_matches,
)
from app.db.models import User
from app.services import db_admin

router = APIRouter(prefix="/api/v1/db", tags=["database"])

PG_FIELDS = ("host", "port", "user", "password", "name")


class PostgresCfg(BaseModel):
    backend: str = "postgresql"
    host: str = ""
    port: int = 5432
    user: str = ""
    password: str = ""
    name: str = ""


class ConfigureBody(PostgresCfg):
    persist_env: bool = True


class TestBody(PostgresCfg):
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
    # B-4: seeding the first administrator is an explicit opt-in, requires the
    # configured SETUP_TOKEN, and has NO default password. The previous shipped
    # default ("admin123") is gone — an operator must supply a strong one.
    seed_admin: bool = False
    setup_token: str | None = None
    admin_username: str = Field(default="admin", min_length=3, max_length=64)
    admin_password: str | None = Field(default=None, max_length=128)


def _validate_postgres(cfg: dict, required: bool = True) -> None:
    if cfg.get("backend", "postgresql") != "postgresql":
        raise HTTPException(422, "backend must be 'postgresql' (MySQL and SQLite are retired)")
    if not (1 <= int(cfg.get("port") or 5432) <= 65535):
        raise HTTPException(422, "port must be 1-65535")
    if required:
        for field in ("host", "user", "name"):
            if not cfg.get(field):
                raise HTTPException(422, f"{field} is required for PostgreSQL")


@router.get("/status")
async def db_status(user: User = Depends(get_current_user)):
    status = await asyncio.to_thread(db_admin.db_status)
    status["admin"] = user.role == "ADMIN"
    return status


@router.post("/test")
async def db_test(body: TestBody, _user: User = Depends(require_admin)):
    cfg = body.model_dump()
    _validate_postgres(cfg)
    if not cfg.get("password"):
        cfg["password"] = db_admin.saved_password()
    return await asyncio.to_thread(db_admin.test_postgres_connection, cfg)


@router.post("/configure")
async def db_configure(body: ConfigureBody, _user: User = Depends(require_admin)):
    cfg = body.model_dump()
    _validate_postgres(cfg)
    url = db_admin.build_postgres_url(cfg)
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
    # B-4: validate the seeding request BEFORE any database work, so a bad setup
    # token cannot trigger a migration run (or reveal whether the target exists).
    if body.seed_admin:
        if not bootstrap_enabled(settings):
            raise HTTPException(
                403,
                "Seeding is disabled: set SETUP_TOKEN before creating the "
                "first administrator.",
            )
        if not setup_token_matches(body.setup_token, settings):
            raise HTTPException(403, "Invalid or missing setup token")
        if is_weak_admin_password(body.admin_password):
            raise HTTPException(
                422,
                "admin_password is required and must be at least "
                f"{MIN_ADMIN_PASSWORD_LENGTH} characters with no common "
                "value - there is no default.",
            )
    log: list[str] = []
    try:
        # --- resolve the target URL ---
        if body.url:
            if db_admin.parse_db_url(body.url)["backend"] != "postgresql":
                raise HTTPException(422, "url must be a PostgreSQL URL (postgresql+asyncpg://...)")
            url = body.url
            source = "explicit url"
        else:
            cfg: dict = {}
            provided = {"host": body.host, "user": body.user, "name": body.name}
            if body.backend not in (None, "", "postgresql"):
                raise HTTPException(422, "backend must be 'postgresql' (MySQL and SQLite are retired)")
            if any(provided.values()) or body.password:
                cfg = {
                    "backend": "postgresql",
                    "host": body.host or "",
                    "port": body.port or 5432,
                    "user": body.user or "",
                    "password": body.password or "",
                    "name": body.name or "",
                }
                if not cfg["password"]:
                    cfg["password"] = db_admin.saved_password()
                _validate_postgres(cfg)
                url, source = db_admin.build_postgres_url(cfg), "form"
            elif body.use_saved and db_admin.saved_config():
                # Empty form (fresh default) — fall back to the saved
                # configuration, then the current database, instead of 422ing.
                cfg = dict(db_admin.saved_config())
                cfg["password"] = db_admin.saved_password()
                url, source = db_admin.build_postgres_url(cfg), "saved configuration"
            else:
                url, source = db_admin.resolve_url(), "current database"

        log.append(f"Target: {db_admin.masked_url(url)} ({source})")

        # --- PostgreSQL: create the database if missing ---
        created = await asyncio.to_thread(db_admin.ensure_database, url)
        if created:
            log.append("Database created (CREATE DATABASE, UTF8)")

        # --- migrate to head (H11: never auto-stamps) ---
        before = db_admin.current_revision(url)
        steps, _stamped = await asyncio.to_thread(db_admin.run_migrations, url)
        after = db_admin.current_revision(url)
        log.extend(steps or ["Schema is already at head."])
        log.append(f"Revision: {before or 'none'} -> {after or 'none'}")

        # --- seed the first admin (new installs only) ---
        # The setup token + password were validated at the top of this handler.
        seeded = None
        if body.seed_admin:
            result = await asyncio.to_thread(
                db_admin.seed_admin, url, body.admin_username, body.admin_password
            )
            seeded = result
            log.append(
                f"Admin: {result.get('username', '')} created — password change "
                "required at first login"
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
    except db_admin.MigrationDriftError as exc:
        # This service error is a bounded schema diagnostic with no connection
        # credentials or filesystem paths; preserve its established detail.
        log.append(f"ERROR: {exc}")
        raise HTTPException(500, {"error": str(exc), "log": log}) from exc
    except Exception as exc:  # database drivers may include credentials or paths
        safe_error = "Database initialization failed"
        log.append(f"ERROR: {safe_error} (error_type={type(exc).__name__})")
        raise HTTPException(500, {"error": safe_error, "log": log}) from exc


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


@router.post("/backups/{name}/verify")
async def db_backup_verify(name: str, _user: User = Depends(require_admin)) -> dict:
    """Verify a backup archive's integrity (header/SQL markers + SHA-256)."""
    try:
        return await asyncio.to_thread(db_admin.verify_backup, name)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(422, str(exc)) from exc


class BackupScheduleBody(BaseModel):
    enabled: bool = False
    time: str = Field(default="02:00", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    retention_count: int = Field(default=7, ge=1, le=90)


@router.get("/backup-schedule")
async def db_get_backup_schedule(_user: User = Depends(require_admin)) -> dict:
    return await asyncio.to_thread(db_admin.get_backup_schedule)


@router.put("/backup-schedule")
async def db_save_backup_schedule(
    body: BackupScheduleBody, _user: User = Depends(require_admin)
) -> dict:
    try:
        return await asyncio.to_thread(
            db_admin.save_backup_schedule, body.enabled, body.time, body.retention_count
        )
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
