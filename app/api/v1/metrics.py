"""Prometheus /metrics endpoint (hand-rolled text exposition, zero new deps).

Emits the standard Prometheus text format at GET /metrics so a Prometheus
scraper (see deploy/prometheus.yml) can collect, per node:

  - instance identity (paxs_info)  — labels every series with PAXS_INSTANCE_ID
  - worker queue depth             — PENDING/RUNNING compression jobs by state
  - routing queue depth            — smart-router queue by state
  - read-replica health & lag      — from replica_probe()
  - HTTP request counter           — per-node traffic (HAProxy has its own)
  - in-process cache effectiveness — hit/miss/insert/evict per cache, plus
                                     bytes, entries, budget and utilisation

The endpoint is deliberately best-effort: a DB outage yields 200 with
paxs_db_up=0 (and zeroed gauges) instead of a scrape error, so Prometheus can
alert on "db down" rather than "target down".

MetricsMiddleware counts HTTP requests in-process (reset on restart; rate()
over the scrape window is unaffected).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api.v1.deps import get_optional_user
from app.core.config import settings
from app.core.concurrency import run_blocking, run_blocking_stats

log = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])


async def require_metrics_access(
    request: Request,
    user=Depends(get_optional_user),
) -> None:
    """Allow explicitly allowlisted peers or authenticated administrators.

    The default allowlist is loopback only. A direct network peer may be added
    explicitly for a Prometheus scraper; forwarded client headers are never
    consulted here. Other callers must authenticate as an administrator.
    """
    from app.core.proxy_prefix import TrustedHosts

    client = request.scope.get("client")
    client_ip = client[0] if client else None
    if client_ip and client_ip in TrustedHosts(settings.METRICS_ALLOW_IPS):
        return
    if user is None:
        raise HTTPException(status_code=403, detail="Metrics access forbidden")
    if getattr(user, "role", None) != "ADMIN":
        raise HTTPException(status_code=403, detail="Requires ADMIN role")

# Per-node in-process request counter (reset on restart — fine for rate()).
_http_requests: int = 0
_http_metrics_lock = threading.Lock()
_HTTP_DURATION_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)
_HTTP_METRIC_MAX_SERIES = 256
_http_duration: dict[tuple[str, str, str], dict[str, object]] = {}

_INFO_KEYS = ("paxs_up", "paxs_http_requests_total", "paxs_db_up",
              "paxs_worker_queue_depth", "paxs_routing_queue_depth",
              "paxs_replica_configured", "paxs_replica_connected",
              "paxs_replica_lag_seconds",
              "paxs_volume_assembly_budget_bytes",
              "paxs_volume_assembly_reserved_bytes",
              "paxs_volume_assembly_waiters",
              "paxs_volume_assembly_inflight_keys",
              "paxs_volume_assembly_active_builds",
              "paxs_worker_queue_oldest_pending_seconds",
              "paxs_db_pool_checked_out", "paxs_db_pool_size",
              "paxs_db_pool_overflow",
              "paxs_event_loop_lag_seconds",
              "paxs_dicom_decode_failures_total",
              "paxs_storage_operations_total",
              "paxs_storage_operation_duration_seconds_bucket",
              "paxs_storage_operation_duration_seconds_sum",
              "paxs_storage_operation_duration_seconds_count",
              "paxs_process_resident_memory_bytes",
              "paxs_dimse_operations_total",
              "paxs_dimse_operation_duration_seconds_bucket",
              "paxs_dimse_operation_duration_seconds_sum",
              "paxs_dimse_operation_duration_seconds_count",
              "paxs_db_pool_checkout_seconds_bucket",
              "paxs_db_pool_checkout_seconds_sum",
              "paxs_db_pool_checkout_seconds_count",
              "paxs_cache_operations_total", "paxs_cache_bytes",
              "paxs_cache_entries", "paxs_cache_budget_bytes",
              "paxs_cache_utilisation_ratio",
               "paxs_blocking_io_workers_active", "paxs_blocking_io_queue_depth",
               "paxs_blocking_io_rejected_total", "paxs_blocking_io_wait_seconds_total",
               "paxs_blocking_io_wait_seconds_count", "paxs_blocking_io_wait_seconds_max",
               "paxs_redis_circuit_state", "paxs_redis_circuit_failures",
               "paxs_redis_errors_total",
              "paxs_http_request_duration_seconds_bucket",
              "paxs_http_request_duration_seconds_sum",
              "paxs_http_request_duration_seconds_count")


class MetricsMiddleware:
    """ASGI send-wrapper counting HTTP requests per node.

    Registered as pure ASGI middleware (no BaseHTTPMiddleware) to stay off
    the hot path; only increments a module-level counter.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        global _http_requests
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        with _http_metrics_lock:
            _http_requests += 1
        started = time.perf_counter()
        status = 500

        async def capture_status(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", None)
            if route_path != "/metrics":
                _record_http_request(
                    scope.get("method", "OTHER"),
                    route_path if isinstance(route_path, str) else "unmatched",
                    status,
                    time.perf_counter() - started,
                )


def _record_http_request(method: str, route: str, status: int, seconds: float) -> None:
    """Record bounded-cardinality HTTP latency without raw URL or PHI labels."""
    method_label = method.upper() if method.upper() in {
        "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"
    } else "OTHER"
    route_label = route[:200] if route.startswith("/") else "unmatched"
    status_label = f"{min(max(int(status) // 100, 1), 5)}xx"
    key = (method_label, route_label, status_label)
    with _http_metrics_lock:
        normal_series_limit = _HTTP_METRIC_MAX_SERIES - 5
        if key not in _http_duration and len(_http_duration) >= normal_series_limit:
            key = ("OTHER", "other", status_label)
        values = _http_duration.setdefault(
            key, {"count": 0, "sum": 0.0, "buckets": [0] * len(_HTTP_DURATION_BUCKETS)}
        )
        values["count"] = int(values["count"]) + 1
        values["sum"] = float(values["sum"]) + max(0.0, seconds)
        buckets = values["buckets"]
        assert isinstance(buckets, list)
        for index, bound in enumerate(_HTTP_DURATION_BUCKETS):
            if seconds <= bound:
                buckets[index] += 1


def _http_metric_lines() -> list[str]:
    """Snapshot the bounded request histogram as Prometheus exposition lines."""
    with _http_metrics_lock:
        snapshot = [
            (key, int(value["count"]), float(value["sum"]), list(value["buckets"]))
            for key, value in _http_duration.items()
        ]
    lines: list[str] = []
    for (method, route, status), count, total, buckets in snapshot:
        labels = f'method="{_esc(method)}",route="{_esc(route)}",status_class="{status}"'
        cumulative = 0
        for index, bound in enumerate(_HTTP_DURATION_BUCKETS):
            cumulative += buckets[index]
            lines.append(
                f'paxs_http_request_duration_seconds_bucket{{{labels},le="{bound:g}"}} {cumulative}'
            )
        lines.append(
            f'paxs_http_request_duration_seconds_bucket{{{labels},le="+Inf"}} {count}'
        )
        lines.append(f"paxs_http_request_duration_seconds_sum{{{labels}}} {total:.9f}")
        lines.append(f"paxs_http_request_duration_seconds_count{{{labels}}} {count}")
    return lines


def _storage_metric_lines() -> list[str]:
    """Expose fixed-cardinality S3 request counts and operation latency histograms."""
    from app.core.observability import storage_operation_snapshot

    lines: list[str] = []
    snapshot = storage_operation_snapshot()
    for (operation, result), values in snapshot.items():
        labels = f'backend="s3",operation="{operation}",result="{result}"'
        cumulative = 0
        buckets = values["buckets"]
        for index, bound in enumerate(values["bounds"]):
            cumulative += buckets[index]
            lines.append(
                f'paxs_storage_operation_duration_seconds_bucket{{{labels},le="{bound:g}"}} {cumulative}'
            )
        count = int(values["count"])
        lines.append(
            f'paxs_storage_operation_duration_seconds_bucket{{{labels},le="+Inf"}} {count}'
        )
        lines.append(f"paxs_storage_operation_duration_seconds_sum{{{labels}}} {float(values['sum']):.9f}")
        lines.append(f"paxs_storage_operation_duration_seconds_count{{{labels}}} {count}")
        lines.append(f"paxs_storage_operations_total{{{labels}}} {count}")
    return lines


def _dimse_metric_lines() -> list[str]:
    """Expose fixed-cardinality DIMSE outcome and duration telemetry."""
    from app.core.observability import dimse_operation_snapshot

    lines: list[str] = []
    for (operation, result), values in dimse_operation_snapshot().items():
        labels = f'operation="{operation}",result="{result}"'
        cumulative = 0
        for index, bound in enumerate(values["bounds"]):
            cumulative += values["buckets"][index]
            lines.append(
                f'paxs_dimse_operation_duration_seconds_bucket{{{labels},le="{bound:g}"}} {cumulative}'
            )
        count = int(values["count"])
        lines.append(
            f'paxs_dimse_operation_duration_seconds_bucket{{{labels},le="+Inf"}} {count}'
        )
        lines.append(f"paxs_dimse_operation_duration_seconds_sum{{{labels}}} {float(values['sum']):.9f}")
        lines.append(f"paxs_dimse_operation_duration_seconds_count{{{labels}}} {count}")
        lines.append(f"paxs_dimse_operations_total{{{labels}}} {count}")
    return lines


def _db_pool_checkout_metric_lines() -> list[str]:
    """Expose sync QueuePool acquisition latency, including connection creation."""
    from app.core.observability import db_pool_checkout_snapshot

    sample = db_pool_checkout_snapshot()
    lines: list[str] = []
    cumulative = 0
    for index, bound in enumerate(sample["bounds"]):
        cumulative += sample["buckets"][index]
        lines.append(
            f'paxs_db_pool_checkout_seconds_bucket{{le="{bound:g}"}} {cumulative}'
        )
    count = int(sample["count"])
    lines.append(f'paxs_db_pool_checkout_seconds_bucket{{le="+Inf"}} {count}')
    lines.append(f"paxs_db_pool_checkout_seconds_sum {float(sample['sum']):.9f}")
    lines.append(f"paxs_db_pool_checkout_seconds_count {count}")
    return lines


def _esc(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_process_help() -> str:
    return (
        "# HELP paxs_up Whether the node process is up (1).\n"
        "# TYPE paxs_up gauge\n"
        "# HELP paxs_info Static node identity (instance, version, app).\n"
        "# TYPE paxs_info gauge\n"
        "# HELP paxs_http_requests_total HTTP requests seen by this node process.\n"
        "# TYPE paxs_http_requests_total counter\n"
        "# HELP paxs_http_request_duration_seconds HTTP request duration by route template and status class.\n"
        "# TYPE paxs_http_request_duration_seconds histogram\n"
        "# HELP paxs_db_up Whether the primary database is reachable (1).\n"
        "# TYPE paxs_db_up gauge\n"
        "# HELP paxs_worker_queue_depth Compression jobs by state.\n"
        "# TYPE paxs_worker_queue_depth gauge\n"
        "# HELP paxs_worker_queue_oldest_pending_seconds Age of the oldest pending compression job.\n"
        "# TYPE paxs_worker_queue_oldest_pending_seconds gauge\n"
        "# HELP paxs_db_pool_checked_out Connections checked out from the sync database pool.\n"
        "# TYPE paxs_db_pool_checked_out gauge\n"
        "# HELP paxs_db_pool_size Configured base size of the sync database pool.\n"
        "# TYPE paxs_db_pool_size gauge\n"
        "# HELP paxs_db_pool_overflow Connections beyond the base sync database pool size.\n"
        "# TYPE paxs_db_pool_overflow gauge\n"
        "# HELP paxs_db_pool_checkout_seconds Sync DB pool acquisition duration, including new connection creation.\n"
        "# TYPE paxs_db_pool_checkout_seconds histogram\n"
        "# HELP paxs_event_loop_lag_seconds Latest event-loop scheduling delay sample.\n"
        "# TYPE paxs_event_loop_lag_seconds gauge\n"
    )


def _fmt_storage_help() -> str:
    return (
        "# HELP paxs_dicom_decode_failures_total Pixel decode failure contexts emitted by the DICOM diagnostic helper.\n"
        "# TYPE paxs_dicom_decode_failures_total counter\n"
        "# HELP paxs_storage_operations_total S3 operation attempts by bounded operation and result.\n"
        "# TYPE paxs_storage_operations_total counter\n"
        "# HELP paxs_storage_operation_duration_seconds S3 operation duration by bounded operation and result.\n"
        "# TYPE paxs_storage_operation_duration_seconds histogram\n"
        "# HELP paxs_process_resident_memory_bytes Resident memory used by this PAXS process, or 0 when unavailable.\n"
        "# TYPE paxs_process_resident_memory_bytes gauge\n"
        "# HELP paxs_dimse_operations_total Inbound DIMSE operations by operation and outcome.\n"
        "# TYPE paxs_dimse_operations_total counter\n"
        "# HELP paxs_dimse_operation_duration_seconds Inbound DIMSE operation duration by outcome.\n"
        "# TYPE paxs_dimse_operation_duration_seconds histogram\n"
    )


def _fmt_workflow_help() -> str:
    return (
        "# HELP paxs_routing_queue_depth Smart-router queue items by state.\n"
        "# TYPE paxs_routing_queue_depth gauge\n"
        "# HELP paxs_replica_configured Whether a PostgreSQL read replica is configured (1).\n"
        "# TYPE paxs_replica_configured gauge\n"
        "# HELP paxs_replica_connected Whether the replica answered a probe (1).\n"
        "# TYPE paxs_replica_connected gauge\n"
        "# HELP paxs_replica_lag_seconds Replica lag in seconds (0 when unconfigured).\n"
        "# TYPE paxs_replica_lag_seconds gauge\n"
        "# HELP paxs_volume_assembly_budget_bytes Configured aggregate working-set budget for volume assembly.\n"
        "# TYPE paxs_volume_assembly_budget_bytes gauge\n"
        "# HELP paxs_volume_assembly_reserved_bytes Estimated working-set bytes reserved by active volume assemblies.\n"
        "# TYPE paxs_volume_assembly_reserved_bytes gauge\n"
        "# HELP paxs_volume_assembly_waiters Requests waiting for volume assembly capacity.\n"
        "# TYPE paxs_volume_assembly_waiters gauge\n"
        "# HELP paxs_volume_assembly_inflight_keys Distinct active volume build keys.\n"
        "# TYPE paxs_volume_assembly_inflight_keys gauge\n"
        "# HELP paxs_volume_assembly_active_builds Active volume materializations including fallback builds.\n"
        "# TYPE paxs_volume_assembly_active_builds gauge\n"
        "# HELP paxs_cache_operations_total In-process cache operations by cache and result.\n"
        "# TYPE paxs_cache_operations_total counter\n"
        "# HELP paxs_cache_bytes Bytes currently held by an in-process cache.\n"
        "# TYPE paxs_cache_bytes gauge\n"
        "# HELP paxs_cache_entries Entries currently held by an in-process cache.\n"
        "# TYPE paxs_cache_entries gauge\n"
        "# HELP paxs_cache_budget_bytes Configured byte budget for an in-process cache.\n"
        "# TYPE paxs_cache_budget_bytes gauge\n"
        "# HELP paxs_cache_utilisation_ratio Fraction of the byte budget in use (0-1).\n"
        "# TYPE paxs_cache_utilisation_ratio gauge\n"
    )


def _fmt_help_type() -> str:
    return _fmt_process_help() + _fmt_storage_help() + _fmt_workflow_help()


def _cache_budgets() -> dict[str, int]:
    """Byte budgets read at scrape time, so a runtime override shows up live."""
    out: dict[str, int] = {}
    try:
        out["volume"] = max(0, int(getattr(settings, "VOLUME_CACHE_BUDGET_MB", 0) or 0)) * 1024 * 1024
    except (ValueError, TypeError, AttributeError) as exc:  # never fail a scrape over a gauge
        log.debug("metrics_volume_budget_unreadable error_type=%s", type(exc).__name__)
        out["volume"] = 0
    try:
        out["frame"] = max(0, int(getattr(settings, "DICOM_FRAME_CACHE_MB", 0) or 0)) * 1024 * 1024
    except (ValueError, TypeError, AttributeError) as exc:  # never fail a scrape over a gauge
        log.debug("metrics_frame_budget_unreadable error_type=%s", type(exc).__name__)
        out["frame"] = 0
    try:
        out["ai"] = max(0, int(getattr(settings, "AI_MASK_CACHE_BUDGET_MB", 0) or 0)) * 1024 * 1024
    except (ValueError, TypeError, AttributeError) as exc:  # never fail a scrape over a gauge
        log.debug("metrics_ai_budget_unreadable error_type=%s", type(exc).__name__)
        out["ai"] = 0
    try:
        out["bundle"] = max(0, int(getattr(settings, "VOLUME_BUNDLE_CACHE_BUDGET_MB", 0) or 0)) * 1024 * 1024
    except (ValueError, TypeError, AttributeError) as exc:
        log.debug("metrics_bundle_budget_unreadable error_type=%s", type(exc).__name__)
        out["bundle"] = 0
    return out


def _cache_metric_lines() -> list[str]:
    """Cache hit/miss/insert/evict counters plus byte/entry/budget gauges.

    These turn V1-2 / V3-6 tuning into measurement: a miss rate that stays high
    means the budget is too small (or the access pattern is un-cacheable), and a
    utilisation ratio pinned at 1.0 means the cache is evicting to serve every
    request.
    """
    from app.core import cache_stats

    lines: list[str] = []
    try:
        snap = cache_stats.snapshot()
    except Exception as exc:  # Best-effort scrape fallback; never log exception text.
        log.error("metrics_cache_snapshot_failed error_type=%s", type(exc).__name__)
        return lines

    budgets = _cache_budgets()
    for cache in cache_stats.CACHES:
        stats = snap.get(cache, {})
        for result, counter in (("hit", "hits"), ("miss", "misses"),
                                ("insert", "inserts"), ("evict", "evictions")):
            lines.append(
                f'paxs_cache_operations_total{{cache="{cache}",result="{result}"}} '
                f'{int(stats.get(counter, 0))}'
            )
        held = int(stats.get("bytes", 0))
        entries = int(stats.get("entries", 0))
        budget = int(budgets.get(cache, 0))
        lines.append(f'paxs_cache_bytes{{cache="{cache}"}} {held}')
        lines.append(f'paxs_cache_entries{{cache="{cache}"}} {entries}')
        lines.append(f'paxs_cache_budget_bytes{{cache="{cache}"}} {budget}')
        ratio = (held / budget) if budget > 0 else 0.0
        lines.append(f'paxs_cache_utilisation_ratio{{cache="{cache}"}} {ratio:.6f}')
    return lines


def _volume_assembly_metric_lines() -> list[str]:
    """Expose bounded, PHI-free live volume assembly capacity gauges."""
    try:
        from app.services.volume_service import volume_assembly_stats

        stats = volume_assembly_stats()
        return [
            f"paxs_volume_assembly_budget_bytes {int(stats['budget_bytes'])}",
            f"paxs_volume_assembly_reserved_bytes {int(stats['reserved_bytes'])}",
            f"paxs_volume_assembly_waiters {int(stats['waiters'])}",
            f"paxs_volume_assembly_inflight_keys {int(stats['inflight_keys'])}",
            f"paxs_volume_assembly_active_builds {int(stats['active_builds'])}",
        ]
    except Exception as exc:  # metrics remain scrapeable during partial startup
        log.debug("metrics_volume_assembly_unavailable error_type=%s", type(exc).__name__)
        return [
            "paxs_volume_assembly_budget_bytes 0",
            "paxs_volume_assembly_reserved_bytes 0",
            "paxs_volume_assembly_waiters 0",
            "paxs_volume_assembly_inflight_keys 0",
            "paxs_volume_assembly_active_builds 0",
        ]


def _blocking_io_metric_lines() -> list[str]:
    """Expose bounded executor load and PHI-free queue-wait aggregates."""
    from app.services.redis_service import redis_circuit_stats

    io = run_blocking_stats()
    circuit = redis_circuit_stats()
    state = {"closed": 0, "half_open": 1, "open": 2}.get(str(circuit["state"]), 3)
    return [
        f"paxs_blocking_io_workers_active {int(io['active'])}",
        f"paxs_blocking_io_queue_depth {int(io['queue_depth'])}",
        f"paxs_blocking_io_rejected_total {int(io['rejected'])}",
        f"paxs_blocking_io_wait_seconds_total {float(io['wait_total_seconds']):.9f}",
        f"paxs_blocking_io_wait_seconds_count {int(io['wait_count'])}",
        f"paxs_blocking_io_wait_seconds_max {float(io['wait_max_seconds']):.9f}",
        f"paxs_redis_circuit_state {state}",
        f"paxs_redis_circuit_failures {int(circuit['failures'])}",
        f"paxs_redis_errors_total {int(circuit['errors'])}",
    ]


@router.get("/metrics")
async def metrics(request: Request, _access=Depends(require_metrics_access)) -> Response:
    """Prometheus text-format metrics (scraped every 15s by default)."""
    global _http_requests
    with _http_metrics_lock:
        request_count = _http_requests
    instance = _esc(settings.PAXS_INSTANCE_ID)
    app_name = _esc(settings.APP_NAME)
    version = _esc(settings.APP_VERSION)

    lines: list[str] = [
        "paxs_up 1",
        f'paxs_info{{instance="{instance}",version="{version}",app="{app_name}"}} 1',
        f"paxs_http_requests_total {request_count}",
    ]
    lines.extend(_http_metric_lines())
    lines.extend(_storage_metric_lines())
    lines.extend(_dimse_metric_lines())
    lines.extend(_db_pool_checkout_metric_lines())

    # --- DB-backed gauges: best-effort, zeroed on failure ------------------
    db_up = 0
    queue_pending = queue_running = 0
    queue_oldest_seconds = 0.0
    db_pool_checked_out = db_pool_size = db_pool_overflow = 0
    route_pending = route_failed = route_completed = 0
    try:
        from app.db.models import CompressionJob, RoutingQueueItem
        from app.db.session import AsyncSessionLocal, sync_engine

        async with AsyncSessionLocal() as s:
            async def _count(model, *where):
                stmt = select(func.count(model.id))
                if where:
                    stmt = stmt.where(*where)
                return (await s.execute(stmt)).scalar_one()

            queue_pending = await _count(CompressionJob, CompressionJob.status == "PENDING")
            queue_running = await _count(CompressionJob, CompressionJob.status == "RUNNING")
            oldest_created = (await s.execute(
                select(func.min(CompressionJob.created_at)).where(
                    CompressionJob.status == "PENDING"
                )
            )).scalar_one_or_none()
            if oldest_created is not None:
                if oldest_created.tzinfo is None:
                    oldest_created = oldest_created.replace(tzinfo=timezone.utc)
                queue_oldest_seconds = max(
                    0.0, (datetime.now(timezone.utc) - oldest_created).total_seconds()
                )
            try:
                route_pending = await _count(RoutingQueueItem, RoutingQueueItem.status == "PENDING")
                route_failed = await _count(RoutingQueueItem, RoutingQueueItem.status == "FAILED")
                route_completed = await _count(RoutingQueueItem, RoutingQueueItem.status == "COMPLETED")
            except Exception as exc:  # noqa: BLE001 - boundary guard: table missing on an older DB
                # Routing-queue gauges stay at zero rather than failing the whole
                # scrape; the type tells an absent table from a broken query.
                log.debug("metrics_routing_queue_unavailable error_type=%s",
                          type(exc).__name__)
        db_up = 1
        from app.db.perf import pool_snapshot

        pool = pool_snapshot(sync_engine.pool)
        db_pool_checked_out = int(pool["checkedout"])
        db_pool_size = int(pool["pool_size"])
        db_pool_overflow = int(pool["overflow"])
    except Exception as exc:  # Best-effort scrape fallback; never log exception text.
        log.error("metrics_db_query_failed error_type=%s", type(exc).__name__)

    lines.append(f"paxs_db_up {db_up}")
    lines.append(f"paxs_worker_queue_oldest_pending_seconds {queue_oldest_seconds:.3f}")
    lines.append(f"paxs_db_pool_checked_out {db_pool_checked_out}")
    lines.append(f"paxs_db_pool_size {db_pool_size}")
    lines.append(f"paxs_db_pool_overflow {db_pool_overflow}")
    from app.core.observability import event_loop_lag_seconds
    from app.core.observability import dicom_decode_failures
    from app.core.observability import process_resident_memory_bytes

    lines.append(f"paxs_event_loop_lag_seconds {event_loop_lag_seconds():.6f}")
    lines.append(f"paxs_dicom_decode_failures_total {dicom_decode_failures()}")
    rss_bytes = await run_blocking(process_resident_memory_bytes)
    lines.append(f"paxs_process_resident_memory_bytes {rss_bytes or 0}")
    lines.append(f'paxs_worker_queue_depth{{state="pending"}} {queue_pending}')
    lines.append(f'paxs_worker_queue_depth{{state="running"}} {queue_running}')
    lines.append(f'paxs_routing_queue_depth{{state="pending"}} {route_pending}')
    lines.append(f'paxs_routing_queue_depth{{state="failed"}} {route_failed}')
    lines.append(f'paxs_routing_queue_depth{{state="completed"}} {route_completed}')

    # --- Read-replica health & lag ----------------------------------------
    replica_configured = 0
    replica_connected = 0
    replica_lag = 0.0
    try:
        from app.db.session import replica_probe

        probe = await replica_probe()
        replica_configured = 1 if probe.get("configured") else 0
        replica_connected = 1 if probe.get("connected") else 0
        lag = probe.get("lag_seconds")
        if lag is not None:
            replica_lag = float(lag)
    except Exception as exc:  # Best-effort scrape fallback; never log exception text.
        log.error("metrics_replica_probe_failed error_type=%s", type(exc).__name__)
    lines.append(f"paxs_replica_configured {replica_configured}")
    lines.append(f"paxs_replica_connected {replica_connected}")
    lines.append(f"paxs_replica_lag_seconds {replica_lag}")

    # --- Decode-cache effectiveness (V1-2 / V3-6) -------------------------
    lines.extend(_cache_metric_lines())
    lines.extend(_volume_assembly_metric_lines())
    lines.extend(_blocking_io_metric_lines())

    body = _fmt_help_type() + "\n".join(lines) + "\n"
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
