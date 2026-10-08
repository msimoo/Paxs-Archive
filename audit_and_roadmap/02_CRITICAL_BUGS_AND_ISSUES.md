# 02 — Critical Bugs & Issues Register

> Every entry is grounded in the shipping source: exact file path, function, line numbers, the offending code, impact, and a concrete fix. Severity reflects **clinical-data-integrity and availability** risk first, then security, then performance.
>
> Legend: **C** = Critical · **H** = High · **M** = Medium · **L** = Low. IDs are stable for the roadmap in `04`.

---

## CRITICAL

### C-1 — Study split/merge rewrites the DICOM header, then overwrites it with the original bytes
**Files:** `app/services/split_merge_service.py` — `_rewrite_instance_study_uid` (L242–261), callers in `split_series_to_new_study` (L101–120) and `merge_studies` (L200–219).

**Code:**
```python
# _rewrite_instance_study_uid (L251-258)
ds = storage_service.read_dataset(file_key, backend_name=backend)
ds.StudyInstanceUID = new_study_uid
ds.PatientID = new_patient_id
if new_patient_name:
    ds.PatientName = new_patient_name
storage_service.save_dataset(ds, backend_name=backend)   # writes to key derived from NEW StudyInstanceUID
...
# caller (L109-120)
new_key = storage_service.instance_key(new_study_uid, s.series_instance_uid, inst.sop_instance_uid)
if inst.file_path != new_key:
    old_bytes = storage_service.read_bytes(inst.file_path, ...)   # ORIGINAL, un-rewritten bytes
    storage_service.save_bytes(new_key, old_bytes, ...)           # overwrites the rewrite at new_key
    storage_service.delete_file(inst.file_path, ...)
    inst.file_path = new_key
```

**Root cause:** `save_dataset` derives its key from the *rewritten* `StudyInstanceUID` (i.e. `new_key`), so it already lands the corrected object there. The caller then re-reads the **old** path and writes the original bytes to the same `new_key`, discarding the header rewrite. `_rewrite_instance_study_uid` also swallows every exception (L259–260), so a failed rewrite proceeds silently.

**Impact:** After a split/merge the DB moves the series to the new study, but the stored object still carries the **old** `StudyInstanceUID`/`PatientID`. Any subsequent re-index, DICOMweb serve, C-STORE forward, or DICOMDIR export exposes the wrong study/patient identity — a PHI-misattribution and clinical-safety defect. The object also becomes un-findable by its own header UID.

**Fix:**
```python
def _rewrite_instance_to(ds, new_study_uid, new_patient_id, new_patient_name) -> bytes:
    ds.StudyInstanceUID = new_study_uid
    ds.PatientID = new_patient_id
    if new_patient_name:
        ds.PatientName = new_patient_name
    buf = io.BytesIO(); ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()
# caller: write the REWRITTEN bytes to new_key; delete old key; never re-read old_bytes.
```
Propagate the exception (do not catch-and-continue), and add a verification read-back of `StudyInstanceUID` before deleting the source object.

---

### C-2 — "Lossy compression" re-renders pixels through the display pipeline and destroys diagnostic data

> **REMEDIATED (2026-10-08).** `lossy_compression_service.transcode_dataset_lossy` now re-encodes the dataset's **native** `pixel_array` via a real DICOM codec (pydicom `Dataset.compress` → `pyjpegls`/`openjpeg`); `imaging.dataset_to_image` is gone from the pipeline. Bit depth/geometry are asserted unchanged after encoding, the codestream is decoded once to prove readability, and the object is staged via `StagedObject` and promoted atomically. Gated by `LOSSY_COMPRESSION_ENABLED=false`. Regression: `tests/test_lossy_compression_safety.py::test_16bit_slice_preserves_bit_depth_and_voxel_distribution`.
**File:** `app/services/lossy_compression_service.py` — `transcode_dataset_lossy` (L31–66).

**Code:**
```python
img = dataset_to_image(ds)                 # imaging.py: modality LUT + VOI windowing + 1-99% stretch → uint8
buf = io.BytesIO()
if img.mode != "RGB":
    img = img.convert("RGB")
img.save(buf, format="JPEG", quality=quality_factor)
ds.PixelData = encapsulate([compressed_bytes])
ds.PhotometricInterpretation = "YBR_FULL_422" if target_ts_uid == JPEG_BASELINE_LOSSY else "RGB"
ds.SamplesPerPixel = 3
ds.BitsAllocated = 8
ds.BitsStored = 8
ds.Rows = img.height
ds.Columns = img.width
```

**Root cause:** `dataset_to_image` (`app/services/imaging.py:40–79`) is a **display** renderer: it applies the modality LUT, VOI windowing, a percentile clip (`_normalize_u8`) and `MONOCHROME1` inversion. The "compression" path therefore:
* discards the original 16-bit grayscale values (windowed to 8-bit),
* converts `SamplesPerPixel 1 → 3`,
* can change `Rows`/`Columns` (if a thumbnail was rendered).

This is **irreversible destruction of diagnostic data**, not compression.

**Impact:** Irreversible loss of diagnostic information on any study matched by a lossy rule. Unrecoverable from the archive.

**Fix:** transcode the **actual** pixel array with a real codec preserving bit depth and photometric interpretation (`pydicom.Dataset.compress` / `pylibjpeg` / GDCM for JPEG baseline; `pyjpegls` for JPEG-LS lossy). Never route through `imaging.dataset_to_image`. Add a pre-flight assertion that `new_ds.BitsAllocated == orig.BitsAllocated` unless the rule explicitly opts into bit-depth reduction.

