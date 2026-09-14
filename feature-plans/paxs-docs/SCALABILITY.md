# قابلية التوسع — Scalability Analysis

> تحليل معمّق لقدرة نظام PAXS على التوسع الرأسي والأفقي، مع الأرقام الفعلية من الكود والإعدادات،
> والتوصيات المبنية على المنطق الهندسي. يُقرأ بالتزامن مع `ARCHITECTURE.md` و`STABILITY.md`.
>
> Grounded in: `app/core/config.py`, `app/db/session.py`, `app/services/worker.py`,
> `app/dicom/receiver.py`, `deploy/LOAD-BALANCING.md`, `deploy/haproxy.cfg`.

---

## 1. The scaling model at a glance

PAXS is a **stateless API + shared-state backend** architecture. Every HTTP/DICOM node is
stateless (JWT auth, no server-side sessions); all state lives in MySQL + object storage.
That single fact is what makes horizontal scaling possible: add nodes, share the DB and the
storage tier, and the system fans out.

| Layer | Component | Concurrency model today | Horizontal scale lever |
|---|---|---|---|
| Web API | FastAPI (async, single event loop) | I/O-bound; many concurrent requests per process | Stateless → N processes behind HAProxy |
| DIMSE | pynetdicom AE, `maximum_associations = 16` | 1 worker thread per association | HAProxy TCP balancing on :10400 |
| Worker | 1 daemon thread, poll 5 s | serial batch processing | Standalone worker processes (DB-claimed jobs) |
| DB | MySQL 8 (utf8mb4, 23 performance indexes) | async `NullPool` (1 conn/request) + sync `QueuePool` (5+10) | Read replica + bigger buffer pool |
| Storage | local FS **or** S3/MinIO (multi-backend) | per-object reads/writes | S3 = the true horizontal enabler |

---

## 2. Vertical scaling (scale up) — what the single node can do

### 2.1 Web/API path
- FastAPI's single async event loop multiplexes thousands of idle sockets; each request is
  I/O-bound (DB query + object read). CPU cost per request is low (JSON serialization,
  pydicom parsing on ingest).
- **The async DB engine uses `NullPool`** (`app/db/session.py`): one connection per request,
  never reused across event loops. This is *deliberate* — aiomysql connections bind awaits to
  the loop that created them, and the receiver + tests create fresh loops. Cost: connection
  setup per request (a handshake round-trip). On MySQL this is ~1–3 ms overhead per request;
  fine up to hundreds of requests/second, but a **pooled async engine**
  (`AsyncAdaptedQueuePool`) removes it once loop discipline is guaranteed.

### 2.2 DIMSE ingest path
- pynetdicom starts one thread per accepted association (cap `DICOM_MAX_ASSOCIATIONS = 16`).
  Each C-STORE handler writes the file, then indexes into MySQL **through the sync engine's
  bounded `QueuePool` (pool_size 5, max_overflow 10, pre-ping, recycle 1800 s)**.
- 15 concurrent MySQL connections shared by receiver threads + worker = the ingest throughput
  ceiling on one node. With typical 500 KB–2 MB CT slices, disk/network I/O dominates — a
  single node comfortably ingests **dozens of studies/min** (hundreds of instances/min).

### 2.3 Worker
- The compression/thumbnail/triage/lifecycle/routing/replication worker is **one thread**,
  processing a batch of **10 compression jobs per tick**, triage claims **5 series per tick**.
  Compression (RLE/JPEG-LS/JPEG2000/HTJ2K) is CPU-bound → this is the first vertical
  bottleneck for big-archive workflows. On one node, CPU core count bounds compression
  throughput (roughly 1 core per active transcode).

### 2.4 Data-volume envelope (reasoned estimate)
- **Indexed hot path:** every filter in the Studies/QIDO lists uses one of the 23 indexes
  (composites: `(tenant_id, created_at)`, `(study_id, series_number)`, `(series_id,
  instance_number)`, `(status, station)` MWL, `(reviewed, severity)` triage, …).
- A single node is comfortable with **hundreds of thousands of instances** (CT-heavy ~10⁵–10⁶
  objects) on local/SATA, and **millions** with SSD/S3, before the Web/DIMSE path — not the
  DB — becomes the limiter.
- The known quadratic pain point is `storage_usage_bytes()` — it walks the storage tree on
  every `/stats` call (O(objects)). Below ~10⁵ objects it is fine; beyond that, **cache it**.

---

## 3. Horizontal scaling (scale out) — the supported topology

### 3.1 Stateless web nodes + HAProxy (roadmap #47)
`deploy/haproxy.cfg` + `docker-compose.lb.yml` ship a ready load balancer:
- **Liveness/readiness probes**: `GET /api/v1/health/live` (process up) and
  `GET /api/v1/health/ready` (DB reachable → 503 removes a broken node).
- Each node reports `PAXS_INSTANCE_ID` in `/health` so the dashboard and logs show which
  node served a request.
- JWT is self-contained (HS256, `SECRET_KEY` shared) → **no sticky sessions needed**;
  any node can serve any request.

### 3.2 Read replica (offload the hottest reads)
- `DB_READ_REPLICA_URL` (new) creates a second async engine. When the admin toggle is on,
  `get_read_db` routes `/api/v1/stats` and `/api/v1/studies` (the hottest dashboard reads)
  through the replica, **with an automatic ping + fallback to the primary** if the replica is
  unreachable. Replication lag is surfaced in the DB Pool panel (`SHOW REPLICA STATUS`).
