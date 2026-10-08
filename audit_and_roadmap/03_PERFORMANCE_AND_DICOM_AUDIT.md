# 03 — Performance & DICOM Conformance Audit

> Measured/derived from the shipping code on the verified baseline (`pydicom 3.0.2`, `pynetdicom 3.0.4`, `numpy 2.5.1`, `imagecodecs 2026.6.26`, `redis 8.1.0`). Where a number is an estimate it is labelled; where it is read from code it is cited.

---

## 1. Transfer Syntax & Transcoding Evaluation

### 1.1 Codec availability (ground truth)

`compression_service.codec_status()` on this host:

| Method | Transfer syntax UID | Available | Provider |
|---|---|---|---|
| RLE | `1.2.840.10008.1.2.5` | ✅ | pydicom (built-in) |
| JPEG-LS | `1.2.840.10008.1.2.4.80` | ✅ | `pyjpegls` |
| JPEG2000 | `1.2.840.10008.1.2.4.90` | ✅ | `pylibjpeg-openjpeg` / `imagecodecs` |
| HTJ2K | `1.2.840.10008.1.2.4.201` | ✅ | `imagecodecs` (bundled openjph) |

**Lossless compression path** (`compression_service.compress_file`, L182–245) is well-designed:
* Skips re-encoding when `current_ts == target_uid` (idempotent).
* Falls back to RLE when the configured encoder is unavailable (`encoder_available`), and raises only if RLE itself is unavailable.
* HTJ2K on pydicom 3.0.x is handled explicitly via `_compress_htj2k` (imagecodecs `htj2k_encode`, reversible), with an RLE fallback if the encode fails mid-way — and crucially `_compress_htj2k` only mutates `ds` after **every** frame encodes, so a failure leaves the dataset intact.
* Atomic replace: writes `*.tmp` then `os.replace` — a crash never leaves a half-written instance.

**Lossy compression path is unsafe** — see `02 §C-2/C-3/C-4`. It re-renders pixels through the display pipeline (irreversible) and can emit a baseline-JPEG codestream labelled JPEG-LS. **Do not enable lossy rules until C-2/C-3/C-4 are fixed.**

### 1.2 Decode / transcode paths on the read side

| Path | Where | Notes |
|---|---|---|
| Raw codestream passthrough | `wado_rs_bulk_service.extract_frame_bytes` (L109–188) | `pydicom.encaps.generate_frames` → one frame at a time (pydicom 3.x); correct MIME per syntax family (`.4.90/91/201/202` → `image/jp2`, `.4.50/51/57/70` → `image/jpeg`, else `application/octet-stream`). |
| Server-side decode to raw LE | `decode_frames_to_raw` (L191–270) | Cache-first (L1 + Redis), decodes only missing frames, falls back to the raw codestream on decode failure (never hard-fails). |
| Full-stack volume materialisation | `volume_service.iter_volume_bytes` (L559–620) | Streams one slice per chunk; validates shape uniformity; optional float32 HU rescale. |

**Transcoding throughput characteristics:**
* Decode cost is dominated by the codec. The code comments cite a measured **~2.3 s for a 3072×2560 JPEG2000 frame**; this is why `DICOM_FRAME_CACHE_MB` was raised from a 16-entry cap (~8 MB) to a 256 MB byte budget.
* The **frame cache key is mode-qualified** (`paxs:frame:{sop}:{resolved}:{mode}:{mp|sp}`) so a `?transcode=1` request can never be served a native codestream body (a previously-fixed poisoning bug — see `dicomweb.py` L858–863).
* **Compression is CPU-bound and scales ~linearly with worker processes** (`worker.run_standalone`); the batch claim is atomic (`_claim_jobs`, guarded UPDATE + rowcount).
* **Scaling risk:** `DEFAULT_COMPRESSION=JPEG-LS` in `config.py` but `RLE` in `.env.example`; and the worker only compresses `sop_class_uid != GSPS`. A modality that sends **already-compressed** JPEG-LS will be transcoded again to the configured target (lossless→lossless, acceptable but wasteful). Already-**lossy** JPEG-LS arriving from a modality is re-encoded losslessly as JPEG-LS/JPEG2000 — which does **not** reduce its (already-degraded) quality but does consume CPU; the lossless worker should skip instances whose source syntax is already `.is_compressed`.

### 1.3 Transfer-syntax conformance gaps