---

### C-3 — Lossy JPEG-LS rule emits a baseline-JPEG codestream mislabeled as JPEG-LS

> **REMEDIATED (2026-10-08).** The encoder is resolved **from** `target_ts_uid` and the output transfer syntax is asserted equal to it before anything is written; `Dataset.compress(generate_instance_uid=False)` is used so the SOP Instance UID (and therefore the storage key) is preserved for an in-place re-encode. JPEG Baseline (`.50`) has no pydicom encoder and is rejected outright rather than hand-rolled. Regression: `tests/test_lossy_compression_safety.py::test_unencodable_target_aborts_instead_of_mislabelling`.
**File:** `app/services/lossy_compression_service.py` — same function (L49–64).

**Code:** `img.save(buf, format="JPEG", ...)` always writes **baseline JPEG**, but L64 sets `ds.file_meta.TransferSyntaxUID = target_ts_uid`. For `JPEG_LS_LOSSY` (`1.2.840.10008.1.2.4.81`) the stored object is a baseline-JPEG bitstream declared as JPEG-LS.

**Impact:** The instance is **non-conformant and undecodable** by any spec-compliant reader (the decoder selected from the transfer syntax cannot parse the codestream). Combined with C-2 the original is already gone.

**Fix:** branch on `target_ts_uid` and encode with the matching codec, or restrict the rule engine's allowed transfer syntaxes to those actually implemented (and validated at rule-creation time).

---

### C-4 — Lossy engine re-compresses already-lossy data (generational quality loss)

> **REMEDIATED (2026-10-08).** The candidate query excludes every known lossy syntax (`.50/.51/.81/.91/.93`) and `is_eligible_source()` admits only uncompressed or verified-lossless sources — enforced again per-instance against the file's own `file_meta`. Regression: `tests/test_lossy_compression_safety.py::test_already_lossy_instance_is_skipped_and_unchanged` (asserts the object's SHA-256 is unchanged).
**File:** `app/services/lossy_compression_service.py` — `evaluate_and_run_lossy_compression` (L137).

**Code:** `.where(Instance.transfer_syntax_uid != JPEG_BASELINE_LOSSY)`

Only the exact baseline UID is excluded. JPEG-LS lossy (`.81`), JPEG2000 lossy (`.91`) and third-party JPEG baseline variants (`.51`) are still selected and re-encoded.

**Impact:** Generational lossy re-encoding of already-lossy studies — cumulative diagnostic degradation, again irreversible.

**Fix:** exclude **every** lossy and every `.is_compressed` transfer syntax from eligibility; only raw/uncompressed (or verified-lossless) sources may be lossily transcoded.

---

### C-5 — FHIR `/Patient` search and read are not tenant-scoped (cross-tenant PHI disclosure)

> **REMEDIATED (2026-10-08).** `app/api/v1/fhir.py` now applies a shared `_tenant_predicate(column, tenant_id, include_all)` (identical semantics to `query_service.apply_study_tenant_scope`): `search_patient` injects it into its conditions (so the page query and the total-count agree) and `read_patient` verifies the fetched row's tenant, returning **404** for a cross-tenant patient. `search_imagingstudy` was switched to the same helper to prevent drift. Regression: `tests/test_fhir_tenant_isolation.py` (6 tests — search, name search, identifier probing, direct id read, the symmetric tenant-B case, and an ImagingStudy guard).
**File:** `app/api/v1/fhir.py` — `search_patient` (L125–157) and `read_patient` (L160–170).

**Code:**
```python
@router.get("/Patient")
async def search_patient(db=Depends(get_db), _id=..., identifier=..., name=..., _user=Depends(get_current_user_any)):
    stmt = select(Patient)                     # NO tenant filter
    ...
    patients = (await db.execute(stmt.limit(_count))).scalars().all()
```
```python
@router.get("/Patient/{patient_id}")
async def read_patient(patient_id, db=Depends(get_db), _user=Depends(get_current_user_any)):
    patient = (await db.execute(select(Patient).where(Patient.id == pid))).scalar_one_or_none()   # NO tenant filter
```

Contrast the sibling `search_imagingstudy` (L189–193), which **does** apply `tenant_scope(_user)`, and `read_imagingstudy` (L249), which calls `require_study_access`.

**Impact:** Any authenticated user — including a tenant-A physician — can enumerate and read **every institution's** patient demographics (name, MRN, DOB, sex) via `/api/v1/.../fhir/Patient?name=…` or `/Patient/{id}`. This defeats the VNA isolation that the rest of the codebase enforces (C04). PHI disclosure across tenants.

**Fix:** apply `tenant_scope(_user)` to `search_patient` (add the `Patient.tenant_id` predicate to `conds`, matching `search_imagingstudy`) and route `read_patient` through a tenant-scoped check (mirror `require_study_access` with a `Patient`-level equivalent). Add a regression test asserting a tenant-A caller cannot read a tenant-B patient.

---

## HIGH

### H-1 — Prior-study prefetch calls a non-existent function; all C-MOVE prefetch silently fails

> **REMEDIATED (2026-10-08).** `prefetch_service.evaluate_and_trigger_prefetch` now calls `sender.send_move` via `functools.partial` with the correct mapping — `dest_ae_title=settings.DICOM_AE_TITLE`, `our_ae_title=settings.DICOM_AE_TITLE`, and a STUDY-level query carrying the prior's `StudyInstanceUID` (`_study_move_query`). A non-`ok` result is now logged instead of swallowed. `rule.max_prior_age_days` is applied via `_within_max_age` (also closes L-13). Regression: `tests/test_prefetch_and_routing.py::test_prefetch_invokes_send_move` and `::test_prefetch_applies_max_prior_age`.
**File:** `app/services/prefetch_service.py` — `evaluate_and_trigger_prefetch` (L149–157).

