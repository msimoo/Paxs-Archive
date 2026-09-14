"""Redis client helper, caching layer, and full administration engine.

Provides a thread-safe Redis connection pool, key namespace helpers, cache decorators,
real-time performance metrics, server process/service lifecycle controls (Start/Stop/Restart/Install),
and administrative controls for fast sub-5ms DICOM metadata & rendered image retrieval.
Gracefully falls back to Tier-1 In-Memory LRU RAM cache if Redis is unreachable or disabled.
"""

from __future__ import annotations

import fnmatch
import json
import logging
import os
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from collections import OrderedDict
from pathlib import Path
from typing import Any

import redis
from sqlalchemy import select

from app.core.config import settings
from app.db.models import ServerSetting
from app.db.session import SyncSessionLocal

log = logging.getLogger("paxs.redis")

REDIS_CONFIG_KEY = "redis_config"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOOLS_REDIS_DIR = PROJECT_ROOT / ".tools" / "redis"

_redis_client: redis.Redis | None = None
_redis_available: bool = False
_redis_lock = threading.Lock()

# High-speed In-Memory LRU Cache (Layer 1 RAM Cache - max 256MB / 4000 items)
_MEM_CACHE: OrderedDict[str, tuple[bytes, float]] = OrderedDict()
_MEM_LOCK = threading.Lock()
_MEM_MAX_ITEMS = 4000
_MEM_MAX_BYTES = 256 * 1024 * 1024  # 256 MB cap
_CURRENT_MEM_BYTES = 0


# ------------------------------------------------------------- URL helpers --
def parse_redis_url(url: str) -> dict:
    """Split a Redis URL into component connection fields."""
    if not url:
        return {
            "host": "127.0.0.1",
            "port": 6379,
            "db": 0,
            "password": "",
            "ssl": False,
            "user": "",
        }
    try:
        parsed = urllib.parse.urlparse(url)
        scheme = parsed.scheme.lower()
        ssl = scheme in ("rediss",)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 6379
        path = parsed.path.lstrip("/")
        db = int(path) if path.isdigit() else 0
        user = parsed.username or ""
        password = parsed.password or ""
        return {
            "host": host,
            "port": port,
            "db": db,
            "password": password,
            "ssl": ssl,
            "user": user,
        }
    except Exception as exc:
        log.debug("parse_redis_url error for %s: %s", url, exc)
        return {
            "host": "127.0.0.1",
            "port": 6379,
            "db": 0,
            "password": "",
            "ssl": False,
            "user": "",
        }


def build_redis_url(cfg: dict) -> str:
    """Assemble a redis:// URL from {host, port, db, password, ssl, user}."""
    host = cfg.get("host") or "127.0.0.1"
    port = int(cfg.get("port") or 6379)
    db = int(cfg.get("db") or 0)
    password = cfg.get("password") or ""
    user = cfg.get("user") or ""
    ssl = bool(cfg.get("ssl", False))
    scheme = "rediss" if ssl else "redis"

    auth = ""
    if user and password:
        auth = f"{urllib.parse.quote_plus(user)}:{urllib.parse.quote_plus(password)}@"
    elif password:
        auth = f":{urllib.parse.quote_plus(password)}@"

    return f"{scheme}://{auth}{host}:{port}/{db}"


def masked_redis_url(url: str) -> str:
    """Return connection string with password hidden."""
    p = parse_redis_url(url)
    scheme = "rediss" if p.get("ssl") else "redis"
    host = p.get("host") or "127.0.0.1"
    port = p.get("port") or 6379
    db = p.get("db", 0)
    user = p.get("user")
    pwd = p.get("password")
    if user and pwd:
        auth = f"{user}:{'*' * max(4, len(pwd))}@"
    elif pwd:
        auth = f":{'*' * max(4, len(pwd))}@"
    else:
        auth = ""
    return f"{scheme}://{auth}{host}:{port}/{db}"


# ------------------------------------------------------------- Persistence --
def env_file_path() -> Path:
    return Path(getattr(settings, "ENV_FILE_PATH", ".env") or ".env")


def read_env() -> dict[str, str]:
    path = env_file_path()
    if not path.exists():
        return {}
    data: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        data[key.strip()] = value.strip().strip("\"'")
    return data


