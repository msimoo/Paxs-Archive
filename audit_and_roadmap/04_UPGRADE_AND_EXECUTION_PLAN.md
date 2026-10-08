# 04 — Upgrade & Execution Plan

> Phased roadmap derived from `02_CRITICAL_BUGS_AND_ISSUES.md` and `03_PERFORMANCE_AND_DICOM_AUDIT.md`.
> Ordering principle: **clinical-data integrity and availability first**, then architecture stabilisation, then viewer UX, then modernisation.
> Each milestone has a **Definition of Done (DoD)** and a **verification checklist** that can be run against this repo.

---

## Phase A — Hotfixes (data corruption, critical crashes, memory leaks)

> Target: stop the bleeding. Every item here is a **correctness** fix, not tuning. Ship behind a patch release.

### A-1 — Fix study split/merge header rewrite (bug C-1)
**Work:**
1. Change `_rewrite_instance_study_uid` to return the **rewritten bytes** (`ds.save_as(BytesIO, enforce_file_format=True)`).
2. In `split_series_to_new_study` / `merge_studies`, write those rewritten bytes to `new_key` and delete the old key — never re-read `old_bytes`.
3. Remove the swallow-all `except` in the rewrite; propagate failures so the caller aborts (staged-write + rollback).
4. Add a read-back assertion: `read_dataset(new_key).StudyInstanceUID == new_study_uid` before deleting the source.

**DoD:** after a split/merge, the stored object's `StudyInstanceUID`/`PatientID` match the DB; a failed rewrite leaves the source object intact and the DB unchanged.

**Verification:**
- New test `tests/test_split_merge_integrity.py`: build a 2-instance study, split it, read back every object's `StudyInstanceUID` == the new study UID.
- Fault-injection test: force `save_bytes` to raise mid-split; assert the source object still exists and the DB has not been mutated.

---

### A-2 — Disable / rewrite the lossy compression engine (bugs C-2, C-3, C-4)
**Work (interim, ship now):** gate `evaluate_and_run_lossy_compression` behind an explicit `LOSSY_COMPRESSION_ENABLED=false` default and refuse to run rules whose `transfer_syntax_uid` is not implemented. Log a startup warning if any enabled lossy rule exists.
**Work (real fix):**
1. Replace `dataset_to_image` with a true codec transcode that **preserves** `BitsAllocated`/`BitsStored`/`PhotometricInterpretation`/`SamplesPerPixel`/`Rows`/`Columns`.
2. Branch on `target_ts_uid`: encode JPEG baseline (`.50`), JPEG-LS lossy (`.81`), JPEG2000 lossy (`.91`) with the matching codec; reject unimplemented targets at rule-creation time.
3. Restrict eligibility to **raw/uncompressed or verified-lossless** sources only (exclude every `.is_compressed` syntax).
4. Route the write through `StagedObject` (stage → DB flush → promote) instead of in-place `save_dataset`.

**DoD:** a lossy-rule run produces objects that (a) decode with a spec-compliant reader for their declared transfer syntax, (b) preserve geometry and bit depth, and (c) are never applied to an already-lossy source.

**Verification:**
- Round-trip test: transcode a 16-bit CT to JPEG-LS lossy, re-read, assert `BitsAllocated == 16`, `PhotometricInterpretation == "MONOCHROME2"`, and the codestream decodes via `pyjpegls`.
- Negative test: an instance already in `.81` is not selected by the rule.

---

### A-3 — Close the FHIR cross-tenant PHI leak (bug C-5)
**Work:** apply `tenant_scope(_user)` to `search_patient` (add the `Patient.tenant_id` predicate to `conds`); route `read_patient` through a tenant-scoped patient check (mirror `require_study_access` for `Patient`).

**DoD:** a tenant-A caller receives 404/empty for a tenant-B patient by `_id`, `identifier` and `name`.

**Verification:** test with two tenants and two patients sharing an MRN; assert tenant A cannot see tenant B's patient on `/fhir/Patient` or `/fhir/Patient/{id}`.

---

