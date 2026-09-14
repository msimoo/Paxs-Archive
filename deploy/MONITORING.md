# PAXS Monitoring — Prometheus + Grafana

Metrics for the three things that matter in a scale-out archive: **per-node
HAProxy traffic**, **worker queue depth** (compression backlog), and
**read-replica lag**. Two scrape sources, one dashboard.

## What's scraped

| Source | Endpoint | Metrics |
| --- | --- | --- |
| **HAProxy** (built-in exporter, HAProxy ≥ 2.4) | `http://haproxy:8405/metrics` | per-node `haproxy_server_http_responses_total{server="paxs-node-N"}` (traffic split), `haproxy_frontend_http_requests_total` (total rate), server status |
| **PAXS nodes** (new `/metrics`) | `http://paxs-node-N:8000/metrics` | `paxs_worker_queue_depth{state="pending|running"}`, `paxs_routing_queue_depth{state=...}`, `paxs_replica_lag_seconds` / `paxs_replica_connected` / `paxs_replica_configured`, `paxs_db_up`, `paxs_http_requests_total`, `paxs_info{instance=PAXS_INSTANCE_ID}` |

The node `/metrics` endpoint is **zero-dependency** (hand-rolled Prometheus
text format — no `prometheus_client` needed) and **best-effort**: if the DB is
down it returns `200` with `paxs_db_up 0` and zeroed gauges, so Prometheus
alerts on "db down" instead of "target down".

## Files

| File | Purpose |
| --- | --- |
| `app/api/v1/metrics.py` | `/metrics` endpoint + `MetricsMiddleware` (per-node HTTP counter) |
| `deploy/haproxy.cfg` | adds `metrics_in` frontend on `:8405` with `stats prometheus` |
| `deploy/prometheus.yml` | scrape config: `paxs-haproxy` + `paxs-nodes` jobs |
| `deploy/docker-compose.monitoring.yml` | Prometheus + Grafana (joins the LB stack's network) |
| `deploy/grafana/provisioning/` | datasource + dashboard auto-provisioning |
| `deploy/grafana/dashboards/paxs-overview.json` | the dashboard (3 rows, 6 panels) |

## Start the stack

```bash
# 1) the archive itself (3 nodes + HAProxy + MySQL + MinIO)
docker compose -f deploy/docker-compose.lb.yml up -d --build

# 2) monitoring — SAME network (both files live under deploy/, so both
#    compose projects share the `deploy_default` network and service names)
docker compose -f deploy/docker-compose.monitoring.yml up -d

# 3) check scrapes
curl -s localhost:8405/metrics | head                 # HAProxy exporter
curl -s localhost:8000/metrics | head                 # PAXS node (via HAProxy)
curl -s 'localhost:9090/api/v1/targets' | head        # Prometheus targets
```

Open **Grafana** at `http://localhost:3000` (default `admin` / `admin` —
change on first login). The **PAXS Archive Overview** dashboard is
auto-provisioned (folder "PAXS") within ~30s.

## Bare-metal (no Docker)

Point Prometheus at the same targets directly and mount the Grafana
provisioning:

```bash
# prometheus.yml targets:
#   - targets: ["127.0.0.1:8405"]          # HAProxy on the lb host
#   - targets: ["node1:8000","node2:8000"] # PAXS nodes (hostnames/IPs)

prometheus --config.file=deploy/prometheus.yml
grafana server --homepath=/usr/share/grafana \
  --config /etc/grafana/grafana.ini   # with provisioning dirs copied in
```

## Dashboard panels

1. **Per-node HAProxy traffic** — `rate(haproxy_server_http_responses_total{server=~"paxs-node-.*"}[5m])`
   summed by server. You see the round-robin split; a flat line on one node
   means it's out of rotation (check its `/api/v1/health/ready`).
2. **Worker queue depth** — `paxs_worker_queue_depth` by state. `pending`
   climbing with `running` flat means the workers are undersized — add
   `python -m app.services.worker` processes (see `deploy/SCALE-OUT-GUIDE.md`).
3. **Read-replica lag** — `paxs_replica_lag_seconds` + a status stat
   (`replica_connected`, `replica_configured`, `paxs_db_up`). Lag climbing
   toward 30s+ means the replica can't keep up — consider flipping the
   `dbp_read_replica` admin toggle off until it catches up.

## Alerting (Prometheus rules — copy into a rules file)

```yaml
groups:
  - name: paxs
    rules:
      - alert: PaxsWorkerBacklog
        expr: sum(paxs_worker_queue_depth{state="pending"}) > 50
        for: 10m
        annotations:
          summary: "Compression backlog ({{ $value }} pending jobs)"
      - alert: PaxsReplicaLag
        expr: max(paxs_replica_lag_seconds) > 120
        for: 5m
        annotations:
          summary: "Read replica lag {{ $value }}s"
      - alert: PaxsNodeDown
        expr: up{job="paxs-nodes"} == 0
        for: 2m
        annotations:
          summary: "PAXS node {{ $labels.instance }} unreachable"
      - alert: PaxsReplicaDown
        expr: paxs_replica_connected == 0
        for: 5m
        annotations:
          summary: "Read replica not answering"
```

## Notes

- `paxs_http_requests_total` is per-process and resets on restart — use
  `rate()` over a window, never the raw counter, for traffic graphs.
- The `/metrics` endpoint is **unauthenticated by design** (Prometheus
  standard). Don't expose it publicly; HAProxy's `metrics_in` binds `*:8405`
  — restrict it to your monitoring subnet if needed.
- HAProxy metrics only exist after traffic has touched the relevant
  frontend/backend; empty graphs right after boot are normal.
