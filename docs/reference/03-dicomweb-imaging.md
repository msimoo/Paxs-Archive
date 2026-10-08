# DICOMweb (QIDO / WADO / STOW), volume / MPR / MIP, GSPS, KIN, DICOMDIR, export, sharing

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/02-dicomweb-volume.md](5a32aa4/02-dicomweb-volume.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

Date of review: 2026-10-08. Method: static reading only. No tests were run, no server was started, no HTTP requests were made. Every claim below cites code that was read in this session. Items marked **[inferred]** depend on library or runtime behaviour that was not executed.

Severity labels: **BLOCKING**, **IMPORTANT**, **NIT**.

---

## 1. Files covered

### 1a. In-scope files (all read in full)

| Path | Lines | Purpose |
|---|---|---|
| `app/api/v1/dicomweb.py` | 905 | DICOMweb router: WADO-URI, QIDO-RS, WADO-RS (metadata, study/series/instance, rendered, thumbnail, frames, bulk), STOW-RS |
| `app/services/dicomweb_service.py` | 308 | DICOM JSON builders, multipart build/parse, streaming STOW part parser, `dataset_from_bytes` |
| `app/services/dicomweb_client.py` | 509 | Outbound DICOMweb client for remote nodes (QIDO / WADO-RS pull / STOW-RS push), SSRF guard |
| `app/services/wado_rs_bulk_service.py` | 245 | Frame extraction, transfer-syntax decoding to raw, bulk attribute extraction, in-process frame LRU |
| `app/api/v1/volume.py` | 456 | Volume info / raw-data stream / MIP / MPR slice endpoints |
| `app/services/volume_service.py` | 1133 | Series -> z-ordered volume stack, bundle + array caches, reslice, MIP, VRT |
| `app/api/v1/fusion.py` | 42 | Fusion manifest route (FoR grouping) |
| `app/services/fusion_service.py` | 134 | Frame-of-Reference grouping and same-FoR pairs |
| `app/api/v1/gsps.py` | 68 | GSPS (presentation state) read routes |
| `app/services/gsps_service.py` | 444 | GSPS parser, indexing, lookups, overlay rendering (shutters, graphics, text) |
| `app/api/v1/hanging_protocols.py` | 136 | Hanging protocol CRUD and match |
| `app/services/hanging_protocol_service.py` | 93 | Hanging protocol queries and scoring |
| `app/api/v1/kin.py` | 114 | Key Image Note (KO) create / read routes |
| `app/services/kin_service.py` | 253 | KO parse, synthesize KO DICOM, index KIN rows, lookups |
| `app/api/v1/dicomdir.py` | 119 | DICOMDIR export (ZIP) and DICOMDIR preview routes |
| `app/services/dicomdir_service.py` | 194 | DICOMDIR record hierarchy and ZIP packaging |
| `app/api/v1/export.py` | 76 | De-identified study export (ZIP) |
| `app/api/v1/share.py` | 120 | Patient share links: create / public read / QR / delete |
| `app/services/sharing_service.py` | 79 | Share token creation, lookup, expiry, URL and QR helpers |

### 1b. Supporting modules read (targeted, to verify specific claims)

| Path | What was read |
|---|---|
| `app/api/v1/deps.py` | Full (120 lines): `get_current_user` (bearer only), `get_optional_user` (bearer or cookie, anonymous allowed), `get_current_user_any`, `require_destructive_role` |
| `app/main.py` | 40-160, 240-380, 420-610: middleware order, `ViewerAuthGateMiddleware` gated paths, `RateLimitMiddleware` skip for `/dicom-web`, CORS, router registration, fusion mounted only under `FHIR_ENABLED` |
| `app/core/config.py` | 120-335 plus grep: STOW caps, DICOMweb client caps, volume caps, share expiry, `SECRET_KEY`, `DICOMWEB_PUBLIC`, `CORS_ORIGINS`, warm settings |
| `app/db/models.py` | Targeted: Study, Series, Instance, PresentationState, KeyImageNote, PatientShare, AuditLog, HangingProtocol, Patient, User |
| `app/db/session.py` | 186-235: `get_db` commit/rollback; `expire_on_commit=False` (lines 91-92, 117, 136); read-replica dependency (not used by any in-scope route) |
| `app/services/storage_service.py` | 40-150, 254-300 and grep: `_safe_component`, `instance_key`, `thumbnail_key`, `save_dataset`, `read_dataset`, `resolve_study_backend` |
| `app/services/indexing_service.py` | 94-120, 190-320: `get_or_create_study`, `upsert_instance`, `index_new_instance` (STORED audit), GSPS/KIN dispatch |
| `app/services/query_service.py` | Full (235 lines) |
| `app/services/tenant_service.py` | Full: `scope_from_user` (line 83) |
| `app/services/redis_service.py` | 262-320, 1026-1120: `get_redis_client`, `cache_get`, `cache_set` |
| `app/services/warm_service.py` | 30-245: `schedule_warm`, `_warm_study` (threads, Redis keys, volume pre-warm) |
| `app/services/imaging.py` | Full (92 lines): `dataset_to_image`, `dataset_to_bytes` |
| `app/services/audit_service.py` | Full: `add_audit` (stages `AuditLog`, does not commit) |
| `app/services/anonymizer.py` | 99-145 (UID remap), 160-195 (`anonymize_files_to_zip`) |
| `app/services/thumbnail_service.py`, `app/dicom/parser.py` | Not read. Referenced only. |
| `app/api/v1/dashboard.py` | 697-712: `study_detail` (used by viewer, outside area) |
| `app/api/v1/nodes.py` | 398-425, 588-606: call sites of `wado_retrieve` / `stow_upload` (both via `asyncio.to_thread`) |
| `app/static/viewer.js` | 108-150, 174, 195: share-link and image-URL flow |
| `app/services/settings_service.py` | 50-66: `DICOMWEB_PUBLIC` admin label |
| `app/services/storage/`, `app/services/storage.py` | **Do not exist** on disk or in git history (see F26) |
| `tests/test_share.py`, `tests/test_dicomweb.py`, etc. | Grep only, for auth and tenant expectations |
| `README.md` | Grep: line 827 documents `DICOMWEB_PUBLIC=true` as "Open LAN access for viewers (tighten for production)" |

Not in scope and not reviewed: `app/services/anonymizer.py` beyond the calls above, `storage` backends (missing), `compression` worker, `parser.py`, `thumbnail_service.py`, dashboard and nodes routers beyond the lines listed.

---

## 2. API endpoint reference

Auth legend:
- **NONE**: no auth dependency on the route.
- **OPT**: `get_optional_user` (bearer token or HttpOnly session cookie; anonymous allowed; tenant scope derived from user).
- **BEARER**: `get_current_user` (bearer token only; any role; no tenant check unless noted).
- **DEST**: `require_destructive_role` (ADMIN or RADIOLOGIST; bearer or cookie).

Global middleware (main.py): `ViewerAuthGateMiddleware` only redirects HTML entry points (`/`, `/viewer`, `/studies-ui`, `/settings`) and never touches `/dicom-web` or `/api/v1/*` (main.py:295-300). `RateLimitMiddleware` skips `/dicom-web` entirely (main.py:358-359).

### 2a. DICOMweb (prefix `/dicom-web`, `app/api/v1/dicomweb.py`), 19 route paths

| # | Method | Full path | Auth | Request params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 1 | GET | `/dicom-web/wado` | NONE | `requestType=WADO` (required), `studyUID`/`StudyInstanceUID`, `seriesUID`/`SeriesInstanceUID`, `objectUID`/`SOPInstanceUID` (required), `contentType` (default `application/dicom`; `image/jpeg`, `image/jpg`, `image/png` render), `If-None-Match` | 200 `application/dicom` (inline `objectUID.dcm`) or rendered JPEG/PNG; 304; 400/404/422 | `wado_uri` dicomweb.py:136-191 |
| 2 | GET | `/dicom-web/studies` | OPT (tenant scope) | Any QIDO study keyword (`PatientName`, `PatientID`, `PatientBirthDate`, `StudyDate`, `StudyTime`, `AccessionNumber`, `StudyDescription`, `StudyInstanceUID`, `ReferringPhysicianName`, `StudyID`, `ModalitiesInStudy`); `limit` (default 100, max 1000); `offset`; `cursor` (keyset token); `includefield` (ignored) | 200 `application/dicom+json` array; header `X-Next-Cursor` when a further page exists | `qido_studies` dicomweb.py:197-253 |
| 3 | GET | `/dicom-web/series` | OPT | `Modality`, `SeriesInstanceUID`, `SeriesNumber`, `SeriesDescription`, `BodyPartExamined`; `limit`, `offset` | 200 `application/dicom+json` | `qido_series` dicomweb.py:256-274 (decorator 256) |
| 4 | GET | `/dicom-web/studies/{study_uid}/series` | OPT | as #3 | as #3 | `qido_series` dicomweb.py:257 |
| 5 | GET | `/dicom-web/instances` | OPT | `SOPInstanceUID`, `SOPClassUID`, `InstanceNumber`; `limit`, `offset` | 200 `application/dicom+json` | `qido_instances` dicomweb.py:277-297 (decorator 277) |
| 6 | GET | `/dicom-web/studies/{study_uid}/instances` | OPT | as #5 | as #5 | `qido_instances` dicomweb.py:278 |
| 7 | GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances` | OPT | as #5 | as #5 | `qido_instances` dicomweb.py:279 |
| 8 | GET | `/dicom-web/studies/{study_uid}/metadata` | NONE | none. Side effect: `warm_service.schedule_warm(study_uid)` (dicomweb.py:334) | 200 `application/dicom+json`, one DICOM JSON per instance (`stop_before_pixels`); 404 | `wado_study_metadata` dicomweb.py:327-336 |
| 9 | GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/metadata` | NONE | none | 200 `application/dicom+json`; 404 | `wado_series_metadata` dicomweb.py:339-347 |
| 10 | GET | `/dicom-web/studies/{study_uid}` | NONE | none | 200 `multipart/related; type="application/dicom"`, all instances as full bytes; 404 | `wado_study` dicomweb.py:359-373 |
| 11 | GET | `/dicom-web/studies/{study_uid}/series/{series_uid}` | NONE | none | 200 multipart (full bytes); 404 | `wado_series` dicomweb.py:376-387 |
| 12 | GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}` | NONE | `Accept` containing `multipart` returns multipart | 200 `application/dicom` (ETag, `private, max-age=31536000, immutable`) or multipart; 404 | `wado_instance` dicomweb.py:403-411 |
| 13 | GET | `.../instances/{sop_uid}/metadata` | NONE | none | 200 `application/dicom+json` (includes TransferSyntaxUID fallback) | `wado_instance_metadata` dicomweb.py:414-422 |
| 14 | GET | `.../instances/{sop_uid}/rendered` | NONE | `window="center,width"`, `viewport="w,h"` (only max(w,h) used), `presentationUID` (GSPS). `Accept` containing `jpeg` gives JPEG q88, else PNG | 200 image; 304; 404; 422 on render failure | `wado_rendered` dicomweb.py:425-509 |
| 15 | GET | `.../instances/{sop_uid}/thumbnail` | NONE | `window`, `viewport` (default max 256). `Accept` containing `jpeg` gives JPEG q82 | 200 image; 304; 404; 422 | `wado_thumbnail` dicomweb.py:512-590 |
| 16 | GET | `.../instances/{sop_uid}/frames/{frame_list}` | NONE | `frame_list` comma list, 1-based file-order frame numbers; `zorder=1` treats them as 0-based z-order indices; `transcode=1` decodes to raw; `Accept`: `application/dicom` (single frame full file), `application/octet-stream`, `multipart`, or `transfer-syntax=*` (viewer mode) | 200 raw frame bytes or `multipart/related`; 404; 304 | `wado_frame` dicomweb.py:593-732 |
| 17 | GET | `.../instances/{sop_uid}/bulk/{tag_path}` | NONE | `tag_path`: keyword or hex tag (PixelData, OverlayData, or any element) | 200 `application/octet-stream` or `text/plain`; 400; 404 | `wado_bulk_data` dicomweb.py:735-759 |
| 18 | POST | `/dicom-web/studies` | OPT (anonymous allowed) | body `multipart/related` (boundary from Content-Type) or single part | 200 JSON with DICOM result payload and `summary` (when any stored); 409 when none stored; 413 over caps; 400 empty or malformed body | `stow_studies` dicomweb.py:804-905 (decorator 804) |
| 19 | POST | `/dicom-web/studies/{study_uid}` | OPT (anonymous allowed) | as #18; path `study_uid` is used only for the response `base_uri`, not checked against the parts | as #18 | `stow_studies` dicomweb.py:805 |

### 2b. Volume (prefix `/api/v1/volume`, `app/api/v1/volume.py`), 4 routes

Every volume route takes `user: User | None = Depends(get_optional_user)` but **never uses `user`** and never passes tenant to `volume_service`. Auth is therefore effectively NONE (see F2).

| # | Method | Full path | Auth | Request params | Response | Handler |
|---|---|---|---|---|---|---|
| 20 | GET | `/api/v1/volume/{series_uid}/info` | OPT (unused) | none | 200 JSON geometry (rows, columns, slices, dtype, spacing, IOP/IPP, window, `frame_map`, `instance_order`); 404; 503 when `VOLUME_STREAMING_ENABLED` is false | `volume_info_endpoint` volume.py:65-77 |
| 21 | GET | `/api/v1/volume/{series_uid}/data` | OPT (unused) | `slices=all` or `a-b` (0-based inclusive; `:` also accepted); `rescale=1` (float32 HU); `downsample=1..8`; `format=raw` or `npy` | 200 `application/octet-stream` or `application/x-npy`; headers `X-Volume-Slices/Rows/Cols/Downsample/Dtype`, `X-Cache: HIT` on Redis hit; `Cache-Control: public, max-age=86400, immutable` | `volume_data_endpoint` volume.py:80-225 |
| 22 | GET | `/api/v1/volume/{series_uid}/mip` | OPT (unused) | `format=png|raw|npy` (default png); `axis=z|x|y`; `mode=mip|minip|average|vrt`; `offset`; `thickness`; `window_center`; `window_width` | 200 `image/png`, `application/octet-stream` or `application/x-npy`; `X-MIP-*` headers; `Cache-Control: no-store` | `volume_mip_endpoint` volume.py:234-336 (decorator 234) |
| 23 | GET | `/api/v1/volume/{series_uid}/mpr/slice` | OPT (unused) | `format=png|raw|npy`; `plane=axial|sagittal|coronal` with `index`, or oblique with `origin=x,y,z&row=dx,dy,dz&col=dx,dy,dz&size=WxH` (capped by `VOLUME_MPR_MAX_PIXELS`); `window_center`; `window_width` | 200 PNG / octet-stream / npy; `X-MPR-*` headers; `Cache-Control: no-store` | `volume_mpr_slice_endpoint` volume.py:339-456 (decorator 339) |

### 2c. Fusion (prefix `/api/v1/fusion`, `app/api/v1/fusion.py`), 1 route, mounted only when `FHIR_ENABLED` (main.py:554-556, config.py:187 default True)

| # | Method | Full path | Auth | Params | Response | Handler |
|---|---|---|---|---|---|---|
| 24 | GET | `/api/v1/fusion/{study_uid}` | OPT, tenant-scoped at the study lookup (fusion.py:36-39) | none | 200 manifest (`frames_of_reference`, `pairs`, `unregistered`, `series_count`); 404 when not in scope | `fusion_manifest` fusion.py:29-42 |

### 2d. GSPS (prefix `/api/v1/gsps`, `app/api/v1/gsps.py`), 3 routes, all NONE

| # | Method | Full path | Auth | Params | Response | Handler |
|---|---|---|---|---|---|---|
| 25 | GET | `/api/v1/gsps/{sop_uid}` | NONE | none | 200 JSON from `_ps_to_dict` (includes `file_path`); 404 | `get_gsps` gsps.py:44-50 |
| 26 | GET | `/api/v1/gsps/study/{study_uid}` | NONE | none | 200 JSON list (includes `file_path`) | `list_gsps_for_study` gsps.py:53-57 |
| 27 | GET | `/api/v1/gsps/instance/{sop_uid}/presentations` | NONE | `study_uid` (optional) | 200 JSON list | `find_presentations_for_instance` gsps.py:60-68 |

### 2e. Hanging protocols (prefix `/api/v1/hanging-protocols`, `app/api/v1/hanging_protocols.py`), 5 routes, all BEARER

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 28 | POST | `/api/v1/hanging-protocols` | BEARER | `CreateHangingProtocolRequest`: `name`* (unique), `description`, `modality`, `body_part_examined`, `user_id` (any value), `priority=10`, `layout_rows=1`, `layout_columns=2`, `configuration_json="{}"` (not validated as JSON) | 201 JSON; 400 on any exception (message echoed) | `create_hanging_protocol` hanging_protocols.py:62-75 |
| 29 | GET | `/api/v1/hanging-protocols` | BEARER | none | 200 JSON list of all protocols (any owner) | `list_hanging_protocols` hanging_protocols.py:78-85 |
| 30 | GET | `/api/v1/hanging-protocols/match` | BEARER | `modality`, `body_part` | 200 best match, or fallback `Default 1x2 Layout` | `match_hanging_protocol` hanging_protocols.py:88-109 |
| 31 | GET | `/api/v1/hanging-protocols/{protocol_id}` | BEARER | path int | 200 JSON; 404 | `get_hanging_protocol` hanging_protocols.py:112-122 |
| 32 | DELETE | `/api/v1/hanging-protocols/{protocol_id}` | BEARER (any role, any owner) | path int | 200 message; 404 | `delete_hanging_protocol` hanging_protocols.py:125-136 |

### 2f. KIN / key image notes (prefix `/api/v1/kin`, `app/api/v1/kin.py`), 4 routes, all BEARER

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 33 | POST | `/api/v1/kin` | BEARER (no tenant or study check) | `CreateKINRequest`: `study_instance_uid`*, `series_instance_uid`*, `referenced_sop_instance_uids`* (non-empty list, unbounded), `notes`, `title_concept_code="113000"` (not validated) | 201 JSON (includes `file_path`); 400 empty list; 500 with exception text | `create_key_image_note` kin.py:54-78 |
| 34 | GET | `/api/v1/kin/study/{study_uid}` | BEARER | none | 200 JSON list | `list_kin_for_study` kin.py:81-89 |
| 35 | GET | `/api/v1/kin/instance/{sop_uid}/notes` | BEARER | none (substring match, see F16) | 200 JSON list | `find_kin_for_instance` kin.py:92-100 |
| 36 | GET | `/api/v1/kin/{sop_uid}` | BEARER | none | 200 JSON; 404 | `get_key_image_note` kin.py:103-114 |

### 2g. DICOMDIR export (prefix `/api/v1/export/dicomdir`, `app/api/v1/dicomdir.py`), 2 routes, both BEARER

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 37 | POST | `/api/v1/export/dicomdir` | BEARER (any role; no tenant check) | body `{study_instance_uids: [...]}`, non-empty | 200 `application/zip`, `Content-Disposition: attachment; filename="DICOM_EXPORT_<patient_id>.zip"`; 404 no match; 500 | `export_dicomdir_package` dicomdir.py:33-58 |
| 38 | POST | `/api/v1/export/dicomdir/preview` | BEARER (no tenant check) | same body | 200 JSON structure; 404 | `preview_dicomdir_structure` dicomdir.py:61-119 |

### 2h. De-identified export (prefix `/api/v1`, `app/api/v1/export.py`), 1 route, BEARER

| # | Method | Full path | Auth | Params | Response | Handler |
|---|---|---|---|---|---|---|
| 39 | GET | `/api/v1/export/{study_uid}/anonymized` | BEARER (any role; no tenant check) | none | 200 `application/zip` (`paxs_anonymized_<uid>.zip`); 404; 422 on unreadable file | `export_anonymized` export.py:23-76 |

### 2i. Share links (prefix `/api/v1/share`, `app/api/v1/share.py`), 4 routes

| # | Method | Full path | Auth | Params / body | Response | Handler |
|---|---|---|---|---|---|---|
| 40 | POST | `/api/v1/share` | BEARER (any role; no tenant check on the study) | `CreateShareBody`: `study_uid`* (min length 1), `expiry_days` 1-365 (default `SHARE_EXPIRY_DAYS`=30), `phone` (no validation) | 201 `{token, url, study_uid, expires_at, expiry_days, phone, qr_base64}`; 404 study missing | `create_share_endpoint` share.py:27-50 |
| 41 | GET | `/api/v1/share/{token}` | NONE (token is the credential) | none. Increments `accessed_count` (share.py:58) | 200 `{study_uid, expires_at, accessed_count, patient{name,id}, study{date,description,accession}, series[...]}`; 404 expired or unknown | `get_share_info` share.py:53-100 |
| 42 | GET | `/api/v1/share/{token}/qr.png` | NONE (token) | none | 200 `image/png`; 404 | `share_qr` share.py:103-111 |
| 43 | DELETE | `/api/v1/share/{token}` | DEST | none | 204; 403 for non-ADMIN/RADIOLOGIST; 404 | `delete_share` share.py:114-120 |

Total: 43 route paths across 9 routers (19 DICOMweb, 4 volume, 1 fusion, 3 GSPS, 5 hanging protocols, 4 KIN, 2 DICOMDIR, 1 export, 4 share).

Auth coverage summary (43 routes): NONE 16 (DICOMweb WADO-RS/WADO-URI reads 11, GSPS 3, share token reads 2). OPT 13 (QIDO 6 and STOW 2 in DICOMweb, which are scoped or anonymous by design; volume 4, which declares OPT but never uses the user, so it is effectively unscoped; fusion 1, which is tenant-scoped at the study lookup). BEARER 13 (hanging protocols 5, KIN 4, DICOMDIR 2, export 1, share create 1). DEST 1 (share delete). Key consequences are in section 5 (F1, F2, F3, F7, F8, F13).

---

## 3. Service reference

### 3.1 `dicomweb_service.py` (DICOM JSON, multipart, STOW parser)
- Purpose: PS3.18 DICOM JSON builders and multipart utilities.
- Functions:
  - `study_to_dicom_json(study, patient, modalities=None, n_series=None, n_instances=None) -> dict` (line 49)
  - `series_to_dicom_json(series, n_instances=None) -> dict` (line 69)
  - `instance_to_dicom_json(instance) -> dict` (line 80)
  - `dataset_to_dicom_json(ds: Dataset) -> dict` (line 93): `ds.to_json_dict()`, uppercase tag keys, adds TransferSyntaxUID from file_meta
  - `parse_multipart(body: bytes, content_type: str)` (line 114): **no callers** (dead code)
  - `class StowPartTooLarge(Exception)` (line 127)
  - `async iter_stow_parts(stream, content_type, max_part_bytes=0, max_total_bytes=0)` (line 131): streaming parser, yields `(ctype, bytes)` per part
  - `build_multipart(parts: list[bytes], boundary: str, part_type="application/dicom") -> bytes` (line 249): accumulates into a bytearray then copies
  - `new_boundary() -> str` (line 260): `paxs-<uuid4 hex>`
  - `extract_frame(ds, frame_index) -> bytes` (line 267): **no callers** (dead code)
  - `dataset_from_bytes(data: bytes, force=False) -> Dataset` (line 307)
- DB models: none.
- Caching: none.
- File I/O: none (in-memory only).

### 3.2 `dicomweb_client.py` (remote-node DICOMweb client)
- Purpose: QIDO-RS query, WADO-RS pull, and STOW-RS push to registered `dicom_nodes` with a `web_url`. Sync httpx; called via `asyncio.to_thread` from `app/api/v1/nodes.py` (lines 359, 405, 432, 533, 601) and `app/services/accelerator_service.py` (lines 27, 69, 81).
- SSRF guard: `validate_web_url(url, allow_private=None) -> str` (line 44); `_reject_blocked_host(hostname)` (line 67) resolves via `socket.getaddrinfo`; blocks loopback, private, link-local, reserved, unspecified and `169.254.0.0/16`, `100.100.100.0/24`, unless `DICOMWEB_CLIENT_ALLOW_PRIVATE`.
- Functions:
  - `_headers(node) -> dict` (89): Accept `application/dicom+json`; Basic auth from `node.web_username/web_password`
  - `_client(node) -> httpx.Client` (99): `follow_redirects=False`, `timeout=DICOMWEB_CLIENT_TIMEOUT`
  - `_qido_path(level, filters) -> str` (111)
  - `qido_query(node, level="STUDY", filters=None) -> list[dict]` (143): fixed `limit=200`, `includefield=all`
  - `qido_to_match_rows(level, matches) -> list[dict]` (202)
  - `wado_retrieve(node, study_uid, tenant_id=None) -> dict` (229): GET `/studies/{uid}` as multipart; `resp.content` fully loaded (249); size check afterwards (252); each part ingested
  - `_iter_multipart_parts(body, content_type)` (283): `body.split(marker)`
  - `_dataset_from_bytes(data)` (310): `pydicom.dcmread(force=True)`
  - `_ingest_dicomweb_dataset(ds, tenant_id)` (322): `storage_service.save_dataset`, `parse_metadata`, then `asyncio.run(_run())` with `AsyncSessionLocal`
  - `_stow_failed_sops(resp) -> set[str]` (353)
  - `_multipart_body_chunks(targets, boundary, chunk_size, counter)` (385): generator for the streamed STOW body
  - `stow_upload(node, targets, stream=False) -> dict` (409): non-stream path writes a temp file, then `fh.read()` the whole body (462) into memory
- DB models: via `index_new_instance` (`Instance`, `Study`, `Series`, `Patient`, `AuditLog`).
- Caching: none.
- File I/O: `storage_service.read_bytes` per target; temp file via `tempfile.mkstemp` (451) and unlinked (465-467).

### 3.3 `wado_rs_bulk_service.py` (frames, transfer-syntax decode, bulk)
- Constants: `RAW_FRAME_CONTENT_TYPE = "application/octet-stream; transfer-syntax=1.2.840.10008.1.2"` (line 24). `_FRAME_CACHE` OrderedDict, max 16 entries, no lock (lines 28-29).
- Functions:
  - `_cache_get(key) / _cache_put(key, value)` (32, 39): in-process LRU
  - `clear_frame_cache()` (46): **no callers**
  - `extract_frame_bytes(ds, frame_indices) -> list[tuple[bytes, str]]` (51): encapsulated path materializes all frames (`list(generate_frames(...))`, line 81); uncompressed path uses `ds.pixel_array` (98)
  - `decode_frames_to_raw(ds, frame_indices) -> list[tuple[bytes, str]]` (126): in-process LRU, then Redis `paxs:frame:{sop}:{n}` (162), then `ds.pixel_array` (176), writes LRU (195) and Redis (196)
  - `build_multipart_related_payload(frames, boundary="DICOMWEB_BOUNDARY") -> (bytes, media_type)` (204)
  - `extract_bulk_attribute(ds, tag_path) -> (bytes, content_type)` (224): iterates all elements; KeyError when missing
- DB models: none (receives a Dataset).
- Caching: in-process `_FRAME_CACHE` (16 entries) and Redis `paxs:frame:{sop}:{frame_idx}` TTL 3600 s (lines 162, 196; also written by warm_service.py:172).
- File I/O: none directly.

### 3.4 `volume_service.py` (volume stack, MPR, MIP)
- Constants: `NON_IMAGE_SOP_CLASSES` (lines 45-59); `_BUNDLE_CACHE` dict, max 32, FIFO (392-393); `_VOL_CACHE` dict, max 8, with lock (637-639); `_VOL_REDIS_TTL = 3600` (642).
- Functions (signature, line):
  - `dtype_for(bits_allocated, pixel_representation) -> str` (65)
  - `orientation_label(iop) -> str` (74); `dominant_axis(iop) -> int` (99)
  - `npy_header(dtype, shape) -> bytes` (105)
  - `class VolumeSlice` (122)
  - `async series_slices(db, series_uid) -> list[VolumeSlice]` (145): `query_service.instance_query(None, series_uid)` (152), **no tenant**; sync `storage_service.exists` (163) inside async; per-instance header read via `to_thread` (172)
  - `async volume_bundle(db, series_uid) -> (info, slices) | None` (396): process-level cache keyed by `series_uid`, no TTL, no invalidation (399-400, 491-493); sync `read_dataset` (407) inside async
  - `async volume_info(db, series_uid) -> dict | None` (497)
  - `_frame_array(s, cache=None) -> np.ndarray` (502): `ds.pixel_array` on each call (513)
  - `iter_volume_bytes(slices, rescale=False, start=0, end=None, downsample=1) -> Iterator[bytes]` (523): local `cache` dict keeps every decoded dataset (551)
  - `volume_byte_length(...)` (584): ceil-based byte count
  - `write_npy(buf, dtype, shape, slices, ...)` (598): **no callers**
  - `_vol_redis_get / _vol_redis_set(r_key, ...)` (645, 663): no size limit on set (672)
  - `load_volume_array(slices, series_uid=None) -> np.ndarray` (677): float32 HU stack; local `cache` dict (715) keeps all datasets; `np.stack` (730); `_VOL_CACHE` (731-735); Redis (738-741)
  - `_patient_transform(info)` (746), `_trilinear(vol, i, j, k)` (767), `class ResliceResult` (811), `_sample_grid(...)` (826)
  - `reslice_plane(slices, info, plane, index=None, origin=None, row_vector=None, col_vector=None, size=None, max_pixels=4_000_000) -> ResliceResult` (850): `max_pixels` applies to oblique only (925-927)
  - `_unit(vec)` (939); `window_for(info, data, window_center=None, window_width=None) -> (c, w)` (947); `nan_to_float32(data)` (967); `render_plane_png(data, window_center, window_width) -> bytes` (972)
  - `_vrt_raycast(vol, axis, wc, ww)` (1006): per-slice loop with full-plane temporaries
  - `class MipResult` (1038); `mip_project(slices, info, axis="z", mode="mip", offset=0, thickness=None, window_center=None, window_width=None) -> MipResult` (1054): `np.take` copy (1092)
- DB models: `Instance`, `Series`, `Study` (via query_service and direct `select(Series, Study.study_instance_uid)` at 410-414).
- Caching: `_BUNDLE_CACHE` (32, FIFO, no TTL, key series UID); `_VOL_CACHE` (8, no byte cap, key `array:{series}:{n}`); Redis `paxs:vol:array:{series}:{n}` TTL 3600 (739-741, 700-702).
- File I/O: reads every instance header per bundle build (`stop_before_pixels=True`); full-instance reads for pixel decoding (`read_dataset` at 510, 547, 566, 723).

### 3.5 `fusion_service.py`
- `async study_frame_of_references(db, study_uid, *, tenant_id=None, include_all=True) -> dict` (line 30): study lookup via `query_service.study_query` (45-48, tenant-scoped), series by study (53-59), groups by FoR (61-74), pairs (95-106), per-series `_series_geometry` (87)
- `async _series_geometry(db, series) -> dict | None` (118): calls `volume_info` (unscoped, but series already in scope), swallows exceptions
- DB models: `Study`, `Series`, `Instance` (via volume).
- Caching: none in this module (inherits volume bundle caching).
- File I/O: via volume_service (header reads).

### 3.6 `gsps_service.py` (presentation states)
- Constant: `GSPS_SOP_CLASS_UID = "1.2.840.10008.5.1.4.1.1.11.1"` (line 25)
- Dataclasses: `GraphicObject` (32, `units` parsed but unused), `TextObject` (41), `DisplayShutter` (50), `GSPSContent` (66)
- Functions:
  - `parse_gsps(ds) -> GSPSContent` (89)
  - `async index_presentation_state(session, meta, ds, file_path) -> PresentationState | None` (202): upsert keyed by `sop_instance_uid`; called from indexing_service.py:299
  - `async find_for_instance(session, sop_instance_uid, study_instance_uid=None) -> list` (263): `referenced_sop_instance_uids.contains(uid)` (270), i.e. SQL LIKE substring
  - `async get_by_uid(session, sop_instance_uid)` (278)
  - `async list_for_study(session, study_instance_uid)` (292)
  - `apply_gsps_to_image(img, content) -> Image` (311): flip, rotate (`expand=True`), shutters, graphics/text
  - `_apply_shutter`, `_draw_graphic`, `_draw_text` (354, 377, 405); `_draw_text` loads the font each call (415)
- DB models: `PresentationState` (unique `sop_instance_uid`, `referenced_sop_instance_uids` as comma-joined Text, `created_at`).
- Caching: none (rendered output cached by dicomweb.py:500).
- File I/O: `read_dataset(stop_before_pixels=True)` during indexing (indexing_service.py:293-296).

### 3.7 `hanging_protocol_service.py`
- `create_protocol(session, data)` (20): `HangingProtocol(**data)` with flush
- `get_protocol(session, protocol_id)` (27)
- `list_protocols(session)` (35): all rows, priority then id
- `match_best_protocol(session, modality=None, body_part=None, user_id=None)` (45): loads all rows, scores (+100 owner, +50 modality, +30 body part, `max(0, 50 - priority)`)
- `delete_protocol(session, protocol_id)` (87): no owner check
- DB models: `HangingProtocol` (unique `name`, `user_id` FK ondelete CASCADE, `created_at`/`updated_at` server defaults).
- Caching: none. File I/O: none.

### 3.8 `kin_service.py` (Key Object Selection)
- Constants: `KIN_SOP_CLASS_UID` (25), `KIN_TITLE_MAP` (28-36)
- `parse_kin(ds) -> ParsedKINContent` (48)
- `async index_key_image_note(session, meta, ds, file_path) -> KeyImageNote | None` (92): upsert; `AuditLog` `KEY_IMAGE_NOTE_CREATED` on create (136-142)
- `async create_key_image_note(session, study_instance_uid, series_instance_uid, referenced_sop_uids, notes=None, title_code="113000", author_name="radiologist") -> KeyImageNote` (148): `generate_uid()`, builds the KO dataset, writes via `storage_service.save_dataset(ds)` with **no backend argument** (202), indexes via `indexing_service.index_new_instance` (213-215) with no tenant, then `index_key_image_note` (218)
- `async list_kin_for_study(session, study_instance_uid)` (232)
- `async find_kin_for_instance(session, sop_instance_uid)` (244): `contains` substring (250)
- DB models: `KeyImageNote` (unique `sop_instance_uid`, `referenced_sop_instance_uids` Text, `created_at`), `Instance` (via index_new_instance), `Study`, `Series`, `Patient`, `AuditLog`.
- Caching: none. File I/O: one KO file written per create (`save_dataset`).

### 3.9 `dicomdir_service.py`
- Constant: `DICOMDIR_SOP_CLASS_UID = "1.2.840.10008.1.3.10"` (29)
- `create_dicomdir_dataset(studies_structure) -> FileDataset` (32): PATIENT/STUDY/SERIES/IMAGE records; no FileSetID; no offset pointers; IMAGE records written even for files that are not in the ZIP
- `async export_dicomdir_package(session, study_instance_uids, performed_by="system") -> (bytes, filename)` (100): joined query on `Study.study_instance_uid.in_(...)` with no tenant (107-115); per-instance `storage_service.exists` (158) and `read_bytes` (178) inside the coroutine; whole ZIP in `io.BytesIO`; `AuditLog` `EXPORT_DICOMDIR_CREATED` (182-189)
- DB models: `Study`, `Patient`, `Series`, `Instance`, `AuditLog`.
- Caching: none. File I/O: reads every instance file into the ZIP.

### 3.10 `sharing_service.py`
- `class ShareError(RuntimeError)` (21); `_now()` (25); `qr_base64(url) -> str` (30)
- `async create_share(session, study_uid, expiry_days=None, phone=None) -> PatientShare` (37): study lookup with no tenant (43-47); token `secrets.token_urlsafe(32)` (52; 256 bits); expiry `now + days` (54)
- `async get_share(session, token) -> PatientShare | None` (61): plaintext token lookup; returns None when `expires_at < now` (67-68)
- `share_url(token) -> str` (72): `PUBLIC_BASE_URL` (default `http://0.0.0.0`, config.py:281) + `/viewer?share=<token>`
- `human_expiry(share) -> str` (78)
- DB models: `PatientShare` (`share_token` String(64) unique, plaintext), `Study`, `AuditLog`.
- Caching: none. File I/O: none.