### A-4 — Restore the prior-study prefetch path (bug H-1)
**Work:** call `sender.send_move(...)` with the correct signature (`dest_ae_title=settings.DICOM_AE_TITLE`, study UID as the query key), or implement `c_move_study` as a thin wrapper. Also apply `max_prior_age_days` (L-13).

**DoD:** a prefetch rule against a reachable test peer actually issues a C-MOVE and the retrieved instances land in the archive; the job status reflects reality.

**Verification:** integration test with a local test SCP; assert the C-MOVE is attempted and `priors_retrieved` matches.

---

### A-5 — Stop the S3 auto-routing temp-file leak (bug H-2)
**Work:** `try/finally: os.unlink(file_target)` in `dispatch_study_to_node_sync`; stream from `read_stream` rather than `read_bytes`.

**DoD:** no residual `.dcm` files in the system temp dir after routing a study from an S3 backend.

**Verification:** route a 3-instance study from a MinIO backend; assert `tempfile.gettempdir()` gains no new `.dcm` files.

---

### A-6 — Bound the HL7 MLLP listener (bugs H-3, H-4)
**Work:** cap concurrent connections (semaphore/thread pool) + `HL7_MAX_MESSAGE_BYTES` buffer cap + per-connection socket timeout + optional peer allow-list.

**DoD:** 1000 idle connections do not exhaust threads; an unterminated 100 MB message is dropped without OOM.

**Verification:** load test opening 1000 sockets; assert the process RSS is flat and existing connections keep working.

---

### A-7 — Enforce the WADO-RS client size cap while streaming (bug H-5)
**Work:** switch `wado_retrieve` to `client.stream(...)`, accumulate chunks, abort when `DICOMWEB_CLIENT_MAX_BYTES` is exceeded.

**DoD:** a peer returning a body larger than the cap is aborted without buffering the whole body.

**Verification:** mock a peer streaming 1 byte over the cap; assert the process RSS does not grow by the full body.

---

### A-8 — Make lifecycle tiering/purge all-or-nothing (bugs H-6, H-7)
**Work:** track moved keys and roll back on the first failure; set `ARCHIVED` only when `moved == total`. Refuse to delete the study row when any object deletion fails; record failures for retry.

**DoD:** no study is ever left split across tiers with a lying state flag; purge never orphans an object.

**Verification:** fault-injection test forcing one `move_object`/`delete_file` to fail; assert the study state and objects remain consistent.

---

### A-9 — Fix the client-side viewer leaks (bugs H-10, H-12, L-8)
**Work:**
1. `paxs-mpr.js` `prefetchServerRange`: evict `serverFrames[plane]` entries outside `[center±radius]` and `URL.revokeObjectURL`.
2. `paxs-study-list-enhance.js` `thumbFor`: revoke object URLs on image load / implement an LRU; revoke on `pagehide`.
3. `paxs-mpr.js` `closeMpr`: reset `state.processing`, `state.fusion.overlayVol/Info/enabled`, clear `split.fineTimer`, null `split.fine[plane]`.

**DoD:** scrolling a 500-slice study in server mode keeps browser heap flat (no monotonic growth); reopening a study after a GPU-context loss uses the GPU path again.

**Verification:** Chrome DevTools memory timeline — scroll 500 slices twice; assert heap returns to baseline. Manual: force context loss, close, reopen, assert `effectiveMode` is not stuck at `server`.

---

### A-10 — Remove the global `getContext` patch (bug H-11)
**Work:** delete `installVtkContextGuard`'s prototype patch; fix the real `setContext` null-deref at its call site (or scope `preserveDrawingBuffer` to only the canvases that need it).

**DoD:** Cornerstone3D viewports render without a forced `preserveDrawingBuffer`; no `getContext` prototype override remains.

**Verification:** assert `HTMLCanvasElement.prototype.getContext.toString()` contains no `preserveDrawingBuffer` injection; smoke-test scrolling and rendering.

---

