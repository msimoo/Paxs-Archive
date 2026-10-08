# DICOM network services (DIMSE, HL7, TLS) and the ingest / storage / worker pipeline

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/04-ingest-storage-schema.md](5a32aa4/04-ingest-storage-schema.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

Severity legend (plain words used in place of colour emoji): **BLOCKING** = red, **IMPORTANT** = yellow, **NIT** = green.

---

## 0. Headline

1. **BLOCKING, repo does not import.** `app/services/storage/` (the storage backend package) is gitignored by an unanchored `storage/` rule and is absent from disk. `app/main.py` cannot import, so the server and DICOM receiver cannot start from this tree.
2. **BLOCKING, no AE-title validation on the SCP.** Any reachable host can associate with any calling AE. This combines with F-03 (C-GET/C-MOVE return the whole scope) and F-05 (store overwrite/delete) into a bulk-export and data-destruction path.
3. **BLOCKING, archived objects can be deleted.** A retransmitted or parallel-sent instance overwrites the same object key, then the failed request's cleanup deletes it, while the first request's committed DB row still points to it.
4. **BLOCKING, TLS bootstrap deletes operator cert/key.** Any load failure or half-present pair unlinks the files and writes a self-signed replacement (verified by scratch run).
5. **BLOCKING, HL7 listener never ACKs and dies on malformed input.** No reply is ever sent; an ORM^O01 without OBR raises `AttributeError` and drops the connection with any buffered messages.

---

## 1. Files covered

| Path | Lines | One-line purpose |
|---|---|---|
| `app/dicom/receiver.py` | 658 | pynetdicom SCP: C-STORE, C-FIND (Q/R + MWL), C-MOVE, C-GET, MPPS N-CREATE/N-SET, Storage Commitment N-ACTION; server lifecycle |
| `app/dicom/sender.py` | 806 | SCU helpers: C-STORE send, C-FIND, C-MOVE, C-GET pull, C-ECHO, MPPS N-CREATE/N-SET, node-level send/query/pull |
| `app/dicom/parser.py` | 113 | pydicom Dataset to flat metadata dict; file/bytes helpers |
| `app/dicom/hl7.py` | 780 | HL7 v2 TCP listener: ADT, ORM (worklist), PIX/PDQ query parsing (responses not sent) |
| `app/dicom/tls.py` | 191 | Self-signed cert bootstrap, server/client SSLContext builders |
| `app/dicom/commit_sender.py` | 249 | Storage Commitment N-EVENT-REPORT delivery thread + stale-request sweep |
| `app/services/storage_service.py` | 441 | Storage facade: key layout, save/read/delete, usage cache, tiering, compress wrapper |
| `app/services/indexing_service.py` | 341 | Upsert Patient/Study/Series/Instance, AuditLog, CompressionJob, derived indexers |
| `app/services/query_service.py` | 234 | Q/R filter builders, tenant scope, aggregates |
| `app/services/storage_commit_service.py` | 185 | Storage Commitment verification and request rows |
| `app/services/worker.py` | 512 | Background thread/CLI: compression jobs, triage, lifecycle/routing/replication/session sweeps |
| `app/services/compression_service.py` | 245 | Lossless encoders (RLE, JPEG-LS, JPEG2000, HTJ2K) with atomic in-place replace |
| `app/services/imaging.py` | 91 | Pixel data to PIL image (window/level, MONOCHROME1, colour) |
| `app/services/thumbnail_service.py` | 38 | Generates 192 px PNG thumbnails to `.thumbnails/` |
| `app/services/warm_service.py` | 243 | Warm-on-open thread: thumbnails, Redis thumb/frame/volume caches |
| `app/services/accelerator_service.py` | 148 | Parallel streamed STOW-RS transfer (HTTP, not DIMSE) |
| `app/services/triage_service.py` | 160 | Pluggable triage detectors; demo pneumothorax heuristic |

Read-only context (grep or short ranges only, to resolve names/behaviour): `app/core/config.py`, `app/db/models.py`, `app/db/session.py`, `app/services/routing_service.py` (28-60, 218-233), `app/services/webhook_service.py` (grep), `app/main.py` (31-32, 370-404), `app/api/v1/dicomweb.py` (grep), `.gitignore`.

---

## 2. Module reference

### 2.1 `app/dicom/receiver.py` (658 lines)
Purpose: DIMSE SCP. Creates the pynetdicom `AE`, registers contexts and handlers, runs the server in thread `paxs-dicom-scp`.

Module state: `_ae` (51). Attribute maps: `_PATIENT_KEYS` (116), `_STUDY_KEYS` (122), `_STUDY_ROW_KEYS` (136), `_SERIES_KEYS` (137), `_INSTANCE_KEYS` (144).

Key functions:
- `_calling_ae(event) -> str | None` (56)
- `async _index_async(meta, path, sha256, source_ae=None, backend=None) -> None` (60): one `AsyncSessionLocal` transaction; resolves tenant (`tenant_service.resolve_tenant_id_for_ae`, 65); calls `index_new_instance` (68).
- `handle_store(event) -> int` (74): C-STORE SCP. Returns 0x0000 or 0xC212.
- `_fill_from(ds, src, keys, requested) -> None` (153)
- `_query_filters(query, keys) -> dict` (165)
- `handle_worklist_find(event)` generator (180): MWL, sync session across yields.
- `handle_find(event)` generator (206): C-FIND Q/R (PATIENT/STUDY/SERIES/INSTANCE); dispatches to MWL when abstract syntax is MWL (209-212).
- `_decode_attribute_list(event) -> Dataset | None` (299)
- `handle_n_create(event) -> (int, None)` (330), `handle_n_set(event)` (356), `handle_n_action(event)` (385)
- `_matching_instance_keys(query, level, tenant_id=None) -> list[tuple[str, str|None]]` (425): shared C-GET/C-MOVE key resolution.
- `handle_move(event)` generator (460), `handle_get(event)` generator (525)
- `start_receiver(host=None, port=None, ae_title=None, tls_context=None) -> AE` (564)
- `shutdown_receiver() -> None` (631), `start_receiver_in_thread() -> None` (641)

DB models: reads `Patient`, `Study`, `Series`, `Instance` (Q/R, 229-291, 436-457); `DicomNode` via `get_node_sync` (486); `ScheduledProcedureStep` via `worklist_service` / `mpps_service`. Writes indirectly via `index_new_instance` (AuditLog, Patient, Study, Series, Instance, CompressionJob, ReplicationRecord) and `storage_commit_service` (StorageCommitRequest).

Config: `DICOM_AE_TITLE` (default "PAXS_SCP", config.py:28), `DICOM_HOST` ("0.0.0.0", :29), `DICOM_PORT` (10400, :30), `DICOM_MAX_ASSOCIATIONS` (16, :32), `DICOM_TLS_ENABLED` (False, :39).

Threads/queues: pynetdicom association threads call the handlers synchronously; C-STORE creates a fresh event loop per instance via `asyncio.run` (88). Redis: none.

### 2.2 `app/dicom/sender.py` (806 lines)
Purpose: outbound SCU. Every call opens its own `AE(ae_title=our_ae_title)` (default `"PAXS_SCU"`) and association.

Key functions:
- `_tls_args(peer_host)` (37) returns `(ctx, host)` via `tls.scu_tls_args`
- `send_file(host, port, ae_title, file_path, our_ae_title="PAXS_SCU") -> dict` (50)
- `send_find(host, port, ae_title, query=None, query_model=..., our_ae_title) -> dict` (80)
- `_normalize_match(identifier) -> dict` (128)
- `query_remote_node_sync(ae_title, level="STUDY", filters=None, our_ae_title) -> dict` (176)
- `send_move(host, port, ae_title, dest_ae_title, query=None, our_ae_title) -> dict` (238)
- `send_get(host, port, ae_title, query=None, our_ae_title, on_received=None) -> dict` (281)
- `_ingest_pulled_dataset(ds, tenant_id=None) -> None` (364): save + `index_new_instance` in `asyncio.run` (395).
- `pull_from_node_sync(ae_title, level, filters, our_ae_title, tenant_id) -> dict` (398)
- `send_echo(host, port, ae_title, our_ae_title) -> dict` (547)
- `_send_mpps_n_op(...)` (563), `send_mpps_create` (604), `send_mpps_set` (612)
- `send_directory(host, port, ae_title, directory) -> dict` (620)
- `_materialize_for_send(key, backend=None) -> (path, is_temp)` (643)
- `resolve_keys_sync(study_uids, series_uids, instance_uids, tenant_id=None, include_all=True) -> list[dict]` (663)
- `send_keys(host, port, ae_title, targets, our_ae_title) -> dict` (728)
- `send_to_node_sync(ae_title, study_uids, series_uids, instance_uids, our_ae_title, tenant_id, include_all) -> dict` (772)

DB models: reads `DicomNode` (196-199, 418-421, 790-793), `Study`, `Series`, `Instance` (resolve_keys_sync 697-716). Writes via `index_new_instance` in pull path only.

Config: none directly; TLS via `tls.py` (DICOM_TLS_*).

Threads: caller thread (sync). Pull ingest uses `asyncio.run` per instance.

### 2.3 `app/dicom/parser.py` (113 lines)
Purpose: DICOM to metadata dict (`_META_FIELDS` 16-47; `_KEYWORD_MAP` 50-77).
- `_clean(value) -> str | None` (80)
- `parse_metadata(ds) -> dict` (89): returns keys `patient_id`, `patient_name`, ..., `transfer_syntax_uid` (from file_meta).
- `parse_file(path) -> dict` (103): `dcmread(stop_before_pixels=True)`.
- `dataset_from_bytes(data, force=False) -> Dataset` (109)

DB: none. Config: none. Threads: none.

### 2.4 `app/dicom/hl7.py` (780 lines)
Purpose: HL7 v2 TCP listener (ADT, ORM, QBP Q22/Q23). Thread `paxs-hl7-listener`; one daemon thread per connection.

Parsers (pure): `_segment(seg, field, component=1)` (61), `_name(pid)` (72), `_dt_parts(dtm)` (82), `_xcn_name(seg, field)` (93), `parse_adt_message(message) -> dict|None` (104), `parse_orm_message(message) -> dict|None` (136), `_parse_qpd_segment(seg)` (475), `_extract_pix_query_params(qpd)` (492), `_extract_pdq_query_params(qpd)` (534).

DB writers: `_ensure_patient(session, data)` (253; savepoint-free IntegrityError rollback 264-276), `_apply_demographics(patient, data)` (426; sets `source="his"`), `_upsert_patient(data)` (282), `_create_default_step(session, patient, data)` (216), `_upsert_order(data)` (398), `_upsert_order_step(...)` (335), `_cancel_order_steps(...)` (376), `_find_open_order_step(...)` (302).

Dispatch: `_handle_message(raw: bytes) -> None` (439). PIX/PDQ: `_handle_pix_query(text)` (581), `_handle_pdq_query(text)` (612), response builders `_build_pix_response(query_tag, results) -> str` (642), `_build_pdq_response` (666).

Listener: `class HL7Listener(threading.Thread)` (694): `stop()` (702), `run()` (710), `_handle_connection(conn, addr)` (739). `start_hl7_listener() -> HL7Listener|None` (764), `shutdown_hl7()` (775).

Config: `HL7_ENABLED` (False, config.py:175), `HL7_HOST` ("0.0.0.0", :176), `HL7_PORT` (2575, :177), `MWL_DEFAULT_STATION_AET` (config.py:192).
DB models: `Patient`, `ScheduledProcedureStep` (accession_number, requested_procedure_id, requested_procedure_description, modality, start_date, start_time, scheduled_performing_physician, scheduled_station_aet, status, sps_id).
Threads: listener thread plus one thread per connection; sync session per message.

### 2.5 `app/dicom/tls.py` (191 lines)
Purpose: TLS for DIMSE. Builds the self-signed cert when missing.
- `ensure_certificate(cert_path, key_path, common_name="PAXS_SCP") -> (cert, key)` (36)
- `server_context() -> ssl.SSLContext` (127): min TLS 1.2; `CERT_REQUIRED` only when `DICOM_TLS_CA` is set (133-136).
- `client_context(trust_cert=None) -> ssl.SSLContext` (141)
- `scu_tls_args(peer_host) -> (ctx, host) | None` (170)
- `ensure_cert_paths_exist() -> None` (184); alias `generate_self_signed` (191)

Config: `DICOM_TLS_ENABLED` (False, config.py:39), `DICOM_TLS_CERT` ("./certs/paxs.crt", :40), `DICOM_TLS_KEY` ("./certs/paxs.key", :41), `DICOM_TLS_CA` ("", :44).
DB/threads: none.

### 2.6 `app/dicom/commit_sender.py` (249 lines)
Purpose: N-EVENT-REPORT delivery for Storage Commitment.
- Module state: `_ASSOCS` (50), `_CREATED_AT` (56), `_QUEUE` (58, `queue.Queue`), `_WORKER` (59).
- Constants: `FINALIZE_AFTER_SECONDS = 2.0` (41), `SWEEP_INTERVAL_SECONDS = 1.0` (43), `_DIMSE_TIMEOUT_SECONDS = 5.0` (47).
- `register_association(tx_uid, assoc) -> None` (63), `queue_report(tx_uid) -> None` (77), `_ensure_worker()` (84)
- `_run()` (92): thread loop; `queue.get(timeout=1.0)`; on empty runs `_finalize_stale()`.
- `_build_report(payload) -> Dataset` (117)
- `_deliver(tx_uid) -> None` (136): sends on original association if alive, else `_deliver_via_node`; sets COMPLETED/FAILED.
- `_deliver_via_node(tx_uid, payload, ae_title) -> bool` (189)
- `_finalize_stale() -> None` (225)

DB: `StorageCommitRequest` (read/write), `DicomNode` via `node_service.get_node_sync`.
Config: `DICOM_AE_TITLE` (commit_sender.py:201).
Threads: `paxs-commit-sender` (daemon). Queue: in-memory `_QUEUE`. Redis: none.

### 2.7 `app/services/storage_service.py` (441 lines)
Purpose: facade over the storage backend. Object layout (module docstring 3-10): `{Study}/{Series}/{SOP}.dcm`, `.thumbnails/{SOP}.png`.

Key functions:
- `_safe_component(value, fallback="unknown") -> str` (48); regexes `_UID_OK` (44), `_UID_STRIP` (45)
- `instance_key(study_uid, series_uid, sop_uid) -> str` (62); `thumbnail_key(sop_uid)` (70)
- `default_backend_name() -> str | None` (82); `resolve_study_backend(study_uid) -> str | None` (96)
- `local_path`, `exists`, `size`, `save_bytes`, `read_bytes`, `delete_file`, `delete_instance_files`, `delete_tree`, `delete_study_files(study_uid, backend_name=None)` (179), `storage_usage_bytes`, `presigned_url`, `sha256_of`, `move_object` (121-180)
- `read_dataset(key, stop_before_pixels=False, backend_name=None) -> Dataset` (142)
- `save_dataset(ds, backend_name=None) -> (key, sha256)` (274): no quota check
- `save_dataset_with_quota(ds, tenant_id, backend_name=None)` (301) and `save_bytes_with_quota` (256): unused in repo
- `move_to_backend(key, to_backend, from_backend=None) -> bool` (343); `move_study_to_backend(study_uid, to_backend) -> dict` (358)
- `compress(key, method="RLE", backend_name=None) -> dict` (408): calls `compression_service.compress_file`
- Usage cache: `cached_storage_usage_bytes(backend_name=None)` (200), `invalidate_usage_cache` (235); module dict `_usage_cache` (197)

Imports: `from app.services.storage import get_storage_backend` (35) and `from app.services.storage.backend import StorageError` (38). **The module is absent (see F-01).**

DB: reads `Study.storage_backend`, `StorageBackendConfig.is_default`; `move_study_to_backend` reads/writes `Study`, `Instance`.
Config: `STATS_USAGE_CACHE_SECONDS` (60, config.py:120), `S3_PRESIGN_EXPIRES` (900, :66), `STORAGE_BACKEND` ("local", :60).
Redis keys: `paxs:usage:{backend|default}` (210, 241) via `redis_service.cache_get/cache_set/cache_delete_pattern`.

### 2.8 `app/services/indexing_service.py` (341 lines)
Purpose: the DB upsert pipeline shared by C-STORE, C-GET pull and STOW (per docstring 1-6).
- `async get_or_create_patient(session, meta) -> Patient` (31): default `"ANONYMOUS"` (32); savepoint insert (42-50)
- `async get_or_create_study(session, meta, patient, tenant_id=None) -> Study` (94)
- `async get_or_create_series(session, meta, study) -> Series` (162)
- `async upsert_instance(session, series, meta, file_path, file_size, storage_backend=None) -> Instance` (190)
- `async index_new_instance(session, meta, file_path, sha256=None, performed_by="dicom-receiver", tenant_id=None, storage_backend=None) -> Instance` (225)
- `utcnow()` (340)
- Module constant `_GSPS_SOP_CLASS_UID` (21); RDSR/KIN SOP classes are literals (307, 322)

DB: writes `Patient`, `Study`, `Series`, `Instance`, `AuditLog` (253-261), `ReplicationRecord` (status to PENDING, 270-279), `CompressionJob` (283-289); reads the same tables; `Series.triage_status` set in `get_or_create_series` (185-186).
Config: `DR_REPLICATION_ENABLED` (False, config.py:246), `COMPRESSION_ENABLED` (True, :107), `DEFAULT_COMPRESSION` (JPEG-LS, :106).
Side effects: `webhook_fire("StudyReceived")` (128), `script_fire` (136), `worklist_service.find_matching_step` / `complete_step` (119-121), `routing_service.evaluate_and_route_study` (148).

### 2.9 `app/services/query_service.py` (234 lines)
Purpose: builders for Q/R (DICOMweb QIDO and C-FIND). Filter maps `STUDY_FILTERS` (18-29), `SERIES_FILTERS` (31-37), `INSTANCE_FILTERS` (39-43).
- `_pattern(value)` (46), `_range(value)` (52), `_apply(stmt, column, kind, value)` (116)
- `apply_study_filters(stmt, filters)` (62), `apply_series_filters(stmt, filters, study_uid=None)` (81), `apply_instance_filters(stmt, filters, study_uid=None, series_uid=None)` (95)
- `apply_study_tenant_scope(stmt, tenant_id=None, include_all=False)` (141): `tenant_id=None` means `Study.tenant_id IS NULL` (150)
- `study_query(filters=None, tenant_id=None, include_all=True)` (157), `series_query(study_uid=None, filters=None, tenant_id=None, include_all=True)` (164), `instance_query(study_uid=None, series_uid=None, filters=None, tenant_id=None, include_all=True)` (177)
- `async study_aggregates(session, study_ids)` (198), `async series_instance_counts(session, series_ids)` (223)

DB: reads only (Study, Patient, Series, Instance).
Config: none. Threads: none.

### 2.10 `app/services/storage_commit_service.py` (185 lines)
Purpose: Storage Commitment verification (`commitment_statuses` concept; table is `StorageCommitRequest`).
- Constants: `FAILURE_PROCESSING=0x0110` (37), `FAILURE_NO_OBJECT=0x0112` (38), `FAILURE_CLASS_UNSUPPORTED=0x0122` (39)
- `_refs_from_ds(ds) -> list[dict]` (42); `_verify_one(session, ref) -> dict` (58); `verify_instances(session, refs)` (85); `_all_ok` (89); `_load_payload(req)` (93)
- `handle_action(session, tx_uid, ae_title, ds) -> list[dict]` (103): upserts request row; queues report when all refs OK.
- `pending_requests_for_sop(session, sop_uid)` (145): scans all PENDING rows.
- `on_instance_stored(sop_uid) -> None` (157): called from `handle_store` after each successful C-STORE.
- `_queue(tx_uid)` (181) to `commit_sender.queue_report`.

DB: reads and writes `StorageCommitRequest` (`transaction_uid` unique, models.py:706); reads `Instance` (`sop_instance_uid` unique, models.py:438).
Config: none. Threads: runs in the C-STORE handler thread or the N-ACTION handler thread.

### 2.11 `app/services/worker.py` (512 lines)
Purpose: background compression and triage loop, plus sweeps; also a standalone CLI.

- `class CompressionWorker(threading.Thread)` (27): `__init__(poll_seconds=None)` (28), `stop()` (37), `run()` (40), `_claim_jobs(session, limit=None)` (51), `_process_batch()` (95), `_process_job(session, job)` (133), `_sweep_sessions()` (184), `_sweep_routing()` (209), `_sweep_lifecycle()` (225), `_process_triage(session)` (241)
- `start_worker() -> CompressionWorker` (354): calls `_recover_stuck_jobs()` (358) and `routing_service.recover_stuck_queue_items()` (361)
- `_recover_stuck_jobs()` (367), `stop_worker()` (389), `run_standalone(poll_seconds=None, once=False)` (399), `_check_codecs_cli() -> int` (446); CLI in `__main__` (489-511)

DB: reads/writes `CompressionJob` (status PENDING/RUNNING/DONE/FAILED, error_message, original_size, compressed_size, ratio_pct, finished_at); `Instance` (transfer_syntax_uid, file_size_bytes, compressed_at); `Series` (triage_status); writes `TriageFlag` (rows); `AuditLog` via `audit_service.add_audit` (161); deletes expired/revoked `Session` rows (196-201).
Config: `WORKER_ENABLED` (True, config.py:109), `WORKER_POLL_SECONDS` (3.0, :110), `WORKER_BATCH_SIZE` (15, :114), `TRIAGE_RUN_IN_WORKER` (True, :208), `THUMBNAIL_ENABLED` (True, :108), `LIFECYCLE_RUN_IN_WORKER` (True, :214), `ROUTING_RUN_IN_WORKER` (True, :226), `DR_REPLICATION_ENABLED` (False, :246), `SESSION_SWEEP_EVERY_TICKS` (60, :150).
Redis: `paxs:lock:worker:triage` (256, TTL 30 s).
Threads: `paxs-compression-worker` (daemon in API process via main.py:387-388; or non-daemon in `run_standalone`).

### 2.12 `app/services/compression_service.py` (245 lines)
Purpose: lossless re-encode in place.
- `COMPRESSION_METHODS` (35): RLE, JPEG-LS, JPEG2000, HTJ2K
- `normalize_method(method) -> str` (50), `_imagecodecs_htj2k_available() -> bool` (55), `_pydicom_htj2k_encoder() -> bool` (65), `encoder_available(transfer_syntax_uid) -> bool` (78)
- `_compress_htj2k(ds) -> None` (111): manual HTJ2K encapsulation via imagecodecs
- `codec_status() -> list[dict]` (156)
- `class CompressionError(RuntimeError)` (178)
- `compress_file(path, method="RLE") -> dict` (182): returns `original_bytes`, `compressed_bytes`, `saved_percent`, `transfer_syntax_uid`, `method`, `already_compressed`.

DB: none. Config: `DEFAULT_COMPRESSION` via callers. Threads: worker thread.

### 2.13 `app/services/imaging.py` (91 lines)
- `_is_color(ds) -> bool` (25), `_normalize_u8(arr) -> ndarray` (30; percentile 1-99), `dataset_to_image(ds, window=None, max_size=None) -> PIL.Image` (40), `dataset_to_bytes(ds, fmt="PNG", window=None, max_size=None) -> bytes` (82).
- Used by WADO-RS rendering (outside scope), thumbnails and warm.

### 2.14 `app/services/thumbnail_service.py` (38 lines)
- `DEFAULT_SIZE = 192` (19); `generate_thumbnail(src_key, dst_key, size=192, backend_name=None) -> bool` (22): best effort, returns False on any error (logged WARNING).
- Callers in scope: worker.py:170 (after compression) and warm_service.py:133.

### 2.15 `app/services/warm_service.py` (243 lines)
Purpose: warm-on-open.
- Module state: `_WARMING` set (39), `_WARMING_LOCK` (40), `_WARM_LOCK_TTL = 120` (44)
- `schedule_warm(study_uid) -> bool` (50): one daemon thread per study (65-72). Callers: `app/api/v1/dicomweb.py:207, 334`.
- `_warm_worker(study_uid)` (78), `_warm_study(study_uid)` (88): thumbnails (129-136), Redis thumb key (142-158), Redis frame cache (162-174), volume cache (176-213), record (215-230).

DB: reads `Instance`, `Series`, `Study`.
Config: `WARM_ON_OPEN_ENABLED` (True, config.py:310), `WARM_MAX_INSTANCES` (16, :311), `WARM_MAX_FRAMES` (4, :312), `THUMBNAIL_ENABLED`, `REDIS_CACHE_ENABLED` (True, :168).
Redis keys: `paxs:warm:lock:{study}` (97, TTL 120), `paxs:thumb:{sop}:auto:256:png` (142, TTL 3600), `paxs:frame:{sop}:1` (172, TTL 3600), `paxs:vol:*{series}*` (199, scanned), `paxs:warm:{study}` JSON record (228, TTL 86400).

### 2.16 `app/services/accelerator_service.py` (148 lines)
Purpose: parallel STOW-RS over HTTP (not DIMSE).
- `accelerated_stow_upload(node, targets, workers=None, batch_size=None, retries=1, on_progress=None) -> dict` (36). `_MAX_WORKERS = 16` (33).
- Uses `dicomweb_client.stow_upload(node, batch, stream=True)` (81) and `validate_web_url` (72). Retry only on transport error or HTTP >= 500 (94-99).
Config: `TRANSFER_ACCELERATOR_ENABLED` (True, config.py:277), `TRANSFER_ACCELERATOR_WORKERS` (4, :278), `TRANSFER_ACCELERATOR_BATCH_SIZE` (16, :279).
Threads: `ThreadPoolExecutor` (108). DB: none (targets are resolved by the caller).

### 2.17 `app/services/triage_service.py` (160 lines)
Purpose: pluggable detectors; findings to `TriageFlag` (written in worker).
- `@dataclass Finding` (28): `finding, severity, confidence, summary, detector, sop_instance_uid, series_instance_uid, meta`.
- `class PneumothoraxHeuristicDetector` (42): `name` (51), `MODALITIES = ("CT",)` (52), `detect(ds_list) -> list[Finding]` (54)
- `DETECTORS = [PneumothoraxHeuristicDetector()]` (107)
- `_slice_subset(ds_list, max_slices)` (113), `run_detectors_on_series(series, instance_rows) -> list[Finding]` (122)

Config: `TRIAGE_ENABLED` (True, config.py:207), `TRIAGE_MAX_SLICES_PER_SERIES` (8, :209). DB: none directly (caller writes TriageFlag). Threads: worker thread.

---

## 3. DICOM services supported

| Service | Role | Handler (file:line) | Notes |
|---|---|---|---|
| C-ECHO | SCP | pynetdicom built-in; Verification context added at `receiver.py:602` (no EVT handler) | Explicit VR LE only (602). No AE gating (F-02). |
| C-ECHO | SCU | `sender.py:547-560` `send_echo` | No try/finally: exception skips `release()` (N-02). |
| C-STORE | SCP | `receiver.py:74-110` `handle_store`, registered at `:608` | Contexts: every `StoragePresentationContexts` entry with `scp_role=True, scu_role=False` (`:584-588`). Transfer syntaxes are pynetdicom's defaults for those contexts (not overridden in code; verify against installed pynetdicom). |
| C-STORE | SCU | `sender.py:50-77` `send_file`; `:620-637` `send_directory`; `:728-769` `send_keys`; `:772-806` `send_to_node_sync` | One association per file/instance (IMPORTANT F-17). Requested context = SOP class + file's TS (`:55-61`); no TLS-mismatch check. |
| C-FIND (Study Root Q/R) | SCP | `receiver.py:206-293` `handle_find`, registered at `:609` | PATIENT/STUDY/SERIES/INSTANCE. Context `StudyRootQueryRetrieveInformationModelFind`, Explicit VR LE (`:590`). Parent UIDs not applied at SERIES/INSTANCE (F-11). |
| C-FIND (MWL) | SCP | `receiver.py:180-203` `handle_worklist_find`; dispatched at `:209-212` | Context `ModalityWorklistInformationFind` (`:593`). No tenant scope, unbounded (F-12). |
| C-FIND | SCU | `sender.py:80-125` `send_find`; `:176-235` `query_remote_node_sync` | Return-field sets `_RETURN_FIELDS` (`:150-173`). Node lookup by `ae_title` in `dicom_nodes`. |
| C-MOVE | SCP | `receiver.py:460-519` `handle_move`, registered at `:611` | Destination from `dicom_nodes` (`:486-490`), no authorization check. Eager read of all matches (`:503-508`). Unscoped identifiers (F-03). |
| C-MOVE | SCU | `sender.py:238-278` `send_move` | Explicit VR LE (`:248`). |
| C-GET | SCP | `receiver.py:525-558` `handle_get`, registered at `:610` | Sub-operation storage contexts with scp_role (`:584-588`). Lazy read per instance (`:552-557`). Unscoped identifiers (F-03); read failure returns 0xA701 (N-07). |
| C-GET | SCU | `sender.py:281-361` `send_get`; `:398-544` `pull_from_node_sync` | Offers storage contexts with `build_role(scp_role=True)` (`:306-309`). Buffers in RAM when `on_received` is None (`:325`). |
| MPPS N-CREATE | SCP | `receiver.py:330-353` `handle_n_create`, registered at `:612` | Context `ModalityPerformedProcedureStep` (`:594`). Any AE can write steps (F-02). |
| MPPS N-SET | SCP | `receiver.py:356-379` `handle_n_set`, registered at `:613` | Returns 0x0112 for unknown SOP instance UID (`:374`). |
| MPPS N-CREATE / N-SET | SCU | `sender.py:563-617` `_send_mpps_n_op`, `send_mpps_create`, `send_mpps_set` | Explicit VR LE; try/finally present. |
| Storage Commitment N-ACTION | SCP | `receiver.py:385-419` `handle_n_action`, registered at `:614` | Context `StorageCommitmentPushModel` with scu+scp roles (`:598-601`). Returns 0x0000 at once; verdict later as N-EVENT-REPORT. |
| Storage Commitment N-EVENT-REPORT | SCU | `commit_sender.py:136-219` `_deliver`, `_deliver_via_node` | On the original association if `is_alive` (see N-04), else fresh association without TLS (F-09). |
| HL7 v2 (not DIMSE) | TCP listener | `hl7.py:694-758` `HL7Listener`; `_handle_message` `:439-469` | ADT A01/A04/A08/A31/A40, ORM^O01 (NW/XO/CA/OC/DC/CR), QBP^Q22/Q23 (responses not sent). |
| Not handled here | N-GET, N-DELETE, C-CANCEL, N-EVENT-REPORT (SCP) | none | pynetdicom default behaviour (not verified). |

---

## 4. Ingest workflow (C-STORE from modality, step by step)

1. **Listener start.** `main.py:389-390` calls `start_receiver_in_thread()` (`receiver.py:641-658`) when `DICOM_RECEIVER_ENABLED` (config.py:31). The daemon thread `paxs-dicom-scp` calls `start_receiver` (`:564-628`), which creates `AE(ae_title=DICOM_AE_TITLE)` (`:577`), sets `maximum_associations = DICOM_MAX_ASSOCIATIONS` (16; `:578`) and calls `ae.start_server((DICOM_HOST, DICOM_PORT), block=False, evt_handlers=..., ssl_context=tls_context)` (`:616-621`). TLS context from `tls.server_context()` only if `DICOM_TLS_ENABLED` (`receiver.py:647-652`).
2. **Association accept.** No calling/called AE check (F-02). pynetdicom negotiates presentation contexts against `ae.supported_contexts` (`:584-602`).
3. **C-STORE request.** pynetdicom invokes `handle_store(event)` in the association thread (`EVT_C_STORE` at `:608`).
4. **Guard.** `ds = event.dataset`; `None` returns 0xC212 (`receiver.py:75-77`). `ds.file_meta = event.file_meta` (`:80`).
5. **Backend choice.** `storage_service.resolve_study_backend(StudyInstanceUID)` (`receiver.py:83-85` to `storage_service.py:96-110`): the study's recorded `storage_backend` if the study exists, otherwise `default_backend_name()` (`:82-93`, reads `StorageBackendConfig.is_default`). Opens a sync session per store.
6. **Object write.** `storage_service.save_dataset(ds, backend_name)` (`receiver.py:86`, `storage_service.py:274-298`):
   - Missing `file_meta` is synthesised with Explicit VR LE (`:286-291`).
   - Key `instance_key(study, series, sop)` = `{study}/{series}/{sop}.dcm` (`:62-67`). Each UID is passed through `_safe_component` (F-04).
   - `ds.save_as(BytesIO, enforce_file_format=True)` (`:294-295`) builds the whole object in memory.
   - `bk.save_bytes(key, bytes)` (`:297`) overwrites any existing object (F-05). Backend module absent (F-01).
   - `bk.sha256(key)` (`:298`) re-reads the object for the audit hash.
7. **Metadata extraction.** `parse_metadata(ds)` (`receiver.py:87`, `parser.py:89-100`): flat dict; `transfer_syntax_uid` from file_meta.
8. **Indexing in a fresh loop.** `asyncio.run(_index_async(meta, path, sha256, source_ae=_calling_ae(event), backend=backend))` (`receiver.py:88-90`). `_index_async` (`:60-71`) opens `AsyncSessionLocal()` (NullPool, `db/session.py:84-93`) and `session.begin()`; resolves tenant via `tenant_service.resolve_tenant_id_for_ae(session, source_ae)` (`:65`).
9. **Patient upsert.** `get_or_create_patient` (`indexing_service.py:31-91`): select by `patient_id` (missing becomes `"ANONYMOUS"`, `:32`); insert inside a savepoint (`:42-50`) and adopt the winner on IntegrityError (`:51-60`). For non-HIS patients, name/DOB/sex are overwritten from this instance (`:79-90`, F-13). HIS-registered patients are only backfilled (`:62-78`).
10. **Study upsert.** `get_or_create_study` (`:94-159`): select by UID (missing becomes `"UNKNOWN"`, `:98`). On create: `Study(..., tenant_id=tenant_id)` and flush (`:103-115`; no savepoint, F-05). Then:
    - MWL close: `worklist_service.find_matching_step` / `complete_step` (`:119-122`).
    - Webhook `StudyReceived` (`:125-135`, `webhook_service.fire` spawns a thread) and script `StudyReceived` (`:136-143`), both before commit (F-14).
    - Auto-routing `routing_service.evaluate_and_route_study(study_uid, meta)` (`:145-154`), which uses a separate session and cannot see the uncommitted study (F-14).
    - On existing study: updates `study_description` only (`:155-158`).
11. **Storage backend pin.** `if storage_backend: study.storage_backend = storage_backend` (`indexing_service.py:240-241`).
12. **Series upsert.** `get_or_create_series` (`:162-187`): select by SeriesInstanceUID (missing becomes `"UNKNOWN"`, `:163`); insert or refresh `frame_of_reference_uid`; on late slices sets `triage_status = "PENDING"` (`:185-186`).
13. **Object size.** `storage_service.size(file_path, backend_name)` (`indexing_service.py:244-247`); 0 on exception.
14. **Instance upsert.** `upsert_instance` (`:190-222`): select by `sop_instance_uid` (missing becomes `"UNKNOWN"`, `:193`), insert or update `file_path`, `file_size_bytes`, `storage_backend`, `transfer_syntax_uid` (F-05 race).
15. **Side-effect rows.** `AuditLog(action="STORED", sha256_hash=sha256, performed_by="dicom-receiver")` (`:253-261`). If `DR_REPLICATION_ENABLED` (default False), a REPLICATED record for the study is set to PENDING (`:267-279`). If `COMPRESSION_ENABLED` and not GSPS (`:282`), one `CompressionJob(status="PENDING", method=DEFAULT_COMPRESSION)` is added (`:283-289`).
16. **Derived indexes.** GSPS (`:292-304`), KIN (`:307-319`), RDSR (`:322-334`). Each re-reads the full object and logs a warning on failure (never retried).
17. **Flush and commit.** `await session.flush()` (`:336`); `session.begin()` commits on context exit (`receiver.py:63`). Commit is the *queue hop* to the worker: `CompressionJob` rows are polled later.
18. **Storage Commitment hook.** After `asyncio.run` returns, `storage_commit_service.on_instance_stored(sop_uid)` (`receiver.py:95-100`) lists all PENDING `StorageCommitRequest` rows (`storage_commit_service.py:145-154`), re-verifies references, and calls `commit_sender.queue_report` (`:181-185`, `commit_sender.py:77-81`) for requests that are now complete. This is the second queue hop, an in-memory `queue.Queue` consumed by thread `paxs-commit-sender`.
19. **Response.** Success returns 0x0000 (`receiver.py:101`). Any exception in steps 4-17 logs `C-STORE handler failed`, deletes the object key if `path` was set (`:104-109`, F-05), and returns 0xC212 (`:110`). Exceptions in the commit hook are logged and swallowed (`:99-100`).
20. **Later, worker hop.** `CompressionWorker._process_batch` (`worker.py:95-131`) claims PENDING jobs (`_claim_jobs`, `:51-92`), runs `_process_job` (`:133-181`): `storage_service.compress` (`storage_service.py:408-441`) to `compression_service.compress_file` (`:182-245`), atomic `os.replace`; AuditLog `COMPRESSED` (`worker.py:158-168`); thumbnail to `.thumbnails/{sop}.png` (`:169-174`, `thumbnail_service.generate_thumbnail`). Commit at `:100`.
21. **Later, triage hop.** If `TRIAGE_RUN_IN_WORKER` (`:101-103`), `_process_triage` (`:241-348`) claims up to 5 PENDING series, runs detectors (`triage_service.run_detectors_on_series`, `:122-160`), inserts `TriageFlag` rows, sets DONE.
22. **Warm hop (on view).** `schedule_warm` (`warm_service.py:50-72`) is triggered by QIDO/WADO requests (`api/v1/dicomweb.py:207, 334`) and makes missing thumbnails and Redis caches.

Queue/hop summary: (a) association thread to fresh event loop (same thread, no queue); (b) DB `compression_jobs` table polled by the worker; (c) `queue.Queue` to commit-sender thread; (d) DB status on `series.triage_status` with optional Redis lock.

---

## 5. Query/Retrieve workflow

### 5.1 C-FIND (SCP, `receiver.py:206-293`)
- Level from `QueryRetrieveLevel` (default STUDY, `:218`). Unknown levels return success with no matches (N-06).
- Tenant: `tenant_service.resolve_tenant_id_for_ae_sync(session, calling_ae)` (`:226-228`); mapped AE sees its tenant; unmapped sees `tenant_id IS NULL` (`query_service.py:149-151`).
- **PATIENT** (`:229-251`): keys from `_PATIENT_KEYS` (`:116-121`). Non-empty values: `*` becomes `%` with LIKE, otherwise equality (`:231-237`). Only patients that own a study in tenant scope (`:238-245`).
- **STUDY** (`:252-262`): `_query_filters(query, _STUDY_KEYS)` (`:253`) non-empty filters; `query_service.study_query` (`:254-256`): PatientName/AccessionNumber/StudyDescription/ReferringPhysicianName use fuzzy substring (`_apply` kind "like", `query_service.py:129-134`); PatientBirthDate/StudyDate range (`_range`, `:52-59`); other fields equality or `*`-LIKE. Returns Patient fields from `study.patient` (`:260`) and Study fields from `_STUDY_ROW_KEYS` (`:136`).
- **SERIES** (`:264-276`): `_SERIES_KEYS` filters only; StudyInstanceUID not applied (F-11). Response includes patient, study and series fields.
- **INSTANCE** (`:278-291`): `_INSTANCE_KEYS` filters only (SOPInstanceUID, SOPClassUID, InstanceNumber, Rows, Columns); parent UIDs not applied (F-11).
- Yields `(0xFF00, ds)` per match and `(0x0000, None)` at end (`:293`). Session stays open across yields (F-21).
- Result set is unbounded (no limit).

### 5.2 C-GET (SCP, `receiver.py:525-558`)
- Identifier missing: yields 0 (success, zero sub-ops) (`:538-540`).
- Tenant: as C-FIND (`:545-548`).
- Keys from `_matching_instance_keys(query, level, tenant_id)` (`:550`): see F-03 for the unscoped default.
- `yield len(keys)` (`:551`), then lazy `read_dataset` and `yield (0xFF00, ds)` per instance (`:552-555`); read failure yields `(0xA701, None)` (`:557-558`).
- pynetdicom auto-sends the final response.

### 5.3 C-MOVE (SCP, `receiver.py:460-519`)
- Destination: `event.move_destination` (`:480`), resolved with `get_node_sync(dest_aet)` (`:486`). Unknown: yields `(None, None)` for 0xA801 (`:487-490`).
- Logs destination host:port (`:493`).
- Tenant from calling AE (`:495-499`).
- Keys from `_matching_instance_keys` (`:504`); each object read eagerly into `datasets` (`:503-508`; F-20).
- Yields `(host, port)`, then count, then `(0xFF00, ds)` per dataset (`:516-519`). pynetdicom then opens an association to the destination with the C-STORE sub-ops (requested contexts at `receiver.py:605`).

### 5.4 `_matching_instance_keys` (`receiver.py:425-457`)
- Default: `instance_query(None, None, {}, tenant_id=..., include_all=False)` (`:436-438`).
- Narrowed only when the identifier contains the UID for the matching level: STUDY (`:439-443`), SERIES (`:444-448`), INSTANCE (`:449-453`).
- For each instance the object must exist (`storage_service.exists`, `:455`).
- PATIENT level and STUDY level without `StudyInstanceUID` fall to the default (F-03).

### 5.5 Outbound (SCU) association creation (`sender.py`)
- Each helper: `ae = AE(ae_title=our_ae_title)` (default `"PAXS_SCU"`); `ae.add_requested_context(...)`; `ae.associate(host, port, ae_title=remote_ae, tls_args=_tls_args(host))` (e.g., `:63, :99, :250, :328-332, :553, :578`).
- `_tls_args(peer_host)` returns `(client_context(), peer_host)` when `DICOM_TLS_ENABLED` (`sender.py:37-47` to `tls.py:170-181`). With no CA and no pinned cert, the context is unverified (`tls.py:163-167`).
- Node resolution: `DicomNode` by `ae_title` (`sender.py:196-203`, `:418-425`, `:790-797`); must be enabled.
- Pull (`pull_from_node_sync`, `:398-544`): if PatientID given without StudyInstanceUID, runs a STUDY C-FIND first and then one C-GET per study (`:427-482`); else a single C-GET with the level and filters (`:506-534`). Ingest per instance via `_ingest_pulled_dataset` (`:364-395`).
- Push (`send_to_node_sync`, `:772-806`): `resolve_keys_sync` (`:663-725`), then `send_keys` (`:728-769`), one association per instance.

---

## 6. HL7 workflow

- **Listener.** `HL7Listener` (`hl7.py:694-758`): binds `HL7_HOST:HL7_PORT` (default `0.0.0.0:2575`, config.py:176-177) with `SO_REUSEADDR` (`:714`), `listen(5)` (`:716`), 1 s accept timeout (`:717`). Enabled only when `HL7_ENABLED` (False by default, config.py:175). Started from `main.py:391-392`.
- **Connection.** One thread per accepted socket (`:731-735`). `recv(4096)` loop with 5 s per-recv timeout (`:740, :744`). Framing: messages delimited by `\x1c\x0d` (`:749-751`). Leading `\x0b` is stripped in `_handle_message` (`:441`).
- **Dispatch** (`_handle_message`, `:439-469`):
  - Requires `MSH|` as the first segment (`:446-448`); message type from MSH-9 (`:449`).
  - `ORM^O01*` to `parse_orm_message` (`:450-453`) then `_upsert_order` (`:398-423`).
  - Otherwise `parse_adt_message` (`:455`). Trigger A40 to `handle_adt_feed_patient_merge` and A04/A08 to `handle_adt_feed_patient_registration` (`:456-461`, external `pix_pdq_service`), then return.
  - `QBP^Q23*` to `_handle_pix_query` (`:462-464`); `QBP^Q22*` to `_handle_pdq_query` (`:465-467`).
  - Remaining triggers in `_HANDLED_TRIGGERS` (A01, A31; `:47`) to `_upsert_patient` (`:468-469`).
- **ADT upsert** (`_upsert_patient`, `:282-299`): `_ensure_patient` (`:253-279`) creates or adopts a Patient (IntegrityError adoption at `:269-276`), `_apply_demographics` sets `source='his'` and overwrites name/DOB/sex/phone (`:426-436`). A01 (in `_SCHEDULING_TRIGGERS` `:52`) also calls `_create_default_step` (`:216-250`): one SCHEDULED step per patient per day (guard at `:225-235` is check-then-insert, acknowledged at `:236-239`); `sps_id = "SPS-" + 8 hex` (`:243`); station AET from `MWL_DEFAULT_STATION_AET` (`:244`).
- **ORM^O01 order** (`_upsert_order`, `:398-423`): `_ensure_patient` then control:
  - NW or XO (`_ORDER_CREATE`, `:55`): `_upsert_order_step` (`:335-373`): find by accession (filler), then by placer (`_find_open_order_step`, `:302-332`); creates a SCHEDULED step with a new `sps_id` or updates accession, requested_procedure_id, description, modality, start date/time, performing physician, station AET.
  - CA, OC, DC, CR (`_ORDER_CANCEL`, `:58`): `_cancel_order_steps` (`:376-395`) sets status CANCELED for open steps matching accession or placer.
  - Other controls ignored (`:420-422`).
- **ORM field mapping** (`parse_orm_message`, `:136-213`): OBR-2/ORC-2 placer, OBR-3/ORC-3 filler, OBR-4.1/4.2 procedure code and description, OBR-6/7 date-time, ORC-12/OBR-16 provider, ZDS-1 modality.
- **PIX/PDQ** (`:581-639`): QPD parsed; query executed via `pix_pdq_service` (out of scope); response built (`:642-691`) and logged at DEBUG only (`:607-609, :637-639`). **Nothing is sent back** (`:607`, TODO).
- **ACK behaviour.** No ACK or NAK is produced for any message type. `_handle_connection` never writes to the socket (`:739-758`).

---

## 7. Background worker

- **Start.** `main.py:387-388` `start_worker()` when `WORKER_ENABLED` (True). CLI: `python -m app.services.worker` (`worker.py:489-511`), `--once`, `--poll`, `--check-codecs`.
- **Loop** (`CompressionWorker.run`, `:40-48`): each tick calls `_process_batch()` then waits `WORKER_POLL_SECONDS` (3.0) on `_stop`. Exceptions are logged (`Worker batch error`) and the loop continues.
- **Per tick** (`_process_batch`, `:95-131`), one sync session:
  1. Claim up to `WORKER_BATCH_SIZE` (15) PENDING `CompressionJob` (`_claim_jobs`, `:51-92`): SELECT ids, guarded UPDATE PENDING to RUNNING, commit, re-select RUNNING (`:63-92`). Atomic per row.
  2. For each claimed job: `_process_job` (`:133-181`). Missing object sets FAILED (`:138-144`). Otherwise `storage_service.compress` (`:146-147`), updates Instance fields, job DONE; AuditLog COMPRESSED (`:158-168`); thumbnail (`:169-174`). Any exception sets FAILED with message (`:177-181`). No retry.
  3. Commit the batch (`:100`).
  4. Triage if `TRIAGE_RUN_IN_WORKER` (`:101-103`, `_process_triage` `:241-348`): Redis lock `paxs:lock:worker:triage` (TTL 30 s, `:256-261`); claims up to 5 series PENDING to RUNNING (`:265-282`); detectors; `TriageFlag` rows (`:326-336`); DONE. Failure re-queues claimed series to PENDING (`:340-348`), no retry cap.
  5. Lifecycle sweep every `LIFECYCLE_SWEEP_EVERY_TICKS` when `LIFECYCLE_RUN_IN_WORKER` (`:105-109`, `:225-238`).
  6. Routing sweep every `ROUTING_SWEEP_EVERY_TICKS` when `ROUTING_RUN_IN_WORKER` (`:111-115`, `:209-222`, `routing_service.process_queue_batch`).
  7. DR replication sweep every `DR_REPLICATION_SWEEP_EVERY_TICKS` when `DR_REPLICATION_ENABLED` (`:117-126`).
  8. Session pruning every `SESSION_SWEEP_EVERY_TICKS` (60) (`:128-131`, `:184-206`): deletes expired or revoked `Session` rows.
- **Recovery at start** (`_recover_stuck_jobs`, `:367-386`): resets all RUNNING CompressionJobs and RUNNING triage Series to PENDING (F-08). Called from `start_worker` (`:358`) and `run_standalone` (`:424`).
- **Concurrency.** Single thread per process. Multi-process safety relies on the atomic claim (`:51-92`) and is undermined by the start-time reset (F-08). Compression is CPU-bound and runs in the GIL-shared API process when the embedded worker is used.
- **Retries.** Compression: none (FAILED terminal). Triage: unlimited requeue on exception. Commit reports: none for FAILED (`storage_commit_service`/`commit_sender` select PENDING only).
- **Other threads.** `paxs-commit-sender` (`commit_sender.py:92-111`): 1 s poll and stale sweep (`_finalize_stale`, `:225-249`). Warm threads, one per study open (`warm_service.py:65-72`). HL7 listener and per-connection threads. pynetdicom association threads. Thumbnail generation is only called from the worker (`worker.py:170`) and warm (`warm_service.py:133`).

---

## 8. Code review findings

Each finding has the severity, file:line, the offending code (quoted), what is wrong, a concrete failure scenario, and a suggested fix. Only verified items are listed; verification method is shown in brackets (see section 8.2 for verification run details).

### 8.1 Findings

#### BLOCKING

**F-01. Storage backend package is not in the repository; the server cannot import.** [verified: git check-ignore, existence check, git history empty]
- `app/services/storage_service.py:35`: `from app.services.storage import get_storage_backend`
- `app/services/storage_service.py:38`: `from app.services.storage.backend import StorageError`
- `.gitignore:9`: `storage/`
- An unanchored `storage/` pattern also matches `app/services/storage/`. `git check-ignore -v app/services/storage/backend.py` reports `.gitignore:9`. The directory has no history and is absent from disk (no `app/services/storage/` and no `storage.py`).
- Import chain: `app/main.py:31` imports `app.dicom.receiver`, which imports `storage_service` (`receiver.py:42`); `app/main.py:32` imports `app.services.worker`, which imports `storage_service` (`worker.py:21`). `app/api/v1/storage.py:34` and `replication_service.py:39` also import the missing package.
- Failure scenario: fresh clone, then `uvicorn app.main:app` raises `ModuleNotFoundError: No module named 'app.services.storage'`. Tests `tests/test_storage*.py`, `test_multi_storage.py`, `test_cstore.py` cannot run.
- Fix: change `.gitignore:9` to `/storage/` (repo-root data directory only), then `git add app/services/storage/` from the author's working copy. Confirm the package exists on the author's machine (open question 1).

**F-02. No calling or called AE-title validation on the DIMSE SCP; default bind on all interfaces.** [verified: grep]
- `app/dicom/receiver.py:577`: `ae = AE(ae_title=ae_title or settings.DICOM_AE_TITLE)`
- `receiver.py:607-615` lists only C-STORE, C-FIND, C-GET, C-MOVE, N-CREATE, N-SET, N-ACTION handlers. Repo-wide grep for `EVT_REQUESTED`, `EVT_ACCEPTED`, `require_calling_aet`, `require_called_aet`, `ALLOWED_AE` returns no matches.
- Defaults: `DICOM_HOST="0.0.0.0"` (`config.py:29`), TLS off (`config.py:39`), client certificate verification only when `DICOM_TLS_CA` is set (`tls.py:133-136`).
- Impact: any host that can reach port 10400 can use any calling AE title for C-STORE (overwrite, F-05), C-FIND/C-GET/C-MOVE (F-03), MWL read (F-12), MPPS N-CREATE/N-SET (`receiver.py:330-379`, modifies worklist), and N-ACTION (`receiver.py:385-419`). `dicom_nodes.ae_title` (`models.py:745`, unique) exists but is never consulted for inbound.
- Fix: EVT_REQUESTED handler that accepts only AEs present in `dicom_nodes` (or an explicit allowlist setting), `ae.require_called_aet = True` set to DICOM_AE_TITLE, configurable bind address, and TLS plus client-cert as the production profile.

**F-03. C-GET and C-MOVE return every instance in scope when the identifier lacks the parent UID; empty UIDs also match everything.** [verified: code path reading; query_service.py:186-187]
- `app/dicom/receiver.py:436-438`: `stmt = query_service.instance_query(None, None, {}, tenant_id=tenant_id, include_all=False)`
- Narrowing happens only for `level == "STUDY" and "StudyInstanceUID" in query` (`:439`), `"SERIES" and "SeriesInstanceUID"` (`:444`), `"INSTANCE" and "SOPInstanceUID"` (`:449`). PATIENT level, and STUDY/SERIES without the UID, fall through to the unfiltered default.
- Empty value: `"StudyInstanceUID" in query` is true for an empty element, `str(query.StudyInstanceUID)` is `""`, and `query_service.py:186` `if study_uid:` skips the filter. So `""` also returns everything.
- Scope: unmapped AE gets `Study.tenant_id IS NULL` (`query_service.py:149-150`), which covers all legacy rows.
- Consumers: `handle_move` (`receiver.py:504`, eager read `:506`) and `handle_get` (`:550-555`).
- Failure scenario: from any reachable host, `C-GET` with `QueryRetrieveLevel=PATIENT` (or STUDY with no UID) returns the whole archive. `C-MOVE` with the same identifier pushes it to any `dicom_nodes` entry (`:486-490`) without checking that the caller may direct data there.
- Fix: reject Q/R retrieve identifiers that lack the level's unique key or carry empty UID values (return 0xA900/0xC000 per PS3.4 Q/R rules); cap retrieved count; authorize destination per caller.

**F-04. Path-key traversal: a DICOM UID of `..` survives `_safe_component`.** [verified by running the function extracted verbatim from source]
- `app/services/storage_service.py:44`: `_UID_OK = re.compile(r"^[0-9.]{1,64}$")`; `:50-51`: `if _UID_OK.match(value): return value`.
- Because `.` is allowed, values made only of dots pass unchanged. Other separators are rewritten (`a/../b` becomes `a_.._b`, `..\..\x` becomes `.._.._x`).
- Results: `_safe_component('..') == '..'`; `instance_key('..', '1.2.3', '1.2.4') == '../1.2.3/1.2.4.dcm'`.
- Also `delete_study_files(study_uid)` (`:179-180`) calls `delete_tree(_safe_component(study_uid, "study"))`, i.e., `delete_tree("..")` (`:175-176`).
- Whether a `..` key escapes the storage root depends on the backend, which is missing (F-01), so this is not verified end to end. Until the backend is shown to resolve and bound keys to the root, treat as BLOCKING.
- Failure scenario: a modality sends StudyInstanceUID `..` in C-STORE; the object is written outside the storage root. An admin "delete study" for that study may call `delete_tree("..")` and remove the parent of the root.
- Fix: require at least one digit and forbid all-dot components: `re.compile(r"^(?=.*\d)[0-9.]{1,64}$")`, and reject leading/trailing dots; in the backend, resolve the full path and assert it is under the root.

**F-05. Concurrent or repeated stores delete archived objects (overwrite plus delete-on-error).** [verified: code reading, unique constraints in models.py]
- `app/services/storage_service.py:293-297`: key is deterministic (`instance_key`, `:62-67`) and `bk.save_bytes(key, ...)` overwrites unconditionally.
- `app/dicom/receiver.py:86` saves before indexing. `receiver.py:104-109`: `if path: ... storage_service.delete_file(path, backend_name=backend)` runs on any exception from the save/index block (`:79-101`).
- Racing inserts: `indexing_service.py:194-214` (Instance select-then-insert, no savepoint), `:99-115` (Study), `:164-179` (Series). Unique constraints: `models.py:330` (Study.study_instance_uid), `:392` (Series.series_instance_uid), `:438` (Instance.sop_instance_uid). Only the patient path has a savepoint (`indexing_service.py:42-60`).
- Scenario A: modality retransmits instance X while the first copy is still indexing. Both write the same key. The first commits. The second fails at flush with IntegrityError; its cleanup deletes the object. The committed row now points at nothing. Storage Commitment then reports 0x0110 (`storage_commit_service.py:71-77`), and WADO/C-GET fail for X.
- Scenario B: parallel association sends of a brand-new study. Two first slices both see no Study, both insert, one loses, and its own object is deleted; C-STORE returns 0xC212 for an instance that is lost unless the modality retries.
- Fix: (1) do not delete a key that may have existed before this request (check existence first, or write to a temp key and rename after commit); (2) savepoint plus IntegrityError re-select for Study, Series and Instance, as the patient path does; (3) compare sha256 before overwrite and log conflicts instead of replacing silently.

**F-06. TLS bootstrap deletes operator-supplied certificate and key on any load failure.** [verified: run against scratch files]
- `app/dicom/tls.py:49-57`:
  ```
  try:
      ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
      ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
      return str(cert), str(key)
  except (ssl.SSLError, OSError) as exc:
      log.warning("DICOM TLS: existing cert/key pair is invalid (%s) — regenerating", exc)
  cert.unlink(missing_ok=True)
  key.unlink(missing_ok=True)
  ```
- `tls.py:58-62`: when only one of the pair exists, both are unlinked.
- Verified: an operator cert and an unloadable key were both deleted and replaced by a self-signed pair. A lone operator cert (no key) was also replaced.
- Realistic triggers: encrypted private key (no passphrase support), key unreadable by the service account (`OSError`), truncated copy during deployment.
- Impact: the hospital-issued identity is silently destroyed. Clients that trust the CA reject the self-signed replacement. No backup is made.
- Fix: never unlink. Raise a clear error on load failure. Generate only when both paths are absent, and never overwrite. Create the key with mode 0600 (`tls.py:69-78` and `:110-116` do not restrict permissions).

**F-07. HL7 listener never acknowledges, never answers queries, and one malformed message drops the connection.** [verified: grep for send calls, extracted parser run]
- No ACK: `hl7.py` contains no `send`, `sendall` or `write` call (grep). `_handle_connection` (`:739-758`) only reads.
- PIX/PDQ responses are built (`:642-691`) but only logged at DEBUG (`:607-609`, `:637-639`), with `# TODO: Send response back over MLLP connection` (`:607`).
- `_handle_connection` wraps only `OSError` (`:752-753`). `_handle_message(msg)` (`:751`) is not protected.
- Verified: `parse_orm_message` on an ORM^O01 with no OBR raises `AttributeError: 'NoneType' object has no attribute 'split'` (`hl7.py:178` `placer = _segment(obr, 2) or ...` with `obr=None`; also `:180, :187-189` unguarded).
- Consequence: the exception escapes the thread (logged by the threading excepthook, not `log`), the socket is closed, and messages already buffered in the same read are discarded. The sender sees no ACK and retries. Processing is mostly idempotent (accession and placer matching, `:302-332`), but the default-step guard is check-then-insert (`:225-248`), so replays can create duplicates.
- Fix: ACK every message (MSA|AA|<control id>, AE/AR on errors); catch per-message exceptions; guard optional segments; implement the MLLP reply for PIX/PDQ; never drop buffered messages.

#### IMPORTANT

**F-08. Startup recovery resets every RUNNING job, including jobs owned by live workers; a mid-batch exception strands jobs.** [verified: code reading]
- `app/services/worker.py:367-386`: `update(CompressionJob).where(CompressionJob.status == "RUNNING").values(status="PENDING")`, plus RUNNING triage series reset.
- Called from `start_worker` (`:358`) and `run_standalone` (`:424`). `run_standalone` is documented for horizontal scale-out (`worker.py:399-412`: "N worker processes ... each process claims its own batch"). The embedded worker also starts in the API process (`main.py:387-388`).
- Scenario: standalone worker B starts while A is compressing instance X. X returns to PENDING; B claims and compresses X concurrently. Both use the same temp path `path.with_suffix(path.suffix + ".tmp")` (`compression_service.py:232`), then `os.replace` (`:234`). Interleaved writes or a corrupt object can result.
- Second issue: `worker.py:140` (`storage_service.exists(...)`) is outside the try in `_process_job` (`:146-181`), so an exception there aborts the batch before `session.commit()` (`:100`). The RUNNING claims stay committed (`:81`) and are not reclaimed until the next restart. The loop logs `Worker batch error` (`:45-46`) and keeps polling.
- Fix: lease-based claiming (`claimed_at`/`lease_expires`, heartbeat), recover only expired leases, per-job try/except, unique temp file names.

**F-09. Storage Commitment: transaction UIDs are global and re-openable, the fallback path skips TLS, and the 2-second sweep issues permanent false failures.** [verified: code reading]
- `app/services/storage_commit_service.py:120-137`: request is looked up by `tx_uid` only; payload replaced (`:134-135`); `req.status = "PENDING"` (`:136-137`) even when COMPLETED.
- `app/dicom/receiver.py:409` `register_association(tx_uid, event.assoc)` then `commit_sender.py:71` `_ASSOCS[tx_uid] = assoc` overwrites any earlier association for that UID. The N-EVENT-REPORT for a victim's transaction is routed to the attacker's association, and the report lists referenced SOP UIDs and verdicts.
- `commit_sender.py:205`: `assoc = ae.associate(node.host, node.port, ae_title=ae_title)`. No `tls_args`; compare `sender.py:578` `tls_args=_tls_args(host)`. With `DICOM_TLS_ENABLED=true` the fallback cannot succeed.
- `commit_sender.py:41` `FINALIZE_AFTER_SECONDS = 2.0`; `:248-249` finalises any PENDING request older than 2 s, including instances still in flight (parallel association sends are common). The report is then sent with failures, and later arrivals are never re-reported (`storage_commit_service.py:149`, `on_instance_stored` only looks at PENDING).
- Fix: key requests by (AE, tx_uid); refuse to reopen COMPLETED or FAILED; apply TLS to the fallback; make the sweep window configurable and tied to the N-ACTION timeout.

**F-10. Quotas are not enforced on DICOM ingest; the enforcing helpers have no callers.** [verified: grep]
- `app/services/storage_service.py:277-279` docstring: "This version does NOT enforce quotas". `save_dataset` (`:274-298`) has no `check_quota_before_write` call. `receiver.py:86` and `sender.py:383` use `save_dataset`.
- `save_dataset_with_quota` (`:301-340`) and `save_bytes_with_quota` (`:256-271`) have no callers in the repository.
- `app/services/quota_service.py:13` docstring says `storage_service.save_dataset()` calls `check_quota_before_write()`. This contradicts the code.
- Fix: enforce in `handle_store` or `index_new_instance` before the write, using `tenant_service`'s tenant.

**F-11. C-FIND SERIES and INSTANCE levels ignore the parent UIDs.** [verified: code reading]
- `app/dicom/receiver.py:265` `_query_filters(query, _SERIES_KEYS)` then `:266-268` `series_query(None, filters, ...)`.
- `receiver.py:279` `_query_filters(query, _INSTANCE_KEYS)` then `:280-282` `instance_query(None, None, filters, ...)`.
- `_SERIES_KEYS` (`:137-143`) and `_INSTANCE_KEYS` (`:144-150`) do not include StudyInstanceUID or SeriesInstanceUID, so they are never applied.
- Scenario: a SERIES query with StudyInstanceUID=A returns series from every study in scope. The C-GET/C-MOVE path does apply the UID (`receiver.py:439-453`), so the two behaviours disagree.
- Fix: pass the parent UID into `series_query` and `instance_query`.

**F-12. MWL C-FIND has no tenant scope and no result limit.** [verified: code reading]
- `app/dicom/receiver.py:192-203`: `worklist_service.build_statement(filters)` then `session.execute(...).all()`, with no tenant argument. Every scheduled procedure step with patient name and ID is returned to any AE (depends on F-02).
- Fix: scope by tenant or AE mapping; cap results.

**F-13. Inbound data overwrites patient demographics and collapses identifiers into placeholder values.** [verified: code reading]
- `app/services/indexing_service.py:79-90`: for non-HIS patients, every incoming instance overwrites `patient_name`, `patient_birth_date` and `patient_sex` (last writer wins).
- Placeholders: `"ANONYMOUS"` (`:32`), `"UNKNOWN"` for StudyInstanceUID (`:98`), SeriesInstanceUID (`:163`) and SOPInstanceUID (`:193`). Object keys use `"UNKNOWN"` too (`storage_service.py:281-283`).
- Impact: unrelated studies merge under one study row; instances without SOPInstanceUID overwrite each other on disk and in the index (`upsert_instance` sets `file_path`, `:216-218`), so only the last file survives.
- Fix: reject instances missing mandatory UIDs (C-STORE failure status) rather than inventing values; do not overwrite demographics from unauthenticated instances; log conflicts.

**F-14. Auto-routing never matches a brand-new study, and StudyReceived fires before commit.** [verified: code reading of routing_service.py:28-60, 218-233]
- `app/services/indexing_service.py:145-148` calls `routing_service.evaluate_and_route_study` while the study is only flushed inside the caller's transaction (`:115`). It is committed only at `receiver.py:63`.
- `routing_service.py:224` opens a separate `AsyncSessionLocal()`. `:46-52` selects the Study by UID, and `:54-55` returns `[]` when it is not found. The uncommitted row is not visible to the separate session, so no rule matches on the first instance. This depends on standard isolation; verify on the target DB.
- Same window for `webhook_fire("StudyReceived")` (`indexing_service.py:128-135`) and `script_fire` (`:136-143`). They run before commit. If the transaction later rolls back (F-05), external systems were told about a study that does not exist. Webhook dispatch is threaded (`webhook_service.py:53-64`), so it does not block.
- Fix: move hooks and routing to after commit (outbox row or worker sweep).

**F-15. Volume pre-warm is dead code (missing `import asyncio`), with two related defects.** [verified: grep, module imports read]
- `app/services/warm_service.py:211`: `vol_ok = asyncio.run(_warm_vol_series())`. The module imports (`:24-34`) do not include `asyncio`; the only occurrence in the file is line 211. The NameError is caught at `:212-213` and logged at DEBUG.
- `REDIS_CACHE_ENABLED` defaults to True (`config.py:168`), so this path is reached on default config whenever Redis is reachable and the study has CT, MR or PT series.
- `warm_service.py:200` `r_client.scan(cursor=0, match=..., count=1)` returns one arbitrary page, so `if not matched` is unreliable and existing volumes are recomputed.
- `warm_service.py:181` uses `session` after the `with SyncSessionLocal() as session:` block (`:106-118`) has closed it, so a connection is checked out and never returned explicitly.
- Fix: import asyncio; use `scan_iter`; keep the session inside its with-block.

**F-16. Pull reports success even when sub-operations failed; ingest failures are acknowledged to the remote archive.** [verified: code reading]
- `app/dicom/sender.py:469-470`: `send_get(...)` return value is discarded. `:472-480` returns `"ok": True` regardless of association failure or C-GET status.
- `sender.py:314-326` `_on_store` returns `0x0000` even when `on_received` raised (`:320-323`). The comment at `:318-319` says the caller surfaces this, but the remote has already been told success.
- Impact: the remote archive believes the transfer completed and may delete its copy. The local side reports ok.
- Fix: aggregate `send_get` results; return a failure status to the remote on local ingest error.

**F-17. `send_keys` opens one association per instance; a formatting error hides the real failure reason.** [verified: TypeError reproduced]
- `sender.py:750`: `res = send_file(host, port, ae_title, local, ...)` for every target; `send_file` calls `ae.associate` each time (`:63`). A 1,000-instance study means 1,000 associations (and TLS handshakes when enabled).
- `sender.py:759`: `item["reason"] = res.get("reason") or f"status 0x{res.get('status', 0):04x}"`. When status is None (aborted association), this raises `TypeError: unsupported format string passed to NoneType.__format__`. It is caught at `:760-762` and recorded as the reason.
- Fix: one association for many C-STOREs (or a pooled association); format safely.

**F-18. Triage heuristic can raise HIGH "PNEUMOTHORAX" on ordinary positioning; attribution points at the wrong slice.** [verified: code reading]
- `app/services/triage_service.py:78-82`: splits the whole slice into halves and counts pixels below -900 HU in each. There is no body mask, so out-of-body air (background, typically about -1000 HU) is counted.
- `triage_service.py:92`: `if ratio > 2.0 and max_frac > 0.10:`. Background air alone can satisfy `max_frac > 0.10` on typical CT fields of view, so an off-centre patient can produce a HIGH finding.
- `triage_service.py:59`: `sop_uid = str(getattr(ds, "SOPInstanceUID", "")) or sop_uid` is overwritten on each iteration, so the stored `sop_instance_uid` is the last sampled slice, not the slice with the air region.
- `triage_service.py:61`: `ds.pixel_array.astype(np.float64)` decodes each slice to float64.
- The module docstring (`:1-11`) calls it a demo, but `TRIAGE_ENABLED` defaults to True (`config.py:207`), the worker runs it (`worker.py:101-103`), and the docstring says findings are surfaced "immediately" (`:4-5`).
- Fix: disable by default for clinical deployments; add a body mask; attribute findings per slice; do not present as clinical AI.

**F-19. Lossless compression overwrites objects in place with no verification; the stored hash goes stale.** [verified: code reading]
- `app/services/compression_service.py:232-234`: `ds.save_as(tmp)` then `os.replace(tmp, path)`. No decode-and-compare before replacing the original.
- `indexing_service.py:253-261` writes `AuditLog(action="STORED", sha256_hash=sha256)` for the uncompressed bytes. `worker.py:158-168` writes COMPRESSED without a new hash. Any integrity check that re-hashes the stored object will mismatch for every compressed instance.
- `compression_service.py:190` `pydicom.dcmread(str(path))` loads the whole file, pixels included, with no size limit.
- Fix: record a post-compression hash; verify decoded pixels equal the original before replacing; keep the original until verified.

**F-20. Unbounded memory on large objects and studies.** [verified: code reading]
- `receiver.py:503-508`: C-MOVE reads every matching dataset before the first sub-op (contrast the lazy C-GET read at `:552-557`).
- `sender.py:325`: C-GET buffers the whole study when `on_received` is None.
- `imaging.py:42` `arr = ds.pixel_array` decodes every frame of a multi-frame object. `imaging.py:32` `np.asarray(arr, dtype=np.float64)` makes an 8-byte-per-sample copy, then `np.percentile` runs on it. `triage_service.py:61` does the same per slice.
- `storage_service.py:149-150` `read_dataset` loads all bytes even with `stop_before_pixels=True`. `storage_service.py:294-298` builds the whole object in memory and re-reads it for the hash (remote backends download twice).
- `sender.py:655` `_materialize_for_send` loads the whole S3 object into memory; `sender.py:656-660` leaks the temp file if `fh.write` raises.
- `accelerator_service.py:65-70`: the disabled path calls `stow_upload(node, targets, stream=False)` with the full target list. Whether that builds the whole multipart body in memory is in `dicomweb_client.py` (not read).
- Fix: stream to disk or backend; limit frames decoded; cap per-request bytes.

**F-21. DB sessions stay open across generator yields during C-FIND, MWL, C-GET and C-MOVE.** [verified: code reading]
- `receiver.py:196-203` (MWL) and `:223-292` (C-FIND): `with SyncSessionLocal() as session:` encloses every `yield`. pynetdicom sends each response while the generator is suspended, so a slow SCU holds a pooled connection for the whole response.
- Combined with F-02 (any AE) and `DICOM_MAX_ASSOCIATIONS=16` (`config.py:32`), a few stalled SCUs can exhaust the sync pool used by the API and worker.
- Fix: fetch a bounded result set and close the session before yielding.

**F-22. HL7 listener: unbounded threads and buffer, slow-drip exposure, silent loss at EOF, no source allowlist.** [verified: code reading]
- `hl7.py:731-735`: one thread per connection, no cap.
- `hl7.py:744-747`: `buffer += chunk` with no maximum. `hl7.py:740` `conn.settimeout(5.0)` is applied per `recv`, so a sender trickling bytes keeps the connection open indefinitely.
- `hl7.py:745-750`: on EOF the loop exits and bytes after the last `\x1c\x0d` are discarded. A sender that omits the MLLP end block loses its last message silently.
- Bind default `0.0.0.0` (`config.py:176`); no allowlist setting.
- Fix: maximum message size, connection cap, absolute deadline per connection, process or NAK trailing data, and an IP allowlist setting.

**F-23. PIX/PDQ query parsing: QPD-1 and QPD-2 swapped; IHE issuer format not parsed; PIX MSH misplaces message type.** [verified: extracted parser run]
- `hl7.py:486-487`: `"query_tag": parts[1]`, `"profile": parts[2]`. Per HL7 v2 and IHE, QPD-1 is the Message Query Name (e.g., "IHE PIX Query") and QPD-2 is the Query Tag (e.g., "QRY_123"). Verified: `query_tag='IHE PIX Query'`, `profile='QRY_123'`. MSA and QAK would therefore echo the wrong tag (`hl7.py:647-648, 671-672`).
- `hl7.py:510-513` takes the assigning authority from component 2 of QPD-3. IHE PIX uses `ID^^^&OID&ISO`. Verified: issuer is `''`, so the identifier query branch (`hl7.py:595`) never runs. Confirm against the partner profile.
- `hl7.py:646`: `RSP^K23` sits in MSH-8, not MSH-9. Splitting the segment on `|`, the message type is at index 7 and MSH-9 is index 8. The PDQ MSH (`:670`) is correct. Latent, because nothing is sent yet.
- Outbound fields (`hl7.py:654-661`, `:679-689`) interpolate DB values without escaping `|^&~\`.
- Fix: correct the QPD mapping, parse `&` subcomponents, fix the PIX MSH, and escape delimiters.

**F-24. HL7 A04 never schedules a default worklist step; A08 goes to a different handler.** [verified: control flow]
- `hl7.py:52` `_SCHEDULING_TRIGGERS = {"A01", "A04"}`, and the docstring (`:49-52`) says A04 opens a default step.
- `hl7.py:455-461`: for A04, A08 and A40 the code calls `handle_adt_feed_patient_registration` or `handle_adt_feed_patient_merge` and returns before `_upsert_patient` (`:468-469`). So `_upsert_patient` and `_create_default_step` are unreachable for A04. A08 demographic updates go to the external `pix_pdq_service`, not `_upsert_patient`.
- Fix: decide one path for A04/A08 and make the docstring match.

**F-25. Performance: a new DB connection and event loop per C-STORE; a full PENDING scan on every store.** [verified: code reading]
- `receiver.py:88` `asyncio.run(...)` per instance. `db/session.py:79-86` uses NullPool by design, so each store opens a new DB connection (correct across loops, but costly).
- `storage_commit_service.py:145-154` loads all PENDING rows and parses each JSON payload. `on_instance_stored` (`:157-178`) runs on every C-STORE (`receiver.py:96`), opens a new `SyncSessionLocal` (`:168`), and re-verifies all references of each matching request (`:171`). Cost is roughly (stores) x (references).
- `storage_service.py:96-110` `resolve_study_backend` opens a session per store.
- Fix: batch per association; index the status column; cache backend resolution per study.

**F-26. `_ingest_pulled_dataset` diverges from the C-STORE pipeline.** [verified: code reading; transfer-syntax consequence unverified]
- `sender.py:364-395` vs `receiver.py:74-110`: no storage-commit hook, no cleanup on failure, and no `file_meta` set (`sender.py:380-383`). `save_dataset` then forces `TransferSyntaxUID = ExplicitVRLittleEndian` (`storage_service.py:286-291`) regardless of the peer's syntax. Whether the saved body and meta agree for non-Explicit peers is not verified.
- Fix: share one ingest function between C-STORE and C-GET.

**F-27. Listener bind failures are logged and swallowed; the app keeps running with no DICOM or HL7 listener.** [verified: code reading]
- `receiver.py:654-656` and `hl7.py:718-720` log at ERROR and return. Health checks will not notice.
- Fix: fail startup, or expose listener status on the health endpoint.

**F-28. TLS fail-open when a CA path is configured but missing (SCU side).** [verified: code reading]
- `tls.py:150-152`: `if ca and Path(ca).exists():`. If `DICOM_TLS_CA` points at a missing file, the code falls through to pinned-cert trust (`:155-160`) or to an unverified `CERT_NONE` context (`:163-167`) with only a warning (`:161-162`). The server-side equivalent (`server_context`, `:133-136`) would fail at `load_verify_locations`.
- Fix: raise when a configured CA path does not exist.

#### NIT

- **N-01** `tls.py:69-78`, `:110-116`: private key written with default file permissions; no chmod 0600.
- **N-02** `sender.py:63-74` (`send_file`) and `sender.py:547-560` (`send_echo`): `send_c_store`/`send_c_echo` run outside try/finally, so an exception leaks the association (`send_find`, `send_move`, `send_get`, `_send_mpps_n_op` do use try/finally).
- **N-03** `sender.py:656-660`: `mkstemp` file leaks if `fh.write` raises (no try).
- **N-04** `commit_sender.py:158`: `getattr(assoc, "is_alive", False)`. pynetdicom documents `is_alive()` as a method (not verified against the installed version). If it is a method, the bound method is always truthy, so a dead association is never skipped.
- **N-05** `receiver.py:409` registers the association before `handle_action` (`:411`). If `handle_action` raises (e.g., no references, `storage_commit_service.py:111-112`), the `_ASSOCS` and `_CREATED_AT` entries are never removed, because no DB row exists for the sweep to finalise.
- **N-06** `receiver.py:218` and `:293`: an unknown QueryRetrieveLevel yields success with no matches instead of a failure status.
- **N-07** `receiver.py:558`: sub-operation read failure yields `0xA701`. PS3.7 Annex C defines 0xA701 as "unable to calculate number of matches" (C-FIND); the sub-operation code is 0xA702. Verify against the spec and pynetdicom.
- **N-08** `query_service.py:46-49`, `:129-134`; `receiver.py:236`: only `*` is translated (`?` is not supported); user `%` and `_` are not escaped; "like" fields use substring matching (`%value%`), which is broader than DICOM wildcard matching.
- **N-09** `receiver.py:122-131`: `ModalitiesInStudy` is supported by `query_service.py:63-72` but missing from `_STUDY_KEYS`, so it is ignored.
- **N-10** `receiver.py:161-162` and `sender.py:223-224`: setattr failures are swallowed silently.
- **N-11** `indexing_service.py:282-289`: a CompressionJob is enqueued for every non-GSPS instance, including RDSR, KO, SR and encapsulated PDF, which the image codecs cannot compress. These produce recurring FAILED rows (`worker.py:177-181`).
- **N-12** `indexing_service.py:185-186`: every late slice sets `triage_status = "PENDING"`, so a long acquisition re-runs triage repeatedly on the same series.
- **N-13** `worker.py:340-348`: on triage exception, all of `pending_ids` (including series already marked DONE in this batch) go back to PENDING, which can duplicate TriageFlag rows. `worker.py:322-325` `scalar_one()` raises for a deleted study, which then requeues forever.
- **N-14** `warm_service.py:65-72`: one thread per opened study, with dedup per study but no global cap. `warm_service.py:162-174`: raw decoded frames cached in Redis for 1 h.
- **N-15** `storage_service.py:197-232`: `_usage_cache` is an unsynchronised dict (benign races) and is per process only.
- **N-16** `accelerator_service.py:72`: `validate_web_url` is described as a DNS-rebinding guard, but it is only a pre-check. Whether the actual connection pins the validated IP is in `dicomweb_client.py` (not read).
- **N-17** `receiver.py:77` and `:110`: processing failure returns 0xC212. Verify against PS3.4 Annex B for the Storage service.
- **N-18** `indexing_service.py:300-334`: GSPS, KIN and RDSR indexing failures are logged as warnings and never retried; the instance is stored without its derived index.
- **N-19** `receiver.py:99-100`: a storage-commit hook failure is logged and swallowed, so the pending request resolves only via the 2-second sweep (F-09), producing a false failure.
- **N-20** `parser.py:80-86`: `_clean` does no length or charset checks before DB insert; overlong values fail the DB write and return 0xC212.
- **N-21** `hl7.py:198-204`: only the first PID-3 repetition is used; additional identifiers are dropped.
- **N-22** `hl7.py:225-248` and `:337-345`: check-then-insert for default and order steps (acknowledged in comments at `:236-239`).

### 8.2 Verification performed

| # | Check | Result |
|---|---|---|
| V1 | Extracted `_UID_OK`, `_UID_STRIP`, `_safe_component`, `instance_key` verbatim from `storage_service.py` and ran them | `'..'` returned unchanged; `instance_key('..','1.2.3','1.2.4') = '../1.2.3/1.2.4.dcm'` (F-04) |
| V2 | Extracted `_segment`, `_name`, `_dt_parts`, `_xcn_name`, `parse_orm_message`, `parse_adt_message`, `_parse_qpd_segment`, `_extract_pix_query_params` verbatim from `hl7.py` and ran them | ORM^O01 without OBR: `AttributeError: 'NoneType' object has no attribute 'split'` (F-07). QPD: `query_tag='IHE PIX Query'`, `profile='QRY_123'`, issuer `''` for `^^^&OID&ISO` (F-23) |
| V3 | Replicated the f-string at `sender.py:759` with `None` | `TypeError: unsupported format string passed to NoneType.__format__` (F-17) |
| V4 | Extracted `ensure_certificate` verbatim from `tls.py`, ran against scratch files (unloadable key and lone cert) | Both operator files deleted and replaced with a self-signed pair (F-06) |
| V5 | `git check-ignore -v` on `app/services/storage/backend.py` and `__init__.py`; `git log --all -- app/services/storage`; filesystem existence | Ignored by `.gitignore:9`; no history; absent on disk (F-01) |
| V6 | Grep: `EVT_REQUESTED`, `EVT_ACCEPTED`, `require_calling_aet`, `require_called_aet` | No matches (F-02) |
| V7 | Grep: `save_dataset_with_quota`, `save_bytes_with_quota`, `check_quota_before_write` | Definitions only; no callers (F-10) |
| V8 | Grep: `asyncio` in `warm_service.py` | Only line 211 (F-15) |
| V9 | Grep: `sendall`, `send(`, `write` in `hl7.py` | No matches (F-07) |
| V10 | Grep: `app/services/storage` importers | `app/api/v1/storage.py`, `replication_service.py`, `storage_service.py`, plus tests (F-01) |
| V11 | Config defaults (`config.py`) | Confirmed values cited above |
| V12 | `db/session.py:79-86` | NullPool for async engine (mitigates loop-binding concern, F-25) |

Not verified: pynetdicom behaviour (not installed: `ModuleNotFoundError: No module named 'pynetdicom'`); backend key normalisation (module absent); DB isolation semantics (no DB run); `dicomweb_client.py` (outside scope).

---

## 9. Open questions

1. Does the author's working copy contain `app/services/storage/` (untracked)? If yes, `.gitignore:9` is the defect (anchor it as `/storage/`). If no, the server has never run from this tree.
2. How does the storage backend map keys to paths? Does it `resolve()` and reject escapes, and what does `delete_tree` do with a prefix (F-04 severity depends on this)?
3. Which pynetdicom version is pinned (`requirements.txt` says `>=2.1`)? Confirm: `Association.is_alive` (method or property, N-04); `send_n_event_report` signature; `pynetdicom.dsutils.decode` path (`receiver.py:313`); storage context transfer syntaxes; default AE acceptance behaviour (F-02); status codes 0xA701/0xA702 (N-07) and 0xC212 (N-17).
4. Is port 10400 and the HL7 port reachable only from trusted networks in target deployments? If so, F-02 to F-05 drop in severity but remain integrity risks from any internal host.
5. Does any upstream interface engine ACK HL7 on PAXS's behalf? If modalities or RIS expect enhanced ACK mode from PAXS, F-07 is blocking.
6. Are the SERIES/INSTANCE C-FIND results without parent UIDs intentional for legacy modalities (F-11)?
7. Does `dicomweb_client.validate_web_url` / the httpx connection pin the resolved IP (N-16)? Does `stow_upload(stream=False)` build the full multipart body in memory (F-20)?
8. Is `send_n_event_report` from the `paxs-commit-sender` thread on an association owned by the N-ACTION thread supported by pynetdicom (thread-safety)?
9. Are standalone workers (`python -m app.services.worker`) used in production alongside the embedded worker (`main.py:387-388`)? This sets the severity of F-08.
10. If `COMPRESSION_ENABLED=False`, nothing in ingest generates `.thumbnails/` (only `worker.py:170` and `warm_service.py:133` call `generate_thumbnail`). Is that intended?
11. Should auto-routing fire only on the first instance of a study (`indexing_service.py:102-154`), and was the author's intent that the study be visible to the routing session (F-14)?
12. `pull_from_node_sync` with no filters at STUDY level (`sender.py:506-534`) pulls everything the remote returns. Does the API route forbid empty filters?
13. Does pydicom write a consistent object when `file_meta` is forced to Explicit VR LE for a pulled dataset decoded from another syntax (F-26)?
14. `ModalitiesInStudy` is excluded from `_STUDY_KEYS` (N-09). Intended?
15. HL7 A08 and A40 are routed to `pix_pdq_service` (out of scope). Does that module update demographics, and does it reproduce the PID-only mapping used by `_upsert_patient`?
