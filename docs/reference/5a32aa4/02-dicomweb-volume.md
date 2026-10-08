# DICOMweb and volume / MPR backend — changes in 5a32aa4

> **Re-review of commit `5a32aa4`** (baseline `e90d32c`), part of the [PAXS Archive documentation](../../README.md). Static analysis, 2026-10-08; line numbers refer to `5a32aa4`. Baseline area reference: [03-dicomweb-imaging.md](../03-dicomweb-imaging.md). Findings are consolidated and prioritized in [CODE_REVIEW.md](../../CODE_REVIEW.md), which takes precedence where the two differ. Finding IDs here (N1, N-01, V4-01, …) are local to this file; CODE_REVIEW.md maps them to NEW-xx IDs.

- Commit reviewed: HEAD `5a32aa4` (old reviewed commit `e90d32c`).
- Working tree matches HEAD for every in-scope file (checked with `git status` and `git diff HEAD`), so all line numbers below are HEAD line numbers.
-
- Severity: 🔴 security, data integrity or memory-exhaustion DoS; 🟡 correctness, auth gap, blocking or contract issue; 🟢 low or hygiene.
- Files read in full: `app/api/v1/volume.py` (1258 lines), `app/services/volume_service.py` (1770), `app/api/v1/dicomweb.py` (1097). Read in targeted ranges: `wado_rs_bulk_service.py`, `deps.py` (diff plus helper bodies), `gsps.py`, `fusion.py`, `export.py`, `dicomdir.py`, `dicomdir_service.py`, `dicomweb_service.py` (`iter_stow_parts`), `indexing_service.py` (series/instance upsert), `storage_service.py` (staging), `warm_service.py`, `config.py`, `query_service.py`, `tenant_service.py`, `sharing_service.py`.
- `warm_service.py` and `tenant_service.py` are unchanged at HEAD. `dicomweb_service.py` is unchanged too.

## Headline

- The default-config auth gap is closed. `DICOMWEB_PUBLIC` now defaults to `False`, which makes DICOMweb and volume reads require a credential or a valid share token. Volume endpoints now require auth regardless of that flag.
- Tenant checks (C04) were added to volume, GSPS, export, DICOMDIR and WADO-URI. Two gaps remain: STOW can still write into another tenant's Series or Instance through UID reuse (N1, 🔴), and some GSPS and rendered-path lookups are still not tenant-scoped.
- New memory-exhaustion paths (🔴): `/volume/{uid}/data` materializes up to 4096 slices with no byte cap (N2); `/mip` and `/mpr/slice` load the full stack with no slice cap (N3); frame requests amplify memory through repeated indices (N4).
- New endpoints `/volume/{uid}/negotiate` and `/volume/{uid}/tiles`. `/tiles` skips the client-mode guard and silently ignores its `size` parameter (N8).
- F-06 (warm `asyncio` import) is still open. The volume pre-warm never runs, and the NameError is swallowed (`warm_service.py:211`, `:213`). D-06 is only partly fixed (N5).
- Blocking: `storage_service` is imported by the V2a modules, but `app/services/storage/` is absent from this checkout and ignored by `.gitignore:9`. That needs verifying (N30, 🔴).

## 1. Change summary

### 1.1 Per file