def saved_redis_password() -> str:
    """Read saved Redis password from .env or database."""
    try:
        url = read_env().get("REDIS_URL", "")
        if url.startswith("redis"):
            return parse_redis_url(url).get("password") or ""
    except Exception:
        pass
    try:
        saved = saved_redis_config()
        if saved and saved.get("password"):
            return saved["password"]
    except Exception:
        pass
    return ""


def saved_redis_config() -> dict | None:
    """The operator's last saved Redis configuration from server_settings."""
    with SyncSessionLocal() as session:
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == REDIS_CONFIG_KEY)
        ).scalar_one_or_none()
        if not row or not row.value:
            return None
        try:
            return json.loads(row.value)
        except Exception:
            return None


def write_env_redis(url: str, enabled: bool) -> Path:
    """Rewrite REDIS_URL and REDIS_CACHE_ENABLED inside .env (preserving other keys)."""
    path = env_file_path()
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    url_line = f'REDIS_URL="{url}"'
    enabled_line = f'REDIS_CACHE_ENABLED={"true" if enabled else "false"}'

    url_found = False
    enabled_found = False
    for i, raw in enumerate(lines):
        if raw.strip().startswith("REDIS_URL="):
            lines[i] = url_line
            url_found = True
        elif raw.strip().startswith("REDIS_CACHE_ENABLED="):
            lines[i] = enabled_line
            enabled_found = True

    if not url_found:
        lines.append(url_line)
    if not enabled_found:
        lines.append(enabled_line)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def save_redis_config(cfg: dict, persist_env: bool = True) -> dict:
    """Persist Redis configuration to database and .env, then live-reload client."""
    host = cfg.get("host") or "127.0.0.1"
    port = int(cfg.get("port") or 6379)
    db = int(cfg.get("db") or 0)
    password = cfg.get("password") or ""
    enabled = bool(cfg.get("enabled", True))
    ssl = bool(cfg.get("ssl", False))

    if not password:
        password = saved_redis_password()

    clean_cfg = {
        "host": host,
        "port": port,
        "db": db,
        "password": password,
        "enabled": enabled,
        "ssl": ssl,
    }
    url = build_redis_url(clean_cfg)

    # Save to server_settings table
    with SyncSessionLocal() as session:
        payload = json.dumps(clean_cfg)
        row = session.execute(
            select(ServerSetting).where(ServerSetting.key == REDIS_CONFIG_KEY)
        ).scalar_one_or_none()
        if row is None:
            session.add(ServerSetting(key=REDIS_CONFIG_KEY, value=payload))
        else:
            row.value = payload

        # Also store REDIS_URL & REDIS_CACHE_ENABLED keys
        row_url = session.execute(
            select(ServerSetting).where(ServerSetting.key == "REDIS_URL")
        ).scalar_one_or_none()
        if row_url is None:
            session.add(ServerSetting(key="REDIS_URL", value=url))
        else:
            row_url.value = url

        row_en = session.execute(
            select(ServerSetting).where(ServerSetting.key == "REDIS_CACHE_ENABLED")
        ).scalar_one_or_none()
        if row_en is None:
            session.add(ServerSetting(key="REDIS_CACHE_ENABLED", value="1" if enabled else "0"))
        else:
            row_en.value = "1" if enabled else "0"

        session.commit()

    env_path = None
    if persist_env:
        env_path = str(write_env_redis(url, enabled))

    # Live reload the client
    reset_redis_client(new_url=url, enabled=enabled)

    return {
        "ok": True,
        "url_masked": masked_redis_url(url),
        "enabled": enabled,
        "env_path": env_path,
    }


# ------------------------------------------------------------- Client Pool --
def reset_redis_client(new_url: str | None = None, enabled: bool | None = None) -> redis.Redis | None:
    """Reset and re-establish the Redis client connection pool with new settings."""
    global _redis_client, _redis_available
    with _redis_lock:
        if new_url is not None:
            setattr(settings, "REDIS_URL", new_url)
        if enabled is not None:
            setattr(settings, "REDIS_CACHE_ENABLED", enabled)

        if _redis_client is not None:
            try:
                _redis_client.close()
            except Exception:
                pass
            _redis_client = None
            _redis_available = False

    return get_redis_client()