**Code:**
```python
cmove_ok = await loop.run_in_executor(
    None, sender.c_move_study,
    rule.source_node.host, rule.source_node.port, rule.source_node.ae_title,
    st_uid, settings.DICOM_AE_TITLE,
)
```
`app/dicom/sender.py` defines `send_move` (L238) — there is **no `c_move_study`** (verified by repo-wide grep). The `AttributeError` is caught at L160 and logged as a warning.

**Impact:** Prior fetching silently never happens; radiologists read without priors while the job row is recorded `COMPLETED`/`FAILED` with no real signal. Also, `send_move`'s signature takes `dest_ae_title` as the 4th positional arg, so a naive rename would still pass `st_uid` in the wrong slot.

**Fix:** call `sender.send_move(host, port, ae_title, dest_ae_title=settings.DICOM_AE_TITLE, study_uid=st_uid, ...)` with the correct keyword arguments, or implement `c_move_study`. Add an integration test that asserts the C-MOVE is actually attempted.

---

### H-2 — Auto-routing leaks a temp file per S3-backed instance

> **REMEDIATED (2026-10-08).** The materialized temp file is now removed in a `finally` block (`os.unlink`, failures logged) that wraps the send, so it is cleaned up even when `send_file` raises. Regression: `tests/test_prefetch_and_routing.py::test_routing_s3_temp_files_cleaned` and `::test_routing_temp_cleaned_even_when_send_fails`.
**File:** `app/services/routing_service.py` — `dispatch_study_to_node_sync` (L141–160).

**Code:**
```python
data = storage_service.read_bytes(inst.file_path, backend_name=bk)
with tempfile.NamedTemporaryFile(suffix=".dcm", delete=False) as tmp:
    tmp.write(data)
    file_target = tmp.name
# ... send_file(...) ... file_target never unlinked
```
`delete=False` and no `os.unlink` — every routed S3 instance leaves a full DICOM file in the temp dir, and the whole object is also buffered in RAM.

**Impact:** Unbounded temp-disk growth and memory amplification proportional to routed volume; eventually fills the system temp volume.

**Fix:** `try/finally: os.unlink(file_target)` (mirroring `sender.send_keys` L749–756), and stream via a `read_stream`/`upload_fileobj` path rather than `read_bytes`.

---

### H-3 — HL7 MLLP listener: unbounded thread-per-connection (trivial DoS)
**File:** `app/dicom/hl7.py` — `HL7Listener.run` (L731–744).

**Code:**
```python
conn, addr = self._sock.accept()
threading.Thread(target=self._handle_connection, args=(conn, addr), daemon=True).start()
```
No concurrency cap and no peer allow-list on the MLLP port.

**Impact:** A few thousand idle TCP connections exhaust threads/memory and take the process down (HL7 is off by default, but when enabled it is typically on a hospital LAN).

**Fix:** bound concurrency with a semaphore/`ThreadPoolExecutor`; reject beyond a max; add an optional peer allow-list; set a per-connection socket timeout.

---

### H-4 — HL7 MLLP listener: unbounded buffer growth
**File:** `app/dicom/hl7.py` — `_handle_connection` (L748–760).

**Code:**
```python
buffer = b""
while not self._stop.is_set():
    chunk = conn.recv(4096)
    if not chunk: break
    buffer += chunk                       # O(n²) copy, no cap
    while b"\x1c\x0d" in buffer:
        msg, _, buffer = buffer.partition(b"\x1c\x0d")
        _handle_message(msg)
```
A client that never sends the `0x1C 0x0D` terminator grows `buffer` without bound.

**Impact:** Per-connection memory exhaustion (DoS).

**Fix:** cap `len(buffer)` at an `HL7_MAX_MESSAGE_BYTES` (e.g. 4 MB), drop the connection when exceeded, and use a `bytearray`.

---

### H-5 — Remote WADO-RS retrieve buffers the whole body before enforcing the size cap
**File:** `app/services/dicomweb_client.py` — `wado_retrieve` (L245–256).

**Code:**
```python
resp = client.get(f"/studies/{urllib.parse.quote(study_uid, safe='')}")
if resp.status_code not in (200, 206): raise ConnectionError(...)
if len(resp.content) > settings.DICOMWEB_CLIENT_MAX_BYTES:   # cap checked AFTER full buffer
    raise ConnectionError(...)
```
`resp.content` materializes the entire response first.

**Impact:** A hostile or misconfigured peer OOMs the PAXS process before the `DICOMWEB_CLIENT_MAX_BYTES` (4 GiB) guard fires.

**Fix:** `with client.stream("GET", ...) as r:` accumulate chunks and abort once the cap is exceeded.

---

### H-6 — Lifecycle tiering leaves a study split across tiers with an inconsistent state flag
**File:** `app/services/lifecycle_service.py` — `_tier_study` (L108–145).

**Code:**
```python
if storage_service.move_object(key, dest, backend_name=inst.storage_backend):
    inst.file_path = dest
    moved += 1
if moved:
    if to_archive:
        study.lifecycle_state = "ARCHIVED"
```
`moved > 0` flips the state even if some objects failed to move. `restore_study` (L122–124) only reverses keys that carry the archive prefix, so the failed hot objects are never brought back.