| File | Change at HEAD |
|---|---|
| `app/api/v1/dicomweb.py` | Router-level guard `dependencies=[Depends(require_dicomweb_access)]` (L56-65). WADO-URI owning-study tenant and share check (L192-207). `_anonymous_share_scope` forces the share study into QIDO (L237-246, L302-303, L365-369, L390-397). `_reject_cross_tenant_ingest` (L249-266) and `_validate_ingest_uids` (C05, L269-285). Instance lookups now use `current_scope()` (L414-423, L507-524). Frame cache key is mode-qualified (L818, L857-863) and the old `paxs:frame:{sop}:{n}` write is removed. Accept header is normalized for quoted `transfer-syntax` (L806-812). STOW: quota staging (L1019-1023), commit per part then promote (L1044-1045), HTTP 507 when a part is refused for quota (L935-936, L1096-1100), 400 for C05/C04 (L1061-1067). |
| `app/api/v1/deps.py` | New C03/C04 helpers (see 1.3). Expired sessions are refused in `get_current_user_from_cookie` (H04). `DESTRUCTIVE_ROLES` is imported from `app/core/roles.py`. |
| `app/api/v1/volume.py` | New `/negotiate` (L460-563) and `/tiles` (L925-1109). Server and client mode guards with `X-PAXS-Mode-Hint` (L281-457, enforced at L630-632, L834-836, L1143-1145). All read routes now use `require_series_read` plus `require_series_access` (old code used `get_optional_user`, see the old file's L69, L85, L239, L344). `/data` keeps `downsample` (L657) with `strided_dim` headers (L671-672), adds a Redis cache (L737-789), and builds the body by materialize-then-join (L760-789). |
| `app/services/volume_service.py` | New `series_slices` and `volume_bundle` (L169-287, L420-526). `iter_volume_bytes` (L559-620). Byte-budgeted L1 volume cache (L697-747). Single-flight volume materialization (L762-773, L1297-1358). Tile cache with in-flight coalescing (L783-969). `render_tiles` (L1081-1131). Reslice and MIP (L1487-1765). Session registry (L1164-1190). Redis volume read and write (L1207-1236). |
| `app/services/wado_rs_bulk_service.py` | Byte-budgeted decoded-frame cache with a lock (L42-106). `extract_frame_bytes` and `decode_frames_to_raw` now log the codec context (L109-270). |
| `app/api/v1/fusion.py` | Auth changed from `get_optional_user` to `get_current_user_any` (L33). Anonymous now gets 401. Scope still `scope_from_user` (L36). |
| `app/api/v1/gsps.py` | Auth `get_current_user_any` on all three routes (L46-47, L59-60, L69-70). Tenant checks added (L53-54, L63, L78, L92-93). `_ps_to_dict` still returns `file_path` (L41). |
| `app/api/v1/export.py` | `require_study_access` replaces the direct lookup (L34). Auth remains `get_current_user` (bearer only, L31). AuditLog row added (L62-66). |
| `app/api/v1/dicomdir.py` | `require_study_access` per UID on export (L43-44) and preview (L74-75). Auth remains `get_current_user` (L37, L68). |
| `app/services/warm_service.py` | Unchanged. F-06 still applies (L211). |
| `app/services/tenant_service.py` | Unchanged. `scope_from_user(None)` is `(None, False)` (L83-95). |

### 1.2 Routes at HEAD

DICOMweb auth: the router guard (`require_dicomweb_access`, `deps.py:329`) requires a bearer token or session cookie, or a share token for the study named in the path. Exception: when `DICOMWEB_PUBLIC=true`, anonymous reads are allowed. For POST (STOW), the identity requirement is the same, except that `DICOMWEB_PUBLIC=true` allows anonymous writes.

| Method | Full path | Handler (line) | Params | Response | Change vs e90d32c |
|---|---|---|---|---|---|
| GET | `/dicom-web/wado` | `wado_uri` (154) | `requestType=WADO`, `studyUID`, `seriesUID`, `objectUID`, `contentType=application/dicom\|image/jpeg\|image/png` | DICOM Part 10 or JPEG/PNG, `ETag`, `Cache-Control: private, max-age=31536000, immutable` | Owning-study tenant or share check added (192-207). Bearer token ignored (200). Audit `RETRIEVED` (228). |
| GET | `/dicom-web/studies` | `qido_studies` (292) | QIDO filters, `limit` (max 1000), `offset`, `cursor=<date>\|<id>` | `application/dicom+json`, `X-Next-Cursor` | Share study forced (302-303). `schedule_warm` before tenant scope (307-308). Tenant via `scope_from_user` (309). |
| GET | `/dicom-web/series`, `/dicom-web/studies/{study_uid}/series` | `qido_series` (359) | QIDO filters | JSON | Share study forced (365-369). |
| GET | `/dicom-web/instances`, `/dicom-web/studies/{study_uid}/instances`, `/dicom-web/studies/{study_uid}/series/{series_uid}/instances` | `qido_instances` (386) | QIDO filters | JSON | Share study forced (393-397). |
| GET | `/dicom-web/studies/{study_uid}/metadata` | `wado_study_metadata` (445) | none | JSON | `current_scope()` (414-423). Warm scheduled (451). |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/metadata` | `wado_series_metadata` (457) | none | JSON | `current_scope()`. |
| GET | `/dicom-web/studies/{study_uid}` | `wado_study` (477) | none | multipart/related, every instance | Whole study read into memory (477-490). |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}` | `wado_series` (494) | none | multipart/related | Same as above. |
| GET | `.../instances/{sop_uid}` | `wado_instance` (528) | `Accept` | DICOM or multipart | Via `_get_instance` (507-524). |
| GET | `.../instances/{sop_uid}/metadata` | `wado_instance_metadata` (540) | none | JSON | Via `_get_instance`. |
| GET | `.../instances/{sop_uid}/rendered` | `wado_rendered` (550) | `window`, `viewport`, `presentationUID` | PNG/JPEG, ETag, immutable | GSPS overlay lookup not scoped (585-589). Redis `paxs:rendered` (569, 624). Audit (630-632). |
| GET | `.../instances/{sop_uid}/thumbnail` | `wado_thumbnail` (637) | `window`, `viewport` | PNG/JPEG (pre-rendered 192px or rendered) | Redis `paxs:thumb` (687-690, 710). No audit. |
| GET | `.../instances/{sop_uid}/frames/{frame_list}` | `wado_frame` (718) | `frame_list` (comma list), `zorder=1`, `transcode=1`, `Accept` | raw / codestream / multipart | Accept normalization (806-812). Redis key mode-qualified (818). `zorder` uses `volume_bundle` unscoped (760). |
| GET | `.../instances/{sop_uid}/bulk/{tag_path}` | `wado_bulk_data` (869) | `tag_path` (keyword or hex) | octet-stream or text | Via `_get_instance`. No audit. |
| POST | `/dicom-web/studies`, `/dicom-web/studies/{study_uid}` | `stow_studies` (940-943) | `multipart/related` body | 200 / 409 / 507 (quota) / 413 / 400 | `require_dicomweb_write` (`deps.py:273`). C05 identifier check (997). Quota staging (1019). Commit then promote (1044-1045). |

Volume (`/api/v1/volume`). Auth for all routes: `require_series_read` (`deps.py:295`) plus `require_series_access` (`deps.py:412`). A share token for the owning study is accepted. Otherwise the caller gets 401 regardless of `DICOMWEB_PUBLIC`.

| Method | Full path | Handler (line) | Params | Response | Change |
|---|---|---|---|---|---|
| GET | `/api/v1/volume/{series_uid}/negotiate` | `volume_negotiate_endpoint` (460) | `webgl_score` (0-100), `rtt_ms`, `gpu_vram_mb`, `hardware_concurrency` | JSON: `configured_mode`, `effective_mode`, `endpoints`, `capabilities`, `role_override`, `hybrid_thresholds`, `hybrid_decision`. Headers `X-PAXS-Processing-Mode`, server-load | NEW. |
| GET | `/api/v1/volume/{series_uid}/info` | `volume_info_endpoint` (583) | none | JSON geometry (`frame_map`, `instance_order`, window, orientation) | Auth changed (was anonymous). |
| GET | `/api/v1/volume/{series_uid}/data` | `volume_data_endpoint` (609) | `slices=a-b` or `all`, `rescale=1`, `downsample` (1-8), `format=raw\|npy`, `force=1` | `application/octet-stream` or `application/x-npy`, `X-Volume-*` | Server-mode guard (630-632). `VOLUME_MAX_SLICES` 416 (651-654). Redis cache (737-789). |
| GET | `/api/v1/volume/{series_uid}/mip` | `volume_mip_endpoint` (799) | `axis=z\|x\|y`, `offset`, `thickness`, `mode=mip\|minip\|average\|vrt`, `window_center`, `window_width`, `format=png\|raw\|npy`, `force=1` | PNG/raw/npy, `X-MIP-*` | Client-mode guard (834-836). New float parsing (870-871). |
| GET | `/api/v1/volume/{series_uid}/tiles` | `volume_tiles_endpoint` (926) | `plane=axial\|sagittal\|coronal`, `range=a-b` or `N` (alias `start`), `window_center`, `window_width`, `size` (ignored), `inline=1` | Concatenated PNG with `X-PAXS-Tile-Offset`/`Length`, or `multipart/mixed` | NEW. No client-mode guard. Registers server session in server mode (1041-1043). |
| GET | `/api/v1/volume/{series_uid}/mpr/slice` | `volume_mpr_slice_endpoint` (1113) | `plane` + `index`, or oblique `origin=x,y,z&row=dx,dy,dz&col=dx,dy,dz&size=WxH` (max 4,000,000 px), `window_center`, `window_width`, `format=png\|raw\|npy` | PNG/raw/npy, `X-MPR-*` | Client-mode guard (1143-1145). Float window parse outside the try (1214-1215). |
| GET | `/api/v1/fusion/{study_uid}` | `fusion_manifest` (29-42) | none | JSON manifest | Auth changed to `get_current_user_any` (33). |
| GET | `/api/v1/gsps/{sop_uid}` | `get_gsps` (45-55) | none | JSON including `file_path` (41) | Auth. Tenant check only when `study_instance_uid` is non-null (53-54). |
| GET | `/api/v1/gsps/study/{study_uid}` | `list_gsps_for_study` (58-65) | none | JSON list | Auth and `require_study_access` (63). |
| GET | `/api/v1/gsps/instance/{sop_uid}/presentations` | `find_presentations_for_instance` (68-94) | `study_uid` (optional) | JSON list | Auth. Check skipped when the owning study is not found (92-93). |
| GET | `/api/v1/export/{study_uid}/anonymized` | `export_anonymized` (23-73) | none | `application/zip` | `require_study_access` (34). AuditLog `EXPORTED` (62-66). Synchronous anonymizer (51). Auth is bearer only. |
| POST | `/api/v1/export/dicomdir` | `export_dicomdir_package` (33-61) | body `{study_instance_uids[]}` | `application/zip` | `require_study_access` per UID (43-44). Auth is bearer only. |
| POST | `/api/v1/export/dicomdir/preview` | `preview_dicomdir_structure` (64-125) | body `{study_instance_uids[]}` | JSON, includes patient name (93-94) | `require_study_access` per UID (74-75). |

### 1.3 New or changed service functions (signatures)

`deps.py`:
- `get_share_scope(request: Request) -> str | None` (164). Reads `?share=`, `X-PAXS-Share-Token` or the `paxs_share` cookie, then validates through `sharing_service.get_share`.
- `share_cookie_ttl(token: str) -> int | None` (187).
- `require_dicomweb_read(request, study_uid=None, credentials=...) -> User | None` (226).
- `require_dicomweb_write(request, credentials=...) -> User | None` (273).
- `require_series_read(request, series_uid=None, credentials=...) -> User | None` (295).
- `require_share_or_user(request) -> None` (316).
- `require_dicomweb_access(request, study_uid=None, credentials=...) -> None` (329). This is the router-level guard.
- `tenant_scope(user) -> tuple[int|None, bool]` (353). `read_scope(user, *, share_scope=None)` (358).
- `current_scope() -> tuple[int|None, bool]` (ContextVar `_request_scope`, L378).
- `require_study_access(db, study_uid, user, *, share_scope=None) -> Study` (388). Raises 404 on cross-tenant.
- `require_series_access(db, series_uid, user, *, share_scope=None) -> Series` (412).
- `require_instance_access(db, study_uid, series_uid, sop_uid, user, *, share_scope=None) -> Instance` (455). Defined, and I found no caller among the V2a modules.

`volume_service.py`:
- `series_slices(db, series_uid, *, tenant_id=None, include_all=False) -> list[VolumeSlice]` (169).
- `volume_bundle(db, series_uid, *, tenant_id=None, include_all=False) -> tuple[dict, list[VolumeSlice]] | None` (424).
- `volume_info(db, series_uid, *, tenant_id=None, include_all=False) -> dict | None` (529).
- `iter_volume_bytes(slices, rescale=False, start=0, end=None, downsample=1) -> Iterator[bytes]` (559).
- `volume_byte_length(slices, dtype, rows, cols, samples, start=0, end=None, downsample=1) -> int` (632).
- `load_volume_array(slices, series_uid=None) -> np.ndarray` (1239). Shape `(nz, ny, nx)`, float32 HU.
- `reslice_plane(slices, info, plane, index=None, origin=None, row_vector=None, col_vector=None, size=None, max_pixels=4_000_000) -> ResliceResult` (1487).
- `window_for(info, data, window_center=None, window_width=None) -> tuple[float, float]` (1584).
- `render_plane_png(data, window_center, window_width) -> bytes` (1609).
- `render_plane_tile(slices, info, plane, index, window_center=None, window_width=None) -> RenderedPlane` (892).
- `render_tiles(series_uid, plane, start_idx, end_idx, window_center, window_width, size, slices, info) -> list[bytes]` (1081). `size` is not used in the body.
- `mip_project(slices, info, axis="z", mode="mip", offset=0, thickness=None, window_center=None, window_width=None) -> MipResult` (1691).
- `plane_dims`, `plane_spacing`, `plane_index_max` (1029, 1046, 1069). `strided_dim(n, step)` (109). `npy_header(dtype, shape)` (129). `nan_to_float32(data)` (1604). `tile_cache_key(...)` (871).
- `register_server_session(series_uid)` (1164). `release_server_session(series_uid)` (1171) has no callers. `active_server_session_count()` (1177). `estimated_vol_cache_mb()` (1193). `log_mode_decision(...)` (1134).

`volume.py` helpers: `_slice_range(raw, total)` (46), `parse_role_map()` (84), `resolve_role_override(user)` (127), `resolve_mode_detail(user) -> dict` (156), `resolve_hybrid_mode(webgl_score, rtt_ms, *, ...) -> str` (192), `_reject_raw_streaming` (300), `_reject_server_slicing` (384), `_mode_endpoints` (396), `_mode_capabilities` (419).

`wado_rs_bulk_service.py`: `frame_cache_budget_bytes()` (46), `frame_cache_stats()` (61), `_cache_get` (66), `_cache_put` (77), `clear_frame_cache()` (102), `extract_frame_bytes(ds, frame_indices)` (109), `decode_frames_to_raw(ds, frame_indices)` (191), `build_multipart_related_payload(frames, boundary)` (273), `extract_bulk_attribute(ds, tag_path)` (293).

`dicomweb.py`: `_anonymous_share_scope(request, user)` (237), `_reject_cross_tenant_ingest(db, study_uid, user)` (249), `_validate_ingest_uids(ds)` (269), `_stow_response(..., status_code=200)` (898), `_etag_for` (136), `_cached_response` (140).

### 1.4 Caches, Redis keys and TTLs

| Store | Key / scope | Bound | TTL | Location |
|---|---|---|---|---|
| `_FRAME_CACHE` (process) | `(sop_uid, frame_idx)` raw decoded pixels | 256 MiB default (`DICOM_FRAME_CACHE_MB`), byte-bounded, locked | none | `wado_rs_bulk_service.py:42-106` |
| `_VOL_CACHE` (process L1) | `array:{series}:{nslices}` | 1 GiB default (`VOLUME_CACHE_BUDGET_MB`), byte-bounded LRU, locked | none | `volume_service.py:697-747` |
| `_TILE_CACHE` (process) | `paxs:tile:...` key | 256 entries (count only, no byte cap) | none | `volume_service.py:783-784` |
| `_BUNDLE_CACHE` (process) | series UID → `(info, slices)` | 32 entries (FIFO), no lock, **never invalidated** | none | `volume_service.py:420-525` |
| `_TILE_INFLIGHT` | tile key | 64 entries; waiters wait 20 s | none | `volume_service.py:808-810`, `:942` |
| `_IN_FLIGHT_VOLUMES` | series `cache_key` | waiters wait 180 s, max 3 waits | none | `volume_service.py:762-773`, `:1312` |
| `_ACTIVE_SERVER_SESSIONS` | series UID → monotonic time | 300 s idle timeout | none | `volume_service.py:799-800`, `:1184` |
| Redis `paxs:rendered:{sop}:{window\|auto}:{max\|full}:{presentationUID\|none}:{fmt}` | rendered PNG/JPEG | unbounded key cardinality | 600 s | `dicomweb.py:569`, `:624` |
| Redis `paxs:thumb:{sop}:{window\|auto}:{max\|full}:{fmt}` | rendered thumbnail | unbounded key cardinality | 600 s | `dicomweb.py:687-690`, `:710` |
| Redis `paxs:thumb:{sop}:auto:256:png` | warm thumbnail | | 3600 s | `warm_service.py:142`, `:155` |
| Redis `paxs:frame:{sop}:{resolved}:{raw\|native\|def}:{mp\|sp}` | formatted frame body (≤16 MiB) plus `\|\|PAXS_MT\|\|` separator | | 1800 s | `dicomweb.py:818`, `:862-863` |
| Redis `paxs:frame:{sop}:{idx}` | raw decoded pixels | | 3600 s | `wado_rs_bulk_service.py:227`, `:265`; `warm_service.py:172` |
| Redis `paxs:vol:stream:{series}:{start}_{end}_{ds}_{rescale}_{fmt}` and canonical `paxs:vol:stream:{series}:{nslices}` | `/data` body (≤256 MiB) | | 3600 s | `volume.py:737-742`, `:775-779` |
| Redis `paxs:vol:array:{series}:{n}` (and alt key by first SOP) | full float32 volume, no size cap at write | | 3600 s | `volume_service.py:1279-1346`, `:790` |
| Redis `paxs:tile:{series}:{plane}:{idx}:{window}` | PNG (≤4 MiB) | | 600 s | `volume_service.py:785-786`, `:889`, `:965-966` |
| Redis `paxs:warm:lock:{study}`, `paxs:warm:{study}` | lock, warm record | | lock 120 s, record 86400 s | `warm_service.py` (`_WARM_LOCK_TTL`, `ex=86400`) |

### 1.5 Config settings read by V2a code

| Setting | HEAD default | Read at | Old (e90d32c) |
|---|---|---|---|
| `DICOMWEB_PUBLIC` | `False` (`config.py:427`) | `deps.py:251, 264, 282, 368`; `dicomweb.py:201, 206, 244` | Existed, default `True` (old `config.py:253`). **Default changed.** |
| `DICOM_FRAME_CACHE_MB` | 256 (`config.py:346`) | `wado_rs_bulk_service.py:53` | New (old: 0 matches). |
| `VOLUME_CACHE_BUDGET_MB` | 1024 (`config.py:338`) | `volume_service.py:708` | New. |
| `VOLUME_MAX_SLICES` | 4096 (`config.py:295`) | `volume.py:651`, `volume_service.py:1121` (tiles only) | Existed. Not applied to `/mip` or `/mpr/slice`. |
| `VOLUME_MPR_MAX_PIXELS` | 4,000,000 (`config.py:299`) | `volume.py:1203` (oblique only) | Existed. |
| `VOLUME_STREAMING_ENABLED` | `True` | every volume route | Existed. |
| `VIEWER_PROCESSING_MODE` | `"hybrid"` | `volume.py:80` | New. |
| `VIEWER_PROCESSING_ROLE_MAP` | `""` (`config.py:374`) | `volume.py:97` | New. |
| `HYBRID_CLIENT_SCORE_THRESHOLD` | 40 (`config.py:316`) | `volume.py:244` | New. |
| `HYBRID_RTT_THRESHOLD_MS` | 150 (`config.py:366`) | `volume.py:248`, `:533` | New. |
| `HYBRID_MAX_SERVER_MPR_SESSIONS` / `HYBRID_SERVER_VOL_BUDGET_MB` | 20 / 4096 (new block) | `volume.py:237-238`, `:350-351` | New. |
| `REDIS_CACHE_ENABLED` | `True` (`config.py:238`) | `volume.py:746`; `volume_service.py:931, 965, 1278, 1343` | Existed. Enabled by default, so every `/data` and `/mpr` request does sync Redis work on the loop (see N11). |
| `STOW_MAX_PART_BYTES` / `STOW_MAX_TOTAL_BYTES` | 2 GiB / `0` = unlimited (`config.py:443-444`) | `dicomweb.py:982-983` | Existed. |
| `WARM_ON_OPEN_ENABLED` / `WARM_MAX_INSTANCES` / `WARM_MAX_FRAMES` | `True` / 16 / 4 (`config.py:489-491`) | `warm_service.py` | Existed. |
| `THUMBNAIL_ENABLED` | `True` (`config.py:153`) | `warm_service.py:130` only (the thumbnail route does not read it) | Existed. |

## 2. Prior findings: status at HEAD

| ID | Status | Evidence (HEAD) | Notes |
|---|---|---|---|
| S-04 | **FIXED** (default config). PARTIAL in public mode. | Router guard: `dicomweb.py:56-65` and `deps.py:251` (401 for anonymous when not public). `deps.py:295-313`: `require_series_read` returns 401 for anonymous without a share, regardless of the flag. `volume.py:465, 587, 613, 803, 1117` use it. `gsps.py:46-47, 59-60, 69-70` and `fusion.py:33` use `get_current_user_any`. `config.py:427`: `DICOMWEB_PUBLIC: bool = False`. | In public mode, QIDO still shows legacy NULL-tenant studies (`dicomweb.py:309-310` with `tenant_service.py:91-92`). `DICOMWEB_PUBLIC` default changed from `True` to `False`, which is breaking for LAN deployments. See N32. |
| S-03 (STOW) | **PARTIAL** | Anonymous: `deps.py:273-289` returns 401 unless `DICOMWEB_PUBLIC`. At `deps.py:282-283` the public branch returns an anonymous user. UID reuse: `dicomweb.py:249-266` checks the Study UID only. `indexing_service.py:370-373` finds the Series by UID alone. `indexing_service.py:398-404` finds the Instance by SOP UID alone. `indexing_service.py:424` sets `instance.file_path = file_path`. | Anonymous writes are now blocked by default. Cross-tenant repoint through Series or SOP UID reuse is **still open** (N1, 🔴). |
| S-12 | **PARTIAL** | By-UID lookups are scoped: `_get_instance` (`dicomweb.py:507-524`), `_instances_of` (`:414-423`), WADO-URI owning-study check (`:192-207`), volume `require_series_access` (`deps.py:412-438`), export/DICOMDIR (`export.py:34`, `dicomdir.py:43-44`, `:74-75`), GSPS (`gsps.py:53-54`, `:63`, `:78`, `:92-93`). | Residuals: GSPS fail-open when there is no owning study (`gsps.py:53`, `:92`, N13). Rendered GSPS overlay lookup is unscoped (`dicomweb.py:585-589`, N14). `zorder` frames use an unscoped bundle (`dicomweb.py:760`, N17). `schedule_warm` runs before the tenant query (`dicomweb.py:307-308`, N16). |
| D-06 | **PARTIAL** | Dicomweb no longer writes `paxs:frame:{sop}:{n}` (`dicomweb.py:857-861` comment; the old `cache_set(f"paxs:frame:{sop_uid}:{resolved}", ...)` is gone). The new key is mode-qualified (`dicomweb.py:818`). | `warm_service.py:172` still writes `decoded[0][0]` into the raw namespace (`cache_set(f"paxs:frame:{inst.sop_instance_uid}:1", decoded[0][0], ...)`). `wado_rs_bulk_service.py:251` returns a codestream fallback, so the warm pass can cache compressed bytes as raw pixels. Reader at `wado_rs_bulk_service.py:227`. See N5. |
| F-06 | **OPEN** | `warm_service.py:211` `vol_ok = asyncio.run(_warm_vol_series())`. No `import asyncio` anywhere in the file (grep finds only line 211). The NameError is caught by `warm_service.py:212-213` `log.debug("Volume pre-warm pass failed: %s", exc)`. | Volume pre-warm never runs, and nothing is logged above DEBUG. |
| 03-F4 | **OPEN** | Preamble loop `dicomweb_service.py:179-182` (`while buf.find(marker) == -1`) and header loop `:196-201` (`while True: idx = buf.find(b"\r\n\r\n")`) have no cap. `STOW_MAX_TOTAL_BYTES=0` (`config.py:444`). | Quadratic scan. See N20. |
| 03-F6 | **PARTIAL** | Improved: L1 volume cache is byte-budgeted (`volume_service.py:697-747`). Frame cache is byte-budgeted (`wado_rs_bulk_service.py:46-100`). Still open: `/data` materializes up to `VOLUME_MAX_SLICES` with no byte cap (`volume.py:651`, `:760-784`). `_TILE_CACHE` is count-only (`volume_service.py:783-784`). | N2, N3. |
| 03-F10 | **OPEN** | Sync Redis on the loop: `dicomweb.py:571, 624, 691, 710, 820, 863`, `volume.py:749`. Sync disk or parse on the loop: `volume_service.py:191` (`storage_service.exists` for every instance), `:439-441` (`read_dataset`). MD5 on the loop: `dicomweb.py:137` (`_etag_for`). Multipart copy on the loop: `dicomweb.py:490`. | Improved: reslice, MIP and tiles run via `to_thread` (`volume.py:877, 1025, 1172-1175, 1196`), and decode paths use `to_thread` (`dicomweb.py:709, 851`). See N11. |
| 03-F11 | **PARTIAL** | `add_audit` in WADO-URI (`dicomweb.py:226-228`) and rendered (`:630-632`). DICOMDIR export audit (`dicomdir_service.py:183`). Anonymized export `AuditLog` (`export.py:62-66`). | No audit for volume reads (`volume.py` has none), QIDO, metadata, study/series/instance, thumbnail, frames, bulk, GSPS, or DICOMDIR preview (`dicomdir.py:93-94`). |
| 03-F12 | **FIXED** | `strided_dim` (`volume_service.py:109-126`) is used for the header (`volume.py:671-672`), payload length (`volume_service.py:645-647`), and the slice step (`:579`, `:612`). | Consistent with H13. |
| 03-F20 | **OPEN** | No invalidation path exists. `_BUNDLE_CACHE` (`volume_service.py:420-525`), `_VOL_CACHE` (`:697-747`), `_TILE_CACHE`, and `_FRAME_CACHE` are not cleared on STOW or delete. Redis TTLs at `volume.py:737-742`, `volume_service.py:785-790`, `dicomweb.py:569, 687-690, 818`. PHI responses use `IMMUTABLE_CACHE_CONTROL` (`dicomweb.py:133`, `:144`). | Same-SOP overwrite path exists (N10). |
| 03-F21 | **FIXED** | `_FRAME_CACHE_LOCK` (`wado_rs_bulk_service.py:43`) guards get, put and clear (`:66-75`, `:77-100`, `:102-106`). | `_BUNDLE_CACHE` has no lock, but it is only touched on the event loop. |
| 03-F22 | **PARTIAL** | Per-part commit (`dicomweb.py:1044-1045`) and rollback on failure (`:1072-1075`). | No SAVEPOINT. Commit-before-promote can leave dangling rows (N19). |
| 03-F23 | **OPEN** | Frames read the full dataset before the cache check: `dicomweb.py:736` (`read_dataset` without `stop_before_pixels`) precedes `cache_get` at `:820`. | Also the `zorder` branch (`:760`). |
| 03-F24 | **OPEN** | `wado_study` builds a list of every instance's bytes (`dicomweb.py:482-487`), then `_multipart_response` copies it on the loop (`:490`, `:467-473`, `dicomweb_service.py:249-257`). | Peak memory is about 2x the study. |
| 03-F7 | **PARTIAL** | Tenant check FIXED: `export.py:34`, `dicomdir.py:43-44`, `:74-75`. Role gate: none (`export.py:31`, `dicomdir.py:37`, `:68` use plain `get_current_user`). Blocking: still open, `export.py:51` (sync `anonymize_files_to_zip`), `dicomdir_service.py:158`, `:178` (sync `storage_service`). | See N7/N11. |

## 3. New findings

### 🔴 N1: STOW can overwrite another tenant's Instance by reusing Series or SOP UIDs

- Location: `dicomweb.py:249-266`, `dicomweb.py:997-1040`; `indexing_service.py:370-377`, `:398-424`; `storage_service.py` `stage_dataset_with_quota` (`:382-435`); `deps.py:282-283`.
- Quote (`dicomweb.py:256-257`): `if user is None or user.role == "ADMIN" or user.tenant_id is None:` / `return`.
- Quote (`indexing_service.py:370-373`): `series = (await session.execute(select(Series).where(Series.series_instance_uid == series_uid))).scalar_one_or_none()`.
- Quote (`indexing_service.py:424`): `instance.file_path = file_path`.
- Problem: `_reject_cross_tenant_ingest` only compares the **Study** UID. `get_or_create_series` finds the existing Series by UID alone. `upsert_instance` finds the existing Instance by SOP UID alone and repoints its `file_path` and `storage_backend` (`:424-425`). The Series' `study_id` is never checked against the incoming study. The staged object lands under the attacker's study path, so the victim's row now points at attacker-supplied bytes.
- Failure scenario: a tenant-B user POSTs a STOW part with a new StudyInstanceUID `X`, the SeriesInstanceUID `S_A` of tenant A's series, and the SOPInstanceUID `I_A1` of an existing tenant-A Instance. The C04 study check passes because `X` is new. `get_or_create_series` returns tenant A's series. `upsert_instance` repoints `I_A1` to tenant B's bytes, and the audit row says `STORED` by tenant B. Tenant A's viewer, WADO-RS, thumbnails and volume now show tenant B's pixels under tenant A's identifiers. In public mode (`DICOMWEB_PUBLIC=true`) the same happens to an anonymous caller, because `user is None` skips the check entirely, and `deps.py:282-283` allows anonymous writes. Users with `tenant_id=None` also bypass the check (`dicomweb.py:256-257`).
- Fix: before writing, require that any existing Series belongs to the target study (`series.study_id == study.id`), that any existing Instance belongs to that Series (`instance.series_id == series.id`), and that the tenant matches. On mismatch return 409 and write nothing. Never change `file_path` or `storage_backend` on an existing row owned by a different study. Apply the same rule to the C-STORE receiver (`app/dicom/receiver.py`, not reviewed here). Drop the `tenant_id is None` bypass for non-admin users.

### 🔴 N2: `/volume/{uid}/data` can allocate several GiB per request, with no byte cap and no concurrency gate

- Location: `volume.py:651-654` (only a slice-count cap), `volume.py:760-784`, `volume.py:784`; `volume_service.py:587`, `:605`, `:620`.
- Quote (`volume.py:651`): `if count > settings.VOLUME_MAX_SLICES:`.
- Quote (`volume.py:774`): `data = b"".join(chunks)`.
- Quote (`volume.py:784`): `data = await asyncio.to_thread(build_and_cache)`.
- Quote (`volume_service.py:587`): `cache: dict = {}` (in `iter_volume_bytes`; every Dataset read is kept for the whole loop, see `:546-548`).
- Problem: the cap is on slice count, not bytes. The handler builds `chunks` (a full copy of the payload), then `b"".join` (a second full copy). It also keeps every opened Dataset (raw pixel bytes) alive. Nothing caps concurrent calls, and no `Semaphore` exists anywhere in these modules.
- Failure scenario: a tenant user requests `/api/v1/volume/{S}/data?format=npy&rescale=1` for a 4096-slice 512x512 series. The payload is 4 GiB. Peak memory is about 10 GiB (chunks, join and retained raw data). Two concurrent requests take the node down.
- Fix: stream the response with a generator (`StreamingResponse`) and remove the join. Enforce a byte budget (`VOLUME_MAX_BYTES`) in addition to the slice count. Don't retain Datasets across slices, because `pixel_array` can be re-decoded. Add a process-wide `asyncio.Semaphore` or thread semaphore for materialization and for `/data`.

### 🔴 N3: `/mip` and `/mpr/slice` materialize the entire stack with no slice cap and no concurrency gate

- Location: `volume.py:876-881`, `volume.py:1172-1175`, `volume.py:1196-1204`; `volume_service.py:1716` (`mip_project` calls `load_volume_array`), `:1322-1338`, `:1338` (`vol = np.stack(arrays, axis=0)`), `:1729` (`slab = np.take(...)`), `:1662-1669` (VRT loop).
- Quote (`volume_service.py:1336`): `arrays.append(arr.astype(np.float32) * np.float32(slope) + np.float32(intercept))`.
- Quote (`volume_service.py:1338`): `vol = np.stack(arrays, axis=0)`.
- Problem: `VOLUME_MAX_SLICES` is enforced only in `/data` (`volume.py:651`) and in `render_tiles` (`volume_service.py:1121`). Every reslice, MIP and tiles request goes through `load_volume_array`, which materializes the whole stack. Peak is about 2x the float32 volume (list plus `np.stack`), plus the retained raw Datasets (`:1323`). `_vrt_raycast` adds several more full-size temporaries (`:1654-1657`), and `mip_project` takes a full slab copy (`:1729`). `VOLUME_CACHE_BUDGET_MB=1024` (`config.py:338`) is smaller than a 4096-slice float32 volume (about 4 GiB), so the cache cannot help.
- Failure scenario: a cold `/mpr/slice?plane=axial&index=0` on a 4096-slice 512x512 series peaks at about 10 GiB. A `/mip?mode=vrt` with no `thickness` peaks higher. Requests for two different large series thrash the 1 GiB L1 (the cache keeps only the newest entry when it is over budget), so every tile request re-materializes.
- Fix: apply `VOLUME_MAX_SLICES` (and a byte budget) to `load_volume_array` itself, not only to `/data`. Replace the list-then-stack with a preallocated `np.empty` and an in-place fill. Put a semaphore around materialization. Make the VRT path operate per slab or in chunks. Raise `VOLUME_CACHE_BUDGET_MB` above one maximum-size volume, or fail fast when a volume exceeds it.

### 🔴 N4: Frame requests can amplify memory through repeated indices

- Location: `dicomweb.py:739` (`indices = [int(x.strip()) for x in frame_list.split(",") if x.strip().isdigit()]`), `dicomweb.py:779-781` (per-index bounds check only); `wado_rs_bulk_service.py:146-149` (compressed: appends one entry per requested index), `:170-174` and `:179-183` (uncompressed: appends one entry per requested index); `dicomweb.py:844` (`build_multipart_related_payload` builds the whole body in memory).
- Quote (`wado_rs_bulk_service.py:179-183`): `for frame_idx in frame_indices:` / `idx = frame_idx - 1` / `if 0 <= idx < arr.shape[0]:` / `frame_bytes = arr[idx].tobytes()` / `frames.append((frame_bytes, ...))`.
- Problem: nothing deduplicates or caps the number of indices. The bounds check looks at each index separately, so `frames/1,1,1,...` yields one copy per repetition. The body is then assembled in a `BytesIO` and copied again by `getvalue()`, so the peak is about 3x the output.
- Failure scenario: on a multi-frame uncompressed 512x512 uint16 instance (512 KiB per frame), a request for `frames/1,1,...` with about 8,000 repetitions (roughly the request-line limit in uvicorn/h11 defaults) produces about 4 GB of output and roughly 12 GB of transient memory. An anonymous caller in public mode, or any tenant user, can do this.
- Fix: deduplicate, then cap the number of indices (for example 64) and the total output bytes (for example 256 MiB), returning 413 or 400 above the limit. Stream the multipart body rather than building it.

### 🟡 N5: D-06 is still reachable through `warm_service.py` (raw-pixel cache poisoned with codestream bytes)

- Location: `warm_service.py:169-172`; `wado_rs_bulk_service.py:251`; reader `wado_rs_bulk_service.py:227`.
- Quote (`warm_service.py:172`): `cache_set(f"paxs:frame:{inst.sop_instance_uid}:1", decoded[0][0], expire_seconds=3600)`.
- Quote (`wado_rs_bulk_service.py:251`): `return extract_frame_bytes(ds, frame_indices)`.
- Problem: `decode_frames_to_raw` falls back to the encapsulated codestream when `pixel_array` fails (`:241-251`). The fallback bytes carry a JPEG 2000 or JPEG mime type. The warm pass caches `decoded[0][0]` without checking the mime type, under the raw-pixel key that `decode_frames_to_raw` reads back as raw pixels (`:227`).
- Failure scenario: the first warm pass hits a JPEG 2000 instance whose pixel decode fails (a missing plugin, for instance). For one hour, a `?transcode=1` request for that frame returns codestream bytes labelled as raw little-endian pixels, so the viewer renders garbage.
- Fix: only cache when the returned mime is `RAW_FRAME_CONTENT_TYPE`, or write to a mode-qualified key as the dicomweb path now does.

### 🟡 N6: Non-numeric query values cause HTTP 500 (unhandled parse)

- Location: `volume.py:504-505`, `volume.py:657`, `volume.py:870-871`, `volume.py:1214-1215`.
- Quote (`volume.py:504`): `webgl_score = max(0, min(100, int(q.get("webgl_score", 0) or 0)))`.
- Quote (`volume.py:657`): `downsample = max(1, min(8, int(params.get("downsample", 1) or 1)))`.
- Quote (`volume.py:870`): `wc_f = float(wc) if wc is not None else None`.
- Quote (`volume.py:1214`): `wc = q.get("window_center")` / `wc_f = float(wc) if wc is not None else None` (outside the `try` at `:1216`).
- Problem: `ValueError` escapes. `/mip` and `/mpr/slice` parse `window_center` and `window_width` outside any try. `/negotiate` and `/data` parse the same way. `/tiles` (`_float_param`, `:1003-1010`) does it correctly.
- Failure scenario: `GET /api/v1/volume/{S}/mip?window_center=abc` returns 500 and logs a stack trace.
- Fix: parse through a helper that raises 400, the way `_int_param` (`:857-864`) and `_float_param` (`:1003-1010`) already do.

### 🟡 N7: NaN and infinite window values and origins are accepted and produce silent blank output

- Location: `volume_service.py:1589-1590`; `volume_service.py:1609-1616`; `volume.py:870-871`, `volume.py:1199-1201`.
- Quote (`volume_service.py:1589-1590`): `if window_center is not None and window_width is not None:` / `return float(window_center), float(window_width)`.
- Quote (`volume_service.py:1615-1616`): `t = (data - (float(window_center) - half)) / max(float(window_width), 1e-6)` / `t = np.nan_to_num(t, nan=0.0, posinf=1.0, neginf=0.0)`.
- Problem: `float("nan")` and `float("inf")` pass through. The PNG renders black, and the headers report `nan,nan`. For oblique planes, a NaN origin yields a fully NaN array, and `window_for` then falls back to `(0.0, 1.0)`. Nothing returns 400.
- Failure scenario: a client sends `window_width=nan` and gets a 200 response with an all-black image and `X-MPR-Window: nan,nan`. That can look like a data fault to a clinician.
- Fix: require `math.isfinite` for window values and origin/row/col entries, and return 400 otherwise.

### 🟡 N8: `/tiles` skips the client-mode guard, ignores `size`, and returns up to 4096 PNGs per request

- Location: `volume.py:925-1109` (no `_reject_server_slicing`; calls are only at `volume.py:836` and `:1145`); `volume_service.py:1081-1131` (`size` is in the signature at `:1088` and mentioned only in the docstring at `:1101-1102`); `volume.py:1016-1020`; `volume_service.py:1119-1131`; `volume.py:1087`.
- Quote (`volume_service.py:1119-1123`): `limit = max(1, int(getattr(settings, "VOLUME_MAX_SLICES", 512)))` / `if end - start + 1 > limit:` / `end = start + limit - 1`.
- Problem: (a) the documented `size=N` ("requested edge length in px", `volume.py:942`) is never used, so clients get native resolution whatever they ask for. (b) In `client` mode, `/tiles` renders server-side while `/mip` and `/mpr/slice` refuse, so the thin-server contract is broken. (c) One request can rasterize up to 4096 PNGs (native resolution), held in a list and joined again.
- Failure scenario: a client in explicit `client` mode uses `/tiles` for the whole series and gets server rasterization it did not ask for. A `range=0-4095` request on a 512x512 series produces several hundred MB of PNG and about 2x that in transient memory.
- Fix: either implement `size` (downsample or cap the output) or remove it from the contract. Apply the client-mode guard. Cap total bytes per response and stream the frames.

### 🟡 N9: Single-flight waiters hold worker threads for up to 180 s

- Location: `volume_service.py:768` (`_IN_FLIGHT_WAIT_SECONDS = 180.0`), `volume_service.py:1312` (`event.wait(timeout=_IN_FLIGHT_WAIT_SECONDS)`), `volume_service.py:787` and `:942` (20 s for tiles); the callers run via `asyncio.to_thread` (`volume.py:784`, `:877`, `:1025`, `:1172-1175`, `:1196`).
- Problem: every waiter blocks a thread from the event loop's default executor (`min(32, cpu+4)` workers) for up to 180 s. A few dozen concurrent requests for one cold series can saturate the executor. Frame decodes, thumbnails and warm passes queue behind them.
- Failure scenario: 32 concurrent `/mpr/slice` calls for one cold series. Thirty-one wait on the owner's `Event`. The owner takes 60 s on slow storage. Frame and thumbnail requests from other users stall for that minute.
- Fix: keep waiters on asyncio (an `asyncio.Event` or `Future` keyed by series) so they do not hold threads. Lower the wait and fall back quickly.

### 🟡 N10: Volume caches go stale when an instance is overwritten, and the overwrite is served with immutable caching

- Location: `volume_service.py:420-525` (`_BUNDLE_CACHE` never invalidated); `volume.py:737-742` and `:749-750` (Redis canonical key validated only by length); `volume_service.py:1279-1281` (`paxs:vol:array:{series}:{n}`); `storage_service.py:359-364` (`promote` replaces the canonical object); `dicomweb.py:133` and `:144` (`IMMUTABLE_CACHE_CONTROL` on PHI).
- Quote (`volume.py:749`): `cached = r_client.get(r_key) or (r_client.get(canonical_key) if canonical_key else None)`; the length check is at `:750`: `if cached is not None and len(cached) == payload_len:`.
- Problem: the same SOP or series can be re-sent with new pixels (N1 or same-tenant re-STOW). The new object replaces the old at the same key, but the bundle, L1 array, Redis array and Redis stream payloads still hold the old voxels for up to an hour. Length checks do not detect a content change. Rendered and frame responses are sent with `max-age=31536000, immutable`, so browsers keep the old bytes too.
- Failure scenario: a radiologist's corrected CT is re-sent with the same UIDs. `/mpr/slice` and `/data` keep returning the uncorrected voxels for up to an hour (and, in the L1 cache, until eviction). Browsers keep the old frames for a year.
- Fix: add a content fingerprint (for example a hash of the sha256 list per series) to every process and Redis key. Invalidate the bundle and L1 entries on STOW, delete and re-import. Make PHI image responses revalidate (`no-cache` plus ETag) instead of `immutable`.

### 🟡 N11: Event-loop blocking in the volume and DICOMweb paths

- Location: sync Redis: `dicomweb.py:571` (`cache_get`), `:624` (`cache_set`), `:691`, `:710`, `:820`, `:863`; `volume.py:749` (`r_client.get`, up to 256 MiB, on the loop); `redis_service.py:1026` and `:1078` are plain `def`. Sync disk and parse on the loop: `volume_service.py:191` (`storage_service.exists` per instance inside `series_slices`), `volume_service.py:439-441` (`read_dataset` in `volume_bundle`). MD5 on the loop: `dicomweb.py:137` (`hashlib.md5(data)` over every response body, including whole-file WADO-URI objects at `:232`). Multipart copy on the loop: `dicomweb.py:490` (`_multipart_response`, `build_multipart` at `dicomweb_service.py:249-257`). Export: `export.py:51` (synchronous `anonymize_files_to_zip` inside an async handler), `dicomdir_service.py:158` and `:178` (sync `storage_service` inside `async def export_dicomdir_package`).
- Problem: each of these blocks the event loop for every connected client. `REDIS_CACHE_ENABLED` is `True` by default (`config.py:238`), so the sync Redis calls run on every request.
- Failure scenario: a 256 MiB Redis `GET` takes several hundred milliseconds on the loop. An anonymized export of a 2 GB study blocks all other requests for the whole zip build.
- Fix: wrap the Redis calls, the MD5, the multipart build, the disk `exists`/`read_dataset` calls and the export zip build in `asyncio.to_thread`, or move them to an async Redis client.

### 🟡 N12: Patient share tokens are accepted in the URL query string

- Location: `deps.py:148` (`SHARE_TOKEN_QUERY_PARAM = "share"`), `deps.py:174` (`request.query_params.get(SHARE_TOKEN_QUERY_PARAM)`).
- Problem: a share token grants read access to a whole study's PHI. Query-string tokens are written to access logs, proxy logs, browser history, and `Referer` headers sent to third-party resources on the page.
- Failure scenario: a share link `…/viewer?share=<token>` is opened in a browser that loads a third-party script. The script receives the `Referer`, which contains the token, and can read the study through the DICOMweb endpoints until expiry.
- Fix: accept the token only in the header or the cookie. Strip it from the URL on the first viewer response (redirect and set the cookie). Redact the parameter in access logs. Document the change.

### 🟡 N13: GSPS tenant checks fail open, and the response leaks server file paths

- Location: `gsps.py:41` (`"file_path": ps.file_path`), `gsps.py:53-54` (`if ps.study_instance_uid:`), `gsps.py:92-93` (`if owning:`), `gsps_service.py:263-275` (`find_for_instance` filters only on the optional study UID).
- Quote (`gsps.py:92-93`): `if owning:` / `await require_study_access(db, owning, _user)`.
- Problem: when the presentation state has no study UID, or when the referenced instance is not in the archive (no owning study), the tenant check is skipped. The lookup then returns presentation states from any tenant. `file_path` is an internal storage path, and it is returned to any authenticated caller.
- Failure scenario: tenant-B's GSPS references SOP `X`, and `X` has not been received yet. A tenant-A user calls `/api/v1/gsps/instance/X/presentations` and sees tenant-B's labels and internal `file_path`.
- Fix: fail closed. When no owning study is found, return an empty list (or 404). Drop `file_path` from the response. Scope `find_for_instance` by tenant.

### 🟡 N14: The rendered-image path applies another tenant's GSPS overlay

- Location: `dicomweb.py:578-600`; quote `dicomweb.py:585-589`: `ps = await db.execute(sa_select(PresentationState).where(PresentationState.sop_instance_uid == presentation_uid))`.
- Problem: the `presentationUID` query parameter is resolved without any tenant or study check, and the overlay (`apply_gsps_to_image`, `:612`) is burned into the caller's PNG. The GSPS may belong to another tenant.
- Failure scenario: a tenant-A user renders their own study with `presentationUID=<tenant-B GSPS UID>`. They receive tenant B's annotations and window settings on the image.
- Fix: require that the presentation state's study equals the image's study, and that it is visible to the caller's scope.

### 🟡 N15: Unbounded Redis cache-key cardinality from `window`, `viewport`, and `presentationUID`

- Location: `dicomweb.py:554-556`, `:569` (rendered key includes raw `window` and `presentationUID`); `dicomweb.py:650-652`, `:687-690` (thumbnail key includes raw `window`); `dicomweb.py:559-563` and `:655-658` (`viewport` parsed without bounds: `max(w, h) or None`).
- Problem: the key includes arbitrary client strings, so each distinct value creates a new 10-minute Redis entry holding a full PNG, and each one forces a fresh decode and render. There is no validation or bounding.
- Failure scenario: an attacker varies `window` across thousands of values per second. Redis fills with PNGs for 600 s, evicting the volume and tile caches other users rely on.
- Fix: normalize and validate `window` (numeric, bounded), `viewport` (clamped), and `presentationUID` (must match a known GSPS). Use a fixed-size key or a digest.

### 🟡 N16: `schedule_warm` runs before any tenant check and spawns an unbounded number of threads

- Location: `dicomweb.py:307-308` (`if study_uid_param := params.get("StudyInstanceUID"): warm_service.schedule_warm(study_uid_param)`), which runs before `query_service.study_query(...)` at `:310`; `warm_service.py:50-69` (one thread per distinct study UID, deduplicated only per UID, no global cap).
- Problem: a caller can trigger warm work (thumbnail generation writes to storage, frame decodes, Redis writes, and a `paxs:warm:{uid}` record) for any study UID, including studies they cannot see. Each distinct UID gets its own daemon thread.
- Failure scenario: a tenant-A user issues QIDO `?StudyInstanceUID=` for thousands of random UIDs. That spawns thousands of threads, each opening a DB session and decoding instances.
- Fix: move `schedule_warm` after the tenant check, and gate it through a bounded executor or semaphore.

### 🟡 N17: WADO-URI ignores bearer tokens, and `zorder` frames bypass tenant scope

- Location: `dicomweb.py:200` (`_wado_user = await get_optional_user(request, None)`), `dicomweb.py:760` (`bundle = await volume_service.volume_bundle(db, series_uid)`).
- Problem (a): passing `None` for credentials means the bearer branch in `get_optional_user` (`deps.py:90-100`) is skipped, so bearer-only API clients are treated as anonymous. Their owning-study check then runs with `tenant_scope(None)` = `(None, False)`, so every non-legacy study returns 404, including for ADMIN. Problem (b): `volume_bundle` is called with the default scope (`tenant_id=None, include_all=False`), so `frames/{n}?zorder=1` returns 404 for tenant-owned series. Whether it works depends on whether a previous request warmed `_BUNDLE_CACHE` with a broader scope.
- Failure scenario (a): an ADMIN uses a bearer token with WADO-URI and gets 404 for every tenant study. Failure scenario (b): the same `zorder` request succeeds or fails depending on cache state.
- Fix: pass `credentials` through (or use the router-resolved user from `current_scope`). Call `volume_bundle` with the caller's scope.

### 🟡 N18: Share-scoped QIDO returns nothing for tenant-owned studies; behaviour is inconsistent with WADO

- Location: `dicomweb.py:302-310` (share study forced into `StudyInstanceUID`, then `scope_from_user(None)`); `tenant_service.py:83-95`; `query_service.py:146-159` (`tenant_id=None` becomes `Study.tenant_id IS NULL`); versus `deps.py:358-368` (`read_scope` gives archive-wide scope to the same share caller).
- Problem: a share-scoped anonymous caller is treated as legacy-unscoped in QIDO, so a tenant-owned study is invisible to its own share viewer, while WADO returns it. In public mode, anonymous QIDO also returns only legacy studies, while anonymous WADO is archive-wide.
- Failure scenario: a patient share link for a study owned by a tenant opens the viewer, the study list is empty, and no error is shown.
- Fix: use `read_scope(user, share_scope=share_scope)` in the three QIDO handlers, so QIDO and WADO agree. Decide explicitly whether public mode should be archive-wide everywhere.

### 🟡 N19: STOW commits partial batches before an error and can leave dangling rows

- Location: `dicomweb.py:1044-1045` (commit, then promote), `dicomweb.py:1061-1067` (400 raised after earlier parts are already committed), `storage_service.py:359-364` (`promote`).
- Quote (`dicomweb.py:1061-1064`): `except storage_service.InvalidStorageKey as exc:` / `# C05: a malformed identifier is a bad request, never a partial failure — refuse the whole STOW rather than store anything.` / `raise HTTPException(400, ...)`.
- Problem: the C05 comment says the whole STOW is refused, but parts committed and promoted before the bad part stay stored. The same is true for `CrossTenantIngestError` (`:1065-1067`). Separately, the row is committed before `staged.promote()` (`:1044-1045`). If `promote` fails, the instance row exists but no file does, and the generic handler (`:1068-1075`) discards the staged file.
- Failure scenario: a 100-part upload where part 40 has a bad UID returns 400, yet 39 instances are archived. A promote failure (full disk, backend error) leaves an Instance row whose `file_path` is missing.
- Fix: validate all parts before storing any, or report partial success in the body (not as 400). Promote before commit, or compensate by deleting the row on promote failure.

### 🟡 N20: STOW parser buffers the preamble and headers without a cap, and copies each part several times

- Location: `dicomweb_service.py:179-182`, `:196-201`, `:246` (`yield (ctype, bytes(payload))`); `storage_service.py:404-405` (`ds.save_as(buf)` then `buf.getvalue()`); `config.py:443-444`.
- Problem: the preamble loop and the header loop grow `buf` without limit, rescanning it each time (quadratic). `STOW_MAX_TOTAL_BYTES` is `0` (unlimited). A 2 GiB part is held as `bytearray` and then copied with `bytes(payload)`, and then `ds.save_as` serializes it again into a `BytesIO`. That is roughly 3-4 times the part size per request.
- Failure scenario: an authenticated user sends a multipart body with no boundary and no CRLFCRLF. The server buffers until the connection closes, with no limit. Separately, a few concurrent 2 GiB parts can take the node down.
- Fix: cap the preamble and header bytes (for example 64 KiB) and fail with 413. Cap the total with a non-zero default. Avoid the extra `bytes()` copies by streaming the part to disk.

### 🟡 N21: Oblique reslice has a large transient peak even under `max_pixels`

- Location: `volume_service.py:1463-1484` (`_sample_grid`), `:1423` (`_trilinear` returns `np.where(valid, out, np.nan)`), `:1563-1564` (`n_u * n_v > max_pixels`), `config.py:299`.
- Problem: `max_pixels` (4,000,000) caps the output, but `_sample_grid` builds float64 coordinate arrays of shape `(n_v, n_u, 3)` (`p`, `vox`, about 96 MB each at the cap), and `_trilinear` creates about 16 int64 and float64 temporaries of output size. The transient peak is roughly 0.6-1 GB per request.
- Failure scenario: several concurrent oblique requests at the cap drive the node into swap or OOM.
- Fix: process the plane in row blocks, or lower `max_pixels` for concurrent requests, and cap concurrency (see N2/N9).

### 🟡 N22: `series_slices` trusts `NumberOfFrames` from the header, and `/info` returns the whole stack

- Location: `volume_service.py:213-258` (`nf = int(getattr(ds, "NumberOfFrames", 1) or 1)` then a per-frame loop appending a `VolumeSlice`), `:517-521` (`frame_map` with one dict per slice), `:509` (`instance_order`).
- Problem: a single header declaring `NumberOfFrames = 10^7` creates 10^7 `VolumeSlice` objects and two 10^7-entry lists in `info`, before any cap. `/info` then serializes them to JSON.
- Failure scenario: a crafted or corrupt multi-frame file is ingested (STOW only validates UID characters, not frame counts). Any tenant user calls `/info` and the process allocates gigabytes.
- Fix: cap `nf` at ingest and in `series_slices` (compare with `VOLUME_MAX_SLICES` and reject). Cap `instance_order` and `frame_map` or drop them from `/info`.

### 🟡 N23: One malformed `ImagePositionPatient` fails the whole series

- Location: `volume_service.py:209-210`: `pos = getattr(ds, "ImagePositionPatient", None)` / `position = [float(v) for v in pos] if pos else None`. This sits after the `try` at `:197-208`, which covers only `read_dataset`.
- Problem: a `ValueError` from `float()` propagates out of `series_slices`. `/info`, `/data`, `/mip`, `/mpr/slice` and `/tiles` then return 500 for the whole series. Malformed `NaN` positions also break the sort key (`volume_service.py:286`), because `NaN` compares as neither less nor greater.
- Failure scenario: one slice from a vendor with a malformed value makes the entire series unusable in MPR.
- Fix: wrap the parse in a try that treats the slice as positionless (or skips it with a log). Reject non-finite positions.

### 🟡 N24: Error classification: decode failures become 400, and `/data` decode failures become unmapped 500

- Location: `volume.py:886-887`, `volume.py:1033-1034`, `volume.py:1209-1210` (`except ValueError` mapped to 400); `volume_service.py:605` (`raise ValueError(f"cannot decode slice ...")`); `volume.py:784` (no handler).
- Problem: a corrupt or undecodable file is a server or data fault, but MIP, tiles and MPR report 400 (client error). `/data` lets the same `ValueError` escape as an unhandled 500.
- Failure scenario: a corrupt slice makes `/mip` return 400, and the viewer tells the user their request is malformed.
- Fix: separate input validation (400) from data or decode faults (422 or 500) with a typed exception.

### 🟢 N25: In-flight tile coalescing evicts live renders first

- Location: `volume_service.py:857-859`: `oldest = min(_TILE_INFLIGHT, key=lambda k: _TILE_INFLIGHT[k].done_at)`.
- Problem: in-flight entries have `done_at = 0.0` (set in `:827`, and only updated on resolve), so when the table is full (64 entries), an in-flight render is evicted before a finished one. Waiters still wake when the owner resolves its own object, so this degrades coalescing rather than deadlocking.
- Fix: evict only entries whose `event.is_set()`, and never evict live renders.

### 🟢 N26: Dead code and unused hooks

- `volume.py:675-683`: `def stream():` is defined and never called (the handler uses `build_and_cache`).
- `volume_service.py:1171`: `release_server_session` has no callers. Sessions expire only by the 300 s TTL (`:800`, `:1184-1190`).
- `_mode_endpoints` (`volume.py:396-416`) does not list `/tiles`, so the negotiate response never advertises the tile endpoint.

### 🟢 N27: `iter_volume_bytes` does not check per-slice dtype against the header dtype

- Location: `volume_service.py:606-610` (only the shape is compared), `:620` (`np.ascontiguousarray(arr).tobytes()` uses each slice's own dtype), `volume.py:686-688` (`payload_len` uses `info["dtype"]` from the first slice).
- Problem: if one slice decodes to a different dtype, the body length no longer matches `Content-Length`. Likely unreachable for well-formed series, but unverified.
- Fix: compare `arr.dtype` to the header dtype too.

### 🟢 N28: `/tiles` silently accepts reversed or malformed ranges

- Location: `volume.py:979-1001`. For example `range=3-1` gives `end = max(start, min(end, max_index))` = 3 (one frame), and `range=5--3` gives frame 5. `/data` (`_slice_range`, `:46-68`) returns 416 for reversed ranges.
- Fix: return 400 for `end < start` and for unexpected separators.

### 🟢 N29: `wado_bulk_data` loads the full dataset for every attribute request

- Location: `dicomweb.py:883` (`storage_service.read_dataset(inst.file_path, ...)`, no `stop_before_pixels`), `wado_rs_bulk_service.py:293-313` (`extract_bulk_attribute` returns `str(val)` for non-bytes values, including sequences).
- Fix: read with `stop_before_pixels` unless the requested tag is pixel data, and reject sequence values.

### 🟢 N30 (dependent on verification): `app/services/storage/` is missing from this checkout, and `.gitignore` ignores it

- Location: `.gitignore:9` (`storage/`, unanchored). The package is imported at `storage_service.py:35`, `app/api/v1/storage.py:34`, `app/services/replication_service.py:39`, and `tests/test_multi_storage.py:28`.
- Evidence: `git check-ignore -v app/services/storage/__init__.py` returns `.gitignore:9`. `app/services/storage` does not exist on disk (`ls` error). `git ls-files` lists neither a `storage/` package nor `storage.py`.
- Problem: importing `storage_service` raises `ImportError` in this checkout. Every V2a router imports it (`dicomweb.py:39`, `volume_service.py:42`), so the DICOMweb and volume routers cannot be imported. This can be a repo or deployment defect, not a runtime defect, and it needs verifying against the deployed tree.
- Fix: anchor the ignore rule (`/storage/` or `/app/services/storage/` if the path is intended to be ignored), and commit the package. Or, if it is intentionally external, document it.

### 🟡 N31: Share viewers lose GSPS, fusion, export and DICOMDIR access

- Location: `gsps.py:46-47`, `:59-60`, `:69-70` and `fusion.py:33` (`get_current_user_any`, no share branch); `export.py:31`, `dicomdir.py:37`, `:68` (`get_current_user`, bearer only).
- Problem: a patient share token is accepted by DICOMweb and volume but not by GSPS or fusion. Overlays and fusion fail with 401 for share viewers. Export and DICOMDIR also reject cookie-only browser sessions, which may be a regression for the bundled viewer.
- Fix: decide the intended audience per endpoint, and make the share branch consistent (the DICOMweb and volume routes already use `require_series_read`-style helpers).

### 🟡 N32: `DICOMWEB_PUBLIC` default changed from `True` to `False`

- Location: `config.py:427` (HEAD: `DICOMWEB_PUBLIC: bool = False`), old `config.py:253` (`DICOMWEB_PUBLIC: bool = True`).
- Problem: this is a breaking change for legacy LAN deployments that relied on anonymous DICOMweb. It is not called out in the code comment (`config.py:421-426` describes the intent, not the migration).
- Fix: document the change in the release notes and the DICOMweb reference, and show the one-line setting to restore the old behaviour.

### 🟢 N33: Partial STOW success is reported as HTTP 507

- Location: `dicomweb.py:935-936` (`status = status_code if status_code != 200 else (200 if stored else 409)`), `dicomweb.py:1096-1100` (`status_code=507 if quota_refused else 200`).
- Problem: when some parts are stored and one is refused for quota, the whole batch returns 507, which clients may read as total failure.
- Fix: return 207 (Multi-Status) with per-part results, or keep 200 and report `00081198`.

### 🟢 N34: DICOMDIR preview returns patient names with no audit trail

- Location: `dicomdir.py:93-94` (`"patient_name": st.patient.patient_name`), `dicomdir.py:64-125` (no audit call).
- Fix: add an audit row (`add_audit`, as the DICOMDIR export does at `dicomdir_service.py:183`).

### 🟢 N35: Duplicate auth work per DICOMweb request

- Location: `deps.py:226-270` (`require_dicomweb_read` resolves the user), and `dicomweb.py:293`, `:361`, `:389`, `:943` (`get_optional_user` again in each handler). `get_optional_user` (`deps.py:81-101`) opens a DB session per call.
- Fix: resolve the user once in the router guard and publish it through the same ContextVar that holds the scope.

## 4. Documentation deltas

Endpoints:
- NEW `GET /api/v1/volume/{series_uid}/negotiate`. Query: `webgl_score` (0-100), `rtt_ms`, `gpu_vram_mb`, `hardware_concurrency` (informational). Response: `configured_mode`, `effective_mode` (server | client | split), `endpoints`, `capabilities`, `role_override` (when present), `hybrid_thresholds` and `hybrid_decision` (hybrid only). Headers: `X-PAXS-Processing-Mode`, `X-PAXS-Role-Override`, `X-PAXS-Server-Load`, `X-PAXS-Vol-Cache-MB`.
- NEW `GET /api/v1/volume/{series_uid}/tiles`. Query: `plane` (axial | sagittal | coronal), `range` (`a-b`, or `N`, or `all`; `start` alias), `window_center`/`window_width` (both needed for the `X-MPR-Window` header), `inline=1`. `size` is accepted and ignored (N8). Response: concatenated PNG bytes with `X-PAXS-Tile-Offset` and `X-PAXS-Tile-Length` (comma lists, ascending index order), or `multipart/mixed` with `X-MPR-Index` per part when `inline=1`. Headers `X-MPR-Plane`, `X-MPR-Range`, `X-MPR-Tiles`, `X-MPR-Rows`, `X-MPR-Cols`, `X-MPR-Spacing`, `X-MPR-Index-0`. Limit: at most `VOLUME_MAX_SLICES` frames.
- Auth changed on `/volume/{uid}/info|data|mip|mpr/slice`. Anonymous now returns 401 without a share token (not the old anonymous access). The module docstring at `volume.py:16-17` ("guests stay readable") is stale.
- Auth changed on `/api/v1/fusion/{study_uid}` and `/api/v1/gsps/*` (401 for anonymous and share viewers). `fusion.py:7-9` ("guests stay readable") is stale.
- `/api/v1/export/{study_uid}/anonymized` and `/api/v1/export/dicomdir*`: now `require_study_access`, so cross-tenant is 404. Auth is bearer only. Anonymized export is synchronous and holds the event loop (N11).
- DICOMweb (`/dicom-web/*`): router-level guard. Reads need a session, bearer token or share token for the study in the path. Writes need an identity unless `DICOMWEB_PUBLIC=true`. Status codes: 401 (no auth), 404 (cross-tenant, also for unknown studies), 400 (C05 invalid identifiers, cross-tenant ingest), 413 (STOW part or total too large), 507 (STOW quota, batch-level, see N33), 409 (STOW with no stored parts).
- Share tokens: accepted as `?share=<token>` (see N12), header `X-PAXS-Share-Token`, or cookie `paxs_share` (max-age 60-86400 s, set when the share viewer page is served). A share token authorizes exactly one study and never grants write access.
- WADO-URI: bearer-token callers are treated as anonymous (N17). Document cookie-only behaviour until fixed.
- QIDO: in public mode, anonymous callers get legacy (tenant NULL) studies only. WADO in public mode is archive-wide. Document the discrepancy (N18).

Behavioural contract for MPR and MIP (geometry and mode):
- Mode selection: `VIEWER_PROCESSING_MODE` (default `hybrid`), overridable per role via `VIEWER_PROCESSING_ROLE_MAP` (JSON object). The hybrid engine (`volume.py:192-257`) picks `client`, `split` or `server` from `webgl_score` and `rtt_ms`, using `HYBRID_CLIENT_SCORE_THRESHOLD` (40), `HYBRID_CLIENT_MIN_SCORE` (20, `volume.py:186`), `HYBRID_RTT_THRESHOLD_MS` (150), `HYBRID_MAX_SERVER_MPR_SESSIONS` (20) and `HYBRID_SERVER_VOL_BUDGET_MB`.
- Server mode: `/data` returns 503 with `X-PAXS-Mode-Hint: server` unless `?force=1` (`volume.py:630-632`, `:1144` is the client counterpart). `/mpr/slice`, `/mip` and `/tiles` are served. `/tiles` registers a server session (`volume.py:1041-1043`).
- Client mode: `/mip` and `/mpr/slice` return 503 with `X-PAXS-Mode-Hint: client` unless `?force=1` (`volume.py:834-836`, `:1143-1145`). `/data` and `/info` are served. `/tiles` is **not** guarded (N8).
- Hybrid mode: no guard on any endpoint (`volume.py:281-290`, `:374-381`).
- `force=1` (also `true` or `yes`) bypasses every mode guard. The guards are policy, not authorization.
- Geometry: slices are sorted by `ImagePositionPatient` along the dominant axis (`volume_service.py:273-287`). Enhanced multi-frame instances are expanded into per-frame slices with per-frame positions, or with `DimensionIndexValues` when positions are absent (`:223-258`, `:326-395`). `info.frame_map[i]` gives the `(SOPInstanceUID, 1-based frame)` for slice `i`.
- Native planes: `axial` index k is slice k (rows = ny, cols = nx). `sagittal` index i is column i (rows = nz, cols = ny). `coronal` index j is row j (rows = nz, cols = nx). Source: `volume_service.py:660-672`. Out-of-range indices return 416.
- Oblique: `origin`, `row`, `col` are patient coordinates in mm. `row` and `col` are normalized; `size=WxH` (or `N`) sets the grid, centred on `origin`. The total is limited to `VOLUME_MPR_MAX_PIXELS` (4,000,000). Zero or NaN vectors are not rejected (N7).
- Strided dims: `/data` with `downsample=d` outputs `ceil(n/d)` samples per axis (`strided_dim`, `volume_service.py:109-126`). `X-Volume-Rows`, `X-Volume-Cols`, the `.npy` header and `Content-Length` are all derived from it.
- NaN handling: out-of-volume samples are NaN in reslice output. Raw and `.npy` outputs convert NaN and infinities to **0.0** (`volume_service.py:1604-1606`), which is 0 HU, not air. PNG renders them black. Document this, or change it to a sentinel, before clients interpret zeros as tissue.
- Windowing: explicit `window_center` and `window_width` win, then the series `WindowCenter`/`WindowWidth`, then a 2nd-98th percentile autoscale (`volume_service.py:1584-1601`). Non-finite values are not rejected (N7).
- MIP output shape: `z` gives `(rows, cols)`, `x` gives `(nz, ny)`, `y` gives `(nz, nx)` (`volume_service.py:1749-1754`). `mode=vrt` uses the display window as the opacity ramp.
- Tile framing: default is concatenated PNG with offset and length headers. `inline=1` returns `multipart/mixed` with one `image/png` part per frame.

Other endpoint and header behaviour to document:
- `X-PAXS-Server-Load` (`active/max`) and `X-PAXS-Vol-Cache-MB` (`used/budget`) are on every volume response (`volume.py:333-340`).
- Error codes on volume routes: 401 (no auth, no share), 404 (no readable volume, or cross-tenant), 416 (slice or plane out of range, or more than `VOLUME_MAX_SLICES` slices on `/data`), 400 (bad parameter), 503 (streaming disabled or mode guard).
- `/mpr/slice` and `/mip` do not enforce `VOLUME_MAX_SLICES` (N3). Document the actual behaviour.
- Frame endpoint: `frames/{list}` accepts repeated indices, which return repeated payloads (N4). `zorder=1` uses the volume's z-order (`dicomweb.py:745-777`).
- `window_center`/`window_width` on `/mip` and `/mpr/slice` are parsed as bare floats (N6, N7).

## 5. Open questions

1. Is `app/services/storage/` (gitignored by `.gitignore:9`) present in the deployed tree? If not, `storage_service` cannot be imported and the V2a routers cannot start (N30). Is the ignore rule intentionally unanchored?
2. For an incoming instance whose Series or SOP UID already belongs to another study, should STOW reject it (409), or re-home it? The same question applies to the C-STORE receiver (`app/dicom/receiver.py`, not reviewed) (N1).
3. Should patient share tokens still be accepted in the URL query string (N12)? If so, is there log scrubbing in place?
4. Intended audience for GSPS, fusion, export and DICOMDIR: share viewers, cookie-only browser sessions, or bearer clients only (N31)?
5. Should share-scoped QIDO see tenant-assigned studies (currently it sees tenant NULL only, N18)? Should public-mode QIDO be archive-wide to match WADO?
6. Is `size` on `/tiles` meant as a hint that should be implemented (downsampling), or should it be removed (N8)? Should `/tiles` honour the client-mode guard?
7. `VOLUME_CACHE_BUDGET_MB` (1024) is smaller than one maximum-size volume (about 4 GiB float32 for 4096 512x512 slices). What is the intended peak memory per process, and what is the intended concurrency for materialization (N2, N3)?
8. Do clients depend on NaN becoming 0.0 in raw and `.npy` outputs (N-doc, out-of-volume samples)? Should these be NaN?
9. Is there a supported STOW or C-STORE path that re-sends a corrected series with the same UIDs? That determines how urgent N10 is.
10. Is `warm_service.py`'s silent failure (F-06) intended, given the volume pre-warm never runs? Should the failing `asyncio` call be fixed or removed?
11. Are there DICOMweb tests that exercise `DICOMWEB_PUBLIC=false` for anonymous share access? I did not review test coverage.
