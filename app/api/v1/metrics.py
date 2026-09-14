"""Prometheus /metrics endpoint (hand-rolled text exposition, zero new deps).

Emits the standard Prometheus text format at GET /metrics so a Prometheus
scraper (see deploy/prometheus.yml) can collect, per node:

  - instance identity (paxs_info)  — labels every series with PAXS_INSTANCE_ID
  - worker queue depth             — PENDING/RUNNING compression jobs by state
  - routing queue depth            — smart-router queue by state
  - read-replica health & lag      — from replica_probe()
  - HTTP request counter           — per-node traffic (HAProxy has its own)

The endpoint is deliberately best-effort: a DB outage yields 200 with
paxs_db_up=0 (and zeroed gauges) instead of a scrape error, so Prometheus can
alert on "db down" rather than "target down".

MetricsMiddleware counts HTTP requests in-process (reset on restart; rate()
over the scrape window is unaffected).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request, Response
from sqlalchemy import func, select

from app.core.config import settings

log = logging.getLogger(__name__)

router = APIRouter(tags=["metrics"])

# Per-node in-process request counter (reset on restart — fine for rate()).
_http_requests: int = 0

_INFO_KEYS = ("paxs_up", "paxs_http_requests_total", "paxs_db_up",
              "paxs_worker_queue_depth", "paxs_routing_queue_depth",
              "paxs_replica_configured", "paxs_replica_connected",
              "paxs_replica_lag_seconds")


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
    )


@router.get("/metrics")
async def metrics(request: Request) -> Response:
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

    body = _fmt_help_type() + "\n".join(lines) + "\n"
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