**Impact:** A study is permanently split across hot and cold tiers with a state flag that lies; subsequent reads 404 on the missing tier.

**Fix:** all-or-nothing — track moved keys and roll back on the first failure, or set the state only when `moved == total`.

---

### H-7 — Lifecycle purge deletes metadata even when object deletion fails (orphaned PHI)
**File:** `app/services/lifecycle_service.py` — `_purge_study` (L148–169) + `purge_study` (L217–226).

**Code:**
```python
if storage_service.delete_file(key, backend_name=inst.storage_backend):
    deleted += 1
...
session.delete(study); session.commit()
```
`delete_file` returns `False` on failure (S3 delete swallows exceptions, `storage/s3.py:90–96`) but the row is deleted regardless.

**Impact:** PHI objects are left on storage **forever**, invisible to the archive — a data-retention/compliance violation and unbounded storage growth.

**Fix:** refuse to delete the row when any object deletion fails; queue failures for retry and record them for an operator.

---

### H-8 — DIMSE sender opens one association per instance with no timeouts
**File:** `app/dicom/sender.py` — `send_file` (L50–77), consumed by `send_keys` (L728–769) and `send_directory` (L620–637).

**Code:**
```python
ae = AE(ae_title=our_ae_title)
assoc = ae.associate(host, port, ae_title=ae_title, tls_args=_tls_args(host))
```
A fresh `AE` + association per instance; no `ae.acse_timeout` / `ae.dimse_timeout` (pynetdicom default 30 s each).

**Impact:** A 500-instance study = 500 TCP+TLS handshakes; an unreachable node stalls ~30 s **per instance**. Forwarding and routing throughput collapse; worker threads pin on dead peers.

**Fix:** open one association per study and `send_c_store` all instances over it; set explicit ACSE/DIMSE timeouts (as `commit_sender.py:66–67,203–204` already does); surface sub-operation warnings.

---

### H-9 — C-GET reports Success for instances whose local ingest failed
**File:** `app/dicom/sender.py` — `send_get._on_store` (L314–326).

**Code:**
```python
def _on_store(event) -> int:
    ds = event.dataset
    if ds is not None:
        if on_received is not None:
            try:
                on_received(ds)
            except Exception:
                log.exception("C-GET on_received handler failed")
    return 0x0000        # always Success
```
When `on_received` (the local ingest) fails, the remote is still told the sub-operation succeeded.

**Impact:** The remote archive marks the transfer complete and may purge its copy → **silent data loss** with a false protocol-level success.

**Fix:** return a failure status (`0xA700`/`0xC000` class) when local ingest fails.

---

### H-10 — Viewer: unbounded per-slice `Image` + object-URL cache during scrolling

> **REMEDIATED (2026-10-08).** New `evictServerFrames(plane, center)` in `paxs-mpr.js` enforces a sliding window of `SERVER_FRAME_KEEP_RADIUS = 10` slices either side of the active one; out-of-window entries are detached and their `URL.revokeObjectURL` called. It runs at the top of every `prefetchServerRange` call. Regression: `.workbuddy-ai/tmp/viewer-memory-test.js` §1.
**File:** `app/static/viewer/paxs-mpr.js` — `prefetchServerRange()` (L2630–2673, esp. L2657–2670).

**Code:**
```js
var objUrl = URL.createObjectURL(partial);
var img = new Image();
img._paxsObjectUrl = objUrl;
img.src = objUrl;
state.serverFrames[plane][target] = img;      // keyed by slice index, never evicted on scroll
```
`state.serverFrames[plane]` is only cleared in `releaseServerResources()` (L1190) — i.e. on workspace close/series switch. `prefetchServerRange` runs on **every wheel tick** (L3094) for up to 7 slices.

**Impact:** Scrolling a 500-slice CT retains hundreds of decoded bitmaps + object URLs → hundreds of MB of browser heap and GC pressure (the documented `serverCache` prev-frame cap does **not** cover `serverFrames`).

**Fix:** on each prefetch, evict entries outside `[center±radius]` and `URL.revokeObjectURL(img._paxsObjectUrl)`.

---

### H-11 — Viewer: global `getContext` patch forces `preserveDrawingBuffer` on every WebGL context

> **REMEDIATED (2026-10-08).** The `HTMLCanvasElement.prototype.getContext` override was removed from **both** places it existed — `paxs-study-list-enhance.js` *and* `viewer/index.html` (the inline copy ran first and was the one actually in effect). The single canvas that is read back via `drawImage` (the MPR GPU renderer) now requests `preserveDrawingBuffer: true` on its own `getContext` call in `paxs-mpr.js`. Regression: `.workbuddy-ai/tmp/viewer-memory-test.js` §2 (asserts no prototype assignment in any shipping file, and that the read-back canvas opts in locally).
**File:** `app/static/viewer/paxs-study-list-enhance.js` — `installVtkContextGuard()` (L20–42).

**Code:**
```js
HTMLCanvasElement.prototype.getContext = function (type, attributes) {
  if (type === 'webgl2' || type === 'webgl' || type === 'experimental-webgl') {
    var opts = Object.assign({ preserveDrawingBuffer: true, powerPreference: 'high-performance', ... }, attributes || {});
    ...
```
Patches **every** WebGL context app-wide (including Cornerstone3D/OHIF viewports) and forces `preserveDrawingBuffer: true`, which disables the browser's post-compositing buffer discard.