### 3.11 Other helpers used in scope
- `query_service.py`: `apply_study_filters` (62), `apply_series_filters` (81), `apply_instance_filters` (95), `_apply` (116), `apply_study_tenant_scope` (141), `study_query` (157), `series_query` (164), `instance_query` (177), `study_aggregates` (198), `series_instance_counts` (223). `instance_query` and `study_query` both default `include_all=True` (unscoped) when not passed.
- `tenant_service.scope_from_user(user)` (83): None -> (None, False) = legacy NULL-tenant only; ADMIN -> (None, True); other -> (user.tenant_id, False).
- `indexing_service.index_new_instance` (225): patient/study/series, `upsert_instance` (190), `AuditLog` STORED (252-261), GSPS dispatch (296-303), KIN dispatch (305-316), DR and compression job queueing.
- `storage_service.py`: `_safe_component` (48), `instance_key` (62), `thumbnail_key` (70), `resolve_study_backend` (96), `save_dataset` (274), `read_dataset` (142), `exists` (126), `read_bytes` (138). Backend implementation missing (F26).
- `redis_service.py`: `get_redis_client` (295), `cache_get` (1026), `cache_set` (1078), in-memory LRU `_MEM_CACHE` (44; items <= 16 MB).
- `imaging.py`: `dataset_to_image(ds, window=None, max_size=None)` (40): `ds.pixel_array` (42), `thumbnail()` downsizes only (77-78).