### A-11 — Move blocking calls off the event loop (bug H-13)
**Work:** wrap `nodes.ping_node` (`send_echo`), `config.py` `overrides_map`/`set_override`, `split_merge_service` storage I/O, `scripts.py` FS/`exec_module`, and `replication.py` status/records in `await asyncio.to_thread(...)`.

**DoD:** `/health/live` responds within 200 ms while a C-ECHO to an unreachable node is in flight.

**Verification:** start a slow/unreachable node, fire a ping, and concurrently assert `/health/live` latency stays < 200 ms.

---

### Phase A — exit criteria
- All Critical + High correctness items merged.
- `02` regression tests green.
- A full `pytest` run + the `.workbuddy-ai/tmp` extracted-source suites pass.
- A release note enumerating the data-integrity fixes (C-1/C-2/C-3) so operators can audit affected studies.

---

## Phase B — Refactoring & Architecture Stabilisation

> Target: eliminate the systemic patterns that produced the Phase-A bugs — non-atomic claims, whole-object buffering, sync-in-async, unbounded fan-out.

### B-1 — Streaming storage I/O end-to-end
**Work:** add `read_stream`/`write_stream`/`upload_fileobj` to the `StorageBackend` interface (`storage/backend.py`) and use them in: staging (`stage_dataset_with_quota` — write the encoded dataset directly to the staged file, not a `BytesIO`), S3 `read_bytes`/`sha256`/`usage_bytes`, replication (`replicate_study`), the DICOMweb client (`_multipart_body_chunks`), and routing.
**DoD:** peak ingest RSS for a 2 GB instance < 1.2 GB; no code path reads a whole remote object into RAM to hash or copy it.
**Verification:** memory-profiled ingest of a synthetic 2 GB multi-frame instance; assert peak RSS.

### B-2 — Atomic claims everywhere
**Work:** convert `routing_service.process_queue_batch` to the guarded `UPDATE ... WHERE status='PENDING'` + `rowcount` pattern used by `worker._claim_jobs`; add a unique constraint on `ReplicationRecord.study_instance_uid` (already unique — verify) and make `sweep` idempotent.
**DoD:** two worker processes never dispatch the same queue item.
**Verification:** concurrency test with 2 workers × 100 queue items; assert each item dispatched exactly once.

### B-3 — Batch the O(N) sweeps
**Work:** `lifecycle_service.evaluate_and_run` and `lossy_compression_service.evaluate_and_run_lossy_compression` iterate in ID-ordered batches with `yield_per` and eager-load relationships.
**DoD:** a sweep over 1M studies uses bounded memory and a bounded query count.
**Verification:** seed 100k synthetic studies; assert peak RSS and query count during a sweep.

### B-4 — Bounded background fan-out
**Work:** replace unbounded `threading.Thread(...).start()` in `warm_service`, `scripting_service` with a shared bounded `ThreadPoolExecutor`; add a depth/rate cap on chained script events; lock `scripting_service._load`.
**DoD:** N concurrent study-opens spawn ≤ K warm threads; a recursive script event is capped.
**Verification:** open 100 studies concurrently; assert thread count stays bounded.

### B-5 — DB pool & loop hygiene
**Work:** give the DIMSE receiver its own sync pool (or raise `DB_POOL_SIZE`); batch the per-instance `asyncio.run(_index_async)` into one session per association; separate a CPU-bound decode pool from the I/O pool; make `settings_service` mutation lock-guarded.
**DoD:** a burst of 16 concurrent C-STORE + a worker batch does not exhaust the sync pool; C-STORE ingest no longer creates an event loop per instance.
**Verification:** load test with 16 concurrent C-STORE senders; assert no `QueuePool limit` errors.

### B-6 — Cache correctness
**Work:** fix Redis L1 byte accounting (`02 §M-3`); byte-bound the tile cache; tenant-scope `_BUNDLE_CACHE` (`02 §M-7`); stream S3 usage.
**DoD:** `get_l1_cache_stats().used_bytes` matches the true dict size; a tenant-A caller cannot receive a cached bundle populated by a tenant-B/admin request.
**Verification:** unit tests on the cache counters; a cross-tenant cache test.