def get_redis_client() -> redis.Redis | None:
    """Return a shared thread-safe Redis client instance, or None if unreachable."""
    global _redis_client, _redis_available
    if not getattr(settings, "REDIS_CACHE_ENABLED", True) or not getattr(settings, "REDIS_URL", None):
        return None

    if _redis_client is None:
        with _redis_lock:
            if _redis_client is None:
                try:
                    client = redis.Redis.from_url(
                        settings.REDIS_URL,
                        decode_responses=False,
                        socket_timeout=1.5,
                        socket_connect_timeout=1.5,
                    )
                    client.ping()
                    _redis_client = client
                    _redis_available = True
                    log.info("Connected to Redis at %s", masked_redis_url(settings.REDIS_URL))
                except Exception as exc:
                    _redis_client = None
                    _redis_available = False
                    log.debug("Redis unavailable (%s); using in-memory LRU cache", exc)

    return _redis_client if _redis_available else None


# ------------------------------------------------------------- Server Lifecycle (Start/Stop/Restart/Install) --
def _get_service_status(service_name: str) -> str | None:
    """Query status of a Windows service ('RUNNING', 'STOPPED', etc.)."""
    try:
        out = subprocess.check_output(
            ["sc", "query", service_name],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        if "RUNNING" in out:
            return "RUNNING"
        if "STOPPED" in out:
            return "STOPPED"
        if "PAUSED" in out:
            return "PAUSED"
        return "UNKNOWN"
    except Exception:
        return None


def get_redis_server_installation() -> dict:
    """Detect Redis/Memurai executable paths, Windows service status, and running processes."""
    candidates = [
        ("Memurai (Native Windows Redis)", r"C:\Program Files\Memurai\memurai.exe", "Memurai"),
        ("Redis for Windows", r"C:\Program Files\Redis\redis-server.exe", "Redis"),
        ("Portable Redis (.tools)", str(TOOLS_REDIS_DIR / "redis-server.exe"), None),
        ("Custom Redis (C:\\Redis)", r"C:\Redis\redis-server.exe", None),
    ]

    path_binary = shutil.which("redis-server")
    if path_binary:
        candidates.append(("Redis on PATH", path_binary, None))

    installed_exe = None
    product_name = None
    matched_service = None

    for name, path_str, s_name in candidates:
        if path_str and os.path.exists(path_str):
            installed_exe = path_str
            product_name = name
            matched_service = s_name
            break

    # Check services
    service_found = None
    service_status = None
    for s_name in ["Memurai", "Redis"]:
        st = _get_service_status(s_name)
        if st is not None:
            service_found = s_name
            service_status = st
            break

    # Check running processes
    process_running = False
    process_name = None
    pid = None

    try:
        if os.name == "nt":
            out = subprocess.check_output(
                ["tasklist", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
                text=True,
                timeout=3,
            )
            for line in out.splitlines():
                row = line.replace('"', '').split(",")
                if len(row) >= 2:
                    pname = row[0].lower()
                    if pname in ("memurai.exe", "redis-server.exe", "redis.exe"):
                        process_running = True
                        process_name = row[0]
                        pid = int(row[1]) if row[1].isdigit() else None
                        break
    except Exception:
        pass

    return {
        "installed": bool(installed_exe or service_found or process_running),
        "product_name": product_name or ("Memurai Service" if service_found == "Memurai" else ("Redis Service" if service_found else "Not detected")),
        "exe_path": installed_exe,
        "service_name": service_found or matched_service,
        "service_status": service_status,
        "process_running": process_running,
        "process_name": process_name,
        "pid": pid,
    }


def start_redis_server() -> dict:
    """Start Redis / Memurai via Windows Service or direct background process."""
    # 0. If already running and responsive, return immediately
    probe = test_redis_connection()
    if probe.get("ok"):
        reset_redis_client()
        return {
            "ok": True,
            "message": f"Redis server is already active and responding ({probe.get('latency_ms')} ms)",
            "probe": probe,
        }

    info = get_redis_server_installation()
    service_name = info.get("service_name")
    exe_path = info.get("exe_path")

    # 1. Try starting Windows service if detected
    service_err = None
    if service_name:
        try:
            res = subprocess.run(
                ["net", "start", service_name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
            if res.returncode != 0:
                service_err = (res.stderr or res.stdout or "").strip()
        except Exception as exc:
            service_err = str(exc)
            log.debug("Service start attempt for %s: %s", service_name, exc)

    # 2. Check if service start succeeded
    probe = test_redis_connection()
    if probe.get("ok"):
        reset_redis_client()
        return {
            "ok": True,
            "message": f"Redis service '{service_name}' started successfully ({probe.get('latency_ms')} ms)",
            "probe": probe,
        }

    # 3. If service couldn't start or no service, try direct executable launch
    direct_started = False
    candidates_to_try = []
    if exe_path and os.path.exists(exe_path):
        candidates_to_try.append(exe_path)
    portable_exe = TOOLS_REDIS_DIR / "redis-server.exe"
    if portable_exe.exists() and str(portable_exe) not in candidates_to_try:
        candidates_to_try.append(str(portable_exe))

    for candidate_exe in candidates_to_try:
        try:
            creation_flags = 0
            if os.name == "nt":
                creation_flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
            subprocess.Popen(
                [candidate_exe],
                creationflags=creation_flags,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                cwd=str(Path(candidate_exe).parent),
            )
            direct_started = True
            break
        except Exception as exc:
            log.debug("Direct exe start failed for %s: %s", candidate_exe, exc)

    # 4. Wait up to 4 seconds for Redis to become available
    for _ in range(8):
        time.sleep(0.5)
        probe = test_redis_connection()
        if probe.get("ok"):
            reset_redis_client()
            return {
                "ok": True,
                "message": f"Redis server started successfully (latency: {probe.get('latency_ms')} ms)",
                "probe": probe,
            }

    err_msg = "Failed to start Redis server."
    if service_err and "5" in service_err:  # Access is denied
        err_msg = f"Service '{service_name}' requires Windows Administrator rights to start. Please start the '{service_name}' service in services.msc or run the application as Administrator."
    elif not info.get("installed"):
        err_msg = "No Redis installation found on host. Click 'Install / Setup Portable Redis' to set it up automatically."
    elif service_err:
        err_msg = f"Failed to start Redis service ({service_err})."

    return {
        "ok": False,
        "error": err_msg,
        "probe": probe,
    }


def stop_redis_server() -> dict:
    """Gracefully stop Redis / Memurai service or terminate its process."""
    info = get_redis_server_installation()
    service_name = info.get("service_name")

    # 1. Try sending graceful Redis SHUTDOWN command
    try:
        client = get_redis_client()
        if client:
            try:
                client.shutdown(nosave=True)
            except Exception:
                pass
    except Exception:
        pass

    # 2. Stop Windows service if running
    if service_name:
        try:
            subprocess.run(
                ["net", "stop", service_name],
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=10,
            )
        except Exception as exc:
            log.debug("Service stop attempt for %s: %s", service_name, exc)

    # 3. Terminate process if still active
    if os.name == "nt":
        for proc in ("memurai.exe", "redis-server.exe"):
            try:
                subprocess.run(["taskkill", "/F", "/IM", proc], check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                pass

    reset_redis_client()
    return {"ok": True, "message": "Redis server stopped."}


def restart_redis_server() -> dict:
    """Restart Redis server."""
    stop_redis_server()
    time.sleep(1.0)
    return start_redis_server()


def install_redis_portable() -> dict:
    """Download and set up portable Redis for Windows in .tools/redis/."""
    TOOLS_REDIS_DIR.mkdir(parents=True, exist_ok=True)
    target_exe = TOOLS_REDIS_DIR / "redis-server.exe"

    if target_exe.exists():
        start_res = start_redis_server()
        return {
            "ok": start_res.get("ok", True),
            "message": "Portable Redis is already installed." + (" Started successfully." if start_res.get("ok") else ""),
            "path": str(target_exe),
            "start": start_res,
        }

    # Download from reliable Windows Redis binary archive
    download_url = "https://github.com/tporadowski/redis/releases/download/v5.0.14.1/Redis-x64-5.0.14.1.zip"
    zip_path = TOOLS_REDIS_DIR / "redis_download.zip"

    try:
        urllib.request.urlretrieve(download_url, zip_path)
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(TOOLS_REDIS_DIR)
        if zip_path.exists():
            zip_path.unlink()

        # Start it
        start_res = start_redis_server()
        return {
            "ok": True,
            "message": "Portable Redis downloaded, extracted, and started successfully!",
            "path": str(target_exe),
            "start": start_res,
        }
    except Exception as exc:
        return {"ok": False, "error": f"Failed to download/install portable Redis: {exc}"}


# ------------------------------------------------------------- Probes & Status --
def test_redis_connection(cfg: dict | str | None = None) -> dict:
    """Probe a Redis server, measuring ping latency and reading basic server info."""
    if isinstance(cfg, str):
        url = cfg
    elif isinstance(cfg, dict):
        url = build_redis_url(cfg)
    else:
        url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")

    t0 = time.perf_counter()
    try:
        client = redis.Redis.from_url(
            url,
            decode_responses=True,
            socket_timeout=2.0,
            socket_connect_timeout=2.0,
        )
        ping_ok = client.ping()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        info = client.info()
        dbsize = client.dbsize()
        client.close()
        return {
            "ok": True,
            "ping": ping_ok,
            "latency_ms": latency_ms,
            "version": info.get("redis_version", "unknown"),
            "mode": info.get("redis_mode", "standalone"),
            "os": info.get("os", ""),
            "role": info.get("role", "master"),
            "uptime_seconds": info.get("uptime_in_seconds", 0),
            "connected_clients": info.get("connected_clients", 0),
            "used_memory_human": info.get("used_memory_human", ""),
            "total_keys": dbsize,
            "url_masked": masked_redis_url(url),
        }
    except Exception as exc:
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {
            "ok": False,
            "error": str(exc),
            "latency_ms": latency_ms,
            "url_masked": masked_redis_url(url),
        }


def ping_redis() -> dict:
    """Execute PING on the active Redis client with latency timing."""
    client = get_redis_client()
    if not client:
        return {"ok": False, "error": "Redis client is offline or disabled"}
    t0 = time.perf_counter()
    try:
        res = client.ping()
        latency_ms = round((time.perf_counter() - t0) * 1000, 2)
        return {"ok": True, "ping": res, "latency_ms": latency_ms}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def get_redis_status() -> dict:
    """Comprehensive status, metrics, memory and keyspace info for the Redis panel."""
    url = getattr(settings, "REDIS_URL", "redis://127.0.0.1:6379/0")
    enabled = getattr(settings, "REDIS_CACHE_ENABLED", True)
    parsed = parse_redis_url(url)
    masked = masked_redis_url(url)

    l1_stats = get_l1_cache_stats()
    saved = saved_redis_config()
    server_install = get_redis_server_installation()

    probe = test_redis_connection(url)
    connected = probe.get("ok", False)

    server_info = {}
    memory_info = {}
    stats_info = {}
    persistence_info = {}

    if connected:
        try:
            client = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_timeout=1.5,
                socket_connect_timeout=1.5,
            )
            raw_info = client.info()
            dbsize = client.dbsize()
            client.close()

            hits = int(raw_info.get("keyspace_hits", 0))
            misses = int(raw_info.get("keyspace_misses", 0))
            total_lookups = hits + misses
            hit_rate = round((hits / total_lookups * 100), 1) if total_lookups > 0 else 0.0

            server_info = {
                "version": raw_info.get("redis_version", "unknown"),
                "mode": raw_info.get("redis_mode", "standalone"),
                "os": raw_info.get("os", "unknown"),
                "uptime_seconds": raw_info.get("uptime_in_seconds", 0),
                "uptime_days": round(raw_info.get("uptime_in_seconds", 0) / 86400, 1),
                "role": raw_info.get("role", "master"),
                "tcp_port": raw_info.get("tcp_port", parsed["port"]),
                "connected_clients": raw_info.get("connected_clients", 0),
                "blocked_clients": raw_info.get("blocked_clients", 0),
            }

            memory_info = {
                "used_memory_human": raw_info.get("used_memory_human", "0B"),
                "used_memory_peak_human": raw_info.get("used_memory_peak_human", "0B"),
                "maxmemory_human": raw_info.get("maxmemory_human", "0B"),
                "mem_fragmentation_ratio": raw_info.get("mem_fragmentation_ratio", 1.0),
                "used_memory_lua_human": raw_info.get("used_memory_lua_human", "0B"),
            }

            stats_info = {
                "total_keys": dbsize,
                "keyspace_hits": hits,
                "keyspace_misses": misses,
                "hit_rate_pct": hit_rate,
                "total_commands_processed": raw_info.get("total_commands_processed", 0),
                "evicted_keys": raw_info.get("evicted_keys", 0),
                "expired_keys": raw_info.get("expired_keys", 0),
            }

            persistence_info = {
                "rdb_last_save_time": raw_info.get("rdb_last_save_time", 0),
                "rdb_bgsave_in_progress": raw_info.get("rdb_bgsave_in_progress", 0) == 1,
                "aof_enabled": raw_info.get("aof_enabled", 0) == 1,
            }
        except Exception as exc:
            log.debug("Error gathering detailed Redis stats: %s", exc)

    return {
        "ok": True,
        "connected": connected,
        "enabled": enabled,
        "url_masked": masked,
        "latency_ms": probe.get("latency_ms"),
        "error": probe.get("error") if not connected else None,
        "parsed": {
            "host": parsed["host"],
            "port": parsed["port"],
            "db": parsed["db"],
            "has_password": bool(parsed.get("password") or saved_redis_password()),
            "ssl": parsed.get("ssl", False),
        },
        "saved": saved,
        "saved_present": saved is not None,
        "server_install": server_install,
        "server_info": server_info,
        "memory_info": memory_info,
        "stats_info": stats_info,
        "persistence_info": persistence_info,
        "tier1_ram": l1_stats,
    }


def get_redis_raw_info(section: str | None = None) -> dict:
    """Return raw INFO sections from Redis."""
    client = get_redis_client()
    if not client:
        return {"ok": False, "error": "Redis client is offline"}
    try:
        info_client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_timeout=2.0,
        )
        data = info_client.info(section) if section else info_client.info()
        info_client.close()
        return {"ok": True, "info": data}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ------------------------------------------------------------- Keys & Cache Operations --
def list_redis_keys(pattern: str = "*", limit: int = 150) -> dict:
    """Search and inspect keys in Redis and Tier-1 RAM cache."""
    pattern = pattern.strip() or "*"
    results = []

    # 1. Inspect L1 In-Memory RAM Cache keys
    l1_keys = set()
    with _MEM_LOCK:
        for k, (v, exp) in _MEM_CACHE.items():
            if fnmatch.fnmatch(k, pattern):
                l1_keys.add(k)
                ttl = max(0, int(exp - time.time()))
                results.append({
                    "key": k,
                    "type": "bytes (RAM)",
                    "ttl": ttl,
                    "size_bytes": len(v),
                    "tier": "RAM (L1)",
                })
                if len(results) >= limit:
                    break

    # 2. Inspect Redis keys
    client = get_redis_client()
    redis_scanned = 0
    if client and len(results) < limit:
        try:
            r_client = redis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=True,
                socket_timeout=3.0,
            )
            cursor = 0
            while len(results) < limit:
                cursor, keys = r_client.scan(cursor=cursor, match=pattern, count=100)
                redis_scanned += len(keys)
                for k in keys:
                    if k in l1_keys:
                        continue
                    try:
                        k_type = r_client.type(k)
                        ttl = r_client.ttl(k)
                        size = 0
                        if k_type == "string":
                            size = r_client.strlen(k)
                        elif k_type == "zset":
                            size = r_client.zcard(k)
                        elif k_type == "hash":
                            size = r_client.hlen(k)
                        elif k_type == "list":
                            size = r_client.llen(k)

                        results.append({
                            "key": k,
                            "type": k_type,
                            "ttl": ttl,
                            "size_bytes": size,
                            "tier": "Redis (L2)",
                        })
                    except Exception:
                        pass
                    if len(results) >= limit:
                        break
                if cursor == 0:
                    break
            r_client.close()
        except Exception as exc:
            log.debug("list_redis_keys error: %s", exc)

    return {
        "ok": True,
        "pattern": pattern,
        "count": len(results),
        "limit": limit,
        "keys": results,
    }


def get_key_detail(key: str) -> dict:
    """Fetch value preview, TTL, type and size for a specific key."""
    # Check L1 first
    with _MEM_LOCK:
        if key in _MEM_CACHE:
            val, exp = _MEM_CACHE[key]
            preview = val[:256].decode("utf-8", errors="replace") if len(val) <= 256 else f"{val[:128].decode('utf-8', errors='replace')}... [{len(val)} bytes]"
            return {
                "ok": True,
                "key": key,
                "tier": "RAM (L1)",
                "type": "bytes",
                "ttl": max(0, int(exp - time.time())),
                "size_bytes": len(val),
                "preview": preview,
            }

    client = get_redis_client()
    if not client:
        return {"ok": False, "error": f"Key '{key}' not found or Redis is offline"}

    try:
        r_client = redis.Redis.from_url(
            settings.REDIS_URL,
            decode_responses=False,
            socket_timeout=2.0,
        )
        if not r_client.exists(key):
            r_client.close()
            return {"ok": False, "error": f"Key '{key}' does not exist"}

        k_type = r_client.type(key).decode("utf-8", errors="ignore")
        ttl = r_client.ttl(key)
        preview = ""
        size = 0

        if k_type == "string":
            raw = r_client.get(key) or b""
            size = len(raw)
            if size < 512:
                preview = raw.decode("utf-8", errors="replace")
            else:
                preview = f"{raw[:256].decode('utf-8', errors='replace')}... [{size} bytes total]"
        elif k_type == "zset":
            items = r_client.zrange(key, 0, 10, withscores=True)
            size = r_client.zcard(key)
            preview = json.dumps([{"member": m.decode("utf-8", errors="replace"), "score": s} for m, s in items], indent=2)
        elif k_type == "hash":
            h = r_client.hgetall(key)
            size = len(h)
            preview = json.dumps({k.decode("utf-8", errors="replace"): v[:64].decode("utf-8", errors="replace") for k, v in list(h.items())[:10]}, indent=2)
        elif k_type == "list":
            items = r_client.lrange(key, 0, 10)
            size = r_client.llen(key)
            preview = json.dumps([i[:64].decode("utf-8", errors="replace") for i in items], indent=2)

        r_client.close()
        return {
            "ok": True,
            "key": key,
            "tier": "Redis (L2)",
            "type": k_type,
            "ttl": ttl,
            "size_bytes": size,
            "preview": preview,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def delete_redis_key(key: str) -> bool:
    """Delete a key from RAM cache and Redis."""
    deleted_ram = False
    with _MEM_LOCK:
        if key in _MEM_CACHE:
            del _MEM_CACHE[key]
            deleted_ram = True

    client = get_redis_client()
    deleted_redis = False
    if client:
        try:
            deleted_redis = bool(client.delete(key))
        except Exception as exc:
            log.debug("delete_redis_key error: %s", exc)

    return deleted_ram or deleted_redis


def flush_redis_cache(target: str = "paxs", pattern: str = "paxs:*") -> dict:
    """Flush cache keys according to target: 'paxs', 'pattern', 'l1', or 'all'."""
    deleted_ram = 0
    deleted_redis = 0

    if target == "l1":
        deleted_ram = flush_l1_cache()
        return {"ok": True, "target": "l1", "deleted_ram": deleted_ram, "deleted_redis": 0}

    if target == "all":
        deleted_ram = flush_l1_cache()
        client = get_redis_client()
        if client:
            try:
                client.flushdb()
                return {"ok": True, "target": "all", "deleted_ram": deleted_ram, "message": "Flushed entire Redis DB"}
            except Exception as exc:
                return {"ok": False, "error": str(exc)}
        return {"ok": True, "target": "all", "deleted_ram": deleted_ram, "message": "RAM cache cleared (Redis offline)"}

    # Target is 'paxs' or 'pattern'
    pat = "paxs:*" if target == "paxs" else pattern
    with _MEM_LOCK:
        matching = [k for k in _MEM_CACHE if fnmatch.fnmatch(k, pat)]
        for k in matching:
            del _MEM_CACHE[k]
        deleted_ram = len(matching)

    client = get_redis_client()
    if client:
        try:
            r_client = redis.Redis.from_url(
                settings.REDIS_URL,
                decode_responses=False,
                socket_timeout=3.0,
            )
            keys = r_client.keys(pat)
            if keys:
                deleted_redis = r_client.delete(*keys)
            r_client.close()
        except Exception as exc:
            log.debug("flush_redis_cache error for %s: %s", pat, exc)

    return {
        "ok": True,
        "target": target,
        "pattern": pat,
        "deleted_ram": deleted_ram,
        "deleted_redis": deleted_redis,
        "total_deleted": deleted_ram + deleted_redis,
    }


# ------------------------------------------------------------- Tier 1 RAM Cache --
def get_l1_cache_stats() -> dict:
    """Return metrics for the Tier-1 In-Memory RAM LRU cache."""
    with _MEM_LOCK:
        count = len(_MEM_CACHE)
        used_bytes = _CURRENT_MEM_BYTES
    return {
        "items_count": count,
        "max_items": _MEM_MAX_ITEMS,
        "used_bytes": used_bytes,
        "used_mb": round(used_bytes / (1024 * 1024), 2),
        "max_bytes": _MEM_MAX_BYTES,
        "max_mb": int(_MEM_MAX_BYTES / (1024 * 1024)),
        "utilization_pct": round((used_bytes / _MEM_MAX_BYTES) * 100, 1),
    }


def flush_l1_cache() -> int:
    """Clear all items from Tier-1 RAM cache."""
    global _CURRENT_MEM_BYTES
    with _MEM_LOCK:
        count = len(_MEM_CACHE)
        _MEM_CACHE.clear()
        _CURRENT_MEM_BYTES = 0
        return count


# ------------------------------------------------------------- Core Caching API --
def cache_get(key: str) -> bytes | None:
    """Retrieve raw bytes from in-memory RAM cache first, then Redis."""
    now = time.time()

    # 1. Tier 1: Check In-Memory RAM LRU
    with _MEM_LOCK:
        if key in _MEM_CACHE:
            val, exp = _MEM_CACHE[key]
            if exp > now:
                _MEM_CACHE.move_to_end(key)
                return val
            else:
                del _MEM_CACHE[key]

    # 2. Tier 2: Check Redis
    client = get_redis_client()
    if not client:
        return None
    try:
        data = client.get(key)
        if data:
            cache_set_mem(key, data, expire_seconds=300)
        return data
    except Exception as exc:
        log.debug("Redis cache_get error for key %s: %s", key, exc)
        return None


def cache_set_mem(key: str, value: bytes, expire_seconds: int = 600) -> None:
    """Store directly into Layer 1 In-Memory LRU RAM Cache."""
    global _CURRENT_MEM_BYTES
    if not isinstance(value, bytes):
        return
    val_size = len(value)
    if val_size > 16 * 1024 * 1024:  # do not store single items > 16MB in RAM
        return

    exp = time.time() + expire_seconds
    with _MEM_LOCK:
        if key in _MEM_CACHE:
            old_val, _ = _MEM_CACHE[key]
            _CURRENT_MEM_BYTES -= len(old_val)
            del _MEM_CACHE[key]

        while (len(_MEM_CACHE) >= _MEM_MAX_ITEMS or (_CURRENT_MEM_BYTES + val_size) > _MEM_MAX_BYTES) and _MEM_CACHE:
            _, (evicted_val, _) = _MEM_CACHE.popitem(last=False)
            _CURRENT_MEM_BYTES -= len(evicted_val)

        _MEM_CACHE[key] = (value, exp)
        _CURRENT_MEM_BYTES += val_size


def cache_set(key: str, value: bytes | str, expire_seconds: int = 600) -> bool:
    """Store raw bytes or string into both In-Memory LRU Cache and Redis."""
    val_bytes = value.encode("utf-8") if isinstance(value, str) else value
    cache_set_mem(key, val_bytes, expire_seconds=expire_seconds)

    client = get_redis_client()
    if not client:
        return True
    try:
        client.set(key, val_bytes, ex=expire_seconds)
        return True
    except Exception as exc:
        log.debug("Redis cache_set error for key %s: %s", key, exc)
        return True


def cache_delete_pattern(pattern: str) -> int:
    """Delete all keys matching pattern from memory and Redis."""
    return flush_redis_cache(target="pattern", pattern=pattern).get("total_deleted", 0)