### 3.12 Redis keys and TTLs written/read in this area

| Key pattern | TTL | Written at | Read at | Notes |
|---|---|---|---|---|
| `paxs:rendered:{sop}:{window\|auto}:{max\|full}:{presentationUID\|none}:{fmt}` | 600 s | dicomweb.py:500 | dicomweb.py:445-449 | Key includes raw user `window` and `presentationUID` (unbounded cardinality, F17) |
| `paxs:thumb:{sop}:{window\|auto}:{max\|full}:{fmt}` | 600 s | dicomweb.py:586 | dicomweb.py:563-569 | Warm writes `paxs:thumb:{sop}:auto:256:png` at 3600 s (warm_service.py:142, 155) |
| `paxs:frame:{sop}:{resolved}:{native\|raw\|def}:{mp\|sp}` | 1800 s | dicomweb.py:688, 729 | dicomweb.py:690 | Correct namespace |
| `paxs:frame:{sop}:{resolved}` (no suffix) | 1800 s | **dicomweb.py:730** | wado_rs_bulk_service.py:162 (via decode path) | **Collides** with the raw namespace (F5) |
| `paxs:frame:{sop}:{n}` (raw decoded frame) | 3600 s | wado_rs_bulk_service.py:196; warm_service.py:172 | wado_rs_bulk_service.py:162 | Raw semantics |
| `paxs:vol:stream:{series}:{start}_{end}_{ds}_{rescale}_{fmt}` and `paxs:vol:stream:{series}:{slices}` | 3600 s | volume.py:213, 215 | volume.py:185 | Canonical key used only for full raw stacks |
| `paxs:vol:array:{series}:{n}` (and `paxs:vol:array:{sop}:{n}`) | 3600 s | volume_service.py:739, 741 | volume_service.py:700, 702 | No byte cap (F6) |
| `paxs:warm:lock:{study}` | 120 s | warm_service.py:97 | (lock) | Distributed dedup |
| `paxs:warm:{study}` | 86400 s | warm_service.py:228 | none in area | Summary record |