**Impact:** Higher per-context GPU memory and measurably slower rendering on exactly the large CT/MRI stacks this viewer targets; irreversible and global, silently overriding caller intent.

**Fix:** remove the global patch; fix the actual `setContext` null-deref at its call site or scope the override to only the canvases that need it, and drop `preserveDrawingBuffer`.

---

### H-12 — Viewer: study-list thumbnail object URLs are never revoked

> **REMEDIATED (2026-10-08).** `paxs-study-list-enhance.js` now keeps thumbnail blob URLs in an LRU bounded by `THUMB_CACHE_MAX = 64` (`_cacheThumb` revokes the oldest on overflow) and revokes every cached URL on `pagehide` (`releaseAllThumbs`). The just-created URL is never the eviction victim, so a thumbnail cannot be revoked before its `<img>` loads. Regression: `.workbuddy-ai/tmp/viewer-memory-test.js` §3.
**File:** `app/static/viewer/paxs-study-list-enhance.js` — `thumbFor()` (L158–176, cache declared L78).

**Code:**
```js
return r.blob().then(function (b) {
  var u = URL.createObjectURL(b);
  thumbCache[uid] = u;      // module-global, never revoked
  return u;
});
```
`loadStudies()` fetches `limit=200` studies (L128).

**Impact:** Up to 200 blobs (plus decoded images) pinned for the page lifetime, surviving SPA route changes.

**Fix:** revoke when the `<img>` loads, or implement an LRU with revocation; revoke on `pagehide`.

---

### H-13 — Blocking DIMSE/DB/synchronous I/O inside `async def` (event-loop stalls)

| File | Function | Line | Blocking call |
|---|---|---|---|
| `app/api/v1/nodes.py` | `ping_node` | ~L177 | `send_echo(...)` — full DIMSE association (no `to_thread`; sibling `send_to_node` at L212 does it correctly) |
| `app/api/v1/config.py` | `get_config` / `patch_config` | L58 / L99 | `settings_service.overrides_map()` / `set_override()` — sync DB round trips |
| `app/services/split_merge_service.py` | `split_series_to_new_study` / `merge_studies` | L109–120, L208–219 | `storage_service.read_bytes/save_bytes/delete_file` on the loop |
| `app/services/settings_service.py` | `set_override` (called from async) | L543 | sync DB write + `setattr(settings, …)` |
| `app/api/v1/scripts.py` | `list/reload/toggle/run/source/create` | L36–87 | sync filesystem + `importlib.exec_module` on the loop |

**Impact:** Each stalls the single event loop for the duration of a network round trip (DIMSE ping up to ~30 s), blocking **all** concurrent requests — including other viewers' pixel fetches.

> **REMEDIATED (2026-10-08).** `nodes.ping_node` now `await asyncio.to_thread(send_echo, …)`; `config.get_config`/`patch_config` offload `settings_service.overrides_map`/`set_override`; every `scripts.py` handler offloads its filesystem/import work (`list_scripts`, `reload_scripts`, `set_enabled`, `run_script_now`, `read_text`/`write_text`/`mkdir`); and `split_merge_service._rewrite_and_relocate` offloads its storage reads/writes/deletes. Regression: `tests/test_prefetch_and_routing.py::test_ping_node_does_not_block_event_loop` — verified to fail (1007 ms loop stall) against the pre-fix blocking form.

**Fix:** wrap in `await asyncio.to_thread(...)`, consistent with the many call sites that already do so.

---

### H-14 — Lossy engine loads every candidate instance into memory
**File:** `app/services/lossy_compression_service.py` — L134–139.

**Code:** `instances = (await session.execute(inst_stmt)).scalars().unique().all()`

**Impact:** A large archive materializes the entire `instances` table (with joined series/study) into RAM in one query.

**Fix:** iterate in ID-ordered batches with `yield_per`.

---

### H-15 — `lifecycle_service.evaluate_and_run` scans the whole `studies` table with N+1 lazy loads
**File:** `app/services/lifecycle_service.py` — `evaluate_and_run` (L245–262).

**Code:** `studies = session.query(Study).all()` then per-study `_rule_matches` and lazy `series_list`/`instances` walks.

**Impact:** O(studies × rules) with N+1 queries; OOM/latency on a large archive during every sweep tick.

**Fix:** batch by ID with `yield_per`, eager-load series/instances, and bound the rule scan.

---

### H-16 — `begin_nested()` SAVEPOINT defeats outer rollback on SQLite/aiosqlite (orphan PHI rows)
**File:** `app/services/indexing_service.py` — `resolve_patient_identity` (L178–189).

**Code:**
```python
try:
    async with session.begin_nested():      # SAVEPOINT
        patient = Patient(tenant_id=tenant_id, patient_id=identifier, ...)
        session.add(patient)
        await session.flush()
    created = True
except IntegrityError:
    ...
```

**Root cause:** On SQLite via aiosqlite, a *released* SAVEPOINT's writes are **not** undone by a rollback of the enclosing transaction (pysqlite's well-known savepoint/transaction-handling limitation). Reproduced in isolation during the C-1 fix: with the same `AsyncSessionLocal`, a plain `session.begin()` insert is correctly rolled back, but an insert inside `session.begin_nested()` **survives** the outer rollback (`PLAIN ROLLBACK persisted: False` / `NESTED ROLLBACK persisted: True`).

