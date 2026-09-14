"""Lua / Python scripting — custom server-side scripts (roadmap #43).

Scripts are plain ``.py`` files in ``SCRIPTS_DIR`` (default ``scripts/``),
loaded at runtime with ``importlib`` — no restart needed to add, edit or
remove them (hot reload: each file's mtime is checked on dispatch).

Script contract
---------------
A script may define, in any combination:

* ``EVENTS = [\"StudyReceived\", ...]``  — subscribe to specific events; when
  absent the script is only called for events it has a handler for.
* ``def handle(event: str, ctx) -> None``  — catch-all handler.
* ``def on_<event>(ctx) -> None`` — per-event handler (event name lower-cased,
  e.g. ``on_study_received``).

Handlers receive a :class:`ScriptContext`:

* ``ctx.log``   — a per-script logger (``paxs.scripts.<name>``)
* ``ctx.event`` / ``ctx.payload``  — the event name and its data dict
* ``ctx.study(study_uid)``   — read-only study + series + patient metadata
* ``ctx.instances(study_uid)`` — instance metadata rows (no pixel data)
* ``ctx.read_bytes(key)``    — object bytes from the storage backend
* ``ctx.http_post(url, payload, timeout=8)`` — outbound JSON POST helper
* ``ctx.fire(event, **data)`` — re-dispatch to *other* scripts (chaining)

Dispatch semantics
------------------
``fire(event, **data)`` spawns one daemon thread that walks every enabled
script subscribed to the event and calls its handler. Exceptions are caught
per script and logged — a crashing script never breaks the caller or its
siblings. Scripts can be enabled/disabled via a ``.disabled`` marker file
next to them (``{name}.disabled``) or the Admin → Scripts panel.

Security note: scripts are *trusted admin code* — they run in-process with
the same privileges as the server (this mirrors Orthanc Lua / Conquest
macros). They should not be written by untrusted users.
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
import threading
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.core.config import settings

log = logging.getLogger("paxs.scripts")


# --------------------------------------------------------------------------- #
# Script discovery
# --------------------------------------------------------------------------- #
def scripts_dir() -> Path:
    return Path(settings.SCRIPTS_DIR or "scripts")


def _script_files() -> list[Path]:
    """All loadable ``*.py`` files (skips names starting with ``_``)."""
    d = scripts_dir()
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.glob("*.py")):
        if p.name.startswith("_"):
            continue
        out.append(p)
    return out


def is_enabled(path: Path) -> bool:
    return not path.with_suffix(".py.disabled").exists()


def set_enabled(name: str, enabled: bool) -> bool:
    """Enable/disable a script via a marker file; returns False if unknown."""
    path = scripts_dir() / f"{name}.py"
    if not path.is_file():
        return False
    marker = scripts_dir() / f"{name}.py.disabled"
    if enabled:
        marker.unlink(missing_ok=True)
    else:
        marker.touch()
    _cache.pop(name, None)
    return True


def _snake_case(name: str) -> str:
    """CamelCase event name -> snake_case handler suffix:
    ``StudyReceived`` -> ``study_received`` (=> ``on_study_received``)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# --------------------------------------------------------------------------- #
# Module loading (importlib + hot reload)
# --------------------------------------------------------------------------- #
_cache: dict[str, tuple[float, Any]] = {}


def _load(name: str, path: Path) -> Any | None:
    """Load (or hot-reload) a script module by mtime. Returns None on error."""
    cached = _cache.get(name)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        _cache.pop(name, None)
        return None
    if cached is not None and cached[0] == mtime:
        return cached[1]
    spec = importlib.util.spec_from_file_location(f"paxs_script_{name}", path)
    if spec is None or spec.loader is None:
        _cache.pop(name, None)
        return None
    try:
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    except Exception:
        log.exception("script %s failed to load", name)
        _cache.pop(name, None)
        return None
    _cache[name] = (mtime, module)
    log.info("script %s loaded (mtime %s)", name, mtime)
    return module


def list_scripts() -> list[dict]:
    """Registry of scripts with status metadata (name, events, enabled, mtime)."""
    out: list[dict] = []
    for path in _script_files():
        name = path.stem
        module = _load(name, path)
        events = getattr(module, "EVENTS", None) if module else None
        handlers: list[str] = []
        if module:
            if callable(getattr(module, "handle", None)):
                handlers.append("handle")
            for attr in dir(module):
                if attr.startswith("on_") and callable(getattr(module, attr)):
                    handlers.append(attr)
        try:
            mtime = path.stat().st_mtime
            modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        except OSError:
            modified = None
        out.append({
            "name": name,
            "path": str(path),
            "enabled": is_enabled(path),
            "loaded": module is not None,
            "events": list(events) if isinstance(events, (list, tuple)) else None,
            "handlers": handlers,
            "modified": modified,
        })
    return out


def reload_scripts() -> int:
    """Drop the module cache (next dispatch re-imports from disk)."""
    n = len(_cache)
    _cache.clear()
    return n