In-process caches:

| Name | Bound | Eviction | TTL | Invalidated by | Location |
|---|---|---|---|---|---|
| `_BUNDLE_CACHE` (volume) | 32 entries | FIFO (comment says LRU) | none | nothing | volume_service.py:392-400, 491-493 |
| `_VOL_CACHE` (volume arrays) | 8 volumes, no byte cap | FIFO | none | nothing | volume_service.py:637-639, 691-735 |
| `_FRAME_CACHE` (decoded raw frames) | 16 entries, no byte cap | LRU | none | `clear_frame_cache` (no callers) | wado_rs_bulk_service.py:28-48 |
| `_WARMING` (set) | per study | discard on finish | n/a | n/a | warm_service.py:39-63 |
| redis `_MEM_CACHE` (tier 1 of cache_get/cache_set) | per item <= 16 MB | LRU | 300 s on read-through | n/a | redis_service.py:44, cache_get |

### 3.13 File I/O summary for the area
- Reads: instance files via `storage_service` (`read_bytes`, `read_dataset`, `exists`), local under `STORAGE_ROOT` (default `./storage`, config.py:57) or S3 (missing backend module, F26). Thumbnails at `.thumbnails/{sop}.png` (storage_service.py:70-71).
- Writes: STOW instance files (storage_service.py:293-297, key `study/series/sop.dcm`), KO files (kin_service.py:202), remote-pull ingest (dicomweb_client.py:335), warm thumbnail generation (warm_service.py:133, via thumbnail_service).
- Temp files: `dicomweb_client.stow_upload` non-stream path (451-467).
- In-memory buffers: WADO-RS study/series multipart (dicomweb.py:365-373, 382-387; dicomweb_service.py:249-257), volume `/data` (volume.py:196-210), DICOMDIR ZIP (dicomdir_service.py:168-179), anonymized ZIP (anonymizer.py:179), STOW parts (dicomweb_service.py:215-246).

---

## 4. Workflows

### (a) QIDO-RS study search: `GET /dicom-web/studies`
1. Route `qido_studies` dicomweb.py:197-198 (params `request`, `db`, `user` via `get_optional_user`).
2. `params = {k: v for k in request.query_params}` (200); `_paginate` pops `limit` (default 100, min with 1000) and `offset` (>=0) (201; 59-68); `includefield` popped (202).
3. If `StudyInstanceUID` present: `warm_service.schedule_warm(study_uid_param)` (206-207). This happens before any tenant check (see F9).
4. `tenant_service.scope_from_user(user)` (208; tenant_service.py:83): anonymous -> `(None, False)`.
5. `query_service.study_query(params, tenant_id, include_all)` (209; query_service.py:157-161): `select(Study).options(joinedload(Study.patient))`, then `apply_study_filters` (62-78: inner join Patient at 63; `ModalitiesInStudy` as EXISTS at 65-71; each STUDY_FILTERS key through `_apply` at 116-135: date ranges via `_range`, `*` wildcard -> LIKE, `like` kind -> `%value%`, otherwise equality), then `apply_study_tenant_scope` (141-151): anonymous -> `Study.tenant_id IS NULL`; tenant user -> `tenant_id = X`; ADMIN -> no filter.
6. Keyset: `_keyset_cursor` parses `"<study_date>|<id>"` (71-85); `_keyset_predicate` (88-107) adds the next-page WHERE; `effective_offset` = 0 when a cursor is used (216).
7. Ordering `Study.study_date IS NOT NULL DESC, study_date DESC, id DESC` with `limit(limit).offset(effective_offset)` (217-221).
8. Execute and de-duplicate (222). `query_service.study_aggregates` (223; query_service.py:198-220) returns modalities, series count, and instance count per study.
9. Map each study with `study_to_dicom_json` (224-232; dicomweb_service.py:49-66; `_put` at 22-46).
10. If a full page was returned, probe one row past it (238-249) and, if present, set `X-Next-Cursor: <study_date>|<id>` (250-252).
11. Return `JSONResponse(results, media_type="application/dicom+json", headers=headers)` (253).

### (b) WADO-RS retrieve of instance, frames, rendered, and thumbnail
- Common: `_get_instance` (dicomweb.py:390-400): `instance_query(study, series, {SOPInstanceUID})` (393), **no tenant** (see F2); 404 when the row is missing or the file is absent (396-399, the existence check runs in a thread at 398).
- Instance (raw): `wado_instance` (403-411): `read_bytes` in a thread (407); `Accept` containing `multipart` -> `_multipart_response` (410; dicomweb.py:350-356 -> dicomweb_service.py:249-257); else `_cached_response` (411; MD5 ETag computed on the event loop, 119-130).
- Rendered: `wado_rendered` (425-509):
  1. `_get_instance` (428).
  2. Parse `window`, `viewport` (only max(w,h) kept, 434-439), `presentationUID` (432), `fmt` from `Accept` (441-442).
  3. Redis key (445) and `cache_get` (447): **synchronous call on the event loop** (see F10). Hit -> `_cached_response` and return (448-449). **The audit line 506-508 is skipped on hit.**
  4. If `presentationUID`: look up `PresentationState` (461-466); if the file exists, `read_dataset(stop_before_pixels=True)` (468-470), `parse_gsps` (471), and default window from GSPS (473-474). Exceptions are logged at debug and ignored (475-476).
  5. `_render` in `asyncio.to_thread` (498-499): `read_dataset` (479), `dataset_to_image` (482 -> imaging.py:40-79), `apply_gsps_to_image` (485-488), encode PNG or JPEG q88 (490-496).
  6. `cache_set(..., expire_seconds=600)` (500) on the event loop; errors -> 422 (501-503).
  7. Audit `RETRIEVED` (506-508), then `_cached_response` (509).