* **No `SpecificCharacterSet` persistence or re-encoding guard.** `parser.py:parse_metadata` never reads `SpecificCharacterSet`. Any path that **re-saves** a dataset (compression, split/merge, anonymize, lossy) relies on pydicom's in-memory decoding and re-encoding; a dataset whose names contain non-ASCII but whose `SpecificCharacterSet` is absent/incorrect can be mojibake'd or fail to save. Store the charset with the instance and validate on re-save.
* **Encapsulated-frame extraction assumes `generate_frames` can walk the pixel data.** On failure it logs `decode_context` and falls back to `pixel_array` (good), but the fallback for a syntax the encapsulation layer cannot parse is `pixel_array` — which may itself fail for the same syntax; the two failure modes are conflated in the log.
* **No deflated-explicit-VR handling on the STOW path.** `dataset_from_bytes` (`parser.py` L112–116) uses `pydicom.dcmread(io.BytesIO(data), force=force)` — `force=False` for `application/dicom` parts (`dicomweb.py` L994). A part without the 128-byte DICM preamble (some vendors) fails to parse and is silently counted as failed.

---

## 2. Storage Optimisation

### 2.1 Object layout
`{StudyInstanceUID}/{SeriesInstanceUID}/{SOPInstanceUID}.dcm` + `.thumbnails/{SOPInstanceUID}.png` (`storage_service.instance_key` / `thumbnail_key`). Keys are POSIX-relative and validated (`validate_component`, C05) — no traversal, no absolute paths.

### 2.2 Strengths
* **Staging + atomic publish** avoids partial objects.
* **Multi-storage** (local / S3) with per-study backend affinity; `move_study_to_backend` rolls back already-moved objects if the DB commit fails (L569–577) — a correct all-or-nothing move.
* **Lifecycle tiering** to a cold `.archive/` prefix; **DR replication** to an off-site S3 target.
* **Cached usage counters** (`cached_storage_usage_bytes`) avoid an O(objects) tree walk on every `/stats` (Redis-first, in-process fallback).

### 2.3 Weaknesses
* **`sha256_of` re-reads the whole object** on remote (S3) backends (`storage_service.sha256_of` L216–219) — used by the audit trail on every write.
* **S3 `usage_bytes` lists the entire bucket synchronously** (`storage/s3.py` L134–140).
* **Replication buffers whole objects in RAM** (`02 §M-11`).
* **Temp-file leak on S3 auto-routing** (`02 §H-2`).
* **Legacy-path bypass** while `STORAGE_ALLOW_LEGACY_PATHS=true` (`02 §M-8`).

---

## 3. Database Query Latency & Indexing

### 3.1 Index coverage (from `app/db/models.py`)

Well-covered hot paths:

| Query | Index |
|---|---|
| QIDO studies by tenant, newest first | `ix_studies_tenant_date (tenant_id, study_date)`, `ix_studies_tenant_created` |
| Lifecycle sweeps by state + age | `ix_studies_state_created (lifecycle_state, created_at)` |
| Viewer series list per study | `ix_series_study_number (study_id, series_number)` |
| Viewer instance list / WADO-RS per series | `ix_instances_series_number (series_id, instance_number)` |
| PIX lookup (tenant, AA, identifier) | `ix_patient_identities_tenant_aa_identifier` (unique) |
| Emergency triage queue | `ix_triage_reviewed_severity (reviewed, severity)` |
| Session lookups by token hash | `sessions.refresh_token_hash` (unique, indexed) |

### 3.2 Latency risks

* **Keyset pagination is implemented** for QIDO studies (`_keyset_cursor` / `_keyset_predicate`, `dicomweb.py` L88–124) to avoid `OFFSET` re-scans past ~1000 rows — good. But QIDO series/instances still use `OFFSET` (`qido_series` L372–376, `qido_instances` L399–405); deep pages re-scan.
* **`lifecycle_service.evaluate_and_run`** loads every `Study` then lazy-loads `series_list`/`instances` per study → O(studies) queries with N+1 (`02 §H-15`).
* **`lossy_compression_service.evaluate_and_run_lossy_compression`** loads every non-baseline `Instance` in one query (`02 §H-14`).
* **`study_aggregates`** groups by `Series.id` with a join to `Instance` for the whole page of studies — acceptable, but on a page of 100 studies with 200 instances each it aggregates 20k rows per page load.
* **`dashboard.stats`** (`_count` helper, L245) issues separate COUNT queries per model — on MySQL/InnoDB these are index scans; on a large archive this is the dashboard's slowest endpoint (mitigated by `get_read_db` replica routing and the `/stats` usage cache).
* **Sync DB access inside `async def`** (`config.py`, `replication.py`, `settings_service`) stalls the event loop for a full round trip (`02 §M-15/H-13`).

### 3.3 Connection-pool behaviour

