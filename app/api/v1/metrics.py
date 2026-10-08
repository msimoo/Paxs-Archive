"""Prometheus /metrics endpoint (hand-rolled text exposition, zero new deps).

Emits the standard Prometheus text format at GET /metrics so a Prometheus
scraper (see deploy/prometheus.yml) can collect, per node:

  - instance identity (paxs_info)  — labels every series with PAXS_INSTANCE_ID
  - worker queue depth             — PENDING/RUNNING compression jobs by state
  - routing queue depth            — smart-router queue by state
  - read-replica health & lag      — from replica_probe()
  - HTTP request counter           — per-node traffic (HAProxy has its own)
  - decode-cache effectiveness     — hit/miss/insert/evict per cache, plus
                                     bytes, entries, budget and utilisation

The endpoint is deliberately best-effort: a DB outage yields 200 with
paxs_db_up=0 (and zeroed gauges) instead of a scrape error, so Prometheus can
alert on "db down" rather than "target down".

MetricsMiddleware counts HTTP requests in-process (reset on restart; rate()
over the scrape window is unaffected).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select

from app.api.v1.deps import get_optional_user
from app.core.config import settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])


async def require_metrics_access(
    request: Request,
    user=Depends(get_optional_user),
) -> None:
    """Allow trusted networks to scrape without credentials; authenticate otherwise.

    Prometheus scrapes `/metrics` with no credentials, but the payload is not
    public: it discloses version, topology, queue depths and cache pressure.
    Peers listed in ``METRICS_ALLOW_IPS`` (loopback + RFC1918 by default, which
    covers the shipped Docker/HAProxy topology) are allowed through; every other
    caller must authenticate exactly as before.

    Without this, the shipped `deploy/prometheus.yml` — which sends no
    credentials — received 401 on every scrape, so the whole `paxs-nodes` job
    was down and `PaxsNodeDown` fired for every node.
    """
    if user is not None:
        return
    from app.core.proxy_prefix import TrustedHosts

    client = request.scope.get("client")
    client_ip = client[0] if client else None
    if client_ip and client_ip in TrustedHosts(settings.METRICS_ALLOW_IPS):
        return
    raise HTTPException(status_code=401, detail="Not authenticated")

# Per-node in-process request counter (reset on restart — fine for rate()).
_http_requests: int = 0

_INFO_KEYS = ("paxs_up", "paxs_http_requests_total", "paxs_db_up",
              "paxs_worker_queue_depth", "paxs_routing_queue_depth",
              "paxs_replica_configured", "paxs_replica_connected",
              "paxs_replica_lag_seconds",
              "paxs_cache_operations_total", "paxs_cache_bytes",
              "paxs_cache_entries", "paxs_cache_budget_bytes",
              "paxs_cache_utilisation_ratio")


class MetricsMiddleware:
    """ASGI send-wrapper counting HTTP requests per node.

    Registered as pure ASGI middleware (no BaseHTTPMiddleware) to stay off
    the hot path; only increments a module-level counter.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        global _http_requests
        if scope["type"] == "http":
            _http_requests += 1
        await self.app(scope, receive, send)