- Thumbnail: `wado_thumbnail` (512-590):
  1. Fast path (549-553): if PNG, no window, and (no viewport or size <= 192): `storage_service.thumbnail_key(sop)` (`.thumbnails/{sop}.png`, storage_service.py:70-71) exists -> serve the stored PNG.
  2. Redis key `paxs:thumb:...` (563-569), synchronous `cache_get` (567).
  3. Render in a thread (571-585) with `dataset_to_image(max_size)`, encode, `cache_set` (586, on the event loop), `_cached_response` (590).
- Frames: `wado_frame` (593-732):
  1. `_get_instance` (611).
  2. Full dataset read **before** any cache check: `read_dataset` in a thread (612).
  3. Parse `frame_list` (614-619); `zorder=1` -> translate via `volume_service.volume_bundle` (622-653) and check that the slice belongs to the instance (647-651).
  4. Bounds check against `NumberOfFrames` (655-657).
  5. `wants_full_dicom` (666-675): single-frame instance with explicit `application/dicom` returns the whole file.
  6. Cache key `paxs:frame:{sop}:{resolved}:{mode}:{mp|sp}` (687-688); `cache_get` (690, synchronous). Hit: split on `||PAXS_MT||` (693-695; see F5 about first-occurrence split).
  7. `_extract` in a thread (698-721): `decode_frames_to_raw` for `transcode` or viewer mode; `extract_frame_bytes` otherwise; single frame vs multipart envelope (712-714).
  8. Store: when the payload is <= 16 MB, `cache_set` the key with `||PAXS_MT||` (729) and the unsuffixed key (730; collision, F5).
  9. `_cached_response` (732).
- Bulk: `wado_bulk_data` (735-759): `extract_bulk_attribute` in a thread (751).

### (c) STOW-RS upload: `POST /dicom-web/studies[/{study_uid}]`
1. Route `stow_studies` (804-808): `get_optional_user` (anonymous allowed).
2. Read the first chunk; empty -> 400 (820-830). `_stream()` replays the first chunk (832-835).
3. `iter_stow_parts(_stream(), content_type, max_part_bytes=STOW_MAX_PART_BYTES, max_total_bytes=STOW_MAX_TOTAL_BYTES)` (845-849; dicomweb_service.py:131-246): boundary from Content-Type (147-150); preamble skipped (179-182; **no cap**); part headers to `\r\n\r\n` (197-210; **no cap**); payload to `\r\n--boundary` (215-237); per-part and total caps (230-232, 240-245).
4. Per part: skip empty (850-851). Non-`application/dicom` parts are tried with `force=True` and dropped if unparseable (852-857). `dataset_from_bytes` (859).
5. Study-exists check (861-869) and backend choice `resolve_study_backend` (873-875; storage_service.py:96-110).
6. `storage_service.save_dataset(ds, backend_name=backend)` (876; storage_service.py:274-298): key from UIDs (293), **unconditional overwrite** (297).
7. `parse_metadata(ds)` (877; app/dicom/parser.py, not read).
8. `index_new_instance(db, meta, path, sha256, performed_by="stow-rs", tenant_id=..., storage_backend=backend)` (882-886; indexing_service.py:225): `get_or_create_study` (tenant applied only on create, indexing_service.py:94-115), `upsert_instance` (190-221; existing rows repointed), STORED audit (252-261), DR and compression jobs.
9. Success -> `stored` (887-891). Failure -> `failed` with `0xC212` (892-898). The DB session is shared across parts and is not rolled back on per-part failure (see F22).
10. `StowPartTooLarge` -> 413 (899-900). Any other stream error -> 400 (901-903). Exceptions propagate through `get_db`, which rolls back the DB (session.py:223-232).
11. `_stow_response` (765-801): 200 if any stored, else 409; summary of studies imported vs. existed.
12. Commit at the end of the request in `get_db` (session.py:228).

### (d) MPR, volume, and MIP generation
1. Stack build (`volume_bundle`, volume_service.py:396-494): cache check (399-400). `series_slices` (145-259): `instance_query(None, series)` (151-158, no tenant), `exists` (163, sync), header read via thread (172-177), multi-frame expansion into per-frame slices (185-230; `_per_frame_positions` 298-317, `_dimension_step_order` 326-367), stable z-sort (246-259).
2. Geometry info (416-490): first-slice header read (407-409, sync), series row join (410-414), dtype (422), spacing and isotropy (425-446), window (473-474), `frame_map` (485-489), FIFO cache insert (491-493).
3. Raw data `/data` (volume.py:80-225): `_slice_range` (100; 40-62), `VOLUME_MAX_SLICES` count check (104-107), downsample (110), payload length (134-138; ceil/floor mismatch, F12), Redis lookup (180-194, synchronous), `build_and_cache` in a thread (196-220): `iter_volume_bytes` (523-572) reads each slice (`_frame_array`, 502-520; `ds.pixel_array` at 513), downsamples with `arr[::step, ::step]` (563-564), rescales to float32 (565-571), `b"".join(chunks)` (210), Redis set (211-215).
4. MIP (volume.py:234-336): `mip_project` in a thread (297-302) -> `load_volume_array` (677-743): `_VOL_CACHE` (691-697), Redis (699-708), otherwise per-slice `_frame_array` + float32 conversion (716-728), `np.stack` (730), cache (731-741). Slab `np.take` copy (1092). `np.max`/`np.min`/`np.mean`, or VRT via `_vrt_raycast` (1006-1035). `window_for` (947-964). PNG via `render_plane_png` (972-984; volume.py:334).
5. MPR (volume.py:339-456): `reslice_plane` in a thread (384-387 native, 408-416 oblique). Native axial copies the plane (881); sagittal and coronal use `_sample_grid` (826-847) and `_trilinear` (767-808). Oblique limited by `VOLUME_MPR_MAX_PIXELS` (925-927). Output PNG / raw / npy (443-456).

### (e) Share link creation and public access
1. `POST /api/v1/share` (share.py:27-29): BEARER (`get_current_user`, deps.py:46-63).
2. `create_share` (sharing_service.py:37-58): study lookup by UID with no tenant (43-47), token `secrets.token_urlsafe(32)` (52), expiry = now + days (54), flush (57).
3. Audit `SHARED` (share.py:35-39).
4. Response (41-50): `share_url` (sharing_service.py:72-75, base `PUBLIC_BASE_URL`), `qr_base64` (30-34).
5. Public read `GET /api/v1/share/{token}` (share.py:53-100): `get_share` (sharing_service.py:61-69; expiry at 67-68), `accessed_count += 1` (58), study and patient (60-67), series and counts (69-75), response (77-99). No further auth.
6. Viewer: `/viewer?share=...` is exempt from the auth gate (main.py:308-310). `viewer.js:115-125` fetches `/api/v1/share/{token}`, sets `studyUid`, then `loadStudy` -> `GET /api/v1/studies/{uid}` (viewer.js:139, dashboard.py:698, anonymous, unscoped). Images come from `/dicom-web/studies/{uid}/series/{s}/instances` (viewer.js:174) and `.../rendered` (viewer.js:195), both anonymous (F8).
7. Revocation: `DELETE /api/v1/share/{token}` (share.py:114-120): DEST, then `db.delete(share)`. Image access is unaffected (F8).

### (f) DICOMDIR and de-identified export creation
1. `POST /api/v1/export/dicomdir` (dicomdir.py:33-58): BEARER (any role). Empty list -> 400 (40-41).
2. `export_dicomdir_package` (dicomdir_service.py:100-194): studies with `joinedload` (107-115, no tenant); no match -> `ValueError` -> 404 (116-117, dicomdir.py:54-55).
3. Build structure (119-162): per instance `IMG{n:05d}` under `IMAGES/` (146-148), file appended to DICOMDIR records unconditionally (150-156), file copied only if `exists` (158-159). Missing files leave dangling references (F14).
4. DICOMDIR dataset via `create_dicomdir_dataset` (165; 32-97).
5. ZIP in memory (168-179): `DICOMDIR` at root (172-173); `read_bytes` per file (178) and `writestr` under `IMAGES/IMGnnnnn` (179).
6. Audit `EXPORT_DICOMDIR_CREATED` per study (182-189).
7. Filename `DICOM_EXPORT_<patient_id>.zip` (192). Route commits (dicomdir.py:47) and returns the attachment (49-53).
8. Preview `POST /api/v1/export/dicomdir/preview` (dicomdir.py:61-119): same query shape (71-79) and the structure JSON, without binaries.
- De-identified export: `GET /api/v1/export/{study_uid}/anonymized` (export.py:23-76): study lookup with no tenant (33-35), instances ordered by id (39-47), `anonymize_files_to_zip(paths, zip_name, backends)` (54-56; anonymizer.py:162-192). The anonymizer remaps Study/Series/SOP UIDs with `generate_uid()` (anonymizer.py:134-138), so ZIP member names (182-189) are generated and cannot traverse. `AuditLog` `EXPORTED` (65-69). The call is synchronous inside an `async def` (F7).

### (g) GSPS and KIN creation
- GSPS: no REST creation. Presentation states are ingested by the indexing pipeline when a received SOP class matches (indexing_service.py:296-303) -> `index_presentation_state` (gsps_service.py:202-260) -> `parse_gsps` (89-196) -> upsert `PresentationState` (213-260). Read via gsps.py routes 44-68.
- KIN create: `POST /api/v1/kin` (kin.py:54-78) -> `create_key_image_note` (kin_service.py:148-229):
  1. `generate_uid()` (158); KO dataset with `ConceptNameCodeSequence` (176-181), TEXT item (184-188), IMAGE items with a hard-coded CT `ReferencedSOPClassUID` (190-197, line 194).
  2. `storage_service.save_dataset(ds)` (202): default backend, no study backend (F13).
  3. `index_new_instance(session, meta, file_key, sha256, performed_by=author_name)` (213-215): no tenant. Its KO branch (indexing_service.py:305-316) calls `index_key_image_note` once already.
  4. `index_key_image_note(session, meta, ds, file_key)` (218), upsert with `KeyImageNote` row (122-134) and `AuditLog` `KEY_IMAGE_NOTE_CREATED` (136-142).
  5. Route commits (kin.py:74) and returns 201 (75).

---

## 5. Code review findings

Each finding is tagged **[verified]** (read in code, scenario derived from the code path) or **[inferred]** (needs a runtime or library check). Line numbers are `file:line`.

### BLOCKING

**F1. [verified] Anonymous read of all DICOM objects via WADO-RS, WADO-URI, metadata, rendered, thumbnail, frames, and bulk. The `DICOMWEB_PUBLIC` switch does nothing.**
- Where: dicomweb.py:136-191 (`wado_uri`, no auth), 327-347 (metadata), 359-373 (`wado_study`), 376-387 (`wado_series`), 403-411 (`wado_instance`), 414-422 (instance metadata), 425-509 (`wado_rendered`), 512-590 (thumbnail), 593-732 (`wado_frame`), 735-759 (bulk). Also gsps.py:44-68 (no dependency at all). Middleware: main.py:295-300 (gated paths do not include `/dicom-web`), 358-359 (rate limiter skipped).
- Quote: `async def wado_study(study_uid: str, db: AsyncSession = Depends(get_db)):` (dicomweb.py:360).
- Flag: config.py:253 `DICOMWEB_PUBLIC: bool = True`. settings_service.py:57-60 labels it "DICOMweb public access: Allow QIDO/WADO/STOW without a login token". A grep over `app/` finds no reader of this setting other than its definitions. README.md:827 documents it as "Open LAN access for viewers (tighten for production)". Setting it to false has no effect.
- Failure scenario: any client that can reach the server runs `GET /dicom-web/studies/{uid}/series/{s}/instances/{sop}` and receives the raw DICOM, including PatientName and PatientID. QIDO lists only legacy studies to anonymous callers (F2), but WADO-RS does not apply even that.
- Fix: put an authentication dependency on every DICOMweb read route. Have it honour `DICOMWEB_PUBLIC` (return 401 when false). Make the rate limiter apply to `/dicom-web`. Document that the default value is an intentional LAN-open choice if that is the decision.

