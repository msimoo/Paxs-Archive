# الاستقرار والموثوقية — Stability & Reliability Analysis

> تحليل معمّق لسلوك النظام عند الفشل: فشل قاعدة البيانات، التخزين، العامل الخلفي، المستقبِل،
> الشبكة، والانقطاعات — مع آليات الحماية الفعلية في الكود والتوصيات. يُقرأ مع `ARCHITECTURE.md`
> و`SCALABILITY.md`.
>
> Grounded in: `app/db/session.py`, `app/services/worker.py`, `app/dicom/receiver.py`,
> `app/api/ratelimit.py`, `app/services/routing_service.py`, `app/services/storage_commit_service.py`,
> `app/services/db_admin.py`, `app/db/perf.py`, tests (424 on SQLite).

---

## 1. Design principles baked into the code

1. **Nothing crashes the loop.** The worker wraps every batch/sweep in `try/except` and logs —
   a failed compression, triage batch, routing sweep, or replication sweep never kills the
   daemon thread. The receiver's handlers return DIMSE failure statuses instead of raising.
2. **Durability over speed.** Writes are transactional (async `get_db` commits on success,
   rolls back on failure); ingest is idempotent by SOP UID; the routing queue is a DB table,
   not memory.
3. **Self-healing on restart.** Jobs/series/queue items left `RUNNING` by a crash are
   re-queued at startup (`_recover_stuck_jobs`, `recover_stuck_queue_items`).
4. **Fail-visible, not fail-silent.** Health probes, audit trails, the DB Pool panel and the
   slow-query log make degraded states observable.

---

## 2. Failure-mode matrix (what happens when …)

| Failure | Behavior | Protection in code |
|---|---|---|
| **MySQL down** | `/health/ready` returns 503 → HAProxy drops the node. API requests 500 with logged error; receiver C-STORE returns failure to the modality (modality retries); worker logs and retries next tick | `health_ready()` probe; per-batch `try/except`; transactional sessions |
| **MySQL restart** (brief) | Sync `QueuePool` **pre-ping** drops stale connections; `pool_recycle=1800` rotates before `wait_timeout` | `DB_POOL_PRE_PING`, `DB_POOL_RECYCLE_SECONDS` |
| **Read replica down** | `get_read_db` pings before yielding → transparent fallback to primary; panel shows `down — <error>` | ping + fallback in `get_read_db`; `replica_probe()` |
| **Storage backend down (S3/MinIO)** | `exists()` checks guard every read; ingest failure logged, STOW part recorded as failed; presign path falls back to app WADO-URI | `exists()` before read/write; per-part STOW `FailedSOPSequence` |
| **Worker crash** | Daemon thread dies silently; on restart `_recover_stuck_jobs()` re-queues RUNNING compression jobs + triage series | startup recovery; claim-then-update pattern |
| **Receiver crash / port conflict** | Fails fast at startup with a clear log; process supervisor restarts | daemon thread + `start_receiver_in_thread` |
| **Modality sends duplicate instance** | Same SOP UID overwrites its own file; DB upsert is keyed by UID → no duplicates, no errors | idempotent ingest by design |
| **STOW-RS partial upload** | Each part handled independently; bad parts go to `FailedSOPSequence` (0xC212), good parts stored; overall 200 if any stored, 409 if none | `stow_studies` per-part loop |
| **Auth brute-force** | Login rate-limited (`RATE_LIMIT_LOGIN_PER_MINUTE=10`), global 300/min, 429 returned; static assets exempt so viewer bursts are never throttled | sliding-window limiter + `RateLimitMiddleware` |
| **Routing send fails** (node unreachable) | Durable queue item retried with exponential backoff, `ROUTING_MAX_ATTEMPTS=5`; FAILED items remain visible for manual re-queue (never silently dropped) | `routing_queue` table + `process_queue_batch` |
| **Storage Commitment verify mismatch** | Instance missing on disk → `0x0110` (no such object) in the N-EVENT-REPORT; request stays FAILED and visible | `verify_instances` + `commitment_statuses` table |
| **DR replication target down** | Replication records retried (`DR_REPLICATION_MAX_ATTEMPTS=5`, sweep every 4 ticks); failures logged, never kill worker | `replication_records` + `sweep()` |
| **Crashed mid-transaction** | Async session rolls back on exception; sync sessions use `with` context → rollback on exit | `get_db` / `SyncSessionLocal` semantics |
| **DB schema drift** | Alembic chain is cycle-tested (up → down → up); `run_migrations` reconciles `create_all`-bootstrapped DBs with `stamp head` instead of failing | migration stamp-on-exists |
| **Event-loop misuse** (aiomysql + threads) | Windows selector policy + `NullPool` prevent cross-loop connection reuse (the classic `MissingGreenlet`/crash trap); all thread DB work uses the sync engine | `WindowsSelectorEventLoopPolicy`, dual-engine split |