**Impact:** Any ingest that fails *after* patient resolution — quota refusal, cross-tenant reject, a storage error, or a failed split/merge — leaves an orphan `patients` (and `patient_identities`) row behind even though every other row rolls back. On a multi-tenant archive this accumulates unlinked PHI rows and can collide with the unique `(tenant, patient_id)` constraint on the next legitimate ingest. This is why the C-1 rollback test intentionally omits `target_patient_id`: with it, the split's target patient row survives the rollback.

**Fix:** apply SQLAlchemy's documented pysqlite recipe to the SQLite engines in `app/db/session.py` — set `dbapi_connection.isolation_level = None` in the existing `connect` listener and emit `BEGIN` from a `"begin"` event listener — so SAVEPOINT/ROLLBACK behave correctly. If that proves too broad, replace `begin_nested()` with a pre-check + `IntegrityError` retry that does not rely on savepoint semantics. Regression guard: the caveat is documented in `tests/test_split_merge_integrity.py::test_split_write_failure_leaves_source_intact_and_rolls_back`.

---

## MEDIUM

### M-1 — Routing queue claim is non-atomic (double dispatch under multiple workers)
**File:** `app/services/routing_service.py` — `process_queue_batch` (L269–277).
```python
fresh = session.get(RoutingQueueItem, item.id)
if fresh is None or fresh.status != "PENDING": continue
fresh.status = "SENDING"; fresh.attempts += 1
session.commit()
```
The initial SELECT (L253–264) is a separate transaction; the claim is an unguarded read-modify-write. `worker.run_standalone` explicitly supports multiple worker processes.
**Impact:** Two workers can claim and dispatch the same study → duplicate C-STORE to the destination.
**Fix:** claim with a guarded `UPDATE ... WHERE status='PENDING'` and check `rowcount` (as `worker._claim_jobs` does).

### M-2 — Storage Commitment association bookkeeping leaks on early return
**File:** `app/dicom/commit_sender.py` — `_deliver` (L136–186). Both early returns (L145 `req is None`, L147 `status == "COMPLETED"`) skip the `_ASSOCS` / `_CREATED_AT` cleanup at L180–182.
**Impact:** `_ASSOCS` holds a strong reference to a possibly-open pynetdicom association indefinitely → association/socket leak.
**Fix:** pop in a `finally` block.

### M-3 — Redis L1 byte accounting drifts, defeating the eviction budget
**File:** `app/services/redis_service.py` — `cache_get` expired path (L1032–1038), `delete_redis_key` (L929–932), `flush_redis_cache` pattern branch (L967–971). All three `del _MEM_CACHE[key]` without decrementing `_CURRENT_MEM_BYTES`; only `flush_l1_cache` (L1021) resets it.
**Impact:** The counter permanently over-reports → `cache_set_mem` (L1070) evicts far more than the 256 MB budget intends, degrading the hit rate the cache exists to provide; `get_l1_cache_stats` reports wrong `used_bytes`.
**Fix:** recompute from the dict under the lock (as `wado_rs_bulk_service._cache_put` L91 does) or decrement at every deletion.

### M-4 — Portable-Redis installer: no checksum verification + unsafe `extractall`
**File:** `app/services/redis_service.py` — `install_redis_portable` (L562–596). `urllib.request.urlretrieve(...)` then `zip_ref.extractall(TOOLS_REDIS_DIR)` with no hash check; the extracted binary is later executed (`start_redis_server`, L473–480).
**Impact:** Supply-chain RCE / zip-slip path traversal.
**Fix:** verify a pinned SHA-256, validate each member path before extraction, prefer a package manager.

### M-5 — Unbounded warm threads (one per study opened)
**File:** `app/services/warm_service.py` — `schedule_warm` (L50–72). Dedup is per-study-UID only; N distinct studies opened concurrently spawn N threads each doing pydicom decode + PIL render.
**Impact:** Thread/heap spike proportional to concurrent study opens.
**Fix:** a bounded `ThreadPoolExecutor` (2–4 workers), coalesce beyond capacity.

### M-6 — Unbounded script-dispatch threads (and chained `fire`)
**File:** `app/services/scripting_service.py` — `fire` (L308–322) spawns a daemon thread per event; `ScriptContext.fire` (L274–276) lets a script re-fire.
**Impact:** A misbehaving script can fan out threads without bound. `_load` (L107–130) also has an unguarded module cache (concurrent `exec_module` runs module-level side effects twice).
**Fix:** a single bounded executor + depth/rate cap on chained events; lock `_load`.

### M-7 — Volume bundle cache is not tenant-scoped
**File:** `app/services/volume_service.py` — `_BUNDLE_CACHE` (L420–421), `volume_bundle` (L424–526). The cache is keyed by `series_uid` only and returns the cached `(info, slices)` **without re-checking the caller's tenant**. Currently every call site (`volume.py` L484/593/624, `dicomweb.py` zorder path) runs the tenant ACL first, so it is not exploitable today — but the cache is a latent cross-tenant read that a future caller can trip.
**Fix:** include the resolved scope in the key (e.g. `f"{series_uid}:{tenant_id}:{include_all}"`), or invalidate on tenant mismatch.

### M-8 — Storage legacy-path escape hatch bypasses containment
**File:** `app/services/storage/local.py` — `_resolve` (L57–77). With `STORAGE_ALLOW_LEGACY_PATHS=true`, absolute keys and CWD-relative keys are returned **without** `_contained()`.
**Impact:** While the flag is on, any DB row can read/write/delete outside `STORAGE_ROOT`. Documented as migration-only, but it is a full containment bypass for as long as it stays on.
**Fix:** keep containment enforced even in legacy mode, or confine legacy resolution to a one-shot migration code path.