def _esc(value: str) -> str:
    """Escape a label value per the Prometheus text format."""
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _fmt_help_type() -> str:
    return (
        "# HELP paxs_up Whether the node process is up (1).\n"
        "# TYPE paxs_up gauge\n"
        "# HELP paxs_info Static node identity (instance, version, app).\n"
        "# TYPE paxs_info gauge\n"
        "# HELP paxs_http_requests_total HTTP requests seen by this node process.\n"
        "# TYPE paxs_http_requests_total counter\n"
        "# HELP paxs_db_up Whether the primary database is reachable (1).\n"
        "# TYPE paxs_db_up gauge\n"
        "# HELP paxs_worker_queue_depth Compression jobs by state.\n"
        "# TYPE paxs_worker_queue_depth gauge\n"
        "# HELP paxs_routing_queue_depth Smart-router queue items by state.\n"
        "# TYPE paxs_routing_queue_depth gauge\n"
        "# HELP paxs_replica_configured Whether a MySQL read replica is configured (1).\n"
        "# TYPE paxs_replica_configured gauge\n"
        "# HELP paxs_replica_connected Whether the replica answered a probe (1).\n"
        "# TYPE paxs_replica_connected gauge\n"
        "# HELP paxs_replica_lag_seconds Replica lag in seconds (0 when unconfigured).\n"
        "# TYPE paxs_replica_lag_seconds gauge\n"
        "# HELP paxs_cache_operations_total Decode-cache operations by cache and result.\n"
        "# TYPE paxs_cache_operations_total counter\n"
        "# HELP paxs_cache_bytes Bytes currently held by a decode cache.\n"
        "# TYPE paxs_cache_bytes gauge\n"
        "# HELP paxs_cache_entries Entries currently held by a decode cache.\n"
        "# TYPE paxs_cache_entries gauge\n"
        "# HELP paxs_cache_budget_bytes Configured byte budget for a decode cache.\n"
        "# TYPE paxs_cache_budget_bytes gauge\n"
        "# HELP paxs_cache_utilisation_ratio Fraction of the byte budget in use (0-1).\n"
        "# TYPE paxs_cache_utilisation_ratio gauge\n"
    )


def _cache_budgets() -> dict[str, int]:
    """Byte budgets read at scrape time, so a runtime override shows up live."""
    out: dict[str, int] = {}
    try:
        out["volume"] = max(0, int(getattr(settings, "VOLUME_CACHE_BUDGET_MB", 0) or 0)) * 1024 * 1024
    except Exception:  # pragma: no cover — never fail a scrape over a gauge
        out["volume"] = 0
    try:
        out["frame"] = max(0, int(getattr(settings, "DICOM_FRAME_CACHE_MB", 0) or 0)) * 1024 * 1024
    except Exception:  # pragma: no cover
        out["frame"] = 0
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
    except Exception:  # pragma: no cover
        log.exception("metrics: cache snapshot failed")
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


@router.get("/metrics")
async def metrics(request: Request, _access=Depends(require_metrics_access)) -> Response:
    """Prometheus text-format metrics (scraped every 15s by default)."""
    global _http_requests
    instance = _esc(settings.PAXS_INSTANCE_ID)
    app_name = _esc(settings.APP_NAME)
    version = _esc(settings.APP_VERSION)

    lines: list[str] = [
        "paxs_up 1",
        f'paxs_info{{instance="{instance}",version="{version}",app="{app_name}"}} 1',
        f"paxs_http_requests_total {_http_requests}",
    ]

    # --- DB-backed gauges: best-effort, zeroed on failure ------------------
    db_up = 0
    queue_pending = queue_running = 0
    route_pending = route_failed = route_completed = 0
    try:
        from app.db.models import CompressionJob, RoutingQueueItem
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as s:
            async def _count(model, *where):
                stmt = select(func.count(model.id))
                if where:
                    stmt = stmt.where(*where)
                return (await s.execute(stmt)).scalar_one()

            queue_pending = await _count(CompressionJob, CompressionJob.status == "PENDING")
            queue_running = await _count(CompressionJob, CompressionJob.status == "RUNNING")
            try:
                route_pending = await _count(RoutingQueueItem, RoutingQueueItem.status == "PENDING")
                route_failed = await _count(RoutingQueueItem, RoutingQueueItem.status == "FAILED")
                route_completed = await _count(RoutingQueueItem, RoutingQueueItem.status == "COMPLETED")
            except Exception:  # pragma: no cover — table missing on old DB
                pass
        db_up = 1
    except Exception:
        log.exception("metrics: DB query failed (emitting zeros)")

    lines.append(f"paxs_db_up {db_up}")
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
    except Exception:
        log.exception("metrics: replica probe failed")
    lines.append(f"paxs_replica_configured {replica_configured}")
    lines.append(f"paxs_replica_connected {replica_connected}")
    lines.append(f"paxs_replica_lag_seconds {replica_lag}")

    # --- Decode-cache effectiveness (V1-2 / V3-6) -------------------------
    lines.extend(_cache_metric_lines())

    body = _fmt_help_type() + "\n".join(lines) + "\n"
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