### B-7 — DICOM conformance hardening
**Work:** validate mandatory Type 1 UIDs on ingest (reject or quarantine instead of `"UNKNOWN"` — `02 §M-15`); persist and honour `SpecificCharacterSet` on re-save; complete the anonymizer PS3.15 Basic Profile (recursive private-tag stripping + `PatientIdentityRemoved`/`DeidentificationMethod` — `02 §M-14`); emit **ATNA audit messages** (RFC 3881) to a configurable Audit Record Repository in addition to the private `audit_log`.
**DoD:** ingest rejects/quarantines missing-UID instances; re-saved objects round-trip non-ASCII names correctly; an anonymized export carries the de-identification markers; an ATNA AAR receives audit XML.
**Verification:** conformance tests per item; a charset round-trip test with a Shift-JIS/Arabic `SpecificCharacterSet`.

### B-8 — DIMSE transfer batching & timeouts
**Work:** one association per study in `send_file`/`send_keys`/`send_directory`; set `ae.acse_timeout`/`ae.dimse_timeout`; return failure status from `send_get._on_store` when local ingest fails (`02 §H-8/H-9`).
**DoD:** forwarding a 500-instance study opens 1 association; a failed ingest returns a non-Success C-STORE sub-operation status.
**Verification:** instrument association count; a C-GET test where local ingest fails asserts a failure status is returned.

### Phase B — exit criteria
- No `async def` contains a blocking network/disk call.
- Every queue claim is a guarded UPDATE.
- All object-storage paths stream.
- DICOM conformance test suite green (Type 1, charset, de-identification markers, ATNA).

---

## Phase C — PAXS Viewer Enhancement (rendering performance, UX, tools)

> Target: make multi-slice scrolling and multi-series sessions smooth and memory-stable on commodity workstations.

### C-1 — Client memory budget
**Work:** byte-bound `state.volCache` (evict until `Σ byteLength < budget`), include `state.fusion.overlayVol` in the accounting, release the cache on workspace close; complete the A-9 leak fixes.
**DoD:** a 3-series session's client heap stays under a configured budget; heap returns to baseline after close.
**Verification:** DevTools memory timeline across a 3-series open/close cycle.

### C-2 — Render-path optimisation
**Work:** reuse one `ImageData`/offscreen canvas per viewport (`draw2D`, `drawFusionOverlay`); preallocate the colormap LUT; move fusion to the existing GPU shader path; replace O(n²) `svg.innerHTML +=` with a single assignment; update crosshair elements instead of recreating them; add a dirty flag to the Three.js `animLoop`.
**DoD:** sustained 60 fps during multi-slice scroll on a 512-slice CT in server mode; no per-frame canvas allocation.
**Verification:** Chrome Performance profile during a 30 s scroll; assert frame budget and zero `createElement('canvas')` per frame.

### C-3 — Replace polling with observers
**Work:** replace the five `setInterval` sweeps and the two undebounced body observers (`paxs-study-list-enhance.js`) with debounced `MutationObserver`s + attribute observers; disconnect on `pagehide`.
**DoD:** idle CPU on the study list drops materially; no permanent intervals remain.
**Verification:** Performance monitor idle-CPU measurement before/after; grep asserts no `setInterval(..., 1000)` sweeps remain.

### C-4 — Tooling & UX
**Work:** surface the fix status of the server-side pipeline in the telemetry pill; add per-study memory/cache indicators; improve MPR measurement ergonomics (throttled drag repaint); document the client memory budget in the About modal.
**DoD:** the telemetry pill reflects the negotiated mode and cache pressure; measurement drag is smooth.
**Verification:** manual UX pass + `about-test.js` / `toolbar-test.js` suites green.

### Phase C — exit criteria
- Client heap bounded and released on close.
- 60 fps multi-slice scroll on the reference workstation.
- No permanent polling intervals.
- All viewer JS test suites (`about-test.js`, `toolbar-test.js`, `icon-test.js`, `cascade-test.js`) green.

---

## Phase D — Modernisation & Scalability

> Target: production hardening for HTJ2K throughput, containerised scale-out, and automated verification.

