# PAXS Monitoring — Prometheus + Grafana

Metrics for the three things that matter in a scale-out archive: **per-node
HAProxy traffic**, **worker queue depth** (compression backlog), and
**read-replica lag**. Two scrape sources, one dashboard.

## What's scraped

| Source | Endpoint | Metrics |
| --- | --- | --- |
| **HAProxy** (built-in exporter, HAProxy ≥ 2.4) | `http://haproxy:8405/metrics` | per-node `haproxy_server_http_responses_total{server="paxs-node-N"}` (traffic split), `haproxy_frontend_http_requests_total` (total rate), server status |
| **PAXS nodes** (new `/metrics`) | `http://paxs-node-N:8000/metrics` | `paxs_worker_queue_depth{state="pending|running"}`, `paxs_routing_queue_depth{state=...}`, `paxs_replica_lag_seconds` / `paxs_replica_connected` / `paxs_replica_configured`, `paxs_db_up`, `paxs_http_requests_total`, `paxs_info{instance=PAXS_INSTANCE_ID}`, and the decode-cache family below |

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
4. **Decode-cache effectiveness** — the `paxs_cache_*` family. This is how you
   tune `VOLUME_CACHE_BUDGET_MB` / `DICOM_FRAME_CACHE_MB` from evidence instead
   of guessing:

   | Series | Meaning |
   | --- | --- |
   | `paxs_cache_operations_total{cache,result}` | `result` is `hit` / `miss` / `insert` / `evict` |
   | `paxs_cache_bytes{cache}` | bytes currently held |
   | `paxs_cache_entries{cache}` | entries currently held |
   | `paxs_cache_budget_bytes{cache}` | the configured budget (read at scrape time, so a settings override shows immediately) |
   | `paxs_cache_utilisation_ratio{cache}` | `bytes / budget`, 0–1 |

   `cache` is `volume` (materialised HU volumes) or `frame` (decoded pixel
   bytes). Read it as:

   * **ratio pinned at 1.0 + high miss rate** → the budget is too small; raise it
     (`paxs_cache_utilisation_ratio{cache="volume"} > 0.95` with
     `rate(paxs_cache_operations_total{result="miss"}[5m])` climbing).
   * **ratio low + high hit rate** → the budget is already generous; you can
     lower it and reclaim RSS.
   * **`evict` climbing with `hit` flat** → sessions are evicting each other, so
     the working set does not fit. Either raise the budget or lower
     `HYBRID_MAX_SERVER_MPR_SESSIONS`.
   * **`miss` climbing with `insert` flat** → requests are for data the cache
     never holds (an un-cacheable access pattern, e.g. one pass over a whole
     study), which no budget will fix.

   Note `paxs_cache_budget_bytes` is the honest reference for these ratios — the
   L1 volume cache is *also* capped indirectly by
   `HYBRID_SERVER_VOL_BUDGET_MB` at negotiation time, so if you set
   `VOLUME_CACHE_BUDGET_MB` above it the startup log warns.

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
      # Decode-cache pressure: the budget is full AND requests are still missing,
      # which means the working set does not fit and every extra request costs a
      # disk decode. Raise VOLUME_CACHE_BUDGET_MB / DICOM_FRAME_CACHE_MB, or lower
      # HYBRID_MAX_SERVER_MPR_SESSIONS.
      - alert: PaxsDecodeCacheThrashing
        expr: |
          paxs_cache_utilisation_ratio > 0.95
          and rate(paxs_cache_operations_total{result="miss"}[10m]) > 0.5
        for: 15m
        annotations:
          summary: "Decode cache {{ $labels.cache }} is full and still missing ({{ $value | printf \"%.2f\" }})"
      # A cache that reports hits but has been fully evicted between scrapes is
      # a strong signal the budget is an order of magnitude too small.
      - alert: PaxsDecodeCacheEvicting
        expr: rate(paxs_cache_operations_total{result="evict"}[10m]) > 1
        for: 15m
        annotations:
          summary: "Decode cache {{ $labels.cache }} is evicting continuously"
```

## Notes

- `paxs_http_requests_total` is per-process and resets on restart — use
  `rate()` over a window, never the raw counter, for traffic graphs.
- The `/metrics` endpoint is **not** public. Prometheus scrapes without
  credentials, so access is granted by source address: peers in
  `METRICS_ALLOW_IPS` (default `127.0.0.1,::1` + the RFC1918 ranges, which is
  exactly what the compose network needs) may scrape unauthenticated, and every
  other caller must authenticate. Set it to `*` only on a genuinely trusted
  network. HAProxy's `metrics_in` binds `*:8405` separately — restrict that to
  your monitoring subnet too.

  > This was broken: the route previously required authentication
  > unconditionally while `prometheus.yml` sent none, so every scrape returned
  > 401, the whole `paxs-nodes` job was down, and `PaxsNodeDown` fired for every
  > node. The docs also claimed the endpoint was "unauthenticated by design".
  > Both are fixed.
- HAProxy metrics only exist after traffic has touched the relevant
  frontend/backend; empty graphs right after boot are normal.
- The `paxs_cache_*` counters are per-process and reset on restart, same as
  `paxs_http_requests_total` — always wrap them in `rate()`. The gauges
  (`bytes`, `entries`, `budget_bytes`, `utilisation_ratio`) are absolute and safe
  to read directly.
- Every decode-cache series is emitted for **both** caches even with no traffic,
  so `rate()` and alerting see a continuous series rather than one that appears
  and disappears between scrapes.
- A decode failure now logs the transfer syntax with its human codec name, e.g.
  `transfer_syntax=1.2.840.10008.1.2.4.80 (JPEG-LS Lossless) | modality=CT
  bits=16/16 samples=1 photometric=MONOCHROME2 512x512 | series=… sop=… frame=3`.
  Grep for `transfer_syntax=` to find a systematic codec gap without opening any
  files — see `PAXS_VIEWER_DIAGNOSTIC_AUDIT.md` V1-4 for why that matters.