- Reasoning: dashboard traffic is overwhelmingly reads; moving them off the primary frees
  its connections for the ingest/worker write path. The toggle defaults **off** so
  read-your-writes consistency is never silently broken.

### 3.3 Multi-receiver ingest (HAProxy TCP on :10400)
- Several receivers can accept C-STORE associations simultaneously because **ingest is
  idempotent by SOP Instance UID**: the same instance arriving twice simply overwrites its
  own file under `{StudyUID}/{SeriesUID}/{SOPUID}.dcm`, and the DB upsert is keyed by the
  UID. No locking, no duplicates.
- All receivers share one MySQL (the QueuePool is per-process, so size it per node) and one
  object-storage tier.

### 3.4 Worker scale-out (the honest gap + the path)
- Today the worker is **in-process and single-threaded** per node. The data model is already
  **multi-worker safe**: compression jobs are claimed by status transition
  (PENDING→RUNNING), triage claims series atomically (`UPDATE … WHERE triage_status =
  'PENDING'`), and the routing queue is durable — so **spawning a second worker process
  cannot double-process work**.
- The documented swap-in is Celery/RQ (`worker.py` docstring). Minimum change: run
  `CompressionWorker` in N standalone processes (same DB), raise `DB_POOL_SIZE` per node,
  and keep `_recover_stuck_jobs()` on each startup. Compression scales ~linearly with cores
  across worker processes.

### 3.5 Storage tier = the real horizontal enabler
- **S3/MinIO multi-backend** (`STORAGE_BACKEND=s3`, named backends, `move_study_to_backend`,
  presigned GET URLs) means object bytes live outside any single node. Combined with
  lifecycle tiering (ONLINE → ARCHIVED → PURGE) and lossy compression rules, cold data can
  be pushed to cheap storage while hot data stays fast.
- Presigned URLs (`S3_PRESIGN_EXPIRES=900`) let heavy WADO downloads bypass the API process
  entirely — the browser streams from S3/MinIO directly. This is the single biggest
  throughput win for large studies at scale.

---

## 4. Bottleneck analysis with full reasoning

| # | Bottleneck | Why | Severity | Mitigation / recommendation |
|---|---|---|---|---|
| 1 | Async engine `NullPool` | One MySQL handshake per request | Medium at high QPS | `AsyncAdaptedQueuePool` (MySQL only) once loop discipline is guaranteed; or keep NullPool behind HAProxy with several nodes |
| 2 | `storage_usage_bytes()` O(objects) | `/stats` walks the tree every call | High past 10⁵ objects | Cache with TTL, or store per-backend usage counters |
| 3 | Single worker thread | Compression is CPU-bound; one thread = 1 core | High for big archives | Standalone worker processes / Celery; jobs are already claim-safe |
| 4 | In-memory rate limiter | `_WINDOWS` is per-process; resets on restart; per-node windows under LB | Medium | Move to Redis for a global window (or accept per-node windows) |
| 5 | `EXPLAIN` shows `Using filesort` on studies list | `ORDER BY study_date DESC` over a joined table | Medium | Covering index `(tenant_id, study_date, id)`; verify with the DB Pool EXPLAIN tool |
| 6 | STOW-RS buffers full multipart body in RAM | `request.body()` + parse; client cap 4 GiB | Medium (memory spikes) | Stream parts (e.g. iterate multipart frames); raise per-part not per-request caps |
| 7 | QIDO `limit` cap = 1000 | Page-scroll queries beyond 1000 need cursors | Low | Add keyset pagination on `(study_date, id)` |
| 8 | `DICOM_MAX_ASSOCIATIONS=16` | Hard cap on concurrent modalities per node | Low | Raise with node capacity; HAProxy spreads associations |

---

## 5. Recommendation roadmap (priority order)

1. **Redis rate limiting + Redis-backed job queue** → removes the two single-node memory
   structures (in-memory windows, in-process worker) in one move.
2. **Read replica for all dashboard reads** (already built; flip per-tenant toggles on).
3. **Cache `/stats` usage counters** (1-line backend change; big win).
4. **Async pooled engine for MySQL** under a load test with `get_read_db` (validates loop
   safety before enabling).
5. **Streaming STOW-RS** and **keyset pagination** for >10⁵-object archives.
6. **Standalone worker processes** with `DB_POOL_SIZE` raised per node (compression
   throughput scales with cores).

---

## 6. Conclusion

PAXS is architected for **horizontal scale by design**: stateless JWT nodes, idempotent
ingest, DB-claim-safe workers, shared S3 storage, read-replica routing, and a shipped
HAProxy topology. A single node comfortably serves a mid-size department; the documented
steps in §3 and §5 carry it to multi-site / million-instance scale without redesign — the
bottlenecks to watch are the in-process worker, the NullPool handshake, and the O(n) usage
counter, all of which have concrete, low-risk fixes.

📎 Related: [`STABILITY.md`](STABILITY.md) · [`ARCHITECTURE.md`](ARCHITECTURE.md) ·
[`deploy/LOAD-BALANCING.md`](../deploy/LOAD-BALANCING.md)