**F2. [verified] By-UID retrieval bypasses tenant (VNA) isolation. Only the QIDO list is scoped.**
- Where: `_get_instance` (dicomweb.py:390-400) calls `query_service.instance_query(study_uid, series_uid, {...})`. `instance_query` defaults to `include_all=True` (query_service.py:177-192), so no tenant filter applies. The same holds for `_instances_of` (dicomweb.py:303-306), `wado_uri` (156-162), the thumbnail and frame fast paths (via `_get_instance`), the `zorder` path (`volume_bundle`, dicomweb.py:636), and `presentationUID` lookups (dicomweb.py:461-466).
- Volume: `volume_service.series_slices` calls `query_service.instance_query(None, series_uid)` (volume_service.py:151-152, no tenant). volume.py:65-70, 80-86, 234-240, 339-345 declare `user` but never pass it.
- Other: gsps_service.py:263-289 (no tenant), kin_service.py:232-253 (no tenant), dicomdir_service.py:107-115 (no tenant), export.py:33-35 (no tenant), sharing_service.py:43-47 (no tenant).
- Contrast (correct): QIDO scope at dicomweb.py:208-209 and fusion at fusion.py:36-39 both pass tenant context.
- Failure scenario: a user in tenant A, or an anonymous caller, needs only the StudyInstanceUID or SOPInstanceUID of tenant B to read those objects. UIDs are not secret; they appear in DICOM headers, audit exports, viewer URLs, and share responses.
- Fix: one helper, for example `get_instance_for_user(db, uid, user)`, that applies `scope_from_user` to every by-UID query, including volume, GSPS, KIN, DICOMDIR, export, and share creation. Add tests for cross-tenant 404.

**F3. [verified] STOW-RS accepts anonymous writes. Duplicate UIDs silently overwrite stored objects and repoint existing instances, which allows cross-tenant content substitution.**
- Where: dicomweb.py:806-808 (`user: User | None = Depends(get_optional_user)`); dicomweb.py:876 (`save_dataset`); dicomweb.py:882-886 (`index_new_instance`); storage_service.py:293-297 (key derived from UIDs, then `bk.save_bytes(key, ...)` with no existence check); indexing_service.py:215-221 (`upsert_instance` on an existing SOP row sets `instance.file_path` and `storage_backend` but never `series_id`); indexing_service.py:94-115 (`get_or_create_study` applies the tenant only on create).
- Quote: `instance.file_path = file_path` (indexing_service.py:216), inside the `else:` branch that handles an existing `sop_instance_uid`.
- Scenario A (overwrite): anonymous POST with the victim's StudyInstanceUID, SeriesInstanceUID, and SOPInstanceUID. The key matches, so the stored file is replaced. Only the new STORED audit row (with the new SHA-256) remains, so the original hash is lost.
- Scenario B (substitution): anonymous POST reusing the victim's SOPInstanceUID with new study and series UIDs. The object is written under the attacker's study path, and `upsert_instance` repoints the victim's `Instance.file_path` to it (series_id stays with the victim). Subsequent WADO-RS for the victim instance (dicomweb.py:407, 612) returns the attacker's bytes.
- Scenario C (injection): anonymous POST with an existing tenant's StudyInstanceUID. The new instances join that tenant's study because `get_or_create_study` does not change tenant on an existing study.
- Policy note: ADMIN uploads get `tenant_id=None` (dicomweb.py:884), i.e. the legacy unscoped scope, which anonymous QIDO lists (query_service.py:149-150). Anonymous uploads of new studies therefore become publicly listable.
- Fix: require auth on POST. Reject (409) an incoming SOP whose existing row has a different study or series, or a different SHA-256, unless an explicit overwrite policy is set. Never repoint an existing `Instance.file_path` from a different study. Check the study's tenant against the caller. Keep an append-only history of replaced objects.

**F4. [verified] STOW multipart parser buffers unbounded part headers and preamble. This is an anonymous memory and CPU DoS, and the caps do not apply.**
- Where: dicomweb_service.py:179-181 (preamble loop: `while buf.find(marker) == -1:` with no cap) and 197-202 (part headers: `idx = buf.find(b"\r\n\r\n")` in a loop, no cap). Caps are checked only on payload (230-232, 240-245). Defaults: config.py:269-270 `STOW_MAX_PART_BYTES = 2 GiB`, `STOW_MAX_TOTAL_BYTES = 0` (unlimited).
- Scenario: one anonymous request with a valid boundary followed by a header block that never contains `\r\n\r\n`. `_pull` appends every chunk, and each iteration calls `buf.find` over the whole buffer. Memory grows without bound and CPU grows quadratically (O(H^2) for H bytes of headers). The request never reaches the payload caps.
- Also: a 2 GiB part is accumulated in a `bytearray` (dicomweb_service.py:215, 228) and copied again (246: `bytes(payload)`), then wrapped in `BytesIO` (308). That is roughly three copies per part, multiplied by concurrent uploads. Rate limiting does not apply to `/dicom-web` (main.py:358).
- Fix: cap header and preamble bytes (for example 64 KiB) and fail fast. Search incrementally from the last scanned offset. Count header bytes toward the total. Set a finite default for `STOW_MAX_TOTAL_BYTES`. Stream each part to a `SpooledTemporaryFile` rather than a bytearray.

**F5. [verified] Frame cache key collision: non-raw or multipart bytes are stored under the key that the raw decoder reads, so pixel data can be served corrupted.**
- Where: write at dicomweb.py:730 `cache_set(f"paxs:frame:{sop_uid}:{resolved}", data, expire_seconds=1800)`. Read at wado_rs_bulk_service.py:162 `r_cached = cache_get(f"paxs:frame:{sop_uid}:{frame_idx}")`, promoted to the in-process LRU at 165, and returned tagged `RAW_FRAME_CONTENT_TYPE` at 167 and 199. Other writer: wado_rs_bulk_service.py:196 and warm_service.py:172 (both raw, correct).
- Scenario: (1) A client requests `.../frames/1` with `Accept: multipart/related` (or a default Accept). `_extract` returns a multipart envelope or a compressed codestream, and line 730 stores it under `paxs:frame:{sop}:1`. (2) The viewer requests the same frame with `Accept: multipart/related; ...; transfer-syntax=*`. The wado_frame key (dicomweb.py:688) misses, so `decode_frames_to_raw` runs. It reads `paxs:frame:{sop}:1` and returns the envelope or codestream labelled as uncompressed little-endian. The bytes are also promoted to the in-process LRU, so the corruption survives Redis expiry until eviction.
- Impact: a radiologist can be shown a corrupted or wrong image without an error.
- Fix: either remove the write at dicomweb.py:730 (wado_frame never reads that key), or give the raw namespace its own prefix (for example `paxs:frameraw:`). Store the media type with the value. Add a regression test that issues a native request followed by a `transfer-syntax=*` request for the same frame.

**F6. [verified] Anonymous unbounded memory allocation on volume endpoints (process OOM).**
- Where: `/data` count cap is per slice only (volume.py:104-107; `VOLUME_MAX_SLICES = 4096`, config.py:200). The whole block is joined in memory (volume.py:196-210: `data = b"".join(chunks)`, line 210). Redis set up to 256 MiB (volume.py:211-213). The "streaming" iterator `iter_volume_bytes` keeps every decoded dataset in its local `cache` dict for the whole call (volume_service.py:551-555, no eviction). `load_volume_array` does the same (715-716), keeps float32 slices in `arrays` (714, 728), and then calls `np.stack` (730), which doubles the float32 peak. `_VOL_CACHE_MAX = 8` (638) has no byte bound. `_vol_redis_set` (663-674, 738-741) has no size cap.
- Scale: 4096 slices of 512x512 at rescale=1 is about 4.3 GiB of response built in memory. One 512^3 float32 volume is 512 MiB and up to 8 are retained in process memory. MIP adds `np.take` (volume_service.py:1092) and `_vrt_raycast` full-slab temporaries (1017-1024).
- Scenario: anonymous `GET /api/v1/volume/{series}/data?rescale=1` repeated across a few CT series exhausts memory and the process is killed.
- Fix: stream the response with the existing generator (`StreamingResponse`). Drop the per-call dataset cache, or bound it and release each dataset after use. Cap total bytes, not only slice count. Cap or skip Redis values above a limit. Add per-user concurrency limits and a single-flight build lock per series.

**F7. [verified] DICOMDIR and de-identified export let any authenticated user (any role, any tenant) export any study. Whole ZIP is built in memory and file I/O blocks the event loop.**
- Where: dicomdir.py:33-38 (`user: User = Depends(get_current_user)`, no role or tenant check), dicomdir_service.py:107-115 (study query with no tenant), dicomdir_service.py:158 (`storage_service.exists` per instance, synchronous inside `async def`), dicomdir_service.py:168-179 (`read_bytes` per file, all held in `BytesIO`). export.py:28-35 (BEARER, no tenant), export.py:54-56 (`anonymize_files_to_zip` is synchronous inside `async def export_anonymized`; the event loop is blocked for the whole study).
- Failure scenario: the lowest-privileged logged-in user exports every instance of another tenant's study in one call, as a DICOMDIR ZIP or as a de-identified ZIP. A large study also holds the event loop and the memory for the duration.
- Fix: apply `scope_from_user` to these queries. Require a role (for example RADIOLOGIST or ADMIN) for bulk export. Run ZIP creation in a thread and stream to a temp file. Rate-limit exports. The audit trail already exists (dicomdir_service.py:182-189; export.py:65-69), which helps detection but not prevention.

**F8. [verified] Share links do not protect image data. Expiry and revocation apply only to the metadata JSON.**
- Where: share.py:53-100 (token checked for metadata only), sharing_service.py:61-69 (expiry), share.py:114-120 (delete). viewer.js:115-125 (token -> study UID), viewer.js:139 (`/api/v1/studies/{uid}`), viewer.js:174 and 195 (`/dicom-web/...`). dashboard.py:698-712 (`study_detail`: anonymous, unscoped, outside this area but read for verification). All DICOMweb read routes are anonymous (F1).
- Failure scenario: a recipient opens a link, then the link expires or is deleted. Image requests still succeed because the study UID is enough. The UID appears in every viewer URL and in the files the recipient already downloaded.
- Fix: issue a server-side, study-bound access token when the share is first redeemed. Have `/dicom-web` and `/api/v1/studies/{uid}` require it. Revoke it on delete and expiry. The product decision on whether recipients need a login is open (see section 6).

### IMPORTANT

