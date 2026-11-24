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
``fire(event, **data)`` enqueues the dispatch on the shared **bounded**
background pool (:mod:`app.core.concurrency`, C-5) — one task that walks every
enabled script subscribed to the event and calls its handler. Exceptions are
caught per script and logged — a crashing script never breaks the caller or its
siblings. Scripts can be enabled/disabled via a ``.disabled`` marker file
next to them (``{name}.disabled``) or the Admin → Scripts panel.

When the pool's backlog is full the dispatch is dropped (with a debug log)
rather than spawning an unbounded thread per event: a chained script storm must
degrade by shedding events, not by exhausting the process's threads.

A chain is additionally bounded on two axes (C-5), because the pool bounds
*concurrency* but not *volume* — a script that fires from inside a handler would
otherwise keep the pool saturated forever: ``SCRIPTS_MAX_CHAIN_DEPTH`` caps the
number of ``ctx.fire`` hops in one chain (default 5, ``0`` disables chaining),
and ``SCRIPTS_MAX_CHAIN_PER_SECOND`` caps chained dispatches per rolling second
process-wide (default 50, ``0`` disables chaining). Both drops are logged and
counted by :func:`chain_stats`.

Security note: scripts are *trusted admin code* — they run in-process with
the same privileges as the server (this mirrors Orthanc Lua / Conquest
macros). They should not be written by untrusted users.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import logging
import re
import threading
import time
import urllib.request
from collections import OrderedDict, deque
from datetime import datetime, timezone
from heapq import nsmallest
from pathlib import Path
from typing import Any

from app.core.concurrency import submit_background
from app.core.config import settings

log = logging.getLogger("paxs.scripts")


# --------------------------------------------------------------------------- #
# Script discovery
# --------------------------------------------------------------------------- #
def scripts_dir() -> Path:
    return Path(settings.SCRIPTS_DIR or "scripts")


def _script_files(limit: int | None = None) -> list[Path]:
    """Return at most ``limit`` sorted loadable script paths."""
    d = scripts_dir()
    if not d.is_dir():
        return []
    paths = (path for path in d.glob("*.py") if not path.name.startswith("_"))
    return sorted(paths) if limit is None else nsmallest(max(0, limit), paths)


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
    with _CACHE_LOCK:
        _cache.pop(name, None)
    return True