### M-9 — S3 backend: no size caps, full-object buffering, full-bucket synchronous scans
**File:** `app/services/storage/s3.py` — `read_bytes` (L64–71), `sha256` (L131–132), `usage_bytes` (L134–140). Callers (replication, thumbnail, forward) rely on these.
**Impact:** Memory/egress amplification on large objects; `usage_bytes` lists the whole bucket synchronously.
**Fix:** add a `max_bytes` guard; stream for hashing/usage.

### M-10 — DIMSE SCU TLS is unauthenticated by default (`CERT_NONE`)
**File:** `app/dicom/tls.py` — `client_context` (L161–167) / `scu_tls_args` (L170–181).
```python
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
```
**Impact:** Every outbound association is encrypted but trivially MITM-able unless the operator sets `DICOM_TLS_CA`.
**Fix:** fail closed when TLS is enabled without a trusted CA/cert, or pin the receiver's cert.

### M-11 — Replication buffers whole objects and builds S3 keys from unvalidated UIDs
**File:** `app/services/replication_service.py` — `replicate_study` (L142–151). `data = storage_service.read_bytes(...)` per instance (a 2 GB multi-frame instance needs 2 GB RSS); `remote = f"{prefix}{study_uid}/{inst.sop_instance_uid}.dcm"` without `validate_component`.
**Impact:** Memory spike; legacy rows with unsafe UIDs can escape the replica key namespace.
**Fix:** stream via `read_stream`/`upload_fileobj`; validate both UIDs.

### M-12 — Settings runtime mutation is unsynchronized
**File:** `app/services/settings_service.py` — `set_override` (L543–562) / `_apply_to_runtime` (L565–570). Concurrent PATCHes can interleave the DB write and the in-process `setattr(settings, key, value)`.
**Impact:** Readers see torn updates for keys whose consumers cache (e.g. storage backend).
**Fix:** guard with a lock and/or `model_config = ConfigDict(validate_assignment=True)`.

### M-13 — DICOMweb client "streaming" STOW reads each object fully into RAM
**File:** `app/services/dicomweb_client.py` — `_multipart_body_chunks` (L396–403). Despite the docstring, `data = storage_service.read_bytes(...)` buffers each instance whole; the non-stream path (L449–468) writes a temp file then `fh.read()`s it back.
**Impact:** Multi-GB uploads are materialized in memory.
**Fix:** stream from `read_stream()` in `chunk_size` slices.

### M-14 — Anonymizer is an incomplete PS3.15 Basic Profile
**File:** `app/services/anonymizer.py` — `anonymize_dataset` (L99–159).
* **Private tags inside sequences are not stripped** — the loop (L115–118) iterates only top-level elements; private tags nested in retained sequences survive.
* **Missing de-identification markers** — PS3.15 requires `PatientIdentityRemoved (0012,0062) = YES` and `DeidentificationMethod (0012,0063)`; neither is set, so the exported object is non-conformant and downstream systems cannot tell it is de-identified.
* **Date shifting not offered** — dates are removed wholesale (`ANONYMIZER_STRIP_DATES`), losing longitudinal research value.
**Impact:** Residual-PHI risk and non-conformant research export.
**Fix:** recurse into sequences for private-tag stripping; set the de-identification markers; offer date-shifting as an alternative to removal.

### M-15 — No Type 1 identifier validation on ingest (missing UIDs collapse to `"UNKNOWN"`)
**Files:** `app/dicom/receiver.py:handle_store` (L96–99 validates only *present* UIDs), `app/services/storage_service.py:stage_dataset_with_quota` (L391–393 defaults to `"UNKNOWN"`), `app/services/indexing_service.py:get_or_create_study` (L294 `or "UNKNOWN"`).
**Impact:** Instances lacking `StudyInstanceUID`/`SeriesInstanceUID` are all written under `UNKNOWN/UNKNOWN/UNKNOWN.dcm` and merged into one study/series — silently conflating unrelated examinations. Type 1 SOP/Study/Series UIDs are mandatory in DICOM.
**Fix:** reject ingest when a mandatory Type 1 UID is absent (C-STORE `0xC000`, STOW 400), or route to a per-source quarantine bucket rather than a shared `UNKNOWN`.

### M-16 — `_looks_like_asset` gates the viewer auth check on file extension only
**File:** `app/main.py` — `ViewerAuthGateMiddleware.__call__` (L373–375) + `_looks_like_asset` (L283–285). Any request under `/viewer` whose path ends in a known asset extension (including `.json`) bypasses the cookie gate.
**Impact:** An unauthenticated caller can fetch `/viewer/<anything>.json` (e.g. app config, manifest) without a session. Low PHI risk (bundles are static), but the gate's intent is weakened and any future JSON data endpoint placed under `/viewer` would be exposed.
**Fix:** restrict the extension fast-path to genuinely static asset dirs (hashed chunks, wasm, fonts) and keep `.json` behind the gate unless it is a known static config.

### M-17 — Viewer CPU render path allocates a canvas + ImageData per frame
**File:** `app/static/viewer/paxs-mpr.js` — `draw2D()` (L1813–1834) and `drawFusionOverlay()` (L2220–2278). Both create a new detached `<canvas>` + `ImageData` on **every** render (every scroll tick, ×3 planes); fusion additionally runs a synchronous O(w·h) pixel loop with per-pixel `sampleColormap()` allocation.
**Impact:** GC churn and main-thread stalls during multi-slice scrolling — the exact scenario the viewer is built for.
**Fix:** reuse one `ImageData`/offscreen canvas per viewport; hoist the colormap into a preallocated LUT; move fusion to the existing GPU shader path.

