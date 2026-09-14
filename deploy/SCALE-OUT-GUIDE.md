# PAXS Scale-Out Deployment Guide — N Workers + HAProxy + Read Replica

Production topology for a PAXS archive that outgrows a single process: **N
standalone worker processes** for CPU-bound transcoding, **HAProxy** in front
of stateless API/DIMSE/HL7 nodes, and a **MySQL read replica** absorbing the
study-list / dashboard reads.

This guide complements `deploy/LOAD-BALANCING.md` (node statelessness) and
uses the same files: `deploy/haproxy.cfg`, `deploy/docker-compose.lb.yml`,
`deploy/Dockerfile`.

---

## 1. Topology

```
                 ┌──────────────┐   ┌──────────────┐
   :8000  HTTP ─▶│              │──▶│ paxs-node-1  │   WORKER_ENABLED=false
   :10400 DIMSE─▶│   HAProxy    │──▶│ paxs-node-2  │   (or true — safe either way)
   :2575  HL7  ─▶│              │──▶│ paxs-node-3  │
                 └──────────────┘   └──────────────┘
                        │                 │
                 ┌──────┴───────┐  ┌──────┴──────┐
                 │   MySQL 8    │  │  MinIO/S3   │  shared object storage
                 │  (source)    │  └─────────────┘
                 └──────┬───────┘
                        │ async replication
                 ┌──────┴───────┐
                 │ MySQL 8      │  DB_READ_REPLICA_URL (optional read replica)
                 │  (replica)   │
                 └──────────────┘

  Redis (shared, REDIS_URL) — rate limiting, render/thumb/frame caches,
  triage lock. One Redis is fine for all nodes + workers.

  N × standalone workers: python -m app.services.worker
  (run them on the nodes, on dedicated worker hosts, or as systemd units —
  they are stateless and claim work atomically from the shared DB)
```

Two independent axes of scale:

| Axis | Mechanism | When |
| --- | --- | --- |
| HTTP/DICOM/HL7 concurrency | N stateless nodes behind HAProxy (`deploy/haproxy.cfg`) | more associations / API load |
| Compression throughput | N standalone `app.services.worker` processes (this guide) | deep queues on CPU-bound J2K/JPEG-LS/HTJ2K transcodes |
| Read load | MySQL read replica + `get_read_db` routing | study-list / dashboard reads dominate |

All three share one **MySQL source** + one **object store** + one **Redis** —
that is what keeps every process stateless and safe to run in parallel.

---

## 2. Standalone workers (compression scale-out)

### Why standalone

`WORKER_ENABLED=true` (default) starts **one in-process worker thread** inside
each API node. Compression of J2K/JPEG-LS/HTJ2K series is CPU-bound, so a
single thread caps throughput at ~1 core. Standalone worker **processes**
(`python -m app.services.worker`) let you dedicate as many cores as you like.

Safety: jobs are claimed with an atomic `UPDATE … WHERE status='PENDING'`
(rowcount-checked), so **each PENDING job is transitioned to RUNNING by
exactly one worker** — N workers may share the DB with zero double-processing.
Triage is additionally guarded by a Redis lock. Thumbnails, lifecycle,
routing, DR replication and slow-query hooks all run inside the same worker
loop, so the standalone entrypoint is a full replacement, not just a
compression runner.

### Run N workers

```bash
# on each worker host, N = number of cores you want to dedicate:

# systemd-style (loop until Ctrl-C):
python -m app.services.worker --poll 3.0

# cron-style (one batch then exit — good for off-peak surges):
python -m app.services.worker --once

# faster claim cadence for big queues:
python -m app.services.worker --poll 1.0
```

CLI flags (from `app/services/worker.py`):

| Flag | Default | Meaning |
| --- | --- | --- |
| `--poll N` | `WORKER_POLL_SECONDS` (3.0) | poll interval in seconds |
| `--once` | off | claim + process one batch, then exit |

The entrypoint idempotently ensures the schema exists (`Base.metadata.create_all`),
recovers stuck RUNNING jobs, and recovers stuck smart-router queue items before
starting — so a crashed worker leaves no permanent stall.

### Recommended env for a worker host