def _snake_case(name: str) -> str:
    """CamelCase event name -> snake_case handler suffix:
    ``StudyReceived`` -> ``study_received`` (=> ``on_study_received``)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


# --------------------------------------------------------------------------- #
# Module loading (importlib + hot reload)
# --------------------------------------------------------------------------- #
# C-5: every mutation of the module cache is guarded. Dispatch runs on the
# bounded background pool, so two events can call _load for the same script
# concurrently — without the lock, one thread could exec_module into the cache
# while another reads a half-registered module (or two threads both execute the
# module, tearing its module-level state).
_cache: OrderedDict[str, tuple[float, Any]] = OrderedDict()
_CACHE_LOCK = threading.Lock()
_REGISTRY_LOCK = threading.Lock()


def script_limits() -> tuple[int, int]:
    """Return configured script-count and source-byte limits, honoring zero."""
    return (
        max(0, int(settings.SCRIPTS_MAX_COUNT)),
        max(0, int(settings.SCRIPTS_MAX_SOURCE_BYTES)),
    )


def create_script(name: str, source: str) -> str | None:
    """Create a script atomically within configured count and byte limits."""
    max_count, max_source_bytes = script_limits()
    encoded = source.encode("utf-8")
    if not max_source_bytes or len(encoded) > max_source_bytes:
        return "source_limit"
    path = scripts_dir() / f"{name}.py"
    with _REGISTRY_LOCK:
        if path.exists():
            return "exists"
        if not max_count or len(_script_files(max_count)) >= max_count:
            return "registry_full"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="") as script_file:
            script_file.write(source)
        with _CACHE_LOCK:
            _cache.pop(name, None)
    return None


def _load(name: str, path: Path) -> Any | None:
    """Load (or hot-reload) a script module by mtime. Returns None on error.

    Thread-safe (C-5): the cache check, the import and the cache write all
    happen under ``_CACHE_LOCK``. Holding the lock across ``exec_module`` is
    deliberate — script modules are small admin files, and serialising them is
    what makes the cache coherent for the concurrent dispatchers that share it.
    """
    with _CACHE_LOCK:
        cached = _cache.get(name)
        try:
            mtime = path.stat().st_mtime
            max_count, max_source_bytes = script_limits()
            if not max_count:
                _cache.pop(name, None)
                return None
            if not max_source_bytes or path.stat().st_size > max_source_bytes:
                log.warning("script %s rejected: source size limit exceeded", name)
                _cache.pop(name, None)
                return None
        except OSError:
            _cache.pop(name, None)
            return None
        if cached is not None and cached[0] == mtime:
            _cache.move_to_end(name)
            return cached[1]
        spec = importlib.util.spec_from_file_location(f"paxs_script_{name}", path)
        if spec is None or spec.loader is None:
            _cache.pop(name, None)
            return None
        try:
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        except Exception as exc:
            # Loading one optional script must not expose its source path or exception text.
            log.error("script_load_failed error_type=%s", type(exc).__name__)
            _cache.pop(name, None)
            return None
        _cache.pop(name, None)
        while len(_cache) >= max_count:
            _cache.popitem(last=False)
        _cache[name] = (mtime, module)
        log.info("script %s loaded (mtime %s)", name, mtime)
        return module


def _assigned_literal(tree: ast.Module, name: str) -> Any | None:
    """Read a simple module-level literal assignment without executing code."""
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            targets = statement.targets
            value = statement.value
        elif isinstance(statement, ast.AnnAssign):
            targets = [statement.target]
            value = statement.value
        else:
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in targets):
            try:
                return ast.literal_eval(value) if value is not None else None
            except (ValueError, TypeError, SyntaxError):
                return None
    return None


def parse_script_metadata(path: Path) -> dict:
    """Inspect script metadata with AST only; never imports or executes it."""
    name = path.stem
    mtime = None
    try:
        mtime = path.stat().st_mtime
        modified = datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
        _max_count, max_source_bytes = script_limits()
        if not max_source_bytes or path.stat().st_size > max_source_bytes:
            raise ValueError("script exceeds configured source limit")
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=path.name)
    except (OSError, UnicodeDecodeError, SyntaxError, ValueError) as exc:
        # A script with a syntax error (or a race with its deletion) is listed
        # without metadata rather than omitted; type only, the path is a
        # filesystem location and never belongs in a log line (N-2).
        log.debug("script_metadata_unavailable error_type=%s", type(exc).__name__)
        modified = None
        tree = None

    events = _assigned_literal(tree, "EVENTS") if tree is not None else None
    if not isinstance(events, (list, tuple)) or not all(isinstance(item, str) for item in events):
        events = None
    description = _assigned_literal(tree, "DESCRIPTION") if tree is not None else None
    if not isinstance(description, str) and tree is not None:
        description = ast.get_docstring(tree)
    author = _assigned_literal(tree, "AUTHOR") if tree is not None else None
    if not isinstance(author, str):
        author = None
    handlers = [
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and (node.name == "handle" or node.name.startswith("on_"))
    ] if tree is not None else []
    with _CACHE_LOCK:
        cached = _cache.get(name)
        loaded = bool(cached and mtime is not None and cached[0] == mtime)

    return {
        "name": name,
        "path": str(path),
        "description": description,
        "author": author,
        "enabled": is_enabled(path),
        "loaded": loaded,
        "valid": tree is not None,
        "events": list(events) if events is not None else None,
        "handlers": handlers,
        "modified": modified,
    }


def list_scripts() -> list[dict]:
    """List scripts and static metadata without importing their modules."""
    max_count, _max_source_bytes = script_limits()
    if not max_count:
        return []
    return [parse_script_metadata(path) for path in _script_files(max_count)]


def reload_scripts() -> int:
    """Drop the module cache (next dispatch re-imports from disk)."""
    with _CACHE_LOCK:
        n = len(_cache)
        _cache.clear()
    return n


# --------------------------------------------------------------------------- #
# Chained-event budget (C-5)
# --------------------------------------------------------------------------- #
# `ctx.fire()` lets a script re-dispatch an event, which is how scripts chain.
# The bounded pool caps *concurrency*, but not *volume*: a script that fires
# from inside a handler — especially one that fires an event its own handler
# subscribes to — keeps producing work forever, and the pool stays permanently
# saturated. Warm-on-open and every other background task then sit behind a
# script storm that never ends.
#
# Two independent caps, because they catch different storms:
#   * depth — a single chain cannot exceed `SCRIPTS_MAX_CHAIN_DEPTH` hops;
#   * rate  — chained dispatches process-wide cannot exceed
#             `SCRIPTS_MAX_CHAIN_PER_SECOND` per rolling second.
#
# The depth is passed **explicitly** through the dispatch rather than held in a
# thread-local: each hop runs on whichever pool worker picks the task up, so a
# thread-local would read 0 on the next thread and the cap would never trip.
_CHAIN_WINDOW_SECONDS = 1.0
_chain_lock = threading.Lock()
_chain_window: deque[float] = deque()
_chain_dropped_depth = 0
_chain_dropped_rate = 0


def _chain_depth_limit() -> int:
    """Configured chain depth (``0`` = chaining disabled, honoured literally)."""
    try:
        return max(0, int(getattr(settings, "SCRIPTS_MAX_CHAIN_DEPTH", 5)))
    except (TypeError, ValueError):
        return 5


def _chain_rate_limit() -> int:
    """Configured chained dispatches per second (``0`` = unlimited)."""
    try:
        return max(0, int(getattr(settings, "SCRIPTS_MAX_CHAIN_PER_SECOND", 50)))
    except (TypeError, ValueError):
        return 50


def _chain_budget_available(limit: int) -> bool:
    """Take one slot from the rolling one-second chained-dispatch window."""
    if limit <= 0:
        return False  # zero disables chained dispatch rather than removing its cap
    now = time.monotonic()
    with _chain_lock:
        while _chain_window and now - _chain_window[0] > _CHAIN_WINDOW_SECONDS:
            _chain_window.popleft()
        if len(_chain_window) >= limit:
            return False
        _chain_window.append(now)
        return True


def chain_stats() -> dict:
    """Chained-dispatch counters (observability + regression tests)."""
    with _chain_lock:
        in_window = len(_chain_window)
        return {
            "depth_limit": _chain_depth_limit(),
            "rate_limit_per_second": _chain_rate_limit(),
            "window_len": in_window,
            "dropped_depth": _chain_dropped_depth,
            "dropped_rate": _chain_dropped_rate,
        }


def reset_chain_budget() -> None:
    """Clear the rolling window and the drop counters (tests / config reload)."""
    global _chain_dropped_depth, _chain_dropped_rate
    with _chain_lock:
        _chain_window.clear()
        _chain_dropped_depth = 0
        _chain_dropped_rate = 0


# --------------------------------------------------------------------------- #
# Event dispatch
# --------------------------------------------------------------------------- #
class ScriptContext:
    """What a script handler receives — logger, payload, PAXS helpers."""

    def __init__(
        self,
        name: str,
        event: str,
        payload: dict[str, Any],
        chain_depth: int = 0,
        script_paths: tuple[Path, ...] | None = None,
    ) -> None:
        self._name = name
        self.log = logging.getLogger(f"paxs.scripts.{name}")
        self.event = event
        self.payload = payload
        #: How many `ctx.fire` hops deep this dispatch is (C-5). A handler that
        #: fires passes `chain_depth + 1` to the next hop.
        self.chain_depth = chain_depth
        self._script_paths = script_paths

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

    def read_bytes(self, key: str, backend_name: str | None = None) -> bytes:
        """Object bytes from the storage backend (heavy — use sparingly).

        D-6: ``backend_name`` selects the store that actually holds the object.
        A study can live on a non-default backend (``instances.storage_backend``,
        from replication or tiering), and reading it from the env default finds
        nothing — the script saw a spurious miss on an object that exists. Pass
        the ``storage_backend`` that :meth:`instances` reports for the row.
        """
        from app.services import storage_service

        return storage_service.read_bytes(key, backend_name=backend_name)

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
        except Exception as exc:
            # URLs and request failures can contain PHI or credentials; scripts remain non-fatal.
            self.log.warning("script_http_post_failed error_type=%s", type(exc).__name__)
            return 0

    # -- chaining --------------------------------------------------------- #
    def fire(self, event: str, **data: Any) -> None:
        """Re-dispatch an event to *other* scripts (chains).

        Counts one hop against the chain budget (C-5): the child dispatch runs
        at ``chain_depth + 1``, so a self-feeding chain stops at
        ``SCRIPTS_MAX_CHAIN_DEPTH`` instead of occupying the background pool
        forever.
        """
        _queue_event(event, data, self.chain_depth + 1, self._script_paths)


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


def _run(
    event: str,
    payload: dict[str, Any],
    chain_depth: int,
    script_paths: tuple[Path, ...],
) -> None:
    """Run only the bounded script snapshot captured when this event queued."""
    if not settings.SCRIPTS_ENABLED:
        return
    max_count, _max_source_bytes = script_limits()
    if not max_count:
        return
    for path in script_paths[:max_count]:
        name = path.stem
        if not is_enabled(path):
            continue
        module = _load(name, path)
        if module is None:
            continue
        ctx = ScriptContext(
            name, event, payload, chain_depth=chain_depth, script_paths=script_paths
        )
        try:
            _dispatch_one(module, event, ctx)
        except Exception as exc:
            # A script failure must not stop sibling scripts or disclose its event payload.
            log.error("script_execution_failed error_type=%s", type(exc).__name__)


def _chain_dispatch_allowed(event: str, chain_depth: int) -> bool:
    """Apply the depth and rolling-rate caps to one queued script hop."""
    global _chain_dropped_depth, _chain_dropped_rate
    if chain_depth > _chain_depth_limit():
        with _chain_lock:
            _chain_dropped_depth += 1
        log.warning(
            "script_chain_dropped reason=depth depth=%d max_depth=%d",
            chain_depth,
            _chain_depth_limit(),
        )
        return False
    if chain_depth > 0 and not _chain_budget_available(_chain_rate_limit()):
        with _chain_lock:
            _chain_dropped_rate += 1
        log.warning(
            "script_chain_dropped reason=rate limit_per_second=%d",
            _chain_rate_limit(),
        )
        return False
    return True


def _queue_event(
    event: str,
    data: dict[str, Any],
    chain_depth: int,
    script_paths: tuple[Path, ...] | None = None,
) -> None:
    """Queue an event against a stable, entry-bounded script path snapshot."""
    if not settings.SCRIPTS_ENABLED:
        return
    max_count, _max_source_bytes = script_limits()
    if not max_count:
        return
    paths = script_paths if script_paths is not None else tuple(_script_files(max_count))
    paths = paths[:max_count]
    if not paths or not _chain_dispatch_allowed(event, chain_depth):
        return
    payload = {
        "event": event,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    if not submit_background(_run, event, payload, chain_depth, paths):
        log.debug("script_dispatch_dropped reason=background_pool_saturated")


def fire(event: str, *, _chain_depth: int = 0, **data: Any) -> None:
    """Dispatch an event to every subscribed script (non-blocking, bounded).

    C-5: the dispatch runs on the shared bounded background pool rather than a
    raw thread per event. If the pool's backlog is saturated the event is
    dropped and logged — shedding an event is the correct backpressure for a
    chained-script storm; spawning a thread per event would exhaust the process.

    ``_chain_depth`` is the private hop counter for :meth:`ScriptContext.fire`
    (keyword-only, so it can never collide with a payload key). A *chained*
    dispatch is additionally bounded by depth and by rate, because the pool only
    bounds concurrency: an unbounded chain would keep it saturated forever.
    """
    _queue_event(event, data, _chain_depth)


def run_script_now(name: str, event: str, payload: dict) -> dict:
    """Synchronous test-run of one script (used by the Admin API)."""
    path = scripts_dir() / f"{name}.py"
    if not path.is_file():
        return {"ok": False, "error": f"script {name!r} not found"}
    max_count, _max_source_bytes = script_limits()
    script_paths = tuple(_script_files(max_count)) if max_count else ()
    if not max_count or path not in script_paths:
        return {"ok": False, "error": "script registry capacity exceeded"}
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
    ctx = ScriptContext(name, event, full_payload, script_paths=script_paths)
    try:
        _dispatch_one(module, event, ctx)
        return {"ok": True, "name": name, "event": event}
    except Exception as exc:
        return {"ok": False, "name": name, "error": str(exc)}