**F9. [verified] Warm-on-open is triggered anonymously, with no concurrency cap, and warms studies outside the caller's scope.**
- Where: dicomweb.py:205-207 (`warm_service.schedule_warm(study_uid_param)` runs before the tenant scope at 208-209). dicomweb.py:334 (metadata, after the unscoped instance query at 329). warm_service.py:50-72 (one daemon thread per distinct UID; the only guard is an in-process set, so there is no cap). warm_service.py:176-211 (volume pre-warm: `load_volume_array` for every CT/MR/PT series, `asyncio.run` in the worker thread).
- Scenario: a script sends thousands of QIDO requests with distinct `StudyInstanceUID` values, creating thousands of threads, each decoding pixels and building volumes. A warm pass for another tenant's study also writes thumbnails, frame keys, and `paxs:warm:{uid}` records.
- Fix: check scope before warming. Run warm passes through a bounded worker queue with a per-process cap and deduplication. Rate-limit warm triggers.

**F10. [verified] Event-loop blocking: synchronous Redis and disk I/O inside async handlers, and Redis reconnect attempted on every call while Redis is down.**
- Synchronous Redis on the loop: dicomweb.py:447 (`cache_get`), 500 (`cache_set` after `to_thread`, on the loop), 567 (`cache_get`), 586 (`cache_set`), 690 (`cache_get`), 729-730 (`cache_set`); volume.py:185 (`r_client.get`); warm_service.py:143 (`cache_get`, inside the worker thread, acceptable).
- Synchronous disk I/O on the loop: volume_service.py:163-165 (`storage_service.exists` in `series_slices`), 407-409 (`read_dataset` in `volume_bundle`), fusion via `volume_info` for each series; dicomdir_service.py:158, 178; export.py:54.
- CPU on the loop: `_cached_response` computes MD5 over the full payload on the loop (dicomweb.py:119-130; calls at 189, 411, 675, 732). For a multi-hundred-MB instance this blocks for hundreds of milliseconds.
- Redis outage: redis_service.py:295-318 retries `redis.Redis.from_url(...).ping()` on every call when `_redis_client` is None, under `_redis_lock`, with a 1.5 s connect timeout. There is no negative cache or backoff. Each WADO-RS request (several `cache_get` and `cache_set` calls) can stall the loop for seconds, and the lock serializes the stalls.
- Fix: wrap Redis calls in `asyncio.to_thread` or use an async client. Add a negative cache with exponential backoff in `get_redis_client`. Move MD5 and file I/O into threads.

**F11. [verified] PHI access audit trail is incomplete.**
- `add_audit` is called only at dicomweb.py:185 (WADO-URI DICOM branch) and dicomweb.py:508 (rendered). The rendered audit is skipped on cache hits (`return` at 448-449 precedes it). There is no audit for: `wado_study` (359), `wado_series` (376), `wado_instance` (403), metadata routes (327, 339, 414), thumbnail (512), frames (593), bulk (735), any QIDO query (197, 256, 277), any volume route (65, 80, 234, 339), GSPS reads (gsps.py), KIN reads (kin.py), or share metadata reads (share.py:53-100). Audited exports: export.py:65-69, dicomdir_service.py:182-189; share creation: share.py:35-39.
- Failure: a full-study retrieval through `/dicom-web/studies/{uid}` (dicomweb.py:359-373) leaves no record.
- Fix: a single audit hook for PHI reads, applied to cache hits as well.

**F12. [verified] Volume downsample: headers and npy shape use floor, the payload uses ceil. Odd dimensions produce a mismatched npy or Content-Length.**
- Where: volume.py:119-120 (`rows // downsample`, floor), 126-127 and 152-155 (npy shape and Content-Length use floor), 138 (rescale Content-Length uses floor), 142-143 and 159-160 (`X-Volume-Rows`/`Cols` use floor). volume_service.py:563-564 (`arr[::step, ::step]` gives ceil(n/step) rows), 592-593 (`volume_byte_length` uses ceil, which matches the body).
- Example: rows=511, downsample=2. Headers say 255 rows; the body has 256 rows. For `format=npy` the header shape and Content-Length are based on 255 rows, so the body is longer than declared, and the npy file does not match its header.
- Fix: one helper, `out_dim = ceil(n / d)`, used for headers, shape, and length. Add a test with odd dimensions.

**F13. [verified] Hanging protocols, KIN, and share creation have no ownership or tenant checks. Any authenticated user can act on other users' data.**
- Hanging protocols: `user_id` comes from the request body (hanging_protocols.py:35, 62-75), so a user can create protocols owned by another user. Any role can delete any protocol (hanging_protocols.py:125-136; hanging_protocol_service.py:87-93), and list all (78-85).
- KIN: any authenticated user can write a KO referencing any study UID (kin.py:54-78; kin_service.py:148-229). The KO is indexed with `tenant_id=None` (kin_service.py:213-215). The KO file is written to the default backend regardless of the study's backend (kin_service.py:202).
- Share: any authenticated user can publish a 30-day public link for any study UID (share.py:27-50; sharing_service.py:43-47). The public read returns patient name and ID with no auth (share.py:77-89).
- Fix: derive `user_id` from the caller. Require ownership or ADMIN for delete. Require RADIOLOGIST or ADMIN and tenant match for KO and share creation. Use the study's backend for KO writes.

**F14. [verified] DICOMDIR output is probably non-conformant, and the records can reference files that are not in the ZIP.**
- Where: dicomdir_service.py:39-41 (no FileSetID, which is required in the DICOMDIR file-set module, not written here). 57-97 (directory records have no offset-to-next or lower-level pointers). 150-159 (records appended for every instance, but the file is copied only when `exists`, so missing files are referenced but absent).
- Fix: write FileSetID and the offset pointers, or use a DICOMDIR writer. Skip or flag missing instances. Validate with `dciodvfy` or `dcmdump` (not run) **[inferred]**.

**F15. [verified] Outbound SSRF guard: DNS rebinding window; response size cap checked after the full download; Basic credentials over plain HTTP.**
- Resolution is done once in `validate_web_url` (dicomweb_client.py:44-64, 67-86), then httpx resolves again at connect (99-105, 245-248, 474-478). A hostname can resolve to a public address during validation and to a private one at connect.
- `resp.content` is loaded fully (249) and only then compared to `DICOMWEB_CLIENT_MAX_BYTES` (252; config.py:262 default 4 GiB).
- `http://` is allowed (dicomweb_client.py:54) and Basic credentials are sent with it (89-96).
- Partly mitigated: `follow_redirects=False` (104, 247, 476). Node URLs are admin-configured (nodes.py), which lowers the severity.
- Fix: validate the IP and connect to that IP (custom transport, or a resolver hook). Stream responses with a byte counter. Require `https` when credentials are set.

**F16. [verified] GSPS and KIN instance lookups use SQL substring matching, which returns the wrong notes.**
- gsps_service.py:269-271 `PresentationState.referenced_sop_instance_uids.contains(sop_instance_uid)`. kin_service.py:248-250 `KeyImageNote.referenced_sop_instance_uids.contains(sop_instance_uid)`. Both columns are comma-joined text (models 473-530 and 935-960).
- Scenario: asking for notes on `1.2.3.4` also returns notes referencing `1.2.3.45`, `11.2.3.4`, and so on. Affects `/api/v1/gsps/instance/{sop}/presentations` and `/api/v1/kin/instance/{sop}/notes`.
- Fix: match with delimiters, for example `CONCAT(',', col, ',') LIKE '%,<uid>,%'`, or move references to a child table.

**F17. [verified] Rendered and thumbnail cache keys contain raw user input (unbounded cardinality).**
- dicomweb.py:445 (`window`, `max_size`, `presentation_uid` in the key) and 563-566 (`window`). `presentationUID` is not checked against the study or against existence (455-476), so random values create new keys (600 s TTL each).
- Fix: validate `window` (numeric pair) and clamp `viewport`. Look up `presentationUID` within the same study before caching.

**F18. [verified] Information disclosure: internal storage paths and raw exception text in responses.**
- File paths: gsps.py:40 (`"file_path": ps.file_path`), kin.py:49 (`"file_path": kin.file_path`), both returned to any caller (GSPS anonymous).
- Exception text: kin.py:78 (`500, f"Failed to create Key Image Note: {exc}"`), hanging_protocols.py:75 (`400, f"Failed to create protocol: {exc}"`), dicomdir.py:58 (`500, f"DICOMDIR export failed: {exc}"`), dicomweb.py:179, 503, 589 (`422, f"Cannot render ...: {exc}"`).
- Fix: omit `file_path` from public responses. Return generic messages and keep the detail in the log, which is already written.

**F19. [verified] CORS allows any origin. Anonymous DICOMweb reads are then readable from any web page the visitor's browser can reach.**
- config.py:254 `CORS_ORIGINS: str = "*"`; main.py:430-436 (`allow_origins=settings.cors_origin_list`, which is `["*"]` for the default; `allow_headers=["*"]`, `allow_methods=["*"]`). `allow_credentials=False` (433), so cookies do not cross origins. The impact is limited to unauthenticated reads, which F1 makes available.
- Fix: restrict origins to the viewer origin. Revisit together with F1.

**F20. [verified] Process-level caches are never invalidated. Deleted or re-imported series keep being served. Some responses are marked `public` or `immutable` for PHI.**
- `_BUNDLE_CACHE` is keyed by series UID, has no TTL, and is checked before any DB lookup (volume_service.py:399-400), so `/info`, `/data`, `/mip`, and `/mpr/slice` keep serving a series after it is deleted or archived, until FIFO eviction (491-493) or a restart. The comment at 390-391 says LRU, but the code is FIFO (491-492).
- `_VOL_CACHE` keyed by `array:{series}:{n}` (volume_service.py:687) has no delete hook. A series re-imported with the same slice count keeps the stale volume.
- `clear_frame_cache` (wado_rs_bulk_service.py:46-48) has no callers.
- Cache headers: `private, max-age=31536000, immutable` on instance and rendered responses (dicomweb.py:116, used at 127). Browsers keep copies for one year, and `immutable` suppresses revalidation after deletion or revocation. `public, max-age=86400, immutable` on volume data (volume.py:146, 163) lets shared caches store PHI.
- Fix: invalidate bundle and array caches from delete, lifecycle, and re-import paths. Key by a series revision, not only the UID. Use `private, no-cache` (or a short `max-age`) for PHI.

**F21. [verified] `_FRAME_CACHE` is a shared OrderedDict used from the worker thread pool without a lock.**
- wado_rs_bulk_service.py:28-43 (`OrderedDict`, no lock). `_cache_get` does `.get()` then `move_to_end()` (32-36). Another thread can evict the key in between (`popitem`, 42-43), and `move_to_end` then raises `KeyError`, which becomes an HTTP 500 for that frame request. Compare `_VOL_CACHE`, which has a lock (volume_service.py:639).
- Fix: guard get and put with a `threading.Lock`.

**F22. [verified / behaviour inferred] STOW per-part error handling does not roll back the shared DB session. One DB error turns the request into a 500 and leaves orphan files.**
- dicomweb.py:845-898: all parts share `db`. The per-part `except` (892-898) does not roll back or use a savepoint. A flush error (for example an IntegrityError from a concurrent insert of the same study or instance; `get_or_create_study` at indexing_service.py:99-115 is check-then-insert with no lock) leaves the session in a failed state.
- **[inferred]** SQLAlchemy 2.x requires an explicit rollback after a failed flush. Subsequent parts then fail with a pending-rollback error, and the final commit in `get_db` (session.py:228) raises, so the request returns 500 even though earlier parts' files were already written.
- 413 and 400 are raised at dicomweb.py:899-903. `get_db` rolls back (session.py:229-231), so DB rows are dropped but files remain (orphans).
- Docstring at dicomweb.py:809-816 says "a single malformed part fails in isolation without aborting the rest". That holds only for parse errors, not DB errors.
- Fix: `begin_nested()` (SAVEPOINT) per part, or a fresh session per part. Delete the object file when its DB write fails.