* **Async engine = `NullPool`** — one connection per request, never reused across event loops (deliberate, because the DICOM receiver runs `asyncio.run()` per instance and tests spin loops). Correctness-safe but means **no connection reuse on the hot API path**; at high request rates this is more connect/disconnect churn than a bounded pool. The comment acknowledges the trade-off; on MySQL with a fast local socket this is acceptable, but it is the first thing to revisit under load.
* **Sync engine = bounded `QueuePool`** (`DB_POOL_SIZE=5`, `DB_MAX_OVERFLOW=10`, pre-ping, recycle 1800 s) shared by the receiver **and** the worker. With `DICOM_MAX_ASSOCIATIONS=16`, the receiver can want more than 15 sync connections under concurrent C-STORE; **connection-pool exhaustion is possible under a burst of simultaneous C-STORE + a running worker batch**. Raise `DB_POOL_SIZE` or give the receiver its own pool.
* **The DIMSE store path opens an async session per instance** via `asyncio.run(_index_async(...))` — a fresh event loop per image (`receiver.py` L108–119). Loop creation/teardown per instance is measurable overhead at high ingest rates; batch per association instead.

---

## 4. Memory Profile

### 4.1 Bulk ingestion

| Stage | Allocation | Notes |
|---|---|---|
| C-STORE receive | Full dataset in memory (pynetdicom) | Unavoidable per-instance. |
| `stage_dataset_with_quota` | `ds.save_as(BytesIO)` → whole object duplicated in RAM | `storage_service.py` L403–405. A 2 GB multi-frame instance ⇒ ≥2 GB RSS during staging. |
| `parse_metadata` | Flat dict (small) | Fine. |
| Indexing | ORM objects + audit row + compression job | Fine. |
| Thumbnail (worker) | Full `pixel_array` decode + PIL | `worker._process_job` L169–174; one instance at a time, `WORKER_BATCH_SIZE=15` sequential. |
| Compression (worker) | Full `pixel_array` + encoded frames | CPU/RAM bound; sequential within a worker. |

**Verdict:** per-instance memory is roughly `2× object size` during staging, then bounded by the worker's one-at-a-time processing. The risk is **large multi-frame instances** (whole-object buffering in staging, replication, and the DICOMweb client) — see `02 §M-9/M-11/M-13`. There is **no streaming write path** for staging; adding one (write the encoded dataset to the staged file directly rather than to a `BytesIO`) halves peak ingest memory.

### 4.2 Multi-series viewing (server side)

* **Volume L1 cache** (`_VOL_CACHE`) — byte-budgeted (`VOLUME_CACHE_BUDGET_MB=1024`), LRU, float32 HU. A 512×512×500 CT is ~500 MB; the budget holds ~2 such volumes. Correctly never evicts the just-inserted entry.
* **Frame cache** (`_FRAME_CACHE`) — byte-budgeted (`DICOM_FRAME_CACHE_MB=256`), LRU, raw decoded bytes. Never evicts the just-inserted entry.
* **Tile cache** (`_TILE_CACHE`) — **count-bounded at 256 entries** (not bytes). A 512×512 PNG tile is small, so 256 entries is modest, but it is the one L1 cache not byte-bounded; a hostile mix of large-window renders could exceed expectations.
* **In-flight coalescing** (`_IN_FLIGHT_VOLUMES`, `_TILE_INFLIGHT`) — bounds duplicate decode work; waiters give up after 180 s / 20 s and decode themselves (no permanent wedge). `_TILE_INFLIGHT_MAX=64`.
* **Bundle metadata cache** — 32 entries, keyed by series only (see `02 §M-7`).

**Verdict:** the server-side caches are the strongest part of the memory design. The **default `VOLUME_CACHE_BUDGET_MB=1024` + `HYBRID_MAX_SERVER_MPR_SESSIONS=20`** means 20 admitted sessions share a 1 GB budget (~2 volumes) — the cache will thrash under the advertised concurrency, which is why `validate_memory_budgets` deliberately no longer warns (the byte budget is authoritative). Operators should size `VOLUME_CACHE_BUDGET_MB` to `sessions × volume_size` for their modality mix.

### 4.3 Multi-series viewing (client side) — the real memory risk

The frontend holds **several full volumes** with no byte budget (`02 §H-10/H-11/M-17`):

* `state.volCache` — **count-bounded at 3**, each a Float32 volume (~500 MB for a 512×512×500 CT) ⇒ up to **~1.5 GB**, and it survives `closeMpr` for the page lifetime.
* `state.fusion.overlayVol` — a **fourth** decoded volume, **not nulled** on close (`02 §L-8`).
* `state.serverFrames[plane]` — **unbounded** map of decoded `Image` objects + object URLs during scrolling (`02 §H-10`).
* `thumbCache` — up to 200 unrevoked object URLs (`02 §H-12`).