# --------------------------------------------------------------------------- #
# Event dispatch
# --------------------------------------------------------------------------- #
class ScriptContext:
    """What a script handler receives — logger, payload, PAXS helpers."""

    def __init__(self, name: str, event: str, payload: dict) -> None:
        self._name = name
        self.log = logging.getLogger(f"paxs.scripts.{name}")
        self.event = event
        self.payload = payload

    # -- read-only archive helpers -------------------------------------- #
    def study(self, study_uid: str) -> dict | None:
        """Patient + study + series summary (metadata only)."""
        from sqlalchemy import select

        from app.db.models import Patient, Series, Study
        from app.db.session import SyncSessionLocal

        with SyncSessionLocal() as session:
            study = session.execute(
                select(Study).where(Study.study_instance_uid == study_uid)
            ).scalar_one_or_none()
            if study is None:
                return None
            patient = session.get(Patient, study.patient_id)
            series = session.execute(
                select(Series).where(Series.study_id == study.id)
            ).scalars().all()
            return {
                "study_uid": study.study_instance_uid,
                "study_date": str(study.study_date or ""),
                "study_description": study.study_description,
                "accession_number": study.accession_number,
                "patient": {
                    "id": patient.patient_id if patient else None,
                    "name": patient.patient_name if patient else None,
                    "birth_date": str(patient.patient_birth_date or "") if patient else None,
                    "sex": patient.patient_sex if patient else None,
                },
                "series": [
                    {
                        "series_uid": s.series_instance_uid,
                        "modality": s.modality,
                        "description": s.series_description,
                        "body_part": s.body_part_examined,
                    }
                    for s in series
                ],
            }

    def instances(self, study_uid: str) -> list[dict]:
        """Instance metadata rows for a study (no pixel data)."""
        from sqlalchemy import select

        from app.db.models import Instance, Series, Study
        from app.db.session import SyncSessionLocal

        with SyncSessionLocal() as session:
            rows = session.execute(
                select(Instance)
                .join(Series, Instance.series_id == Series.id)
                .join(Study, Study.id == Series.study_id)
                .where(Study.study_instance_uid == study_uid)
                .order_by(Instance.id)
            ).scalars().all()
            return [
                {
                    "sop_uid": i.sop_instance_uid,
                    "sop_class_uid": i.sop_class_uid,
                    "instance_number": i.instance_number,
                    "file_size_bytes": i.file_size_bytes,
                    "transfer_syntax": i.transfer_syntax_uid,
                    "storage_backend": i.storage_backend,
                    "key": i.file_path,
                }
                for i in rows
            ]

    def read_bytes(self, key: str) -> bytes:
        """Object bytes from the storage backend (heavy — use sparingly)."""
        from app.services import storage_service

        return storage_service.read_bytes(key)

    # -- outbound helper ------------------------------------------------- #
    def http_post(self, url: str, payload: dict, timeout: float = 8.0) -> int:
        """POST JSON to a URL; returns the HTTP status (0 on failure)."""
        try:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=body,
                headers={"Content-Type": "application/json", "User-Agent": "PAXS-Script/1.0"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return int(resp.status)
        except Exception:
            self.log.warning("http_post to %s failed", url, exc_info=True)
            return 0

    # -- chaining --------------------------------------------------------- #
    def fire(self, event: str, **data) -> None:
        """Re-dispatch an event to *other* scripts (chains)."""
        fire(event, **data)


def _dispatch_one(module: Any, event: str, ctx: ScriptContext) -> None:
    subscribed = getattr(module, "EVENTS", None)
    has_handle = callable(getattr(module, "handle", None))
    on_handler = getattr(module, f"on_{_snake_case(event)}", None)
    if isinstance(subscribed, (list, tuple)) and subscribed and event not in subscribed:
        return  # not subscribed to this event
    if not has_handle and not callable(on_handler):
        return  # nothing to run for this event
    if has_handle:
        module.handle(event, ctx)
    if callable(on_handler):
        on_handler(ctx)


def _run(event: str, payload: dict) -> None:
    for path in _script_files():
        name = path.stem
        if not is_enabled(path):
            continue
        module = _load(name, path)
        if module is None:
            continue
        ctx = ScriptContext(name, event, payload)
        try:
            _dispatch_one(module, event, ctx)
        except Exception:
            log.exception("script %s crashed on event %s", name, event)


def fire(event: str, **data) -> None:
    """Dispatch an event to every subscribed script (non-blocking)."""
    if not settings.SCRIPTS_ENABLED:
        return
    if not _script_files():
        return
    payload = {
        "event": event,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    threading.Thread(
        target=_run, args=(event, payload),
        daemon=True, name=f"script-{event[:12]}",
    ).start()


def run_script_now(name: str, event: str, payload: dict) -> dict:
    """Synchronous test-run of one script (used by the Admin API)."""
    path = scripts_dir() / f"{name}.py"
    if not path.is_file():
        return {"ok": False, "error": f"script {name!r} not found"}
    if not is_enabled(path):
        return {"ok": False, "error": f"script {name!r} is disabled"}
    module = _load(name, path)
    if module is None:
        return {"ok": False, "error": f"script {name!r} failed to load"}
    full_payload = {
        "event": event,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    ctx = ScriptContext(name, event, full_payload)
    try:
        _dispatch_one(module, event, ctx)
        return {"ok": True, "name": name, "event": event}
    except Exception as exc:
        return {"ok": False, "name": name, "error": str(exc)}