**F23. [verified] `wado_frame` reads and parses the whole instance before checking the cache. Frame extraction materializes all frames.**
- dicomweb.py:611-612 reads the full dataset (`read_dataset` without `stop_before_pixels`) before the cache lookup at 690. A cache hit still pays a full file read and decode of the header.
- wado_rs_bulk_service.py:81 `all_frames = list(generate_frames(pixel_data, number_of_frames=number_of_frames))` materializes every frame of an encapsulated instance to return one, even though the comment at 79-80 says "materialize only the requested indices". decode_frames_to_raw (176) decodes all frames with `ds.pixel_array` to satisfy one missing frame.
- Fix: check the cache before reading the file (the cache key needs only sop and frame). Use per-frame decoding (for example pydicom's frame-level API **[inferred]**; verify against the pinned pydicom 3.x). Read with `stop_before_pixels` for the metadata-only path.

**F24. [verified] Whole-study and whole-series WADO-RS hold all bytes, then copy them into a multipart body. Memory is about 2x study size with no cap.**
- dicomweb.py:359-373 and 376-387 (`_read_all` builds `out`), `_multipart_response` (350-356), `build_multipart` (dicomweb_service.py:249-257: `bytearray` then `bytes(body)`). `_instances_of` (303-306) is unbounded.
- Fix: stream the multipart as an async generator, per part, and cap the instance count or total bytes.

**F25. [inferred] No size guard before pixel decode of attacker-supplied DICOM.**
- Pixel decode happens with no limit on Rows x Columns x NumberOfFrames x bits: imaging.py:42, wado_rs_bulk_service.py:98 and 176, volume_service.py:513. The STOW path stores untrusted files (dicomweb.py:876), and the compression worker decodes them later (indexing_service.py:282-287, `COMPRESSION_ENABLED` default True at config.py:107). I did not read the compression worker or pydicom's own limits.
- Fix: validate header dimensions against a configured maximum before any `pixel_array` call, and against the file size.

**F26. [verified, cross-cutting, BLOCKING for a clean checkout] The storage backend package is not in the repository. `storage_service` cannot be imported.**
- storage_service.py:35 `from app.services.storage import get_storage_backend` and line 38 `from app.services.storage.backend import StorageError`. Neither `app/services/storage/` nor `app/services/storage.py` exists on disk (`find`), and no commit in `git log --all` contains `services/storage`. Importers: storage_service.py, replication_service.py, app/api/v1/storage.py, and tests (test_storage_backend, test_multi_storage, test_cstore, test_replication).
- `.gitignore:9` contains `storage/`, which matches any directory named `storage` at any depth, including `app/services/storage/`. A developer who creates the package locally would have it silently ignored.
- Impact: `dicomweb.py:32` imports `storage_service`, so every route in this area fails at import in this checkout.
- Fix: track the backend package (or narrow the ignore to `/storage/` at the repo root), and confirm the deployed artifact contains it.

**F27. [verified config, exploitability conditional] Default JWT signing key.**
- config.py:136 `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"`, used for bearer tokens (deps.py:51, 76; auth.py:208, 269, 327; ldap.py:45). I found no check that rejects the default. The BEARER routes in this area (hanging protocols, KIN, DICOMDIR, export, share creation) would accept a forged token for any username, including ADMIN, if the default is in use.
- Fix: refuse to start with the default key when not in a dev profile. Verify whether a startup guard exists outside the files read **[inferred]**.

### NIT

- **N1** dicomweb.py:59-68: `_paginate` has no lower bound on `limit`. `limit=-1` becomes `.limit(-1)`, which MySQL rejects, so the request fails with 500 **[inferred]**. The same applies to very large `offset`.
- **N2** dicomweb.py:265-270 and 288-294: `qido_series` and `qido_instances` use OFFSET paging. Only studies were moved to keyset (lines 210-216).
- **N3** dicomweb.py:615-619 and 628-633: a non-numeric or negative frame token silently falls back to frame 1 in the non-zorder path (`isdigit` filter, then `indices = [1]`). The zorder path raises 404 instead. Make them consistent (404).
- **N4** wado_rs_bulk_service.py:184-201: duplicate frame indices (for example `frames/1,1`) fill only the first slot. The second slot stays `None` and is dropped, so the multipart has fewer parts than requested.
- **N5** dicomweb.py:897: every STOW failure reports status `0xC212`. dicomweb.py:854-859: lenient parts are parsed twice. dicomweb.py:839-879: the path `study_uid` is not checked against the parts' StudyInstanceUID.
- **N6** dicomweb.py:190: `Content-Disposition` uses the unvalidated `object_uid` in a quoted filename.
- **N7** volume.py:110: `int(params.get("downsample"))` is unguarded, so a bad value gives a 500. volume.py:291-292 and 426-427: `float(window_center)` and `float(window_width)` run outside the try block, so bad input gives a 500, and the `400` at volume.py:431 is unreachable for parse errors.
- **N8** volume_service.py:687 and the `array:` key: changes in content with the same slice count are not detected (see F20).
- **N9** gsps_service.py:36 and 155: `units` is parsed but `_draw_graphic` (377-402) ignores it. DISPLAY-unit coordinates are drawn as pixels.
- **N10** gsps_service.py:415: `ImageFont.truetype("arial", 14)` is loaded for every text annotation.
- **N11** hanging_protocols.py:57-58: `created_at`/`updated_at` are null in the create response. They are server defaults (models 1330-1335) and are not loaded after flush.
- **N12** main.py:554-556 mounts fusion only under `FHIR_ENABLED` (config.py:187, default true). Fusion disappears when FHIR is disabled.
- **N13** fusion_service.py:87 and 95-106: `volume_info` runs once per series and is not cached when a series is not volumetric. Pairs are O(n^2) and repeat full geometry per pair.
- **N14** share.py:58: `accessed_count += 1` is a non-atomic read-modify-write. sharing_service.py:52 stores the token in plaintext (`PatientShare.share_token`, String(64)). Hash it at rest.
- **N15** dicomdir.py:52 and dicomdir_service.py:192: the patient ID goes into `Content-Disposition` unescaped.
- **N16** config.py:281: `PUBLIC_BASE_URL` defaults to `http://0.0.0.0`, so share links and QR codes are unusable unless configured (sharing_service.py:72-75).
- **N17** dicomweb.py:455-476: `presentationUID` is not checked against the instance's study (also in F17).
- **N18** kin_service.py:194: every reference is labelled as CT. Look up the referenced instance's SOPClassUID instead (instances are indexed with it, models 431-472). Also kin.py:33 accepts an unbounded reference list, and kin.py:35 accepts any title code. Reference counts and UIDs should be validated.
- **N19** indexing_service.py:305-316 and kin_service.py:213-218: the KO is indexed twice (once in the indexing pipeline, once explicitly). Harmless (upsert), but wasteful.
- **N20** imaging.py:52-75 and dicomweb.py:478-496: `wado_rendered` on a multi-frame instance receives a 3-D array, and `Image.fromarray(..., mode="L")` will likely fail, returning 422 **[inferred]**. Frame selection is not supported in `rendered`.
- **N21** wado_rs_bulk_service.py:24 hard-codes the explicit little-endian transfer syntax in `RAW_FRAME_CONTENT_TYPE` for every uncompressed instance. A big-endian source would be mislabelled **[inferred]**.
- **N22** gsps_service.py:325-351: flip, then rotate, then shutters, then annotations. The DICOM ordering and coordinate spaces for annotations relative to the spatial transform were not verified against PS3.3 C.10 **[inferred]**.
- **N23** `dicomweb_client._ingest_dicomweb_dataset` (dicomweb_client.py:338-347) and `warm_service._warm_study` (warm_service.py:192-211) call `asyncio.run` in worker threads, each creating a new loop, while `AsyncSessionLocal`'s pool may hold connections bound to another loop. Possible "attached to a different loop" errors under load **[inferred]**; test before relying on it.
- **N24** `dicomweb_service.parse_multipart` (114-124) and `extract_frame` (267-304) and `volume_service.write_npy` (598-605) and `wado_rs_bulk_service.clear_frame_cache` (46-48) have no callers.

---

## 6. Open questions

1. **Intent of anonymous DICOMweb (F1, F8, F19).** README.md:827 calls it "Open LAN access". Is the production default meant to be anonymous, and is DICOMWEB_PUBLIC meant to be the control? If yes, the switch needs to be wired. If no, F1 and F8 are blocking.
2. **Storage backend package (F26).** Is `app/services/storage/` expected in the repo? If it is generated or lives outside git, `.gitignore:9` needs narrowing. I could not locate it in this checkout or in git history.
3. **Default SECRET_KEY (F27).** Is there a startup guard outside the files read (main.py was read only in ranges)? If not, the default must be rejected in production.
4. **Starlette JSONResponse and NaN.** `volume_info_endpoint` returns NaN when a malformed `WindowCenter` is present (`_float(..., math.nan)`, volume_service.py:376). I believe Starlette serializes with `allow_nan=False`, which would 500 the request. Starlette was not installed here, so this was not verified.
5. **pydicom 3.x `Dataset.pixel_array` caching.** The magnitude of F6 and F23 depends on whether pydicom caches decoded arrays between accesses. Not verified against the pinned version (`pydicom>=3.0`).
6. **Compression worker and pixel decode limits (F25).** Not read. Does the worker cap decoded size?
7. **parse_metadata (app/dicom/parser.py) and thumbnail_service.py** were not read. Does `parse_metadata` touch PixelData? That changes STOW cost.
8. **Share access model (F8).** Should share recipients be able to fetch images without a login, after the share expires, or at all? This is a product decision that determines the fix.
9. **Tenant semantics for ADMIN uploads (F3).** ADMIN STOW lands in the legacy NULL-tenant scope, which anonymous QIDO lists. Is that intended?
10. **Concurrent-STOW behaviour (F22).** Confirm with a test that two parallel STOW requests creating the same new study produce a 500 and orphan files.
11. **GSPS and KIN conventions (F16, F18, N18, N22).** Should KIN reference the actual SOPClassUID? Should GSPS annotations honour `GraphicAnnotationUnits` DISPLAY? Confirm against PS3.3 and the intended viewer contract.
12. **DICOMDIR conformance (F14).** Validate the output with `dciodvfy` or pydicom's DICOMDIR tools (not run).
13. **Remote-node SSRF (F15).** Is `DICOMWEB_CLIENT_ALLOW_PRIVATE` ever enabled in production configs? Node URLs are admin-entered, so is exposure limited to admins?
14. **Warm pass semantics (F9).** Is warming studies outside the caller's tenant intended?
15. **Redis outage behaviour (F10).** Is the expected behaviour to degrade slowly, or should requests fail fast? The current code adds up to 1.5 s per Redis call.
16. **Event-loop and asyncio.run (N23).** Confirm `AsyncSessionLocal` works with per-call event loops, or move those paths to sync sessions.

Verification notes:
- No tests were run. Each finding was traced by reading code. Where a finding depends on library behaviour, it is tagged [inferred].