**Verdict:** the browser, not the server, is the memory bottleneck for a multi-series session. On a memory-constrained diagnostic workstation this is an OOM/crash risk. The server-side budgets are well-designed; the client-side ones are not.

---

## 5. Concurrency & Event-Loop Health

* **Default executor widened** to `IO_THREAD_POOL_WORKERS=64` at startup (`main.py` L483–485) — appropriate for a CT/MRI viewer firing dozens of concurrent frame requests. Each `asyncio.to_thread` decode is CPU-bound though; 64 concurrent pylibjpeg decodes can saturate all cores and starve the API. Consider separating a **CPU-bound decode pool** from the I/O pool.
* **Blocking I/O inventory** (`02 §H-13`): `nodes.ping_node`, `config.py`, `split_merge_service`, `scripts.py`, `settings_service` — each stalls the single event loop.
* **`asyncio.run()` per instance** on the C-STORE and DICOMweb-client ingest paths creates and destroys an event loop per object.
* **Worker/triage** use atomic claims + a Redis lock — correct for multi-worker.
* **Routing queue claim is non-atomic** (`02 §M-1`) — the one claim that was not migrated to the guarded-UPDATE pattern.
* **Background thread fan-out** is unbounded in three places: `hl7.py` (per connection), `warm_service.py` (per study), `scripting_service.py` (per event) — each a latent thread-exhaustion vector.

---

## 6. DICOM Conformance Assessment

| Area | Status | Evidence |
|---|---|---|
| **DIMSE service classes** | ✅ Broad | C-ECHO, C-STORE, C-FIND (incl. MWL), C-GET, C-MOVE, MPPS (N-CREATE/N-SET), Storage Commitment (N-ACTION + async N-EVENT-REPORT), IAN. |
| **Presentation contexts** | ✅ Correct | Storage contexts declared with explicit SCP role for C-GET sub-operations; Q/R + MWL + MPPS + Commit + Verification added (`receiver.py` L638–659). |
| **DICOMweb** | ✅ Broad | QIDO-RS (3 levels), WADO-RS (study/series/instance/metadata/rendered/thumbnail/frames/bulk), WADO-URI, STOW-RS (streamed multipart). |
| **Mandatory Type 1 tags** | ❌ Not validated | Missing UIDs collapse to `"UNKNOWN"` (`02 §M-15`). |
| **`SpecificCharacterSet`** | ⚠️ Not persisted | `parser.py` never reads it; re-save paths rely on pydicom's in-memory state (`03 §1.3`). |
| **De-identification (PS3.15 Basic)** | ⚠️ Incomplete | Private tags in sequences survive; de-identification markers absent (`02 §M-14`). |
| **Lossy transcoding** | ❌ Non-conformant | Baseline-JPEG labelled JPEG-LS; pixel data destroyed (`02 §C-2/C-3`). |
| **C-GET sub-operation status** | ❌ Wrong | Returns Success on local ingest failure (`02 §H-9`). |
| **ATNA / audit trail** | ⚠️ Partial | An application-level `audit_log` (STORED/RETRIEVED/COMPRESSED/DELETED/SHARED with SHA-256) + `auth_audit_log` exist, but there is **no DICOM ATNA (RFC 3881 / DICOM PS3.15 A.5) Audit Message** emitted to an Audit Record Repository. Hospital compliance regimes (IHE ATNA) require the XML audit messages, not a private table. |
| **Pixel decompression degradation** | ⚠️ Lossy engine | Only the (defective) lossy path degrades pixels; the lossless path is reversible. |
| **Transfer syntax negotiation** | ✅ | `wants_full_dicom` / `viewer_mode` / `transcode` handling in `wado_frame` (L783–844) correctly distinguishes raw codestream vs decoded-raw vs full-instance. |

---

## 7. Summary of Performance Priorities

1. **Fix the memory-safety defects before performance work** — C-2/C-3 (data destruction) and H-10/H-12 (client leaks) are correctness, not tuning.
2. **Batch DIMSE sends** (H-8) — the single biggest transfer-throughput win (500 handshakes → 1 per study).
3. **Stream instead of buffer** — staging (4.1), S3 read/hash/usage (M-9), replication (M-11), DICOMweb client (M-13). Halves peak ingest memory and removes the OOM vector.
4. **Move blocking calls off the event loop** (H-13) — the single biggest API-latency win under concurrency.
5. **Batch the O(N) sweeps** (H-14/H-15) with `yield_per`.
6. **Separate CPU decode pool from the I/O pool** and consider raising `DB_POOL_SIZE` for the sync engine (3.3).
7. **Byte-bound the tile cache** and **tenant-scope the bundle cache** (M-7).
