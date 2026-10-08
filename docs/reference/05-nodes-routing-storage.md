# Remote nodes, routing, replication, storage tiers, lifecycle, quotas, migration, prefetch, tenants, scripting

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/04-ingest-storage-schema.md](5a32aa4/04-ingest-storage-schema.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

Severity legend: 🔴 blocking (data loss/corruption, authorization bypass with destructive or egress impact, or a feature that cannot work), 🟡 important, 🟢 nit.

Auth legend used in tables: **U** = `get_current_user` (any signed-in user, `deps.py:46`); **D** = `require_destructive_role` (ADMIN or RADIOLOGIST, `deps.py:108`); **A** = `require_admin` (ADMIN, `deps.py:117`). "Handler" is the `def` line; the decorator is the line above it.

## Verification performed and limits

- Every in-scope file was read in full, in chunks where long (26 files, 5,703 lines).
- Targeted peeks outside scope were used only to resolve names or confirm behaviour that a finding depends on (listed in section 1).
- Two stdlib-only reproductions were run locally: (1) the lifecycle worker's `id = 'lifecycle-worker'` comparison returns no rows; (2) Starlette-style first-match routing resolves `GET /api/v1/quotas/status` to `get_quota`.
- Not run: the FastAPI app, the test suite, and MySQL behaviour. `fastapi`, `sqlalchemy`, `pydicom`, `pynetdicom`, `starlette`, and `httpx` are not installed in the system Python, and I did not install packages.
- The storage backend module (`app.services.storage`) is absent from the working tree (see E-27), so every backend `move`/`delete`/`exists` semantic described below is unverified where it matters.

---

## 1. Files covered

| Path | Lines | Purpose |
|---|---|---|
| app/api/v1/nodes.py | 647 | DICOM/DICOMweb node registry CRUD; C-ECHO ping; C-STORE send, C-FIND, C-GET pull; QIDO/WADO/STOW endpoints; NDJSON upload stream |
| app/services/node_service.py | 28 | Sync lookup of an enabled node by AE title (`get_node_sync`) |
| app/api/v1/routing.py | 209 | Auto-routing rule CRUD; manual evaluate; send-queue list, retry, manual sweep |
| app/services/routing_service.py | 425 | Rule matching, queue enqueue, C-STORE dispatch, queue worker with backoff, rule CRUD |
| app/api/v1/replication.py | 62 | DR replication admin API (status, records, sweep, per-study) |
| app/services/replication_service.py | 286 | DR copy of study objects to an off-site S3-compatible target; sweep; status |
| app/api/v1/storage.py | 287 | Storage backend CRUD and connection test; study move between backends |
| app/api/v1/quota.py | 206 | Storage quota CRUD and status views |
| app/services/quota_service.py | 265 | Quota lookup, usage sums, pre-write check (pre-write check has no callers) |
| app/api/v1/lifecycle.py | 218 | Lifecycle rule CRUD; run; per-study archive/restore/purge; studies list |
| app/services/lifecycle_service.py | 264 | Hot/cold tiering via `.archive/` prefix; purge; rule evaluation |
| app/api/v1/lossy_compression.py | 162 | Lossy rule CRUD and on-demand run |
| app/services/lossy_compression_service.py | 220 | JPEG transcode of stored instances, in place |
| app/api/v1/migration.py | 132 | Folder import and C-MOVE batch job API; job status |
| app/services/migration_service.py | 247 | Folder scan/import; C-MOVE batch runner |
| app/api/v1/prefetch.py | 211 | Prefetch rule CRUD; manual trigger; job list |
| app/services/prefetch_service.py | 226 | C-FIND priors and C-MOVE retrieval (C-MOVE is broken, E-11) |
| app/api/v1/priors.py | 60 | Prior-study lookup by study UID or patient ID |
| app/services/prior_service.py | 123 | Prior-study scoring and listing |
| app/api/v1/tenants.py | 272 | VNA tenant CRUD; AE-title mapping; user assignment; tenant context |
| app/services/tenant_service.py | 101 | Process-local AE-to-tenant cache; user scope resolution |
| app/api/v1/split_merge.py | 105 | Split and merge endpoints |
| app/services/split_merge_service.py | 257 | Series re-parenting with DICOM header rewrite |
| app/api/v1/scripts.py | 89 | Admin script management API |
| app/services/scripting_service.py | 345 | In-process Python script loader and event dispatcher |
| app/api/v1/commitments.py | 63 | Storage commitment read API (no service layer) |
| app/services/anonymizer.py | 193 | DICOM de-identification and ZIP assembly |

Peeked, targeted only (not part of scope):
- Fully read: `app/api/v1/deps.py` (120), `app/api/v1/export.py` (77, the only caller of the anonymizer), `app/services/audit_service.py` (signature check), `app/services/imaging.py` (lines 40-90, dataset-to-image rendering used by lossy).
- Selected ranges: `app/services/storage_service.py` (40-453), `app/services/worker.py` (100-240), `app/services/indexing_service.py` (60-160, 225-260), `app/dicom/receiver.py` (60-80), `app/services/dicomweb_client.py` (44-83), `app/dicom/sender.py` (50-125, 176-180, 238, 398-405, 547, 663-727, 772-800).
- Grep only: `app/db/models.py` (1,581 lines; class and constraint lines), `app/core/config.py` (defaults), `app/main.py` (router mounting), `app/db/session.py` (`get_db`, lines 223-231).

Totals: in-scope 26 files, 5,703 lines.

---

## 2. API endpoint reference

86 routes across 14 routers. Prefixes: `nodes` at `/api/v1/nodes`, `routing` at `/api/v1/routing`, `replication` at `/api/v1/replication`, `storage` at `/api/v1/storage`, `quotas` at `/api/v1/quotas`, `lifecycle` at `/api/v1/lifecycle`, `lossy` at `/api/v1/compression/lossy`, `migration` at `/api/v1/migration`, `prefetch` at `/api/v1/prefetch`, `priors` at `/api/v1/priors`, `tenants` at `/api/v1/tenants`, split/merge at `/api/v1/studies`, `scripts` at `/api/v1/scripts`, `commitments` at `/api/v1/commitments`.

No route in scope is unauthenticated. Every route depends on U, D, or A, or performs an inline ADMIN check. The problem is role mismatch, not a missing auth dependency (see section 5).

### 2.1 Nodes (`nodes.py`, prefix `/api/v1/nodes`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 1 | GET | /api/v1/nodes | U | none | list of node objects (`_node_out`: no password, `has_web_auth` flag) | nodes.py:89 |
| 2 | POST | /api/v1/nodes | U + inline ADMIN (403) | `NodeIn`: ae_title 1-64, host 1-255, port 1-65535, description, enabled, web_url ≤512, web_username ≤128, web_password ≤256 | 201 node object; 409 duplicate; 422 invalid web_url | nodes.py:96 |
| 3 | PUT | /api/v1/nodes/{ae_title} | U + inline ADMIN | `NodeIn` (full replace; omitted fields become null, see E-17) | node object; 404 | nodes.py:122 |
| 4 | DELETE | /api/v1/nodes/{ae_title} | U + inline ADMIN | none | 204; 404 | nodes.py:148 |
| 5 | POST | /api/v1/nodes/{ae_title}/ping | U | none | `{ae_title, ok, ...}` from C-ECHO; 409 if disabled; 404 | nodes.py:162 |
| 6 | POST | /api/v1/nodes/{ae_title}/send | D | `SendScope`: study_uids, series_uids, instance_uids (≥1), our_ae_title | `{ae_title, ok, sent, failed, results}`; 404 if nothing attempted; 422 if no scope | nodes.py:185 |
| 7 | POST | /api/v1/nodes/{ae_title}/find | D | `FindQuery`: level, filters, our_ae_title | `{ae_title, ok, level, matches}`; 502 on C-FIND rejection | nodes.py:238 |
| 8 | POST | /api/v1/nodes/{ae_title}/pull | D | `FindQuery` | `{ae_title, ok, pulled, failed, results}`; 502 | nodes.py:276 |
| 9 | POST | /api/v1/nodes/{ae_title}/web/query | D | `FindQuery` | `{ae_title, ok, transport:"dicomweb", level, matches}` | nodes.py:346 |
| 10 | POST | /api/v1/nodes/{ae_title}/web/retrieve | D | `WebRetrieveQuery` (FindQuery + auto_archive); needs filters.StudyInstanceUID or PatientID | `{ae_title, ok, pulled, failed, results, transport, auto_archived, archive_state, archive_error}`; 404; 502 | nodes.py:380 |
| 11 | POST | /api/v1/nodes/{ae_title}/web/upload | D | `SendScope`; query: accelerated (bool), workers (1-16), progress (bool) | JSON `{ae_title, transport, sent, failed, results}`, or NDJSON stream when progress=1 (`progress`/`done`/`error` events); 404; 502 | nodes.py:477 (stream helper nodes.py:559) |

### 2.2 Routing (`routing.py`, prefix `/api/v1/routing`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 12 | POST | /api/v1/routing/rules | U | `CreateRoutingRuleRequest`: name*, description, enabled, priority=10, destination_node_id*, modality, body_part_examined, source_aet, study_description_pattern (regex, unvalidated), delay_seconds=0 | 201 rule dict; 400 on any error | routing.py:81 |
| 13 | GET | /api/v1/routing/rules | U | none | list of rules | routing.py:99 |
| 14 | GET | /api/v1/routing/rules/{rule_id} | U | none | rule; 404 | routing.py:109 |
| 15 | PUT | /api/v1/routing/rules/{rule_id} | U | `UpdateRoutingRuleRequest` (partial) | rule; 404 | routing.py:122 |
| 16 | DELETE | /api/v1/routing/rules/{rule_id} | U | none | `{message}`; 404 | routing.py:143 |
| 17 | POST | /api/v1/routing/evaluate/{study_uid} | U | none | `{message, matched_rules_count}`; enqueues only | routing.py:157 |
| 18 | GET | /api/v1/routing/queue | U | query status, limit=100 | list of queue items | routing.py:170 |
| 19 | POST | /api/v1/routing/queue/{queue_id}/retry | D | none | `{message, item}`; 400 if not FAILED | routing.py:182 |
| 20 | POST | /api/v1/routing/queue/run | D | none | `{message}`; starts a thread that runs `process_queue_batch` | routing.py:195 |

### 2.3 Replication (`replication.py`, prefix `/api/v1/replication`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 21 | GET | /api/v1/replication | A | none | status: enabled, target, prefix, pending, replicated, failed, total_studies, total_bytes | replication.py:28 |
| 22 | GET | /api/v1/replication/records | A | limit=30 (clamped to 1-200 in service) | `{records: [...]}` | replication.py:33 |
| 23 | POST | /api/v1/replication/sweep | A | none | sweep summary (synchronous; request waits for the full sweep) | replication.py:38 |
| 24 | POST | /api/v1/replication/studies/{study_uid} | A | none | replicate summary; 404 unknown study; 422 if ok=false; 400 on exception | replication.py:48 |

### 2.4 Storage (`storage.py`, prefix `/api/v1/storage`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 25 | GET | /api/v1/storage/backends | A | none | `{backends: [...], env_backend: {...}}` with instance counts and usage | storage.py:116 |
| 26 | POST | /api/v1/storage/backends | A | `BackendCreate`: name, kind local/s3, root_path, endpoint_url, region, bucket, access_key, secret_key, presign_expires=900, is_default | 201 backend (no secret returned) | storage.py:137 |
| 27 | PATCH | /api/v1/storage/backends/{name} | A | `BackendPatch` (partial; includes location fields) | backend (instances and usage always 0, N5) | storage.py:177 |
| 28 | POST | /api/v1/storage/backends/{name}/test | A | none | `{ok, kind, latency_ms, detail}` | storage.py:209 |
| 29 | DELETE | /api/v1/storage/backends/{name} | A | none | 204; 409 if instances reference it | storage.py:243 |
| 30 | POST | /api/v1/storage/studies/{study_uid}/move | A | `StudyMove {target_backend}` | `{study_instance_uid, target_backend, moved, from_backend, already}`; 404; 400 | storage.py:268 |

### 2.5 Quotas (`quota.py`, prefix `/api/v1/quotas`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 31 | GET | /api/v1/quotas | A | none | list of quota plus current usage | quota.py:94 |
| 32 | POST | /api/v1/quotas | A | `QuotaCreate`: exactly one of tenant_id / storage_backend_name; max_bytes ≥0; warning_threshold_pct 1-100; enabled | 201 quota plus usage | quota.py:109 |
| 33 | GET | /api/v1/quotas/{quota_id} | A | none | quota plus usage; 404 | quota.py:146 |
| 34 | PUT | /api/v1/quotas/{quota_id} | A | `QuotaUpdate` (partial) | quota plus usage | quota.py:156 |
| 35 | DELETE | /api/v1/quotas/{quota_id} | A | none | 204 | quota.py:173 |
| 36 | GET | /api/v1/quotas/status | A | query tenant_id, backend_name | list of status. **Unreachable**: matched by #33 first, returns 422 (E-21) | quota.py:184 |
| 37 | GET | /api/v1/quotas/status/tenant/{tenant_id} | A | none | list of status | quota.py:194 |
| 38 | GET | /api/v1/quotas/status/backend/{backend_name} | A | none | list of status | quota.py:202 |

### 2.6 Lifecycle (`lifecycle.py`, prefix `/api/v1/lifecycle`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 39 | POST | /api/v1/lifecycle/rules | U | `CreateLifecycleRuleRequest`: name*, description, enabled, priority=10, action ARCHIVE/RESTORE/PURGE, min_age_days=365 (no bounds), modality, body_part_examined | 201 rule; 422 invalid action | lifecycle.py:80 |
| 40 | GET | /api/v1/lifecycle/rules | U | none | list | lifecycle.py:96 |
| 41 | GET | /api/v1/lifecycle/rules/{rule_id} | U | none | rule; 404 | lifecycle.py:103 |
| 42 | PUT | /api/v1/lifecycle/rules/{rule_id} | U | `UpdateLifecycleRuleRequest` (partial) | rule; 422 | lifecycle.py:112 |
| 43 | DELETE | /api/v1/lifecycle/rules/{rule_id} | U | none | `{message}`; 404 | lifecycle.py:132 |
| 44 | POST | /api/v1/lifecycle/run | U | query rule_id (optional) | `{message, scanned, archived, restored, purged}`. Runs **all** enabled rules, including PURGE (E-01) | lifecycle.py:142 |
| 45 | POST | /api/v1/lifecycle/studies/{study_uid}/archive | U | none | `{study_uid, ok, moved, state}`; 404 | lifecycle.py:159 |
| 46 | POST | /api/v1/lifecycle/studies/{study_uid}/restore | U | none | same shape; 404 | lifecycle.py:170 |
| 47 | POST | /api/v1/lifecycle/studies/{study_uid}/purge | D | none | `{study_uid, ok, purged}`; 404 | lifecycle.py:181 |
| 48 | GET | /api/v1/lifecycle/studies | U | query state (default ARCHIVED); max 200 rows incl. patient_id and patient_name | list; not tenant-scoped (E-09) | lifecycle.py:192 |

### 2.7 Lossy compression (`lossy_compression.py`, prefix `/api/v1/compression/lossy`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 49 | POST | /api/v1/compression/lossy/rules | U | `CreateLossyRuleRequest`: name*, description, enabled, priority=10, min_age_days=365, modality, body_part_examined, transfer_syntax_uid (unvalidated; default JPEG Baseline 1.2.840.10008.1.2.4.50), quality_factor=85 (unvalidated) | 201 rule | lossy_compression.py:75 |
| 50 | GET | /api/v1/compression/lossy/rules | U | none | list | lossy_compression.py:91 |
| 51 | GET | /api/v1/compression/lossy/rules/{rule_id} | U | none | rule; 404 | lossy_compression.py:101 |
| 52 | PUT | /api/v1/compression/lossy/rules/{rule_id} | U | `UpdateLossyRuleRequest` | rule; 404 | lossy_compression.py:114 |
| 53 | DELETE | /api/v1/compression/lossy/rules/{rule_id} | U | none | `{message}` | lossy_compression.py:135 |
| 54 | POST | /api/v1/compression/lossy/run | U | query rule_id | `{message, processed_instances}`; runs synchronously in the request (E-03, E-24) | lossy_compression.py:149 |

### 2.8 Migration (`migration.py`, prefix `/api/v1/migration`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 55 | POST | /api/v1/migration/folder | U | `FolderMigrationRequest`: folder_path* (any server path), delete_after_import=false | 202 `{message, job}` | migration.py:56 |
| 56 | POST | /api/v1/migration/cmove | U | `CMoveMigrationRequest`: ae_title*, study_instance_uids* (unbounded list) | 202 `{message, job}`; 400 if empty | migration.py:81 |
| 57 | GET | /api/v1/migration/jobs | U | none | list (source_info contains server folder paths) | migration.py:110 |
| 58 | GET | /api/v1/migration/jobs/{job_id} | U | none | job; 404 | migration.py:121 |

### 2.9 Prefetch (`prefetch.py`, prefix `/api/v1/prefetch`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 59 | POST | /api/v1/prefetch/rules | U | `CreatePrefetchRuleRequest`: name*, description, enabled, priority=10, modality="ALL", body_part_examined, max_prior_age_days=730 (unused), max_priors_count=3, source_node_id* | 201 rule | prefetch.py:106 |
| 60 | GET | /api/v1/prefetch/rules | U | none | list | prefetch.py:123 |
| 61 | GET | /api/v1/prefetch/rules/{rule_id} | U | none | rule; 404 | prefetch.py:133 |
| 62 | PUT | /api/v1/prefetch/rules/{rule_id} | U | `UpdatePrefetchRuleRequest` | rule | prefetch.py:146 |
| 63 | DELETE | /api/v1/prefetch/rules/{rule_id} | U | none | `{message}` | prefetch.py:167 |
| 64 | POST | /api/v1/prefetch/trigger | U | `TriggerPrefetchRequest`: patient_id* (unvalidated), worklist_step_id, modality, body_part_examined | `{message, jobs: [...]}`; C-FIND and C-MOVE run synchronously in the request | prefetch.py:181 |
| 65 | GET | /api/v1/prefetch/jobs | U | query patient_id; fixed limit 100 | list | prefetch.py:204 |

### 2.10 Priors (`priors.py`, prefix `/api/v1/priors`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 66 | GET | /api/v1/priors/study/{study_uid} | U | none | ranked prior matches; not tenant-scoped (E-09) | priors.py:42 |
| 67 | GET | /api/v1/priors/patient/{patient_id} | U | none | prior matches; not tenant-scoped (E-09) | priors.py:53 |

### 2.11 Tenants (`tenants.py`, prefix `/api/v1/tenants`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 68 | GET | /api/v1/tenants | D | none | list with AE titles and study counts | tenants.py:93 |
| 69 | POST | /api/v1/tenants | A | `TenantCreate`: name, code, description | 201 tenant; 409 | tenants.py:100 |
| 70 | PATCH | /api/v1/tenants/{tenant_id} | A | `TenantUpdate`: name, description, is_active | tenant; `is_active` is not enforced anywhere (E-19) | tenants.py:128 |
| 71 | DELETE | /api/v1/tenants/{tenant_id} | A | none | 204; detaches studies and users to tenant NULL (E-20) | tenants.py:154 |
| 72 | GET | /api/v1/tenants/{tenant_id}/aes | D | none | `{tenant_id, ae_titles}` | tenants.py:174 |
| 73 | POST | /api/v1/tenants/{tenant_id}/aes | A | `AeMapping {ae_title}` (normalized) | 201 `{tenant_id, ae_title}`; 409 | tenants.py:188 |
| 74 | DELETE | /api/v1/tenants/{tenant_id}/aes/{ae_title} | A | none | 204; 404 | tenants.py:209 |
| 75 | POST | /api/v1/tenants/{tenant_id}/users | A | `UserAssignment {username}` | 200 `{username, tenant_id, tenant_name}`; 404 | tenants.py:228 |
| 76 | DELETE | /api/v1/tenants/{tenant_id}/users/{username} | A | none | 204; 404 | tenants.py:244 |
| 77 | GET | /api/v1/tenants/context | U | none | `{is_admin, tenant_id, tenant_name, scoped}` | tenants.py:259 |

### 2.12 Split and merge (`split_merge.py`, prefix `/api/v1/studies`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 78 | POST | /api/v1/studies/split | U | `SplitStudyRequest`: source_study_uid*, series_instance_uids* (list), target_patient_id, target_patient_name, new_study_description | 200 `{message, new_study_instance_uid, patient_id}`; 400; 500 | split_merge.py:41 |
| 79 | POST | /api/v1/studies/merge | U | `MergeStudyRequest`: source_study_uids*, target_study_uid* | 200 `{message, target_study_instance_uid}`; 400; 500 | split_merge.py:77 |

### 2.13 Scripts (`scripts.py`, prefix `/api/v1/scripts`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 80 | GET | /api/v1/scripts | A | none | `{scripts: [...], dir, enabled}`; loading executes each script's top-level code (E-22) | scripts.py:35 |
| 81 | POST | /api/v1/scripts/reload | A | none | `{dropped, scripts}` | scripts.py:42 |
| 82 | POST | /api/v1/scripts/{name}/toggle | A | none | `{name, enabled}`; 404 | scripts.py:48 |
| 83 | POST | /api/v1/scripts/{name}/run | A | `ScriptRun {event ≤64, payload dict}` | `{ok, name, event}`; 422 on failure; runs synchronously on the event loop | scripts.py:58 |
| 84 | GET | /api/v1/scripts/{name}/source | A | none | `{name, source}`; 404 | scripts.py:67 |
| 85 | PUT | /api/v1/scripts/{name} | A | raw text body (Python source), no size limit | 201 `{name, created}`; 409 if exists; 422 empty or bad name | scripts.py:75 |

### 2.14 Commitments (`commitments.py`, prefix `/api/v1/commitments`)

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 86 | GET | /api/v1/commitments | U | query status; fixed limit 100 | list of commitment transactions (instance counts, failures); not tenant-scoped (N11) | commitments.py:55 |

---

## 3. Service reference

### 3.1 node_service (`app/services/node_service.py`)
- Purpose: resolve an AE title to an enabled node for sync callers.
- Functions: `get_node_sync(ae_title: str) -> DicomNode | None` (node_service.py:16).
- DB models: `DicomNode` (read only).
- Background jobs: none. Known caller: `migration_service.py:171`. Other callers not checked.
- Redis keys: none.

### 3.2 routing_service (`app/services/routing_service.py`)
- Purpose: evaluate auto-routing rules and dispatch studies to nodes over C-STORE through a durable queue.
- Functions:
  - `evaluate_rules_for_study(session: AsyncSession, study_instance_uid: str, meta: dict | None = None) -> list[RoutingRule]` (28)
  - `dispatch_study_to_node_sync(study_instance_uid: str, node_id: int) -> dict` (102)
  - `enqueue_dispatch(session, study_instance_uid, rule) -> RoutingQueueItem | None` (175)
  - `evaluate_and_route_study(study_instance_uid: str, meta: dict | None = None) -> int` (218), async, opens its own session
  - `process_queue_batch(limit: int = 10) -> dict[str,int]` (239), sync
  - `retry_queue_item(queue_id: int) -> dict` (317); `list_queue(status=None, limit=100) -> list[dict]` (335); `recover_stuck_queue_items() -> None` (373)
  - CRUD: `create_rule`, `get_rule`, `list_rules`, `delete_rule` (390-425)
- DB models: `RoutingRule`, `RoutingQueueItem` (statuses PENDING, SENDING, COMPLETED, FAILED), `DicomNode`, `Study`, `Series`, `Instance` (read), `AuditLog` (via audit_service, action `AUTO_ROUTED`).
- Background jobs: `worker.py:111-115` `_sweep_routing` every `ROUTING_SWEEP_EVERY_TICKS` = 2 ticks (config.py:229) when `ROUTING_RUN_IN_WORKER` (config.py:226). `worker.py:359-361` and `:425-427` call `recover_stuck_queue_items` (worker startup path; the enclosing functions were not fully read). Manual thread: `routing.py:195-209`.
- Settings: `ROUTING_QUEUE_ENABLED`=True (225), `ROUTING_MAX_ATTEMPTS`=5 (227), `ROUTING_RETRY_BACKOFF_SECONDS`=60 (228; delay is 60 × attempts²).
- Redis keys: none.

### 3.3 replication_service (`app/services/replication_service.py`)
- Purpose: copy study objects to an off-site S3-compatible target. Write-only; never deletes on the target (module docstring 23-25).
- Functions: `target_display() -> str` (53); `target_backend()` (63; raises ValueError or KeyError); `enqueue_study(session, study_uid, target=None) -> ReplicationRecord` (82); `replicate_study(study_uid: str) -> dict` (101); `sweep() -> dict` (189); `status() -> dict` (242); `records(limit=30) -> list[dict]` (265).
- DB models: `ReplicationRecord` (unique study_instance_uid, models.py:1494), `Study`, `Series`, `Instance`.
- Background jobs: `worker.py:117-126`, every `DR_REPLICATION_SWEEP_EVERY_TICKS` = 4 ticks, only if `DR_REPLICATION_ENABLED` (default False, config.py:246).
- Settings: `DR_REPLICATION_TARGET` (247), `DR_REPLICATION_PREFIX` = "replica/" (248), `DR_REPLICATION_MAX_ATTEMPTS` = 5 (249).
- Redis keys: none.

### 3.4 quota_service (`app/services/quota_service.py`)
- Purpose: quota lookup, usage sums, status views. Intended as the pre-write gate.
- Functions: `QuotaInfo` dataclass (33-64); `get_effective_quotas(tenant_id=None, backend_name=None) -> list[StorageQuota]` (67); `get_tenant_usage_bytes(tenant_id, backend_name=None) -> int` (88); `get_backend_usage_bytes(backend_name) -> int` (102); `check_quota_before_write(tenant_id, backend_name, additional_bytes=0) -> (bool, list[QuotaInfo])` (112, **no callers**, E-08); `get_quota_status(...)` (169, **no callers**); `get_quota_status_async(db, tenant_id=None, backend_name=None) -> list[dict]` (199, used by quota.py).
- DB models: `StorageQuota`, `Instance`, `Series`, `Study`, `Tenant`, `StorageBackendConfig`.
- Background jobs: none (the docstring at line 15 mentions a worker evaluation that does not exist).
- Redis keys: none.

### 3.5 lifecycle_service (`app/services/lifecycle_service.py`)
- Purpose: hot/cold tiering by moving objects under `.archive/`; purge; rule evaluation.
- Constants: `ARCHIVE_PREFIX = ".archive/"` (38); `VALID_ACTIONS = ("ARCHIVE","RESTORE","PURGE")` (40).
- Functions: `create_rule(session, data) async` (46); `create_rule_sync` (56, no callers); `get_rule` (67); `list_rules` (73); `delete_rule` (85); `_study_age_days(study) -> int` (97); `_tier_study(study, to_archive, session, performed_by) -> int` (108); `_purge_study(study, session, performed_by) -> int` (148); `_rule_matches(rule, study) -> bool` (180); `archive_study(study_uid, performed_by) -> dict` (193); `restore_study` (206); `purge_study` (217); `evaluate_and_run(rule_id=None, performed_by="lifecycle-worker") -> dict` (229).
- DB models: `StudyLifecycleRule`, `Study` (lifecycle_state, archived_at), `Series`, `Instance` (file_path, storage_backend), `AuditLog`.
- Background jobs: `worker.py:104-109` and `:225-238`, every `LIFECYCLE_SWEEP_EVERY_TICKS` = 12 ticks when `LIFECYCLE_RUN_IN_WORKER` (config.py:214-215). **The call passes the wrong argument (E-12).**
- Other triggers: `nodes.py:463` (archive on WADO pull when `auto_archive` or `LIFECYCLE_AUTO_ARCHIVE_WADO_PULL`, config.py:220); `lifecycle.py` endpoints.
- Redis keys: none.

### 3.6 lossy_compression_service (`app/services/lossy_compression_service.py`)
- Purpose: transcode stored instances to JPEG and overwrite the object in place.
- Functions: `transcode_dataset_lossy(ds, target_ts_uid=JPEG_BASELINE_LOSSY, quality_factor=85) -> (Dataset, int)` (31); `transcode_instance_lossy(session, instance, target_ts_uid, quality_factor) -> bool` (69, async; sync I/O inside); `evaluate_and_run_lossy_compression(session, rule_id=None) -> dict` (119, async); CRUD helpers (183-220).
- DB models: `LossyCompressionRule`, `Instance` (transfer_syntax_uid, file_size_bytes, photometric_interpretation, bits_allocated), `Series`, `Study`, `AuditLog`.
- Background jobs: none. Only the API calls it (`lossy_compression.py:155`).
- Redis keys: none.

### 3.7 migration_service (`app/services/migration_service.py`)
- Purpose: bulk import from a local folder (optionally deleting originals) and batch C-MOVE from a remote node.
- Functions: `scan_and_import_folder_sync(folder_path, delete_after_import=False) -> dict` (33, sync; runs in executor); `create_migration_job(session, job_type, source_info) -> MigrationJob` (98); `run_folder_migration(job_id, folder_path, delete_after=False)` (121, async); `run_cmove_migration(job_id, ae_title, study_uids)` (167, async).
- DB models: `MigrationJob`; writes through `indexing_service.index_new_instance` (`Patient`, `Study`, `Series`, `Instance`, `AuditLog`).
- Background jobs: FastAPI `BackgroundTasks` (migration.py:67-72, 96-101).
- Redis keys: none.

### 3.8 prefetch_service (`app/services/prefetch_service.py`)
- Purpose: on demand, C-FIND a remote archive for a patient's priors and C-MOVE the missing ones.
- Functions: `query_remote_priors_sync(host, port, ae_title, patient_id, modality=None, our_ae_title="PAXS_SCU") -> list[dict]` (27); `evaluate_and_trigger_prefetch(session, patient_id, worklist_step_id=None, modality=None, body_part=None) -> list[PrefetchJob]` (71, async); `create_rule`, `get_rule`, `list_rules`, `delete_rule` (180-215); `list_jobs(session, patient_id=None, limit=100)` (218).
- DB models: `PrefetchRule`, `PrefetchJob`, `Study` and `Patient` (existing UID lookup), `AuditLog`.
- Background jobs: none. Only `prefetch.py:187` calls the evaluator.
- Redis keys: none.

### 3.9 prior_service (`app/services/prior_service.py`)
- Purpose: rank prior studies for the same PatientID (score 50 base, +25 modality match, +25 body part match).
- Functions: `find_prior_studies(session, study_instance_uid, patient_id=None) -> list[PriorStudyMatch]` (37).
- DB models: `Study`, `Series`, `Patient` (read). No tenant filter.
- Background jobs: none. Redis keys: none.

### 3.10 tenant_service (`app/services/tenant_service.py`)
- Purpose: VNA multi-tenancy helpers: AE-title → tenant resolution and user scope.
- Process-local cache: `_AE_CACHE` (31) guarded by `_AE_CACHE_LOCK`, with no TTL (E-19).
- Functions: `invalidate_ae_cache()` (39); `resolve_tenant_id_for_ae(session, ae_title) -> int | None` (49, async); `resolve_tenant_id_for_ae_sync(session, ae_title)` (67, used by `receiver.py:226, 497, 546`); `scope_from_user(user) -> (tenant_id, include_all)` (83); `get_tenant_names(session) -> dict[int,str]` (98).
- DB models: `Tenant`, `TenantAeTitle`, `User` (read).
- Background jobs: none. Redis keys: none (the cache is in-process only).

### 3.11 split_merge_service (`app/services/split_merge_service.py`)
- Purpose: move series between studies and rewrite DICOM StudyInstanceUID and PatientID.
- Functions: `split_series_to_new_study(session, source_study_uid, series_uids, target_patient_id=None, target_patient_name=None, new_study_description=None, performed_by="technologist") -> Study` (26); `merge_studies(session, source_study_uids, target_study_uid, performed_by="technologist") -> Study` (150); `_rewrite_instance_study_uid(file_key, new_study_uid, new_patient_id, new_patient_name=None, backend=None) -> None` (239).
- DB models: `Study`, `Series`, `Instance` (file_path, storage_backend), `Patient`, `AuditLog` (SPLIT / MERGED per instance).
- Background jobs: none. Redis keys: none.

### 3.12 scripting_service (`app/services/scripting_service.py`)
- Purpose: load admin-supplied `.py` files from `SCRIPTS_DIR` and run them on archive events.
- Functions: `scripts_dir() -> Path` (60); `_script_files()` (64); `is_enabled(path)` (77); `set_enabled(name, enabled) -> bool` (81); `_snake_case` (95); `_load(name, path)` (107, executes the module); `list_scripts() -> list[dict]` (133); `reload_scripts() -> int` (164); `class ScriptContext` (174) with `study(uid)`, `instances(uid)`, `read_bytes(key)`, `http_post(url, payload, timeout=8.0)`, `fire(event, **data)`; `_dispatch_one(module, event, ctx)` (279); `_run(event, payload)` (293); `fire(event, **data)` (308, starts a daemon thread); `run_script_now(name, event, payload) -> dict` (325).
- DB models: read-only (`Study`, `Patient`, `Series`, `Instance`).
- Background jobs: one daemon thread per `fire()` call (319-322). Callers: `indexing_service.py:136` (StudyReceived), `dashboard.py:864` (StudyDeleted), `critical_results_service.py:82`.
- Settings: `SCRIPTS_DIR` = "scripts" (config.py:239), `SCRIPTS_ENABLED` = True (240).
- Redis keys: none.

### 3.13 anonymizer (`app/services/anonymizer.py`)
- Purpose: PS3.15-style de-identification and ZIP assembly for research export.
- Constants: `PHI_KEYWORDS` (34-59), `PHI_SEQUENCES` (62-69), `DATE_KEYWORDS` (71-76), `TIME_KEYWORDS` (78-82); `_known(keywords)` (87) silently filters keywords missing from the installed pydicom.
- Functions: `anonymize_dataset(ds: Dataset, uid_map: dict[str,str]) -> Dataset` (99); `anonymize_files_to_zip(file_paths, zip_name=None, backends=None) -> bytes` (162).
- DB models: none. Storage reads via `storage_service.read_dataset`.
- Settings: `ANONYMIZER_STRIP_PRIVATE_TAGS` = True (config.py:171), `ANONYMIZER_STRIP_DATES` = True (172).
- Caller: `export.py:23-76` (`GET /api/v1/export/{study_uid}/anonymized`, any signed-in user; out of scope but it is the only entry point).
- Background jobs: none. Redis keys: none.

### 3.14 commitments (no service module)
- `commitments.py:54-63` reads `StorageCommitRequest` directly; `_req_out` (28) parses `payload_json`. Writes come from the DICOM N-ACTION SCP per the module docstring (out of scope).
- Redis keys: none.

---

## 4. Workflows

### (a) Add a DICOM node and run a C-ECHO test
1. Admin POSTs to `/api/v1/nodes` (nodes.py:96). Inline role check, 403 unless ADMIN (nodes.py:98-99).
2. Duplicate AE check, 409 (nodes.py:100-104).
3. If `web_url` is given: `validate_web_url` (nodes.py:105-111 → dicomweb_client.py:44-64). It enforces http(s) scheme, no embedded credentials, and resolves the host and rejects loopback, private, link-local, reserved, and metadata ranges (dicomweb_client.py:67-83), unless `DICOMWEB_CLIENT_ALLOW_PRIVATE` (config.py:260).
4. `DicomNode(**body.model_dump())` (nodes.py:112), flush and refresh for server defaults (nodes.py:113-115). The session commits on success (`db/session.py:223-231`).
5. Response is `_node_out` (nodes.py:118, 74-85), which omits `web_password` and returns `has_web_auth`.
6. Ping: POST `/api/v1/nodes/{ae}/ping` (nodes.py:162). Requires only U (nodes.py:163). Node lookup (166-170); 409 if disabled (171-172).
7. `send_echo(node.host, node.port, node.ae_title)` (nodes.py:177; `dicom/sender.py:547`).
8. Result merged with `ae_title` (nodes.py:181). An exception returns `ok: false` with `reason` (178-180). No tenant check: any signed-in user can probe any registered node.

### (b) Routing rule matches an incoming study and forwards it (queue, retries)
1. C-STORE received; `receiver.py:226` resolves tenant; ingest coroutine `_index_async` (receiver.py:60-72) opens a session and `begin()`.
2. `index_new_instance` (indexing_service.py:225-260) → `get_or_create_study` (94-159). For a new study: `session.add` and `flush` (114-115), worklist match (119-121), webhook and script fire (125-143), then routing (145-154, call at line 148).
3. `evaluate_and_route_study` opens a **new** session (routing_service.py:224-226) and calls `evaluate_rules_for_study` (28-99): active rules by priority (35-41), then the study lookup (46-55).
4. **The study row is uncommitted** (it was only flushed inside the outer transaction, which commits later at receiver.py:60-72). The lookup returns None and the function returns `[]` (routing_service.py:54-55) with no log line. Auto-routing therefore never matches a brand-new study (E-07).
5. Intended matching, if the study were visible: node enabled (routing_service.py:72-73); modality (76-78); body part (80-84); source AET (87-89, skipped when `meta` is None); study-description regex (92-95).
6. Enqueue (`enqueue_dispatch`, 175-215): `ROUTING_QUEUE_ENABLED` (186); idempotent on PENDING or SENDING (188-198); `next_retry_at = now + delay_seconds` (202); insert with `max_attempts` (203-212).
7. Worker: `worker.py:111-115` every 2 ticks → `process_queue_batch` (239). Selects due PENDING items, LIMIT 10 (253-264).
8. Per item, claim (270-276): read, check status is PENDING, set SENDING, `attempts += 1`, commit. This is a check-then-update without a row lock (E-16).
9. `dispatch_study_to_node_sync` (102-172): node enabled and study exists; for each instance, `storage_service.exists` (133), local path or a temp copy (137-146, temp file never removed), `send_file` (148), `AUTO_ROUTED` audit (151-157), commit (161).
10. Success: COMPLETED with sent and failed counts (288-294). Failure: `last_error` set (299); FAILED at `max_attempts` (300-305); otherwise PENDING with `next_retry_at = now + 60 × attempts²` (307-310).
11. Operator retry: POST `/api/v1/routing/queue/{id}/retry` (routing.py:182-191) → `retry_queue_item` (317-332) resets attempts to 0 and status to PENDING.
12. Manual path: POST `/api/v1/routing/evaluate/{uid}` (routing.py:157-166) works because the study is committed. It passes no `meta`, so the source AET filter is skipped (routing_service.py:59, 87-89). `POST /api/v1/routing/queue/run` (routing.py:195-209) starts a second sweep in a thread that can race the worker (E-16).

### (c) Replication to a peer (DR)
1. Config: `DR_REPLICATION_ENABLED` (config.py:246, default False); `DR_REPLICATION_TARGET` names a registered backend (247), else the env S3 config (replication_service.py:63-71).
2. Worker: `worker.py:117-126` every 4 ticks → `replication_service.sweep()`.
3. `sweep()` (189-239): enabled gate (193-194); validate target (196-200).
4. Enqueue step (202-215): records for up to 500 newest studies that have none. `.order_by(Study.id.desc()).limit(_STUDY_ENQUEUE_BATCH)` (207-210; E-15).
5. Due step (217-223): PENDING or FAILED with `attempts < DR_REPLICATION_MAX_ATTEMPTS` (5). No limit (E-25).
6. Per study: `replicate_study` (101-183). Target backend (107-108). Record upsert: attempts +1, `first_attempted_at` (111-123).
7. Instances (125-130). Per instance: skip if `file_path` is None (143-145); read from the source backend (147-149); write to `{prefix}{study}/{sop}.dcm` (146, 150). Thumbnail copy is best-effort (156-165).
8. Exception: FAILED with `last_error` (166-173). Success: REPLICATED with counts and timestamp (175-183).
9. API: POST `/api/v1/replication/sweep` (replication.py:37-44) and POST `/api/v1/replication/studies/{uid}` (47-62). Both commit the request transaction first, then run in a thread. Admin only.
10. No delete call exists in `replication_service.py` (verified by reading the whole file). Gaps are listed in E-15.

### (d) Lifecycle policy moving studies between tiers, and deletion
1. Rule create: POST `/api/v1/lifecycle/rules` (lifecycle.py:80-92) → `lifecycle_service.create_rule` (46-53) validates only the action (lifecycle.py:47 leaves min_age_days unbounded).
2. Trigger A, worker: `worker.py:104-109` → `_sweep_lifecycle` (225-238) → `evaluate_and_run("lifecycle-worker")` (worker.py:234). The string binds to `rule_id` (lifecycle_service.py:229), so the query is `id = 'lifecycle-worker'` (236) and matches no rule (repro-verified). The scheduled sweep is a no-op (E-12).
3. Trigger B, API: POST `/api/v1/lifecycle/run` (lifecycle.py:142-155) → `asyncio.to_thread(evaluate_and_run, rule_id, "api:<user>")`. Runs all enabled rules unless `rule_id` is given.
4. `evaluate_and_run` (229-264): enabled rules ordered by priority (234-241). **All** studies are loaded (245). For each study and each rule: `_rule_matches` (180-187): age from `study_date` or `created_at` (97-105); modality (183); body part (185).
5. Matched action per rule (251-260):
   - ARCHIVE when not already ARCHIVED → `_tier_study(study, True)` (108-145): skip objects already under `.archive/` (117-119); `dest = ".archive/" + key` (120); `move_object` (125); on success set `inst.file_path = dest` (126); if `moved`, set `lifecycle_state = "ARCHIVED"` and `archived_at` (128-134); audit ARCHIVED (135-141).
   - RESTORE when ARCHIVED → inverse move, state ONLINE.
   - PURGE when not ARCHIVED → `_purge_study` (148-169): delete each object and thumbnail (151-160), audit PURGED (161-167), then `session.delete(study)` (259), which cascades to series and instances (models.py:367, 418).
6. Single `session.commit()` at the end (263). Object moves and deletes are external side effects with no compensation (E-13, E-14). PURGE does not check replication (E-14).
7. Per-study endpoints: archive (lifecycle.py:159 → `archive_study` 193), restore (170 → 206), purge (181 → 217; requires D). Auto-archive after WADO pull: `nodes.py:461-465` (wrong target for PatientID pulls, E-28).

### (e) Quota enforcement
1. Create: POST `/api/v1/quotas` (quota.py:109-142). Exactly one scope (116-117). Tenant or backend must exist (119-128). Commit (139-140).
2. Intended gate: `storage_service.save_dataset_with_quota` (storage_service.py:301-340) or `save_bytes_with_quota` (256-271) → `check_quota_before_write` (quota_service.py:112-166) → `get_effective_quotas` (67-85) and usage sums (88-109). Denial raises `StorageError`.
3. **Actual:** no call site exists anywhere in the repo for either `*_with_quota` helper or for `check_quota_before_write` (repo-wide grep). Every ingest path uses the unchecked `save_dataset`: `receiver.py:86`, `dicomweb.py:876`, `dicomweb_client.py:335`, `sender.py:383`, `migration_service.py:60`, `kin_service.py:202`. The docstring at quota_service.py:13 and storage_service.py:279 are contradictory (E-08).
4. Status views: `quota_service.get_quota_status_async` (199-250) via `quota.py:189-206`. `GET /api/v1/quotas/status` is shadowed by `/{quota_id}` (E-21).
5. Two-scope filter bug (latent, N7): `get_effective_quotas` ANDs `tenant_id` and `backend_name` filters (75-79), so passing both drops backend-only and tenant-only quotas.

### (f) Migration job from a legacy PACS
- Folder path:
  1. POST `/api/v1/migration/folder` (migration.py:56) → `create_migration_job` (migration_service.py:98-118) → 202 with the job.
  2. Background `run_folder_migration` (121-164): status RUNNING (130-131); executor runs `scan_and_import_folder_sync` (136-138).
  3. Scan (33-95): path must exist and be a directory (41-43); `rglob("*")` builds the full list (50); each file: `dcmread` with `force=False` (55); `save_dataset` into the env backend with no tenant (60); `index_new_instance` run through a fresh `asyncio.run` per file (67-74); unlink if `delete_after_import` (77-81); exceptions counted, first 50 kept (83-94).
  4. Job COMPLETED with totals and errors (145-153) or FAILED (154-164).
- C-MOVE:
  1. POST `/api/v1/migration/cmove` (migration.py:81); empty list 400 (88-89).
  2. `run_cmove_migration` (167): node via `get_node_sync` (171); not found or disabled → FAILED (172-182); RUNNING with total (184-192).
  3. Per study: `send_move(host, port, ae_title, settings.DICOM_AE_TITLE, query_ds)` in executor (208-216). The move destination is `DICOM_AE_TITLE` = "PAXS_SCP" (config.py:28). The remote peer must already have that AE registered (open question Q6).
  4. Progress written per study (228-235). Final status COMPLETED, COMPLETED_WITH_ERRORS, or FAILED (243-247).
- Status: GET `/api/v1/migration/jobs` and `/jobs/{id}` (migration.py:110-132). Any signed-in user can read every job, including folder paths.

### (g) Prefetch of priors when an order arrives
1. **No automatic trigger exists.** `evaluate_and_trigger_prefetch` is called only from `prefetch.py:187`. Grep finds no call from worklist, MPPS, or ADT code. The "order arrives" trigger in the brief is not wired.
2. Manual: POST `/api/v1/prefetch/trigger` (prefetch.py:181-200).
3. Rules by priority (prefetch_service.py:80-86). Existing local UIDs for the patient (90-96; exceptions swallowed).
4. Per rule: source node enabled (103-104); modality filter (106-108, "ALL" passes); body part filter (110-112).
5. C-FIND (115-124, `query_remote_priors_sync` 27-68 → `sender.send_find` 47-51, Patient Root). The PatientID is passed unvalidated (40) and returned StudyInstanceUIDs are not checked against it (57-67; E-10).
6. Needed = remote priors not already local (127), capped at `max_priors_count` (128). `PrefetchJob` IN_PROGRESS (130-142).
7. Per prior: `sender.c_move_study` (151), which does not exist. The AttributeError is raised inside the `try` (148-161), logged as a warning, and the prior is skipped (E-11).
8. Job status COMPLETED if any retrieved or none needed, otherwise FAILED (163-164). Audit `PREFETCH_JOB_COMPLETED` (167-172) is written regardless.

### (h) Split and merge of studies
- Split (POST `/api/v1/studies/split`, split_merge.py:41 → `split_series_to_new_study` 26-147):
  1. Source study lookup by UID only; no owner or tenant check (41-49).
  2. Target patient get-or-create (52-61).
  3. New StudyInstanceUID from `generate_uid()` (64); new `Study` row with accession `SPLIT-<uid8>` (67-78); flush (78).
  4. Series to move selected by UID; unknown UIDs are silently ignored (81-88); 400 if none (87-88).
  5. Per series: re-parent to the new study (92). Per instance:
     a. `_rewrite_instance_study_uid` (98-105): read old object, set StudyInstanceUID, PatientID, PatientName, then `save_dataset`, which writes to the NEW key (storage_service.py:293). Exceptions are swallowed (256-257).
     b. `new_key = instance_key(new_study_uid, ...)` (106-108).
     c. `if inst.file_path != new_key:` read the OLD object (110-112), `save_bytes(new_key, old_bytes)` (113-115) **overwriting the rewritten object**, `delete_file(old)` (116), `inst.file_path = new_key` (117). The header rewrite is lost (E-05).
     d. Audit SPLIT (119-125).
  6. Flush (127). If the source study is now empty, delete it (130-137).
  7. API commits (split_merge.py:60).
- Merge (POST `/api/v1/studies/merge`, split_merge.py:77 → `merge_studies` 150-236):
  1. Target lookup (160-168). Source studies selected by UID with no tenant filter (172-179).
  2. Per source study and series: re-parent to target (190-192). Per instance: same rewrite and copy/delete sequence as split (197-216), including PatientID and PatientName set to the target's patient. Audit MERGED (218-224).
  3. Source Study row deleted (226-227). Replica records keyed by the old UID are left dangling (E-15).
  4. API commits (split_merge.py:93).

### (i) User scripts execution
1. Discovery: `scripts_dir()` (scripting_service.py:60-61) = `SCRIPTS_DIR` default "scripts" (config.py:239). `_script_files` (64-74): sorted `*.py` not starting with `_`. Disabled if `<name>.py.disabled` exists (77-78).
2. Load: `_load` (107-130): `importlib.util.spec_from_file_location` (117) and `spec.loader.exec_module(module)` (123). This runs the file's top-level code. Cache keyed by mtime (104, 115-116, 128). Loading happens during `list_scripts` (138) and on every dispatch.
3. Dispatch: `fire(event, **data)` (308-322): returns if `SCRIPTS_ENABLED` is off (310) or no files exist (312); otherwise starts a daemon thread (319-322). `_run` (293-305) iterates scripts, loads each, and calls `_dispatch_one` (279-290), which filters on `EVENTS`, then calls `handle(event, ctx)` and `on_<event>(ctx)`. Exceptions are caught per script (304-305).
4. Callers: `indexing_service.py:136-143` (StudyReceived, before the commit), `dashboard.py:864` (StudyDeleted), `critical_results_service.py:82`.
5. Manual test: POST `/api/v1/scripts/{name}/run` (scripts.py:57-63) → `run_script_now` (325-345), called synchronously from an async handler.
6. Upload: PUT `/api/v1/scripts/{name}` (scripts.py:74-89). The name is validated (78-79), existence checked (80-82), body read without a limit (83), written verbatim (86-87), reloaded (88). No syntax check, hash, or review.
7. Enable or disable: POST `/api/v1/scripts/{name}/toggle` (scripts.py:47-54) creates or removes the marker file (scripting_service.py:81-92).

### (j) Tenant (VNA) isolation
1. Model: `Tenant` (models.py:52-82, name and code unique), `TenantAeTitle` (84-101, ae_title unique, FK CASCADE), `User.tenant_id` (128-129, FK SET NULL), `Study.tenant_id` (326-327, FK SET NULL). `Patient.patient_id` is globally unique (models.py:198), so one MRN is one Patient row across tenants.
2. AE mapping: resolved from the calling AE title at ingest (receiver.py:63-64 async, 226/497/546 sync; `tenant_service.py:49-80`). Tenant applied on first study creation only (indexing_service.py:94-115 per its docstring).
3. User scope: `scope_from_user` (tenant_service.py:83-95): ADMIN → (None, all); user with tenant → (tenant, False); user without tenant → (None, False), meaning the legacy unscoped set (`Study.tenant_id IS NULL`).
4. Enforced in: `resolve_keys_sync` (`dicom/sender.py:709-713`), used by nodes send and web upload (nodes.py:210, 507-511). Pull ingest tagging (nodes.py:299, 303; sender.py:403). WADO retrieve tagging (nodes.py:408).
5. **Not enforced in:** lifecycle (all 10 routes; `lifecycle_service.py` contains no tenant reference), routing (routing.py all; `routing_service.py:46-55` and `dispatch_study_to_node_sync` 102-115 select by UID), prefetch, priors, lossy, migration (ingests with tenant None, migration_service.py:60), split/merge (split_merge_service.py:41-49, 172-179), commitments, and the export route in `export.py:33-35` (out of scope). See E-09.
6. Tenant deletion (tenants.py:153-170): AE mappings deleted (160-162); studies released to NULL (163-165); users to NULL (166-168); AE cache invalidated before the commit (170).
7. Mapping changes (tenants.py:204 map, 224 unmap) invalidate only the local process cache (E-19).
8. `Tenant.is_active` is stored and displayed (tenants.py:85, 147; dashboard.py:309) and enforced nowhere in scope resolution (E-19).

### (k) Anonymization
1. GET `/api/v1/export/{study_uid}/anonymized` (export.py:23-76, outside scope). Any signed-in user. Loads all instances for the study (33-49). File paths and backends come from the DB (51-52).
2. `anonymize_files_to_zip` (anonymizer.py:162-193): one `uid_map` for the whole ZIP (172), so UIDs stay consistent (`setdefault` at 134, 144). Per file: `read_dataset` (181), `anonymize_dataset` (182), member `"{Study}/{Series}/{SOP}.dcm"` (183-186), `save_as` (188), `writestr` (189). The whole ZIP is built in memory (179-190).
3. `anonymize_dataset` (99-159): deep copy (104); delete PHI keywords and PHI sequences at top level only (106-112); private groups removed when `ANONYMIZER_STRIP_PRIVATE_TAGS` (115-118, default True); dates and times removed when `ANONYMIZER_STRIP_DATES` (121-128, default True); remap StudyInstanceUID, SeriesInstanceUID, SOPInstanceUID, FrameOfReferenceUID (131-139); remap MediaStorageSOPInstanceUID (140-146); keep MediaStorageSOPClassUID (147-149); drop group-length elements (155-157).
4. The export route writes an EXPORTED audit row (export.py:65-69). Pixel data is not scrubbed (documented, anonymizer.py:10-12).

---

## 5. Code review findings

Ordered by severity, then by impact. Each finding is verified by code reading, except where marked as unverified.

### 🔴 E-01: Any signed-in user can create, edit, and run PURGE rules, deleting the archive
- Location: `lifecycle.py:80-82` (create, U), `:112-114` (update, U), `:132-134` (delete, U), `:142-148` (run, U); `lifecycle_service.py:234-236, 245, 257-259`; `lifecycle.py:47` (no bounds on min_age_days).
- Quote: `user: User = Depends(get_current_user)):` on create_rule; `lifecycle_service.evaluate_and_run, rule_id, f"api:{user.username}"`; `elif rule.action == "PURGE" and study.lifecycle_state != "ARCHIVED":`.
- What is wrong: Only the per-study purge route requires D (`lifecycle.py:182`). The rule engine that drives PURGE can be written and run by any signed-in account. `min_age_days` accepts 0 or negative values. The module docstring (lifecycle.py:14-16) says rule writes are gated; they are not. `_purge_study` deletes objects and the Study row (lifecycle_service.py:148-169, 259) with no replica check, no dry-run, and no confirmation.
- Failure scenario: A PHYSICIAN account (the `User.role` default, models.py:111) sends `{"name":"x","action":"PURGE","min_age_days":0}` to `POST /api/v1/lifecycle/rules`, then `POST /api/v1/lifecycle/run`. Every ONLINE study in every tenant is deleted. This is not reversible.
- Fix: Require D (or ADMIN) for all lifecycle writes and for run. Allow PURGE only for ADMIN. Enforce a minimum `min_age_days` floor. Require DR replica confirmation before purge. Add a dry-run that returns candidate counts.

### 🔴 E-02: Any signed-in user can route any study to any registered node
- Location: `routing.py:80-92` (create, U), `:156-166` (evaluate, U), `:163`; `routing_service.py:218-233`; `routing_service.py:112-114` (dispatch selects by UID only).
- Quote: `count = await routing_service.evaluate_and_route_study(study_uid)`; `select(Study).where(Study.study_instance_uid == study_instance_uid)`.
- What is wrong: No role, tenant, or destination check on rule creation or manual evaluation. A rule with no filters forwards every future ingested study, across all tenants, to its destination (`indexing_service.py:148` → `routing_service.py:218`). Manual evaluate enqueues any UID.
- Failure scenario: A PHYSICIAN creates a rule with `destination_node_id` set to an external AE and no filters. Every new study is forwarded to that node from then on. Or `POST /api/v1/routing/evaluate/<another tenant's study UID>` forwards that study.
- Fix: Rule writes ADMIN-only. Evaluate requires D and a tenant check on the study. Restrict destinations to admin-approved nodes. Require confirmation for unfiltered rules.

### 🔴 E-03: Lossy engine irreversibly rewrites diagnostic images as 8-bit windowed JPEG and keeps rescale tags
- Location: `lossy_compression_service.py:43-44` (`dataset_to_image`), `:49` (JPEG save), `:54` (photometric), `:63-64` (TS label), `:80-91` (read, then `save_dataset` at `:87`), `:119-177`. `imaging.py:50-75` (grayscale branch returns 8-bit "L"). `lossy_compression.py:148-155` (U, sync).
- Quote: `img.save(buf, format="JPEG", quality=quality_factor)`; `storage_service.save_dataset(new_ds, backend_name=bk)`.
- What is wrong: (1) For grayscale CT and MR, `dataset_to_image` applies the modality LUT and a VOI window, then normalizes to uint8 (`imaging.py:50-75`). The transcoder stores that rendering and discards the original 16-bit pixel values. (2) `RescaleSlope`, `RescaleIntercept`, `WindowCenter`, and `WindowWidth` are not modified (lossy_compression_service.py:43-66). Consumers that apply the rescale to the new 0-255 values read wrong quantitative values (for CT, HU). (3) `save_dataset` derives the key from the unchanged UIDs (storage_service.py:293), so the original object is overwritten with no backup. (4) No `LossyImageCompression` (0028,2110) attributes are set (no occurrence in the Python source). (5) The run is open to any signed-in user (`lossy_compression.py:152`) and executes synchronously in the event loop (`:155`).
- Failure scenario: A CT with RescaleIntercept -1024 is transcoded. Afterwards the pixel values 0-255 are read as HU -1024 to -769. Recovery is possible only from a DR replica taken earlier, and only if one exists.
- Fix: Never overwrite. Write a new object and Instance row, and keep the original until verified. Keep 16-bit for CT and MR, or refuse lossy for those modalities. Set `LossyImageCompression`, `LossyImageCompressionRatio`, and `LossyImageCompressionMethod`. Restrict to ADMIN. Run in the worker via `to_thread`.

### 🔴 E-04: Transfer-syntax label is user-controlled and not matched to the encoder
- Location: `lossy_compression.py:39-42` (`transfer_syntax_uid: str`, no allowlist; `quality_factor: int`, no range); `lossy_compression_service.py:49` (always JPEG); `:54` (photometric only special-cased for JPEG Baseline); `:63-64` (TS set to the rule value).
- Quote: `ds.file_meta.TransferSyntaxUID = target_ts_uid`.
- What is wrong: The bytes are always JPEG Baseline. The file meta is set to whatever UID the rule specifies, including JPEG-LS (1.2.840.10008.1.2.4.81, defined at service line 28), JPEG 2000, or uncompressed transfer syntaxes with encapsulated pixel data.
- Failure scenario: A rule with `transfer_syntax_uid` = 1.2.840.10008.1.2.4.81 stores JPEG bytes labeled JPEG-LS. The DB records JPEG-LS. Forwarding and viewers fail to decode.
- Fix: Allowlist `1.2.840.10008.1.2.4.50`, or implement a real encoder per syntax. Validate `quality_factor` to 1-100 on create and update.

### 🔴 E-05: Split and merge silently lose the DICOM header rewrite; merge relabels other patients' images with no check
- Location: `split_merge_service.py:98-117` (split), `:197-216` (merge), `:239-257` (rewrite helper; swallows at `:256-257`), `:172-179` (merge sources selected without tenant); `storage_service.py:274-298` (`save_dataset` key from UIDs at `:293`); `split_merge.py:40-105` (U).
- Quote (split_merge_service.py:99-116): `_rewrite_instance_study_uid(inst.file_path, ...)` / `new_key = storage_service.instance_key(...)` / `if inst.file_path != new_key:` / `old_bytes = storage_service.read_bytes(inst.file_path, ...)` / `storage_service.save_bytes(new_key, old_bytes, ...)` / `storage_service.delete_file(inst.file_path, ...)`.
- What is wrong: `_rewrite_instance_study_uid` writes the rewritten dataset to the NEW key, because `save_dataset` derives the key from the new UIDs. The caller then reads the OLD object, which was never modified, writes its bytes over the new key, and deletes the old object. The rewritten StudyInstanceUID and PatientID are lost, while the DB already points the instance at the new study. The helper also swallows every exception (`:256-257`), so the failure is invisible. In merge, any signed-in user can name any study UID as a source (`:172-179`, no tenant filter). Every instance's PatientID and PatientName are then set to the target's patient (`:197-204`), and the source Study row is deleted (`:226-227`).
- Failure scenario A (split): After a split, the DB shows the new study. The file still carries the old StudyInstanceUID and PatientID. Forwarding (routing, STOW, C-MOVE) sends the instance under the old study, so the DICOM header and the DB disagree.
- Failure scenario B (merge): A signed-in user merges another patient's study into their own target. That patient's images now carry the target's PatientID in the DB, and the header rewrite is reverted in the file. The audit row records only MERGED per SOP, with no before-values.
- Fix: Write the rewritten dataset once, to the final key, then delete the old object. Verify StudyInstanceUID and PatientID after the write. Do not swallow exceptions. Restrict split and merge to ADMIN or RADIOLOGIST. Require the same PatientID or an explicit override. Apply tenant scope to both source and target. Record before and after values in the audit.

### 🔴 E-06: Folder migration lets any signed-in user import from, and delete DICOM files at, arbitrary server paths
- Location: `migration.py:30-32` (`folder_path` unrestricted; `delete_after_import` default False), `:55-77` (U); `migration_service.py:41-43` (exists and is_dir only), `:50` (rglob), `:55` (dcmread), `:60` (save_dataset with no tenant), `:77-81` (unlink).
- Quote: `if delete_after_import:` ... `file_p.unlink()`.
- What is wrong: No allowlist of import roots, no role check, no quarantine. Deletion happens after a successful index.
- Failure scenario: `POST /api/v1/migration/folder {"folder_path":"C:\\Users\\<victim>\\Documents","delete_after_import":true}`. Every DICOM file under that tree is copied into the archive and removed from disk. Only files that parse as DICOM are at risk (`:55` requires a DICM preamble), but any such file on the host qualifies. Imports also land in the unscoped tenant set (`:60`).
- Fix: ADMIN-only. Resolve `folder_path` with `realpath` and check it against configured import roots. Move originals to a quarantine directory instead of unlinking. Require explicit confirmation. Bound the rglob.

### 🔴 E-07: Auto-routing never matches a brand-new study; the failure is silent
- Location: `indexing_service.py:145-154` (routing call inside `get_or_create_study`, before the caller commits), `:114-115` (flush only); `routing_service.py:218-226` (new session); `:46-55` (study lookup); `:54-55` (returns empty list); `receiver.py:60-72` (commit at block exit).
- Quote: `await routing_service.evaluate_and_route_study(study_uid, meta)`; `async with AsyncSessionLocal() as session:`; `if not study: return []`.
- What is wrong: The new Study row exists only inside the uncommitted caller transaction. `evaluate_and_route_study` opens a separate session, cannot see the row, and returns 0 without logging. No exception is raised, so the try/except at `indexing_service.py:149-154` is never triggered. The same pre-commit point fires the StudyReceived webhook and script (`:125-143`). Script threads can call `ctx.study(uid)` before the commit and receive None.
- Failure scenario: An admin configures a CT → PACS rule. A new CT study arrives by C-STORE. No queue row is created, nothing is logged, and nothing is forwarded. Only a later manual `POST /api/v1/routing/evaluate/{uid}`, after the commit, works.
- Fix: Run routing and the webhook and script fire after commit (for example, enqueue an in-process event after commit), or evaluate using the same session. Log when the study is not found. Add an integration test that ingests a study and asserts a queue row exists.

### 🔴 E-08: Quota is never enforced on any write path
- Location: `storage_service.py:256-271` (`save_bytes_with_quota`) and `:301-340` (`save_dataset_with_quota`), with no callers repo-wide; `quota_service.py:112-166` (`check_quota_before_write`, referenced only from those two helpers). Unchecked ingest paths: `receiver.py:86`, `dicomweb.py:876`, `dicomweb_client.py:335`, `sender.py:383`, `migration_service.py:60`, `kin_service.py:202`.
- Quote: `storage_service.py:279`: `save_dataset_with_quota() instead.` (the docstring confirms `save_dataset` does not enforce). `quota_service.py:13`: `storage_service.save_bytes() / save_dataset() call check_quota_before_write()` (false).
- What is wrong: The quota APIs and status views report limits that never stop a write. The check is also a TOCTOU (check, then write, with no lock or reservation) and issues N+1 queries (`:126-135`).
- Failure scenario: A tenant with a 10 GB quota continues ingesting far past the limit. Operators see quota status that claims the limit applies.
- Fix: Route all ingest, STOW, and migration writes through `save_dataset_with_quota` with `tenant_id`. Make the check atomic (a reservation counter or a locked read). Correct the two docstrings.

### 🔴 E-09: Tenant (VNA) isolation is not applied to destructive or egress endpoints
- Location: scope logic exists only in `nodes.py` (send, web upload, pull, WADO), `sender.py:709-713`, `tenants.py`, `quota.py`. Missing in `lifecycle.py` (all routes; `lifecycle_service.py` has no tenant reference), `routing.py` and `routing_service.py` (`:46-55`, `:112-114`), `prefetch.py` and `prefetch_service.py`, `priors.py` and `prior_service.py` (`:70-83`), `lossy_compression.py`, `migration.py`, `split_merge.py` and `split_merge_service.py` (`:41-49`, `:172-179`), `commitments.py:54-63`, and `export.py:33-35` (outside scope).
- Quote: `lifecycle.py:182`: `user: User = Depends(require_destructive_role)` with no tenant argument; `lifecycle_service.py:217-226` selects the study by UID only.
- What is wrong: The role gate is not a tenant gate. Study lookups in these services do not filter on `Study.tenant_id`.
- Failure scenario 1: A RADIOLOGIST assigned to tenant A calls `POST /api/v1/lifecycle/studies/<tenant B UID>/purge` and the study is deleted.
- Failure scenario 2: A PHYSICIAN in tenant A calls `GET /api/v1/priors/patient/<MRN>`. Because `Patient.patient_id` is globally unique (models.py:198), the response includes tenant B's studies for that MRN.
- Failure scenario 3: `GET /api/v1/lifecycle/studies` lists archived studies with patient ID and name across all tenants (`lifecycle.py:192-218`).
- Fix: Apply `scope_from_user` to every study lookup in these services (filter `Study.tenant_id`). Consider a tenant-aware patient identity. Deny cross-tenant purge, archive, restore, and routing.

### 🔴 E-27 (cross-cutting): The storage backend package is git-ignored and absent; a clean clone cannot import the storage stack
- Location: `.gitignore:9` (`storage/`). Imported at `storage_service.py:35`, `api/v1/storage.py:34`, `replication_service.py:39`, and by tests (`tests/test_multi_storage.py:28`, `tests/test_storage_backend.py:19`, `tests/test_replication.py:20`, `tests/test_cstore.py:405`).
- Evidence: `git check-ignore -v app/services/storage/__init__.py` → `.gitignore:9:storage/`. `git ls-files` lists no file under `app/services/storage`. `app/services` contains `storage_commit_service.py` and `storage_service.py` but no storage package.
- Import chain: `main.py:528` mounts `routing.router`; `routing.py:28` imports `routing_service`; `routing_service.py:23` imports `storage_service`; `storage_service.py:35` imports `app.services.storage` at module top level. The app therefore fails at import time from this checkout.
- What is wrong: The ignore pattern `storage/` is unanchored and matches `app/services/storage/` at any depth, so the package was never committed. All backend semantics used in this area (`exists`, `move`, `delete`, `save_bytes`, `read_bytes`, `sha256`, `get_storage_backend`) are unverifiable.
- Fix: Anchor the pattern (`/storage/`), commit the package, and add a CI import check (`python -c "import app.main"`).

### 🟡 E-10: Prefetch accepts wildcard PatientIDs and never checks that returned studies belong to the requested patient
- Location: `prefetch.py:57` (patient_id unvalidated); `prefetch_service.py:40` (`query_ds.PatientID = patient_id`); `:57-67` (PatientID not in returned fields); `:127-128` (capped per rule); `:151`.
- Quote: `query_ds.PatientID = patient_id`.
- Failure scenario: `POST /api/v1/prefetch/trigger {"patient_id":"*"}`. The remote peer returns studies for many patients. Up to `max_priors_count` of them are C-MOVEd into the archive per rule. The study-to-patient relationship is never verified.
- Fix: Validate `patient_id` (no wildcards, length limit); restrict to ADMIN or RADIOLOGIST; require the returned PatientID to equal the requested one.

### 🟡 E-11: Prefetch C-MOVE calls a function that does not exist; no prior is ever retrieved; no automatic trigger
- Location: `prefetch_service.py:149-161`. `sender.c_move_study` is not defined anywhere in the repo (grep). Only `prefetch.py:180-200` calls the evaluator.
- Quote: `cmove_ok = await loop.run_in_executor(None, sender.c_move_study, rule.source_node.host, ...)`.
- What is wrong: The AttributeError is raised inside the `try` while evaluating the argument, then caught by `except Exception` and logged as a warning (`:160-161`). The job becomes FAILED (`:163-164`). `dicom/sender.py:238` already provides `send_move`, which is not used. The trigger from the brief is not wired to worklist or ADT.
- Fix: Implement the C-MOVE call using `send_move`. Wire the trigger to worklist step creation. Surface the exception rather than logging it as a warning.

### 🟡 E-12: The scheduled lifecycle sweep never applies any rule
- Location: `worker.py:234` `counts = lifecycle_service.evaluate_and_run("lifecycle-worker")`; `lifecycle_service.py:229` (`rule_id` is the first parameter); `:236` (`stmt = stmt.where(StudyLifecycleRule.id == rule_id)`).
- What is wrong: The string binds to `rule_id`, producing `WHERE id = 'lifecycle-worker'`. On SQLite that returns no rows (reproduced with stdlib sqlite3). On MySQL, the default `DATABASE_URL` (config.py:25), the string is expected to coerce to 0 and match no rule (reasoning, not run). No exception is raised, so the handler at `worker.py:237-238` never runs. Automatic ARCHIVE, RESTORE, and PURGE never run on schedule.
- Failure scenario: An operator creates an ARCHIVE rule expecting automatic tiering. Studies stay ONLINE indefinitely, and the logs show no error.
- Fix: `evaluate_and_run(performed_by="lifecycle-worker")`. Add a test that the worker sweep evaluates a seeded rule.

### 🟡 E-13: Lifecycle runs are not transactional; partial moves leave DB rows pointing at missing objects; ARCHIVED is set after partial success
- Location: `lifecycle_service.py:117-127` (per-object move; `file_path` updated in memory); `:128-141` (state set if anything moved); `:229-264` (one session, one commit at `:263`, all studies loaded at `:245`); `lifecycle.py:146-148`.
- What is wrong: (1) Object moves are external side effects. If a move raises, or the process dies before the commit at `:263`, the rolled-back DB rows point at hot keys while the objects are already under `.archive/`. Whether `move_object` raises or returns False is unverified because the backend module is missing (E-27). (2) `if moved:` (`:128`) sets ARCHIVED even when only some instances moved. `archive_study` then returns early (`:199-200`), so the remaining hot objects are never archived. (3) No lock: the API and the worker can move the same study concurrently.
- Fix: Per-study transactions with a move journal and compensation. Set state only when every object moved, or track per-instance state. Use a per-study lock. Iterate studies in batches (`:245` loads the whole table).

### 🟡 E-14: Purge deletes metadata even when object deletion fails, and ignores DR replicas
- Location: `lifecycle_service.py:151-160` (delete result only counted); `:259` (`session.delete(study)` unconditional); no `ReplicationRecord` reference in the file.
- What is wrong: A failed object delete leaves orphan blobs with no index. Purge does not check replication state. ARCHIVED studies are excluded from PURGE (`:257`) by design, but this is undocumented.
- Fix: Delete the DB row only after object deletes succeed, or record orphan keys. Require replication status before purge. Document the ARCHIVED exclusion.

### 🟡 E-15: Replication backfill covers only the 500 newest studies; partial records are marked REPLICATED; later instances are never copied
- Location: `replication_service.py:43` (`_STUDY_ENQUEUE_BATCH = 500`); `:204-215` (enqueue from `.order_by(Study.id.desc()).limit(_STUDY_ENQUEUE_BATCH)`); `:218-223` (due query, no limit); `:143-145` (`if not key: continue`); `:175-178` (REPLICATED regardless of skipped count).
- Quote: `.order_by(Study.id.desc())` followed by `.limit(_STUDY_ENQUEUE_BATCH)`; `if not key: continue`.
- What is wrong: Enqueue considers only the 500 newest studies. After those have records, older studies are never enqueued, so an archive that predates DR is never backfilled. Instances added to a REPLICATED study are never copied, because REPLICATED records are never swept again. A study with instances missing `file_path` is marked REPLICATED with `object_count` below the instance count.
- Fix: Page through the whole study table with keyset pagination for enqueue. Mark studies dirty on new instance arrival. Count skipped instances and mark PARTIAL. Note DR is disabled by default (config.py:246).

### 🟡 E-16: Routing queue: non-atomic claim, whole-study resends, temp-file leak, regex on the event loop, one bad rule aborts all evaluation, source-AET filter fails open
- Location: `routing_service.py:270-276` (claim: read then update, no conditional update or lock); `:280` (dispatch); `:300-310` (retry); `:131-159` (instance loop, no per-instance state); `:144` (`NamedTemporaryFile(suffix=".dcm", delete=False)`, never removed); `:94` (`re.search` in async code); `:92-95` (no exception handling around regex); `:87-89` (source-AET check skipped when `source_aet` is empty); `routing.py:44` (pattern unvalidated); `routing.py:194-209` (second sweep thread).
- Quote: `if fresh is None or fresh.status != "PENDING": continue` then `fresh.status = "SENDING"`; `if not re.search(pattern, study_desc, re.IGNORECASE):`.
- What is wrong: (1) Claim is check-then-update. The worker, the manual sweep, and any second API process can each dispatch the same item, producing duplicate C-STORE and duplicate AUTO_ROUTED audit rows. (2) Every retry resends all instances, including those already accepted. (3) For remote backends, a temp file is created per instance and never removed. (4) `re.search` runs on the event loop with a user-supplied pattern (any user can create one, `routing.py:84`). Catastrophic backtracking blocks the server. A malformed pattern raises `re.error` and aborts evaluation for all remaining rules. (5) `source_aet` fails open: when `meta` is None (manual evaluate, `routing_service.py:59`) or missing, the check is skipped and the rule matches every source.
- Fix: Conditional update (`UPDATE ... WHERE status='PENDING'` with a rowcount check, or `SELECT ... FOR UPDATE SKIP LOCKED` on MySQL). Track per-instance sent state. Delete temp files in a `finally`. Validate regex on save and run matching in a thread with a time limit. Fail closed on `source_aet`.

### 🟡 E-17: Credentials are plaintext at rest; a node PUT clears them when omitted; the DICOMweb SSRF guard is check-then-use; http is allowed
- Location: `models.py:752-753` (`web_username`, `web_password` String(256)); `models.py:1087-1088` (`access_key`, `secret_key` String(255)); `nodes.py:131` (`data = body.model_dump()`) and `:139-140` (setattr loop); `dicomweb_client.py:54-55` (http and https both allowed), `:67-83` (resolve and check), `:155`, `:241`, `:442` (validate, then request; httpx resolves again); `nodes.py:107-109`, `:134-136` (validation only on create and update); `storage.py:45`, `:58` (`endpoint_url` unvalidated, ADMIN only).
- Quote: `data = body.model_dump()` then `for field, value in data.items(): setattr(node, field, value)`.
- What is wrong: (1) A PUT that omits `web_password` sets it to NULL silently, so the Basic-auth password is cleared with no error. (2) DNS rebinding: a hostname can resolve to a public address during validation and to a private address at request time, because the request resolves the name again. (3) `web_password` is sent over cleartext http. (4) Secrets are stored unencrypted.
- What is correct: `_node_out` (nodes.py:74-85) and storage `_serialize` (storage.py:70-84, `has_secret`) never return secrets. `follow_redirects=False` at `dicomweb_client.py:104, 247, 476` prevents redirect-based SSRF.
- Fix: PATCH semantics (`exclude_unset`). Encrypt secrets at rest with a key from the environment or a KMS. Pin the resolved IP for the request, or connect by IP with a Host header. Require https unless explicitly allowed.

### 🟡 E-18: Storage admin API: no guard for in-use backends on PATCH; a move repoints the DB even when the source is missing; cross-backend move deletes the source without verifying the copy
- Location: `storage.py:176-205` (PATCH changes `root_path`, `bucket`, `endpoint_url` with no in-use check; compare DELETE at `:250-260`); `storage_service.py:343-355` (copy at `:353`, delete at `:354`, no size or hash verification); `storage_service.py:387-393` (`if move_to_backend(...)` returns False, then `inst.storage_backend = to_backend` still runs at `:393`).
- Quote: `dst.save_bytes(key, src.read_bytes(key))` then `src.delete(key)`.
- Failure scenario 1: An admin changes `bucket` on an in-use S3 backend. Every instance key now resolves into an empty bucket.
- Failure scenario 2: A study move finds a missing source object, `move_to_backend` returns False, and the DB still repoints the instance to a backend that never received it.
- Failure scenario 3: A destination write that does not raise, but is incomplete, is followed by a delete of the only source copy.
- Fix: Refuse PATCH of location fields while instances reference the backend. Verify destination size or hash before deleting the source. Repoint only when the move returned True.

### 🟡 E-19: AE-to-tenant cache is per-process, has no TTL, is invalidated before commit, and caches "unmapped"; Tenant.is_active is not enforced
- Location: `tenant_service.py:31` (`_AE_CACHE`), `:39-43` (invalidate), `:54-63` (caches the result, including None); `tenants.py:170` (delete invalidates before commit), `:204` (map), `:224` (unmap); `db/session.py:223-231` (commit happens after the handler returns); `main.py:390` (receiver runs in-process as a thread); `receiver.py:226`, `:497`, `:546` (sync resolver); `storage_service.py:193-195` (comment refers to multiple API nodes behind HAProxy).
- Quote: `_AE_CACHE[key] = result` (tenant_service.py:63); `invalidate_ae_cache()` (tenants.py:204, before the commit).
- What is wrong: Race: a mapping change invalidates the cache, a concurrent C-STORE from that AE resolves before the commit and caches "unmapped" (None), and after the commit the stale None persists in that process until restart or the next mutation. Multi-node: each process has its own cache, so only the handling node is invalidated; the other nodes keep the old mapping indefinitely, including after an unmap. `Tenant.is_active` is written (`tenants.py:147`) and displayed (`tenants.py:85`, `dashboard.py:309`), but never checked during resolution, so deactivating a tenant has no effect on ingest or query.
- Fix: Use a TTL or a shared version key in Redis. Invalidate after commit (SQLAlchemy `after_commit` event or a post-response hook). Do not cache None. Enforce `is_active` at resolution, or remove the control.

### 🟡 E-20: Deleting a tenant releases its studies to the unscoped set, visible to every user without a tenant
- Location: `tenants.py:160-168` (studies and users set to NULL); `tenant_service.py:88-89` (no tenant means the unscoped set).
- What is wrong: Intended per the comment at `tenants.py:157-159`, but the effect is that every study of a deleted tenant becomes visible to all unscoped users. This is an exposure event that the endpoint does not surface.
- Fix: Move the studies to an admin-only quarantine tenant, or require explicit confirmation of the exposure.

### 🟡 E-21: The quota status route is unreachable; GET /api/v1/quotas/status is shadowed by /{quota_id}
- Location: `quota.py:145-152` (`GET /{quota_id}`, declared first); `quota.py:183-190` (`GET /status`).
- What is wrong: Starlette matches routes in declaration order. Reproduced with a first-match simulation (`repro_area_e.py`, part 2): `GET /api/v1/quotas/status` resolves to `get_quota`, which then fails `int` validation for `quota_id` and returns 422 (expected FastAPI behaviour; the app itself was not run).
- Fix: Declare `/status` routes before `/{quota_id}`, or constrain the path parameter (`{quota_id:int}`).

### 🟡 E-22: Scripting: trust model is by design, but file-drop execution, unbounded threads, blocking calls on the loop, and inconsistent name validation
- Answer to the brief: Yes, scripts are executed. `_load` runs `spec.loader.exec_module` on each `.py` file (scripting_service.py:117-123). There is no `eval` of strings. There is no sandbox: no RestrictedPython, no subprocess, no seccomp, no resource limit. Scripts run in the server process with full privileges. The module docstring (scripting_service.py:35-37) acknowledges this.
- Who can upload: `PUT /api/v1/scripts/{name}` (ADMIN only, scripts.py:74-89), which writes the raw request body verbatim (`:86-87`). Any process that can write into `SCRIPTS_DIR` also gains code execution at the next list or dispatch, with no hash, signature, or allowlist check.
- Sub-findings:
  - **E-22a (design risk):** An ADMIN session is code execution on the server, and so is any filesystem write access to `scripts/`. Recommend directory ACLs and hash pinning.
  - **E-22b:** `fire()` starts one daemon thread per event with no pool or cap (scripting_service.py:319-322). `ScriptContext.fire` re-dispatches with no depth guard (`:274-276`). A handler that calls `ctx.fire("StudyReceived")` spawns threads without bound.
  - **E-22c:** `list_scripts` (scripts.py:35-38, async) and `run_script` (scripts.py:58-60, async) call synchronous loading and execution on the event loop. A script that loops or sleeps stalls every request.
  - **E-22d (nit):** `_NAME_RE` is applied only on create (scripts.py:78-79). `script_source` (`:68`) and `run_script` (scripting_service.py:327) build `scripts_dir() / f"{name}.py"` from the raw path parameter. Admin-only.
- Fix: Cap the thread pool or use a queue. Add a re-entrancy depth guard. Run script execution in a worker with a timeout. Validate names on every route. Consider a hash allowlist and a directory ACL.

### 🟡 E-23: Anonymizer: incomplete PS3.15 coverage; scrubbing is top-level only; de-identification claims exceed behaviour
- Location: `anonymizer.py:34-59` (PHI_KEYWORDS), `:62-69` (PHI_SEQUENCES), `:87-96` (`_known` silently drops unknown keywords), `:106-112` (top-level delete only), `:131-139` (UID remap top-level only), `:140-149` (file_meta).
- Quote: `for kw in PHI_KEYWORDS: if kw in out: del out[kw]` (top level only).
- Not in the lists (to be verified against PS3.15 Table E.1-1 and the installed pydicom, since pydicom is not installed here): PatientBirthName, EthnicGroup, Occupation, AdditionalPatientHistory, ReferringPhysicianAddress, ReferringPhysicianTelephoneNumbers, AdmittingDiagnosesDescription, StudyComments, RequestedProcedureComments, ReasonForRequestedProcedure, DerivationDescription, ContentCreatorName, PatientInstitutionResidence, CurrentPatientLocation, PatientState, AcquisitionDateTime, ReferencedPatientSequence (absent from PHI_SEQUENCES).
- What is wrong: `_known` removes any keyword that the installed pydicom does not know, with no warning, so a typo silently disables that PHI tag. Nested sequences are not scrubbed, and UIDs inside Referenced*Sequence items are not remapped, so the export can link back to the original study. BurnedInAnnotation, PatientIdentityRemoved, and DeidentificationMethod are not set, although PS3.15 expects them. Burned-in pixel PHI is out of scope (anonymizer.py:10-12).
- Failure scenario: A research export contains a ReferencedPatientSequence item with the original PatientName and PatientID, or a StudyComments field with a clinician's name.
- Fix: Recursive scrub with an explicit action table per tag. Unit test that every listed keyword exists in the installed pydicom. Remap every UID, including Referenced*Sequence, through the same map. Set PatientIdentityRemoved and DeidentificationMethod. Stream the ZIP to a file.

### 🟡 E-24: Blocking I/O on the event loop in async handlers
- Locations and fix (use `asyncio.to_thread` or the worker):
  - `lossy_compression.py:155` → `lossy_compression_service.py:119-180` (sync storage, PIL encode, and save inside async code).
  - `split_merge_service.py:110-116`, `:209-216` (sync storage calls in async functions, called from `split_merge.py:51, 87`).
  - `export.py:54` (synchronous anonymization of a whole study in an async handler).
  - `scripts.py:35-38`, `:58-60` (synchronous exec in async handlers).
  - `routing_service.py:94` (user regex on the loop).
  - `storage.py:221-223` (boto call inside `async def test_backend`).
  - `migration_service.py:171` (sync DB lookup in async `run_cmove_migration`).
- Note: the node endpoints correctly use `asyncio.to_thread` (nodes.py:212, 260, 301, 362, 414, 432, 462, 529).

### 🟡 E-25: Unbounded memory and loops
- `lifecycle_service.py:245` (`session.query(Study).all()` with lazy series and instance loads per study).
- `lossy_compression_service.py:134-139` (every non-baseline instance eager-loaded).
- `migration_service.py:50` (`[f for f in path.rglob("*") if f.is_file()]` materializes the full tree).
- `anonymizer.py:179-190` and `export.py:51-56` (whole study ZIP in memory).
- `replication_service.py:218-223` (due query without LIMIT).
- `migration.py:35-37` (`CMoveMigrationRequest.study_instance_uids`, no size limit) and `:81-106` (batch runs sequentially).
- Fix: keyset pagination and batch processing.

### 🟡 E-26: Swallowed exceptions hide failures
- `split_merge_service.py:256-257`: rewrite failure becomes a warning (see E-05).
- `lossy_compression_service.py:114-116`: any exception returns False. Between the `save_dataset` at `:87` and the DB update at `:90-94`, a failure leaves a rewritten file with stale DB metadata, and the next run transcodes it again (double lossy).
- `prefetch_service.py:160-161`: a permanent AttributeError is logged as a warning (E-11). `:94-96`: failure to read existing UIDs returns an empty set, so already-stored studies are re-C-MOVEd.
- `indexing_service.py:149-154`: routing failures are logged and dropped.
- `nodes.py:420-421`, `:436-437`: WADO per-study exceptions swallowed (partial success).
- `migration_service.py:83-94`: per-file exceptions counted; first 50 kept.
- Fix: Record a per-item status and surface it, or fail the job.

### 🟡 E-28: WADO pull by PatientID with auto_archive archives nothing and reports a misleading error
- Location: `nodes.py:409` (auto_archive flag), `:411-424` (PatientID branch fills `study_uids`; `study_uid` stays ""), `:460-465` (`archive_study, study_uid, ...` passes the request filter value, not each pulled UID), `:469-472` (response fields).
- Quote: `archive_result = await asyncio.to_thread(` / `archive_study, study_uid, performed_by=f"wado-pull:{user.username}"`.
- What is wrong: For PatientID pulls, `study_uid` is "" (`:399`). `archive_study("")` returns `{"ok": False, "error": "Study not found"}` (lifecycle_service.py:198-199), so `auto_archived` is false and `archive_error` reads "Study not found" even though the pull succeeded. Only a pull by StudyInstanceUID archives, and then only the one requested study. Every other pulled study stays on the hot tier.
- Failure scenario: An operator enables `LIFECYCLE_AUTO_ARCHIVE_WADO_PULL` (or sends `auto_archive: true`) and pulls a patient's history by PatientID. Every study lands on the hot tier, and the response suggests the archive step failed.
- Fix: Archive each pulled study UID (loop over `study_uids` with a per-study result), and report per-study archive state.

### 🟡 E-29 (cross-cutting, out of scope; verify deployment): Default secrets in config
- `config.py:136`: `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"`. If the environment does not override it, JWTs can be forged, which would bypass every ADMIN, D, and U check in this area.
- `config.py:25`: default `DATABASE_URL` embeds the username and password `paxs_user:paxs`.
- Severity: 🔴 if deployed with defaults; otherwise a hygiene item. Not verified in any deployment.

### 🟢 Nits
- **N1:** `nodes.py:57` `pattern="^(?i)(PATIENT|STUDY|SERIES|INSTANCE)$"`. Python's `re` rejects global flags that are not at the start of the expression (verified with the local Python 3.14: "global flags not at the start of the expression at position 1"). pydicom-core's Rust regex accepts it, so runtime validation likely works (unverified here). Tools that compile the pattern with Python `re` will fail. Use `^(PATIENT|STUDY|SERIES|INSTANCE)$` and normalize case.
- **N2:** `nodes.py:131-140`: PUT can rename `ae_title` through the body. Renaming onto an existing AE raises an IntegrityError (`models.py:745` unique) and returns 500.
- **N3:** Role checks are inline in `nodes.py:98, 124, 150` rather than `require_admin`. `list_nodes` and `ping_node` are open to any user (`:89`, `:163`).
- **N4:** `storage.py:152-160` (create_backend): two concurrent first creates both see count 0 and both become default; there is no DB uniqueness on the default flag. `storage.py:186-190` (patch with `is_default=False`) can leave no default.
- **N5:** `storage.py:173`, `:205`, `:264`: create and patch responses report `instances: 0` and `usage_bytes: 0` (`_serialize(row, 0, 0)`).
- **N6:** `storage.py:221`: uses the private `backend._s3()`.
- **N7:** `quota.py:93-105`: N+1 full-table SUM per quota. `quota_service.py:75-79`: both filters ANDed (see section 4e step 5).
- **N8:** `tenants.py:92-96` and `:173-184`: RADIOLOGIST can list all tenants and AE mappings. Confirm this is intended.
- **N9:** `tenants.py:109-112`: `scalar_one_or_none()` on an OR across name and code can raise MultipleResultsFound and return 500.
- **N10:** `scripts.py:83`: unbounded body read into memory.
- **N11:** `commitments.py:54-63`: not tenant-scoped; any signed-in user sees commitment records (instance counts, failure reasons) for all tenants.
- **N12:** `prefetch.py:64-65` (`_rule_to_dict` and `_job_to_dict`): read `__dict__` directly, so unloaded relationships silently return null AE titles.
- **N13:** `migration.py:109-117`: `source_info` returns server folder paths to every signed-in user.
- **N14:** `replication_service.py:85-94`: select-then-insert on a unique column (`models.py:1494`) can raise IntegrityError under concurrent sweep and on-demand replication.
- **N15:** `lifecycle_service.py:251-262`: `break` after the first matching rule even when that rule's action is a no-op, so a higher-priority ARCHIVE rule can block a lower-priority PURGE for the same study.
- **N16:** `routing_service.py:102-172`: `ok` is `failed == 0`, so an empty study (`sent == 0`) completes as success.
- **N17:** `split_merge_service.py:81-88`: unknown `series_instance_uids` are silently ignored; only "none found" errors.
- **N18:** `scripts.py:80-88`: check-then-write race on create (TOCTOU), minor.

---

## 6. Open questions

1. **Q1 (storage package):** Does a local, uncommitted `app/services/storage` package exist anywhere? If not, how was the app tested? What do `exists`, `move`, `delete`, and `save_bytes` do on failure: raise, or return False? This decides whether E-13 and E-18 are reachable as described.
2. **Q2 (session lifetime for NDJSON):** `_stream_web_upload` (nodes.py:589-647) audits and flushes inside the streaming generator after the response has started, using the request session. Does FastAPI keep the `get_db` session open for the stream, and is the flush committed? (Only `get_db`, `db/session.py:223-231`, was read.)
3. **Q3 (multi-frame lossy):** `dataset_to_image` renders `ds.pixel_array`. For `NumberOfFrames > 1`, is only the first frame encoded, or is an error raised? Either way the transcoder never updates `NumberOfFrames`. The imaging code (`imaging.py:40-90`) was read only as far as the grayscale path.
4. **Q4 (DR intent):** Is the 500-study enqueue window intended (E-15), or is a full backfill expected when DR is enabled on an existing archive?
5. **Q5 (routing concurrency):** Is more than one worker process or API node expected to run `process_queue_batch` (E-16)? Is `ROUTING_RUN_IN_WORKER` set in deployment?
6. **Q6 (C-MOVE destination):** Migration and prefetch C-MOVE to `DICOM_AE_TITLE` ("PAXS_SCP", config.py:28). Does the remote peer have that AE registered with PAXS's host and port? Otherwise C-MOVE cannot deliver.
7. **Q7 (roles):** Which role values exist beyond ADMIN, RADIOLOGIST, and the PHYSICIAN default (`models.py:111`)? `users.py` was not read. E-01, E-02, and E-06 apply to every non-ADMIN role.
8. **Q8 (lifecycle intent):** Should ARCHIVED studies be purgeable by rule (currently excluded at `lifecycle_service.py:257`), and should purge require replica confirmation?
9. **Q9 (move collisions):** On RESTORE or ARCHIVE, what happens when the destination key already exists (for example, a re-received instance on the hot key)? Is the existing object overwritten?
10. **Q10 (tenant identity):** Is `Patient` intended to be global across tenants (`models.py:198` unique `patient_id`)? If so, E-09 is an architectural decision, not only a missing filter.
11. **Q11 (DICOMweb private hosts):** Who can change `DICOMWEB_CLIENT_ALLOW_PRIVATE` at runtime (`settings_service.py:257`, not read)? It disables the SSRF guard.
12. **Q12 (tests):** The test suite (`tests/`) was not read or run. Does any existing test cover the behaviours in E-05, E-07, E-12, or E-16? A passing suite would contradict some findings if the tests exercise these paths differently.
13. **Q13 (PS3.15 keyword validity):** Confirm each keyword in `anonymizer.py` against the installed pydicom `keyword_dict`, and confirm the missing-attribute list in E-23 against PS3.15 Table E.1-1. pydicom is not installed here.
14. **Q14 (scheduling cadence):** `WORKER_POLL_SECONDS` default was not checked, so tick intervals are given in ticks only.