### M-18 — Viewer overlay SVG rebuilt via O(n²) `innerHTML +=` on every pointermove
**File:** `app/static/viewer/paxs-mpr.js` — `drawSvgOverlays()` (L2850–2942), triggered from `wrap.onpointermove` (L3045).
**Impact:** Each mouse move re-parses the whole SVG subtree; visibly janky while dragging measurements.
**Fix:** build one string and assign once, or mutate DOM nodes incrementally; throttle the drag repaint.

### M-19 — Viewer: five permanent `setInterval` sweeps + two undebounced body observers
**File:** `app/static/viewer/paxs-study-list-enhance.js` — intervals at L295, L403, L407, L790, L1098; observers at L64, L273 (L267 poll is correctly bounded).
**Impact:** 3+ full-DOM scans per second forever, plus an rAF per DOM mutation — measurable CPU/battery cost; doubled because the script loads in both the SPA and the viewer.
**Fix:** replace polling with debounced `MutationObserver`s; disconnect on `pagehide`.

### M-20 — `warm_service._warm_study` uses a session after its `with` block
**File:** `app/services/warm_service.py` — L106–190: the `with SyncSessionLocal()` closes at L118 but `session.execute(...)` runs again at L181.
**Impact:** Re-opens an implicit transaction outside the intended scope; fragile under future changes.
**Fix:** move the volume query inside the `with`.

---

## LOW

| ID | File / function | Issue | Fix |
|---|---|---|---|
| L-1 | `app/dicom/sender.py:send_move` (L270–273) | Only `NumberOfCompletedSuboperations` is `hasattr`-guarded; the other counters raise if absent | `getattr(status, "NumberOfFailedSuboperations", 0)` for each |
| L-2 | `app/dicom/sender.py:query_remote_node_sync` (L226–235) | On a non-`ok` result with matches, returns raw `Dataset` objects → JSON 500 | Normalize matches on every branch |
| L-3 | `app/services/storage/local.py` (L16–17) | Duplicate `import os` | Remove one |
| L-4 | `app/services/storage/s3.py:_s3` (L46–58) | Lazy client init is not thread-safe; racing threads each build a boto3 client | Guard with a lock |
| L-5 | `app/services/storage/local.py:move` (L173–180) | `shutil.move` is copy+delete across filesystems — non-atomic | Staged copy + `os.replace` |
| L-6 | `app/static/viewer/paxs-mpr.js:loadThree` (L210–214) | Interval never cleared if Three.js fails entirely | Store + clear the handle in `onerror` |
| L-7 | `app/static/viewer/paxs-mpr.js:initGl2` (L2009–2031) | WebGL2 context created then abandoned on oversized 3D texture | `WEBGL_lose_context.loseContext()` before return |
| L-8 | `app/static/viewer/paxs-mpr.js:closeMpr` (L1140–1164) | `state.processing` / `state.fusion.overlayVol` / `split.fineTimer` not reset → stale global state, retained volume | **REMEDIATED (2026-10-08):** `closeMpr` now nulls `fusion.overlayVol/overlayInfo/overlaySeriesUid`, disables fusion, clears `split.fineTimer`, and resets `effectiveMode`/`fallbackIndex`/`clientProbed`/`split` state. Regression: `.workbuddy-ai/tmp/viewer-memory-test.js` §4 |
| L-9 | `app/static/viewer/paxs-mpr.js:animLoop` (L3473–3477) | Three.js renders at display rate with no dirty flag | Render on demand |
| L-10 | `app/static/viewer/paxs-about.js:closeAbout` (L495–513) | Overlay/timeout not tracked → transient double modal | Track + cancel the pending removal |
| L-11 | `app/static/viewer/google.js` | Empty dead file | Delete |
| L-12 | `app/static/viewer/index.html` (L25) | Undebounced body observer scheduling an rAF per mutation | Debounce; cache the matched container |
| L-13 | `app/services/prefetch_service.py` (L128) | `max_prior_age_days` never applied to filter priors | Filter `priors` by `study_date` |
| L-14 | `app/services/replication_service.py:sweep` (L204–215) | Check-then-insert enqueue race (no unique constraint on `study_instance_uid`?) | Add unique constraint / `ON CONFLICT` |
| L-15 | `app/api/v1/config.py` (L58/99) | Sync `settings_service` DB calls inside `async def` | `asyncio.to_thread` |
| L-16 | `app/services/scripting_service.py:ScriptContext.read_bytes` (L251–255) | Ignores the instance's named backend | Pass `backend_name` through |
| L-17 | `app/services/settings_service.py` + `.env.example` | `.env.example` ships `DEFAULT_COMPRESSION=RLE`, `TOKEN_EXPIRE_MINUTES=480` vs code defaults `JPEG-LS`, `15` | Align `.env.example` with the intended defaults |

---

## Cross-reference: already-correct patterns worth reusing

* `worker._claim_jobs` (guarded UPDATE + rowcount) — the correct claim pattern for M-1.
* `storage_service.StagedObject` (stage/promote/discard) — the correct all-or-nothing write pattern for C-1/C-3 and lifecycle (H-6).
* `dicomweb.py:stow_studies` (L1019–1045) — per-part commit-then-promote with compensation.
* `commit_sender` explicit ACSE/DIMSE timeouts — the pattern H-8 needs.
* `decode_context()` PHI-free diagnostics — already applied on the decode-failure paths.