---

## 3. Durability & integrity

- **Audit trail**: every stored/deleted/retrieved/compressed action writes an
  `audit_log` row with a **SHA-256 fingerprint** of the object — tamper-evident history,
  exposed via `/api/v1/audit` (ADMIN/RADIOLOGIST only).
- **Backups**: SQLite uses the sqlite3 hot-backup API (safe under active ingest via
  `busy_timeout=30s`); MySQL uses `mysqldump` (`--single-transaction --routines`) with a
  **pure-SQLAlchemy export fallback** when `mysqldump` is absent. UI: Settings → Database →
  Create backup / Download / Delete.
- **DR (off-site)**: replication to a registered S3/Azure-Blob target with the `replica/`
  prefix — a second copy independent of the primary site.
- **Consistency model**: strong consistency on the primary (read-your-writes default — the
  read-replica toggle is off unless an operator enables it).

---

## 4. Observability (how you know it's healthy)

| Surface | What it exposes | Where |
|---|---|---|
| `/api/v1/health/live` | process liveness (HAProxy `httpchk`) | any node |
| `/api/v1/health/ready` | DB reachability → 503 when broken | HAProxy drain |
| `/api/v1/health` | full posture: DB, DICOM, TLS, HL7, storage, FHIR, version, instance id | dashboard header |
| **Admin → DB Pool** | live pool stats (checked-out/size/overflow), MySQL counters, replica status + lag, **slow-query log**, **EXPLAIN analyzer** | new panel |
| `/api/v1/audit` | tamper-evident event trail | Admin → Audit |
| Structured logs | `paxs.*` loggers, per-batch exceptions | server log |

---

## 5. Testing as a stability guarantee

- **424 tests pass on SQLite** (full suite) plus MySQL subsets (receiver, dicomweb, storage,
  database, reading, replication, db-perf, …) — the same suite runs against both engines, so
  SQL portability regressions are caught, not discovered in production.
- **Real DIMSE integration tests**: pynetdicom SCU ↔ SCP over the socket (C-STORE, C-FIND,
  C-GET, C-ECHO, MWL C-FIND, MPPS N-CREATE/N-SET, Storage Commitment N-ACTION) — the DICOM
  surface is exercised, not mocked.
- **Migration cycle tests** (upgrade → downgrade → upgrade) run for every schema change;
  the live MySQL got `stamp + upgrade` reconciliation without data loss.
- **Worker recovery is tested implicitly** via claim/reclaim patterns and restart recovery.

---

## 6. Known risks & hardening recommendations (reasoned)

| Risk | Reasoning | Recommendation |
|---|---|---|
| **One worker thread per node** | Compression backlog under sustained ingest; triage queue stalls behind a long batch | Run N worker processes (jobs are claim-safe); add per-queue priority |
| **In-memory rate limiter** | Windows reset on restart; per-node windows under HAProxy (an attacker can spread requests) | Redis-backed limiter; keep per-endpoint auth limiter as the hard gate |
| **No alerting** | Degraded states are visible but not pushed | Wire `/health/ready` to uptime monitoring / webhooks (`WEBHOOK_URLS` exists) |
| **`storage_usage_bytes()` O(n)** | `/stats` cost grows with object count | Cache with TTL; store per-backend counters |
| **Custom-script isolation** | Scripts run in-process (trusted admin code) | Keep `SCRIPTS_DIR` admin-gated (already) and reviewed; consider a subprocess sandbox |
| **No circuit breaker on remote DICOMweb** | A slow peer can hold worker threads up to the client timeout | Add per-node breaker / concurrency cap in `dicomweb_client` |
| **In-flight batch at shutdown** | Worker `stop()` joins 5 s — long compressions may be cut mid-flight (they are recoverable at restart) | Graceful drain: wait for current batch with a longer cap |

---

## 7. Conclusion

PAXS is engineered for **fail-safe operation in a clinical context**: idempotent ingest,
transactional writes, self-healing worker recovery, durable routing/commitment/replication
records, health probes for load-balancer draining, tamper-evident audit, and a 424-test
suite that runs on both SQLite and MySQL. The residual risks are operational (alerting,
rate-limit locality, single worker thread) rather than architectural — each has a concrete,
low-risk hardening path listed in §6.

📎 Related: [`SCALABILITY.md`](SCALABILITY.md) · [`ARCHITECTURE.md`](ARCHITECTURE.md) ·
[`deploy/LOAD-BALANCING.md`](../deploy/LOAD-BALANCING.md)