```bash
# .env (or export) on the worker host — same shared state as the nodes:
DATABASE_URL="mysql+aiomysql://paxs_user:CHANGE_ME@mysql:3306/paxs_db?charset=utf8mb4"
REDIS_URL="redis://redis:6379/0"
REDIS_CACHE_ENABLED="true"

STORAGE_BACKEND="s3"                       # shared object storage — never local
S3_ENDPOINT_URL="http://minio:9000"        # or leave empty for AWS
S3_REGION="us-east-1"
S3_BUCKET="paxs"
S3_ACCESS_KEY="CHANGE_ME"
S3_SECRET_KEY="CHANGE_ME"

COMPRESSION_ENABLED="true"                 # worker's main job
THUMBNAIL_ENABLED="true"
DEFAULT_COMPRESSION="JPEG-LS"              # JPEG-LS | JPEG2000 | HTJ2K | RLE
WORKER_POLL_SECONDS="3.0"
WORKER_BATCH_SIZE="15"                     # PENDING jobs claimed per tick

# These keep the worker from touching services it doesn't own:
DICOM_RECEIVER_ENABLED="false"
```

> ⚠️ **Never point a worker at local storage** (`STORAGE_BACKEND=local` with a
> node-local `STORAGE_ROOT`): the node that wrote the object may not be the
> node whose worker tries to compress it. S3/MinIO (or another shared backend)
> is required.

### systemd unit (one unit = one worker process)

`/etc/systemd/system/paxs-worker@.service`:

```ini
[Unit]
Description=PAXS worker %i
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/paxs
EnvironmentFile=/opt/paxs/.env
ExecStart=/opt/paxs/venv/bin/python -m app.services.worker --poll 3.0
Restart=always
RestartSec=5
User=paxs

[Install]
WantedBy=multi-user.target
```

Scale to N cores:

```bash
sudo systemctl daemon-reload
for i in 1 2 3 4; do
  sudo systemctl enable --now paxs-worker@$i
done
sudo systemctl status 'paxs-worker@*'
```

### Optional: disable the in-process worker on API nodes

If you run dedicated worker processes, you can keep `WORKER_ENABLED=true` on
the nodes (safe — atomic claims) or turn it off to keep the nodes purely
request-serving:

```bash
WORKER_ENABLED="false"        # API nodes only; workers claim everything
```

---

## 3. HAProxy frontend (API + DIMSE + HL7)

`deploy/haproxy.cfg` already round-robins:

| Port | Protocol | Backend | Health check |
| --- | --- | --- | --- |
| 8000 | HTTP | `paxs_api` | `GET /api/v1/health/ready` (503 → node out) |
| 10400 | TCP | `paxs_dimse` | TCP connect (900s timeouts for multi-GB C-STORE) |
| 2575 | TCP | `paxs_hl7` | TCP connect |

Add/remove nodes by editing the `server paxs-node-N …` lines (or scale the
anchor block in `docker-compose.lb.yml`). Verify round-robin:

```bash
for i in $(seq 8); do curl -s localhost:8000/api/v1/health | grep instance; done
# expect node-1, node-2, node-3 … rotating
```

Every node needs the shared-state env:

```bash
PAXS_INSTANCE_ID="node-1"                  # unique per node (health/dashboard)
DATABASE_URL="mysql+aiomysql://paxs_user:CHANGE_ME@mysql:3306/paxs_db?charset=utf8mb4"
REDIS_URL="redis://redis:6379/0"
STORAGE_BACKEND="s3"
S3_ENDPOINT_URL="http://minio:9000"
S3_ACCESS_KEY="CHANGE_ME"
S3_SECRET_KEY="CHANGE_ME"
S3_BUCKET="paxs"
DICOM_AE_TITLE="PAXS_SCP"                  # identical on every node
DICOM_RECEIVER_ENABLED="true"
HL7_ENABLED="true"
```

One-command containerized stack (3 nodes + MySQL + MinIO + HAProxy):

```bash
docker compose -f deploy/docker-compose.lb.yml up -d --build
```

---

## 4. MySQL read replica

### 4.1 Set up MySQL 8 replication (source → replica)

On the **source** (`mysql:3306`):

```sql
CREATE USER 'repl'@'%' IDENTIFIED BY 'CHANGE_ME';
GRANT REPLICATION SLAVE ON *.* TO 'repl'@'%';
FLUSH PRIVILEGES;
```

Initial snapshot + position:

```bash
mysqldump -h mysql -u root -p --single-transaction --routines \
  --triggers --all-databases > /tmp/source.sql
# capture the binlog position from the dump header:
grep -m1 "CHANGE MASTER TO" /tmp/source.sql
```

On the **replica** (`mysql-replica:3306`):