### D-1 — HTJ2K & transfer-syntax optimisation
**Work:** benchmark HTJ2K vs JPEG-LS vs JPEG2000 encode/decode on the reference modality mix; make HTJ2K the default only where it wins; skip lossless re-encode of already-compressed instances; expose the codec matrix (`--check-codecs`) as a deploy gate that fails when `DEFAULT_COMPRESSION` is unavailable; align `.env.example` defaults with `config.py` (`02 §L-17`).
**DoD:** a published codec benchmark; the deploy gate fails fast on a missing codec; no wasteful re-encode of compressed sources.
**Verification:** benchmark harness in CI; a deploy smoke test with a missing codec asserts a non-zero exit.

### D-2 — Containerisation & scale-out
**Work:** validate the shipped `deploy/Dockerfile` + `docker-compose.lb.yml` + HAProxy config against the current code; add health/readiness probes wired to `/api/v1/health/live|ready`; document the shared-DB/shared-storage topology; ensure `PAXS_INSTANCE_ID` surfaces in `/health`.
**DoD:** `docker compose -f deploy/docker-compose.lb.yml up` yields a 2-node cluster behind HAProxy serving the viewer, with instance IDs visible.
**Verification:** bring the stack up, run a QIDO/WADO smoke test through HAProxy, kill a node and assert failover.

### D-3 — Automated CI tests
**Work:** wire `pytest` + the `.workbuddy-ai/tmp` extracted-source JS suites + a DICOM conformance suite + a memory regression test into CI; add a synthetic DICOM corpus (uncompressed, RLE, JPEG-LS, JPEG2000, HTJ2K, multi-frame, non-ASCII charset) and run ingest → query → retrieve → transcode end-to-end.
**DoD:** every push runs the full suite; the conformance corpus is a fixture; memory regressions fail the build.
**Verification:** a CI run on the corpus; a deliberately introduced memory regression fails the build.

### D-4 — Observability
**Work:** ensure `/metrics` exposes the L1 cache gauges (`paxs_cache_utilisation_ratio`, hit/miss/evict), queue depths, and per-codec counters; ship Grafana dashboards (`deploy/grafana`); add alerts on cache thrash and pool exhaustion.
**DoD:** a dashboard shows cache pressure and queue depth; an alert fires on sustained cache miss rate.
**Verification:** Prometheus scrape + a synthetic load that trips the alert.

### Phase D — exit criteria
- Codec benchmark published; deploy gate enforced.
- 2-node containerised cluster verified behind HAProxy.
- Full CI suite (Python + JS + conformance + memory) green on the corpus.
- Observability dashboards + alerts live.

---

## Milestone Summary

| Phase | Focus | Blocking defects closed | Exit gate |
|---|---|---|---|
| **A** | Hotfixes | C-1..C-5, H-1..H-13, L-8, L-13 | Data-integrity regression tests green; release note |
| **B** | Refactor & stabilise | M-1..M-16, H-8/H-9, conformance | No sync-in-async; all claims atomic; conformance suite green |
| **C** | Viewer | H-10..H-12, M-17..M-19, L-6..L-12 | 60 fps scroll; bounded client heap; JS suites green |
| **D** | Modernise & scale | L-17, deployment | Codec gate; container cluster; CI + observability |

---

## Global Definition of Done (applies to every milestone)

1. **Grounded change** — the fix references the issue ID and touches only the identified code path (plus its tests).
2. **Regression test** — a failing-before / passing-after test exists for every Critical/High fix.
3. **No new blocking I/O in `async def`** — verified by grep + review.
4. **No new unbounded cache/thread/queue** — verified by review.
5. **PHI-free logging** — new log lines pass the `decode_context` discipline (no demographics, no filesystem paths).
6. **Tenant isolation intact** — any new query is tenant-scoped by default; a cross-tenant test covers it.
7. **Clinical-data integrity** — any write path is stage-then-publish or atomic; a fault-injection test proves rollback.
8. **Documentation** — `README` / `RUN_AND_BUILD_GUIDE` updated for any config or route change.