```bash
mysql -h mysql-replica -u root -p < /tmp/source.sql

mysql -h mysql-replica -u root -p <<'SQL'
CHANGE REPLICATION SOURCE TO
  SOURCE_HOST='mysql',
  SOURCE_USER='repl',
  SOURCE_PASSWORD='CHANGE_ME',
  SOURCE_LOG_FILE='mysql-bin.000001',   -- from the dump header above
  SOURCE_LOG_POS=157;                   -- from the dump header above
START REPLICA;
SQL

mysql -h mysql-replica -u root -p -e "SHOW REPLICA STATUS\G" | grep -E "Replica_IO_Running|Replica_SQL_Running|Seconds_Behind_Source"
# expect: Yes / Yes / small lag
```

### 4.2 Configure PAXS

```bash
# .env on every node (and optionally the workers):
DB_READ_REPLICA_URL="mysql+aiomysql://paxs_user:CHANGE_ME@mysql-replica:3306/paxs_db?charset=utf8mb4"
```

- Only `mysql+aiomysql://…` (or sqlite, for dev) replica URLs are honored —
  the replica engine is built at import when the URL starts with `mysql`/`sqlite`.
- Routing is **off by default**. Enable it at runtime in the admin panel
  (**Admin → DB Pool → Read replica**), which persists `dbp_read_replica=1`
  in `server_settings`. The toggle exists so replication lag never surprises
  anyone — flip it on only when `Seconds_Behind_Source` stays low.
- When the replica is unreachable or unconfigured, `get_read_db` transparently
  falls back to the primary — no outage, just no read offloading.
- **Admin → DB Pool** also shows `replica_probe()`: connection status,
  `Replica_IO_Running` / `Replica_SQL_Running`, and `Seconds_Behind_Source`
  (MySQL only; requires the `repl`-grade `SHOW REPLICA STATUS` privilege).

Which endpoints read via the replica: the hottest read-only routes
(study-list / dashboard QIDO, `/stats`, volume metadata) — anything using
`Depends(get_read_db)`. Writes and consistency-critical reads stay on the
primary.

### 4.3 Verify

```bash
curl -s localhost:8000/api/v1/health/ready            # 200 — node is ready
curl -s localhost:8000/api/v1/health | grep instance  # node-X answered
# Admin → DB Pool → Read replica should show connected + lag
```

---

## 5. Redis (shared, required for multi-process correctness)

```bash
REDIS_URL="redis://redis:6379/0"        # same URL everywhere
REDIS_CACHE_ENABLED="true"
```

Redis backs: rate-limit windows, `/rendered` + thumbnail + frame caches, the
warm-on-open dedupe lock, and the triage lock that makes N workers share the
triage queue safely. One Redis instance is sufficient at this scale; if it
goes down, rate limiting falls back to per-process in-memory windows and
caches degrade to in-process — the archive keeps serving.

---

## 6. Combined quick-start (bare metal, one host, 4 workers)

```bash
# 1) shared state
export DATABASE_URL="mysql+aiomysql://paxs_user:CHANGE_ME@127.0.0.1:3306/paxs_db?charset=utf8mb4"
export DB_READ_REPLICA_URL="mysql+aiomysql://paxs_user:CHANGE_ME@127.0.0.1:3307/paxs_db?charset=utf8mb4"
export REDIS_URL="redis://127.0.0.1:6379/0"
export STORAGE_BACKEND="s3"
export S3_ENDPOINT_URL="http://127.0.0.1:9000"
export S3_ACCESS_KEY="CHANGE_ME" S3_SECRET_KEY="CHANGE_ME" S3_BUCKET="paxs"

# 2) API node (no in-process worker — standalone workers own the queue)
export PAXS_INSTANCE_ID="node-1" WORKER_ENABLED="false"
venv/Scripts/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 &

# 3) four standalone workers (4 cores of transcoding)
for i in 1 2 3 4; do
  venv/Scripts/python -m app.services.worker --poll 3.0 &
done

# 4) (optional) HAProxy instead of direct :8000
#    docker compose -f deploy/docker-compose.lb.yml up -d
```

---

## 7. Operational notes

- **Workers are safe to restart**: the atomic claim + stuck-job recovery means
  a killed worker's RUNNING jobs are re-queued on the next poll.
- **Watch the queue**: `Admin → Overview` shows pending compression jobs. If
  it trends up, add worker processes (or raise `WORKER_BATCH_SIZE`).
- **Don't run the in-process worker AND forget it**: with `WORKER_ENABLED=true`
  on nodes and standalone workers elsewhere, claims are still race-free — but
  if you want a single place that owns the queue, set `WORKER_ENABLED=false`
  on the nodes.
- **Replica lag**: keep `dbp_read_replica` off unless lag is consistently
  low; the toggle is the kill-switch if a burst of writes skews it.
- **Ports are process-local**: each API node binds `:10400`/`:2575`; the
  standalone workers bind nothing (DB + Redis only).
