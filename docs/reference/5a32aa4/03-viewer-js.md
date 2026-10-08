# PAXS viewer add-ons (browser JS) — changes in 5a32aa4

> **Re-review of commit `5a32aa4`** (baseline `e90d32c`), part of the [PAXS Archive documentation](../../README.md). Static analysis, 2026-10-08; line numbers refer to `5a32aa4`. Baseline area reference: [07-web-ui-static.md](../07-web-ui-static.md). Findings are consolidated and prioritized in [CODE_REVIEW.md](../../CODE_REVIEW.md), which takes precedence where the two differ. Finding IDs here (N1, N-01, V4-01, …) are local to this file; CODE_REVIEW.md maps them to NEW-xx IDs.

Commit under review: HEAD `5a32aa4`. Previous reviewed commit: `e90d32c`.

Scope: `app/static/viewer/paxs-mpr.js` (+1778 / -178, now 3830 lines), `paxs-mpr-geometry.js` (new, 480), `paxs-toolbar-enhance.js` (new, 654), `paxs-about.js` (new, 555), `paxs-study-list-enhance.js` (+246 / -66, now 1121), `index.html` (+6 / -22), `app-config.js` (unchanged), `paxs-viewer-theme.css` (+379, skim only).

Method and limits:
- Every file in scope was read at HEAD in full (working tree is clean for these paths, so working tree = HEAD).
- Backend claims were checked in `app/api/v1/reports.py`, `volume.py`, `deps.py`, `auth.py`, `app/services/volume_service.py`, `fusion_service.py`, `app/main.py`.
- One runtime test was run: a headless Chromium reproduction of the MPR GL read-back (section 3, N1). It reproduces the canvas and `drawImage` semantics, not the whole app. No DICOM dataset or running backend was available, so every other item is static analysis.
- Line numbers refer to HEAD.

---

## 0. Headline status changes

- **F-15 (report save, anonymous path): FIXED** at the server. The modal's `POST /api/v1/reports/save` now requires auth, enforces the destructive role for FINALIZED, and calls `finalize_report` with ORU delivery.
- **07-F18 (WebGL / listener leaks): FIXED** for the open/close path. A residual leak remains on `initGl2` error returns (N12).
- **07-F13 (silent refresh): PARTIAL.** Bug 1 (the `document.cookie` guard that made refresh never run) is fixed. Refresh now runs on every page load, which makes the bug 2 cross-tab reuse race more reachable.
- **07-F17 (MPR sagittal): PARTIAL.** The CPU sagittal shear is fixed. There is still no physical aspect correction, and the GL sagittal/coronal read-back is wrong (N1, new Critical).
- **07-F21 (three.js from cdnjs): PARTIAL.** The MPR now loads the vendored `/viewer/three.min.js` first, but the CDN fallback is still coded, has no SRI, and is blocked by the CSP.
- **07-F19 (volume cache): OPEN.** Still a count cap of 3, FIFO, with no byte cap.
- **07-F16 (thumbnail matching): OPEN.** Index fallback still present (study-list:203).
- **07-F24 (hard-coded legal/attribution): OPEN** and extended into the About dialog.
- **New Critical:** N1 (GL plane read-back uses the wrong source rectangle, so sagittal and coronal are wrong on the default GL path for most stacks). N2 (PET/CT fusion is sampled by normalized index, not patient geometry).

---

## 1. Change summary

### 1.1 `app/static/viewer/index.html` (HEAD, 8 lines)

- Purpose: viewer shell. The inline H-11 `HTMLCanvasElement.prototype.getContext` override was removed (lines 2-6, comment only).
- Script load order (all `defer`): `app.bundle.29858d89fb4b9b9d.js`, `paxs-study-list-enhance.js?v=37`, `paxs-mpr-geometry.js?v=37` (new), `paxs-mpr.js?v=37`, `paxs-about.js?v=37` (new), `paxs-toolbar-enhance.js?v=38` (new). CSS: `paxs-study-list-theme.css?v=37`, `paxs-viewer-theme.css?v=38`.
- Unchanged inline script (line 9): "Study List" marker. It runs `setInterval(...,800)` x20 and a body-wide MutationObserver that calls `requestAnimationFrame(mark)`. `mark()` runs `querySelectorAll('div')` and `textContent` on each. This is F22 territory and was not touched.
- Cache note: `paxs-mpr.js` and `paxs-study-list-enhance.js` changed but kept `?v=37` (see N18).

### 1.2 `app/static/viewer/app-config.js`

Unchanged (`git diff` empty). OHIF DICOMweb root `/dicom-web`, no token, cookies only. Still `extensions: []`, `modes: []`.

### 1.3 `app/static/viewer/paxs-mpr-geometry.js` (new, 480 lines)

- Purpose: single source of truth for voxel, viewport and patient transforms and physical measurements. UMD export (`window.PaxsMprGeometry`).
- Key functions: `makeMeta` (80-109), `planeAxes/planeDims/planeSpacing` (132-149), `voxelIndex/sampleVoxel` (172-192), `viewportToVoxel/pointToVoxel` (203-240), `basisOf` (262-279), `voxelToPatient/patientToVoxel` (311-336), `computePhysicalLength/Angle/Cobb/Ratio/EllipseAreaCm2` (353-419), `extractPlane` (433-454).
- Endpoints, DOM, timers, workers, WebGL: none. Pure math.

### 1.4 `app/static/viewer/paxs-mpr.js` (3830 lines)

- Purpose: 3D MPR workspace (launch button, overlay panel, series select, layouts, 2D planes, 3D viewport, measurements, PET/CT fusion overlay).
- Mode negotiation: client GL (WebGL2 R32F 3D texture + fragment-shader slicing), split (coarse x4 volume + server PNG fine slices), server (thin client: `/mpr/slice` PNG, `/tiles` prefetch), rendered (fallback handoff). Chain client -> split -> server -> rendered (700-775).

Backend endpoints (all same-origin `fetch`/`<img>`, **cookie auth; no Authorization header is set by this file**; see 1.9):

| Method | URL | Where |
|---|---|---|
| HEAD | `/api/v1/volume/{uid}/info` (RTT probe, no-store) | 394-397 |
| GET | `/api/v1/volume/{uid}/negotiate?webgl_score&rtt_ms&gpu_vram_mb&hardware_concurrency` | 413-420 |
| GET | `/dicom-web/studies/{uid}/series?includefield=0020000E,00080060,0008103E,00201209` | 1231 |
| GET | `/api/v1/volume/{uid}/info` | 1343; overlay 3299 |
| GET | `/api/v1/volume/{uid}/data` (full, ArrayBuffer) | 1497 |
| GET | `/api/v1/volume/{uid}/data?downsample=2` (progressive preview) | 1449; fusion overlay 3302 |
| GET | `/api/v1/volume/{uid}/data?downsample=4` (split coarse) | 2562-2563 |
| GET | `/api/v1/volume/{uid}/mpr/slice?plane&index&format=png&window_center&window_width` | 2409-2411; split fine 2612-2614 |
| GET | `/api/v1/volume/{uid}/mpr/slice?plane&index&format=raw` (reads `X-MPR-Rows/Cols`) | 2747-2748 |
| GET | `/api/v1/volume/{uid}/tiles?plane&range&window_center&window_width` (reads `X-PAXS-Tile-Offset/Length`) | 2700-2702 |
| GET | `/api/v1/fusion/{studyUid}` | 3271 |
| GET (new tab) | `/paxs-viewer-guide` (shortcut modal link) | 1054 |

Response headers consumed: `X-PAXS-Server-Load`, `X-PAXS-Vol-Cache-MB` (`parseLoadRatio`, 686-709); `X-PAXS-Processing-Mode` is read by toolbar-enhance. All exist in `app/api/v1/volume.py` (328-329, 338-339, 556, 1051, 1096, 1101-1102).

- DOM injection: `btn.innerHTML = '⚡ 3D MPR'` (619); `root.innerHTML` panel template (812) and `viewportHTML` (1015-1028), both static; shortcut modal `dlg.innerHTML` (1036, static); `sel.innerHTML=''` (1248) then option `textContent` (1256); `subhead.innerHTML` with numbers only (1378, 1430, 1545); `hud.innerHTML`/corner `innerHTML` with numbers and constants (2443, 2465, 2632, 2643, 2871, 2877, 2882); `svg.innerHTML +=` built from numbers (2945-3001); `wrap.innerHTML` in `set3dUnavailable` (2785, constant reason strings); `body.innerHTML` in `showError` with `e.message` (3772, see N17). Style element appended (605). Script tags appended for three.js (217-240).
- Event listeners: `btn.click` (626); panel buttons/select `addEventListener` (906-1012); `root.keydown` (908); `window.pointerup` (3525, removed in `disposeThreeScene` 3384-3387); property handlers `wrap.onpointerdown/onpointermove/onpointerup/onwheel` (setupPointer 3011-3158, init3dEngine 3502-3532); `slider.oninput` (1824); canvas `webglcontextlost` (2135, idempotent flag 2133); `img.onload/onerror` for frames.
- Timers: `setInterval` 30 ms poll while Three.js loads (211-214, never times out, N19); `setInterval(ensureButton,800)` x25 (3778); `setTimeout` 500 then a body-wide `MutationObserver` that is never disconnected (3779-3783); cine `setInterval(66)` (1089, cleared on close 1096, 1143); LOD `setTimeout` (2811); split fine-slice debounce 400 ms (2590); server W/L debounce 200 ms (2652); server retry 350 ms (2469); per-request worker decode timeout 30 s (1694); RTT 3 s (396); negotiate 2.5 s (419); volume fetch deadline 120 s (377).
- Workers: blob Worker pool, size clamp(2..8, hardwareConcurrency), created on demand, terminated on close (1611-1679, 1598-1609). Worker decodes int16/uint16/uint8 with rescale; float32 path ignores rescale (N13).
- WebGL: probe contexts created and released (`probeGlSupport` 243-267; `probeClientCapabilities` 298-344, both call `WEBGL_lose_context`). Render context: offscreen `renderCv` WebGL2 with `preserveDrawingBuffer: true` (2035-2050), R32F `TEXTURE_3D`, GLSL 3.0 slice/slab shader (1868-1937), chunked `texSubImage3D` upload above 64 MB (1952-2027). Three.js r128 `WebGLRenderer` (3455), `BoxGeometry`, `PlaneGeometry`, `Data3DTexture` for VRT/MIP3D (3549), `ShaderMaterial` raymarch (3561-3667), `CanvasTexture` from 2D viewport canvases (3679-3695). Context loss handled (2131-2157).

### 1.5 `app/static/viewer/paxs-study-list-enhance.js` (1121 lines)

Covers both the study list and the viewer page. Loaded on both.

- Study-list thumbnails: `loadStudies` (141-146), `ensureResolved` (148-170), `thumbFor` (172-192), `matchStudy` (194-204), `enhanceRow` (206-269). LRU of 64 blob URLs (68-90), revoked on `pagehide` (94).
- Radix dialog title fix (31-48). Viewer logo rewrite (296-312). Click-intercept on logo to `/viewer` (318-337). Viewer footer (342-423). Toolbar icon colorizer (430-525). Report modal (530-685). Sidebar series enhancer (702-807). Auth: silent refresh, fetch and XHR guards, user chip (844-1117).

Endpoints:

| Method | URL | Auth on wire |
|---|---|---|
| GET | `/dicom-web/studies?limit=200&includefield=0020000D,00080061,00100010,00081030` (142) | cookie |
| GET | `/dicom-web/studies/{uid}/series?limit=1&includefield=0020000E,00080060` (151-152) | cookie |
| GET | `/dicom-web/studies/{uid}/series/{suid}/instances?limit=1&includefield=00080018` (158-160) | cookie |
| GET | `/dicom-web/studies/{uid}/series/{suid}/instances/{sop}/thumbnail` (176-178) | cookie |
| GET | `/api/v1/reports/study/{uid}` (592) | Bearer if `paxs_token`, else cookie |
| GET | `/api/v1/templates?modality=` (609) | Bearer if token, else cookie |
| POST | `/api/v1/reports/save` (658; `status` DRAFT or FINALIZED) | Bearer if token, else cookie |
| GET (new tab) | `/api/v1/reports/print/{uid}` (648) | cookie |
| POST | `/api/v1/auth/refresh` (880) | cookie (rotates refresh token) |
| GET | `/api/v1/auth/me` (1047, via `paxsFetch`) | Bearer if token, else cookie |
| POST | `/api/v1/auth/logout` (1098) | cookie |
| GET | `/static/logo/logo.png` (302) | none |

- DOM injection: `wrap.innerHTML` report modal (549-586, static); footer `foot.innerHTML` (351-384, static, with attribution); user chip `chip.innerHTML` (1068-1076) with all dynamic values through `paxsEsc` (854-858); `spans[s].innerHTML = MODALITY_ICONS[txt]+' '+txt` (798, constants). Thumbnails are `<img src=blob:>` inserted into the first `<td>` (249-258). Several `alert()` calls with server `detail` text (672-679).
- Event listeners: document click capture (318); `window.mousemove` (405, added once per footer); `pagehide` (94); MutationObserver on `document.body` subtree for `fixDialogs` (46-47) and for `enhance` via rAF (289-290); `signout` click (1095); per-card click handlers (778).
- Timers (all unbounded unless noted): `setInterval(fixDialogs,1000)` (45); poll `setInterval(enhance,800)` x20 (283-286); `setInterval(updateViewerLogo,1000)` (311); `setInterval(injectViewerFooter,1500)` (419); `setInterval(colorizeToolbarIcons,2000)` (423); `setInterval(enhanceLeftSidebarSeries,1000)` (806); `setInterval(injectUserChip,2000)` (1114).
- WebGL: none (H-11 removed the global `getContext` patch, lines 16-23).
- Auth guards: `window.fetch` wrapper (959-988) and XHR wrapper (990-1020). Both replay on 401 for URLs containing `/dicom-web/`, `/api/v1/volume/`, `/api/v1/reports/` (949-957).

### 1.6 `app/static/viewer/paxs-toolbar-enhance.js` (new, 654 lines)

- Purpose: restyle OHIF header toolbar icons (replace SVG innerHTML from a constant icon table), tag the toolbar container, and add a telemetry pill (mode, RTT, score, GPU).
- Endpoints: none. It wraps `window.fetch` (516-533) to read `X-PAXS-Processing-Mode` from `/volume/.../negotiate` responses. It does not alter requests.
- DOM injection: `svg.innerHTML = markup` from the `PAXS_ICONS` constant table only (343-344, 364-390). Pill built from a static template (546-557); dynamic text via `textContent` (574-582). Pill appended to `<header>` once (567).
- Listeners: `MutationObserver` on `document.body` childList subtree (645-646) with 100 ms debounce (634-643); `setTimeout` 400/1200 (631-632); `setInterval(enhance,1500)` (650). Never disconnected.
- WebGL / workers: none.

### 1.7 `app/static/viewer/paxs-about.js` (new, 555 lines)

- Purpose: intercepts the header menu "About" item (capture phase click at 535-547) and renders a custom modal (processing telemetry from `window.__getPaxsMprState`, GPU, active dataset, standards badges, shortcuts, clinical notice).
- Endpoints: none.
- DOM injection: `dialog.innerHTML` (365-397), all dynamic values through `esc()` (224-228); `<style>` appended (50-193); overlay appended to `body` (442).
- Listeners: overlay bubble listeners for 12 pointer/wheel/touch/click events, new per open (416-431); `keydown` capture on `document` (461), removed on close (501); close button click (464); synthetic `pointerdown` dispatched on `document.body` (485-487) to dismiss Radix menus.
- Timers: `requestAnimationFrame` x2 (473-477); `setTimeout(150)` removal (506).
- WebGL: one probe context per open, released (202-216). Not created when an MPR session exists (uses the bridge).

### 1.8 `app/static/viewer/paxs-viewer-theme.css` (skim)

- 379 lines added (file now 1134 lines). No `url()`, no `@import`, no external hosts, no `display:none`. 528 `!important` declarations (style-override heavy). One `position: fixed` rule at line 613.

### 1.9 Token handling (answers the question in the brief)

- Does viewer code read `localStorage.paxs_token`? Yes, only in `paxs-study-list-enhance.js`: written at 887 (refresh), read at 917 (`paxsFetch`) and 1038 (`paxsAuthHeaders`), removed at 1101 (sign-out). `paxs-mpr.js`, `paxs-mpr-geometry.js`, `paxs-toolbar-enhance.js` and `paxs-about.js` do not touch tokens (grep: no `paxs_token`, `Authorization`, `credentials`).
- Does it send `Authorization` to `/dicom-web` or `/api/v1/volume`? Not on the first attempt. Those requests are cookie-only. The fetch/XHR guards (949-1020) add `Authorization: Bearer` to a **replay after a 401** for URLs matching those path fragments. Because the MPR uses the global `window.fetch`, an MPR fetch that gets 401 is retried with the bearer token. `<img>` PNG loads (2445, 2635) are not wrapped and rely on the cookie only.
- Token in URL? No (grep for `token=` and `access_token=` in viewer JS: none).
- Backend: `get_optional_user` accepts bearer or session cookie (deps.py:90-101), and `require_series_read` (deps.py:295-313) accepts that user or a share grant. Anonymous guests are refused (401) for volume endpoints unless a share grant applies (`require_series_read`, deps.py:306-313). The prior review (F3) records QIDO as guest-readable. `get_current_user_any` accepts bearer or cookie (deps.py:104+).
- Silent refresh: runs on every study-list and viewer page load (1032, 1110). It writes the access token to `localStorage` (887). Refresh rotates the HttpOnly cookie server-side (auth.py:304-330).

---

## 2. Prior findings: status at HEAD

| ID | Status | Evidence (HEAD) | Notes |
|---|---|---|---|
| 07-F13 | **PARTIAL** | Bug 1 FIXED: no `document.cookie` check left; refresh unconditional (study-list 872-875 comment, 877-899 `paxsRefreshToken`, 1027-1033 `paxsSilentRefresh`, 1110 call). Bug 2 OPEN: backend reuse detection unchanged (auth.py:322-327 `if sess is None or sess.is_revoked` -> `_revoke_family` + `token_theft_detected`). Single-flight is per tab only (877-882). | Refresh now fires on every page load, so two tabs opening together can race and revoke the family. Severity stays Important. |
| 07-F16 | **OPEN** | `matchStudy` falls back to index: `return i < studies.length ? studies[i] : null;` (study-list:203). Match is `rk.indexOf(sk)` on `row.innerText` (112-114, 198-200). | Unchanged. |
| 07-F17 | **PARTIAL** | Shear FIXED: `initViewports` sets sagittal `w=rows, h=slices` (mpr 1814-1815) and `extractPlane` writes `k*rows+j` (geometry 433-454; mpr 1796-1798). Still OPEN: no physical aspect correction on GL (`drawImage(... 0,0,dw,dh)` stretches, 2229) or CPU (draw2D 1852-1853). Server path letterboxes by pixel ratio only (2517-2521). NEW GL read-back bug, see N1. | |
| 07-F18 | **FIXED** (residual N12) | `disposeThreeScene` disposes renderer and calls `forceContextLoss` (mpr 3413-3417); `pointerup` removed (3384-3387, added 3525); `closeMpr` calls `disposeThreeScene` (1156) and `releaseGl2` (1166, `loseContext` at 2187-2188); probe contexts released (261-262, 325-326). | `initGl2` early returns still leak a context (N12). |
| 07-F19 | **OPEN** | Cache: count cap 3, FIFO (mpr 1528-1532); comment still claims "~500MB" (1526). Per-volume limit 256M voxels = 1 GiB float32 (2033). Holds CPU copy plus GL copy, and a three.js `Data3DTexture` copy when VRT/MIP is used (3549, shown only in VRT/MIP modes, 3499, 3745). | No byte accounting. |
| 07-F21 | **PARTIAL** | MPR loads `/viewer/three.min.js` first (mpr 217-218), CDN fallback still coded (49, 226-227), blocked by CSP `script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'` (main.py:298-301, no cdnjs). No SRI. `app/static/viewer.html:9` (not in this commit) still loads cdnjs with no `integrity`. | |
| 07-F24 | **OPEN** (extended) | Attribution in footer `© 2026 <b>Mohamed O. Elsamani</b>` (study-list 362) and external link `https://elsamanii.rf.gd` (364). Hard-coded T&C alert with "AES-TLS encryption" and "tamper-evident audit trail" (392). New copies in About: `LEAD_ARCHITECT` (about 44, 377-378), "Clinical notice" text (387-391), "© 2026" (395), "HTJ2K Lossless" badge (327). | Backend has HTJ2K references (`lossy_compression_service.py:85`, `dicom_diagnostics.py:44`), so the badge is a static claim, not verified as viewer-supported. |
| F-15 (report modal) | **FIXED** (server side) | Modal calls `POST /api/v1/reports/save` (study-list 658). Route now `user = Depends(get_current_user_any)` (reports.py:194-198); FINALIZED needs `DESTRUCTIVE_ROLES`, else 403 (217-220); finalize goes through `report_service.finalize_report` with ORU delivery (261-263). Client-side role gate (634-646) is cosmetic and says so ("the server enforces it"). | Modal endpoints now: `GET /api/v1/reports/study/{uid}` (reports.py:180-184, auth + `require_study_access`), `GET /api/v1/templates` (settings.py:162-166, auth), `POST /api/v1/reports/save`, `GET /api/v1/reports/print/{uid}` (reports.py:300-304, auth + `require_study_access`). |
| F1 / F2 (report routes) | PARTIAL | Report read/print/save and templates GET now require auth (see F-15 row). Other F1/F2 routes not in V2b scope. | |
| F10 (token in localStorage, CSP) | OPEN | Token stored and read in localStorage (study-list 887, 917, 1038). CSP unchanged (`unsafe-inline`, `unsafe-eval`, main.py:298). | Refresh writes the token on every page load now (887). |
| F22 (polling / full-DOM scans) | OPEN (more timers) | List in 1.5, 1.6, 1.4 and index.html line 9. New in this commit: toolbar observer plus 1500 ms interval (N7); about and sidebar timers. | |

---

## 3. New findings

Severity: 🔴 Critical (clinical display or data integrity, reproducible from code) · 🟡 Important · 🟢 Minor.

### N1 🔴 GL-path plane read-back copies the wrong rectangle, so sagittal and coronal show wrong pixels (default GL path, most stacks)

- Where: `paxs-mpr.js:2036-2037` (canvas `width = max(cols, rows)`, `height = max(rows, slices)`), `:2203` (`gl.viewport(0, 0, pw, ph)`), `:2229` (`vp.ctx.drawImage(g.renderCv, 0, 0, pw, ph, 0, 0, dw, dh);`). `pw/ph` are the plane's `vp.w`/`vp.h` (2200-2201). For sagittal `ph = slices`, for coronal `ph = slices`, for axial `ph = rows`.
- Quote: `renderCv.height = Math.max(state.rows, state.slices);` and `vp.ctx.drawImage(g.renderCv, 0, 0, pw, ph, 0, 0, dw, dh);`
- What is wrong: WebGL draws with y=0 at the bottom of the drawing buffer, so a pass with `viewport(0,0,pw,ph)` fills the bottom `ph` rows of the canvas. `drawImage(..., 0, 0, pw, ph, ...)` reads the top `ph` rows. The two match only when `renderCv.height == ph`. The source y must be `renderCv.height - ph`.
- Failure scenario: 512x512x100 CT, default GL mode. The axial pass fills all 512 rows. The sagittal pass writes only the bottom 100 rows. The copy then reads the top 100 rows, which still hold axial output from the same frame. The sagittal and coronal panels show a band of the axial slice. The same mismatch hits axial in a 512x512x600 stack (ph=512, height=600). It affects every stack where `slices < rows` (sagittal, coronal) or `slices > rows` (axial).
- Evidence (runtime, partial): `(local verification file)`, run with headless Chrome (`--headless=new --use-angle=swiftshader --dump-dom`). Output:
  - `drawImage result pixel (x=256,y=5) = (255,0,0)` (stale red, not the green pass) and `(x=256,y=95) = (255,0,0)` for the 512x512x100 sagittal case.
  - `candidate fix, sagittal 512x100 (sy = H - ph): top=0,255,0,255 bottom=0,255,0,255`.
  - `case B axial 512x512x600 (H=600, ph=512), as coded: top=255,0,0,255 bottom=0,255,0,255`.
  - Control with matching heights: correct.
  - The test used `scissor` + `clear` in place of the shader. The read-back semantics are the same. The real app was not run.
- Pre-existing: identical lines exist at `e90d32c` (`paxs-mpr.js` 1227-1228, 1330, 1355). The prior review (F17) described only "stretched", so this was missed.
- Fix: `vp.ctx.drawImage(g.renderCv, 0, g.renderCv.height - ph, pw, ph, 0, 0, dw, dh);` (verified in the harness, sagittal case). Optionally `gl.clear` before each pass so stale content can never appear.
- Confidence: high for the mechanism (reproduced), medium for the clinical picture until a real series is opened in client mode.

### N2 🔴 PET/CT fusion is sampled by normalized index fraction, not patient geometry; PET is windowed as CT HU

- Where: `paxs-mpr.js:2272` (`var normPos = count <= 1 ? 0.5 : state.idx[plane] / (count - 1);`), `:2277-2279` (`fZ/fY/fX = floor(normPos * (n-1))` over the overlay's own dimensions), `:2286-2288` (indexing), `:2290` (`var t = Math.max(0, Math.min(1, (hu - 50) / 400));`). Overlay load: `:3268-3320`. Geometry is available but unused: `/api/v1/volume/{uid}/info` returns `image_position_patient`, `image_orientation_patient`, `pixel_spacing`, `slice_spacing` (`volume_service.py:496-500`), and the fusion manifest returns per-series `geometry` (`fusion_service.py` `_series_geometry`).
- What is wrong: the overlay slice is chosen by the fraction of the base volume's length, so the two volumes are assumed to span the same mm range from the same origin. The geometry module already has `voxelToPatient` / `patientToVoxel` (geometry 311-336) but the fusion path does not use them. The manifest says `transform: IDENTITY, registered: true`, while the viewer never applies a transform. PET values (SUV or Bq/mL) are mapped through the CT window `(hu-50)/400`.
- Failure scenario: CT 512x512x300 and PET with a different z-coverage or origin. The uptake drawn at a given CT level comes from the PET slice at the same fraction, which can be tens of millimetres away in patient space. The same lesion appears at a different organ level than the CT shows. Uptake below SUV/Bq value 50 is always transparent, so the colormap is essentially uncalibrated.
- Fix: for each base voxel, `voxelToPatient(baseMeta)` then `patientToVoxel(overlayMeta)` and sample the overlay there (nearest or trilinear). Refuse the overlay when geometry or FoR is missing. Use a PET-specific intensity mapping (SUV or rescaled value) rather than the CT window.
- Confidence: high for the code path, static only. Needs a PET/CT pair with different z-extent to confirm.

### N3 🟡 3D cut-plane textures are not aligned with the volume's index-to-world mapping

- Where: plane meshes `paxs-mpr.js:3485` (axial `create3dPlaneMesh(sx, sy, ...)`, no rotation), `:3486-3487` (sagittal `create3dPlaneMesh(sy, sz, ...)`, `rotation.y = Math.PI / 2`), `:3488-3489` (coronal `create3dPlaneMesh(sx, sz, ...)`, `rotation.x = Math.PI / 2`). Positions: `:3722-3728` (`zPos = (idx.axial/(slices-1) - 0.5)*sz`, same form for X and Y). Texture: `:3679` `new THREE.CanvasTexture(...)`, then `:3738` `mesh.material.map.image = document.getElementById(vps[p].cv)`. VRT sampling `:3596` `uv = (p / u_size) + 0.5` (index 0 at -size/2).
- What is wrong, from the code (not rendered):
  - Sagittal: after `rotation.y = π/2`, local +x maps to world -Z, so the plane's `sy` width lies along Z (box Z extent is `sz`), and the image's horizontal (rows, Y) is drawn along Z. The plane is transposed and mis-sized relative to the box.
  - Axial and coronal: `CanvasTexture` uses `flipY = true`, so canvas row 0 (index 0) maps to local +y. Index 0 is positioned at `-sy/2` (or `-sz/2`), so the image is vertically inverted relative to the volume. Coronal superior/inferior is inverted in 3D; axial anterior/posterior is inverted in 3D.
  - VRT uses the correct orientation, so the cut planes disagree with VRT and with the index mapping.
- Failure scenario: a clinician uses the 3D cut-plane view to read orientation, and superior and inferior are swapped on the coronal plane.
- Scope: 3D view only. 2D measurements and HU readouts are not affected.
- Fix: build all three planes from one basis (local x -> +X, local y -> +Y or +Z per plane, normal -> +X/+Y/+Z as appropriate), with texture v matching index direction. Check visually.
- Confidence: medium (derived from three.js conventions; not rendered).

### N4 🟡 Server-mode overlays and the HU/ROI probe ignore the letterbox rectangle

- Where: `paxs-mpr.js:2506-2527` (`drawServerImage` letterboxes and stores `vp.imgRect`, comment says "shared by overlays + pointer" at 2522). `serverImageRect` (2774-2780) is defined but never called (grep). Pointer uses the wrap rect: `wrap.getBoundingClientRect()` (3013, 3094) to compute `fx/fy` (3014-3015, 3095-3096). Probe and ROI use those fractions (3112-3143, 3163-3166). SVG overlays use the svg rect (2928-2930).
- What is wrong: server frames for sagittal and coronal (`rows x slices`) are letterboxed inside a square canvas. Pointer fractions map over the whole wrap, not the image. The HU readout is taken from the voxel at the wrong row or slice by the letterbox offset. Measurement lines are drawn at wrap fractions, so they do not sit on the anatomy they were placed on.
- Failure scenario: in server mode (or after a server degrade), the probe reports HU for a voxel up to half the letterbox margin away from the cursor. A caliper or ROI drawn on the sagittal panel does not match the anatomy under it.
- Fix: convert pointer coordinates into the `imgRect` (`fx = (clientX - rect.left - img.x)/img.w`, reject outside the image) and draw overlays in image coordinates.
- Confidence: high for the mismatch (code path), server mode only.

### N5 🟡 `prefetchServerRange` leaks blob URLs on every wheel tick and fetches frames that are never drawn

- Where: `paxs-mpr.js:2689-2734`. Overwrite without revoke at `:2723-2730`: `state.serverFrames[plane] = ...; ... state.serverFrames[plane][target] = img;`. Called on every wheel tick at `:3155` (`if (state.mode === 'server') prefetchServerRange(plane, state.idx[plane]);`). `state.serverFrames` is read only by `evictServerFrames` (2663-2684) and `releaseServerResources` (1200-1210). Drawing uses `state.serverCache` only (`serverPlane` 2383-2447).
- Quote: `state.serverFrames[plane][target] = img;` (2730), with `img._paxsObjectUrl = objUrl` set at 2725.
- What is wrong: each tick fetches a 7-slice tile window (radius 3) and replaces ~6 entries that still hold live blob URLs. The replaced `_paxsObjectUrl` is never revoked, so the PNG blobs stay pinned until `closeMpr`. Also, the decoded frames are never displayed, so the requests and memory are wasted.
- Failure scenario: scroll a 300-slice server-mode series. Each wheel tick adds un-revoked blobs; memory grows with scroll distance until the workspace is closed.
- Fix: remove the prefetch, or revoke `prev._paxsObjectUrl` before overwrite and draw from the cache. The `/tiles` endpoint is not needed for the current render path.
- Confidence: high (read-site analysis).

### N6 🟡 A failed chunked texture upload is reported as success; slices after the failure point are silently empty

- Where: `paxs-mpr.js:2013-2022` (`if (err !== 0) { console.warn(...); if (state.texUpload) state.texUpload.failed = true; return; }`), `:2026` (`return true;`), caller `:2076-2079` (`if (!uploadVolumeTexture(...)) {... return;}`, so the caller continues as supported). `state.texUpload.failed` is never read (grep: only set at 1990, 2020).
- Quote: `if (err !== 0) { console.warn('PAXS MPR: chunked 3D texture upload failed at slice ' + z + ' — falling back to CPU rendering');` followed by `return;` (no fallback happens).
- What is wrong: the warning says "falling back to CPU rendering", but nothing falls back. The function returns true before the chunks run. The GL state stays `supported: true`, and `drawPlaneGl` samples a texture where slices beyond `z` are zero (HU 0).
- Failure scenario: a >64 MB volume on a GPU that rejects the second chunk. Slices from the failure point onward render as zero HU (dark or mid-grey depending on W/L). No message is shown, so the clinician sees an apparently complete volume with missing slices.
- Fix: make the upload report completion or failure (promise or callback); on failure set `state.gl2.supported = false`, re-init the CPU path, and surface a badge.
- Confidence: high.

### N7 🟡 Toolbar telemetry pill creates a self-triggering MutationObserver loop (~10 Hz full-document sweep)

- Where: `paxs-toolbar-enhance.js:574-575` (`pill.querySelector('.paxs-tel-icon').textContent = st.icon;` and label) and `:582` (`...paxs-tel-tip').textContent = tip.join(...)`). Observer `:645-646` (`new MutationObserver(scheduleEnhance); mo.observe(document.body, { childList: true, subtree: true });`), debounce `:637-643`, sweep `:623-628` (`enhance` -> `tagToolbar`, `tagPatientInfo`, `refreshPill`).
- What is wrong: assigning `textContent` replaces the child nodes even when the value is the same, so the observer sees a childList mutation inside `<body>`. That schedules another `enhance` 100 ms later, and the loop never stops while the viewer is open. Each pass runs `document.querySelectorAll` for 5 patient selectors (601-605), scans `header span`, and rebuilds class lists.
- Failure scenario: the viewer runs a permanent ~10 Hz DOM sweep on top of the GPU rendering, adding CPU load during measurements.
- Fix: write `textContent` only when the value changes; or disconnect the observer around `enhance()`; or ignore mutations whose target is inside `.paxs-telemetry`.
- Confidence: high from code; a Performance trace would confirm it.

### N8 🟡 XHR auth-retry is non-functional: a completed XHR is sent again

- Where: `paxs-study-list-enhance.js:1003-1019`. The `load` listener (1007-1017) calls `origSend.apply(xhr, args)` at 1015 after `paxsRefreshToken()` resolves. `setRequestHeader` is in a try/catch (1013-1014), but `origSend` is not.
- Quote: `xhr.addEventListener('load', function () { ... paxsRefreshToken().then(function (tok) { ... origSend.apply(xhr, args); }); });`
- What is wrong: by the time `load` fires, `readyState` is DONE (4). `send()` on a DONE XHR throws `InvalidStateError`. The throw happens inside the promise, is not caught, and the retry never happens. The replay code runs only after the 401 has already reached the caller.
- Failure scenario: if the OHIF build uses XHR for WADO-RS (not verified), a 401 on a frame still gives a black viewport; the V1-1 claim in this file does not hold for XHR.
- Fix: re-issue with `origOpen` (re-open the same method and URL) before `send`, or replay with `fetch`.
- Confidence: high for the code path; whether OHIF uses XHR is open (see section 5).

### N9 🟡 MPR overlay shows placeholder patient and series text, which is never replaced

- Where: `paxs-mpr.js:1019` `'<div class="mpr-corner-tl" id="' + id + '-ctl">PATIENT: DICOM<br>MOD: CT</div>'` and `:1020` `'... id="' + id + '-ctr">SERIES: 1<br>ACR STANDARD</div>'`. Grep: no writes to `-ctl` or `-ctr` anywhere. `updateHud` writes only `-cbl` and `-cbr` (2874-2883). Header comment (35-36) says the overlay carries "Patient Name/Modality".
- What is wrong: every MPR panel shows "PATIENT: DICOM", "MOD: CT", "SERIES: 1", "ACR STANDARD" regardless of the study. The volume info does not include patient name (`volume_service.py:480-505`), so the overlay is static text. Screenshots or exports of the MPR carry a fake identity.
- Failure scenario: a clinician screenshots an MPR panel for a consult; the image carries "PATIENT: DICOM" and "SERIES: 1" and is filed or compared with the wrong study.
- Fix: populate from real data (`series_description`, `series_instance_uid`, `study_instance_uid` from `state.info`, with textContent), or hide the corners until populated. Do not ship placeholder identity text.
- Also: the "POS" value at 2876 is `index * spacing` from slice 0, not the patient slice location (IPP). The label "Physical Location" in the header comment (35-36) overstates it.
- Confidence: high.

### N10 🟡 (latent) Patient-space basis transposes X and Y (DICOM orientation vectors mapped to the wrong voxel axes)

- Where: `paxs-mpr-geometry.js:262-279`. `rowVec = orientation[0..2]` (265-267) and `colVec = orientation[3..5]` (268-270). Basis (274-278): `[colVec[0]*s.col, rowVec[0]*s.row, normal[0]*s.slice]` and similarly for y and z. So the i-column is `IOP[3:6]*colSpacing` and the j-column is `IOP[0:3]*rowSpacing`.
- What is wrong: DICOM defines `IOP[0:3]` as the direction of increasing column index (i) and `IOP[3:6]` as the direction of increasing row index (j). The spacing for i is `PixelSpacing[1]` (column), and for j is `PixelSpacing[0]` (row). The backend documents IOP as "(row direction cosines, column direction cosines)" (`volume_service.py:81-82`) and PixelSpacing as `[row, column]` (`volume_service.py:65-66`). The geometry header (16-19, 25) says "i = column index (patient X for an axial acquisition)". With the standard axial `IOP = [1,0,0, 0,1,0]`, the code maps i -> +Y and j -> +X.
- Current impact: none on displayed numbers today. Every in-plane length, angle, Cobb, ratio and ellipse uses `M^T M = diag(col², row², slice²)`, which is invariant under swapping the first two columns, so measurements are correct. Any absolute patient coordinate (`voxelToPatient`) is transposed.
- Fix: `i` column from `IOP[0:3]*colSpacing`, `j` column from `IOP[3:6]*rowSpacing`. Add a unit test with `IOP=[1,0,0,0,1,0]` and one oblique case (the module is Node-requireable, header 5-8).
- Confidence: high on the mapping; measurement invariance is derived algebraically.

### N11 🟢 Silent 1 mm spacing default

- Where: `paxs-mpr-geometry.js:86-95` (`num(spacing.row, 1)`, `num(ps[0], 1)`, `num(info.slice_spacing ..., 1)`); `paxs-mpr.js:1320, 1349, 1478, 1520` (`|| 1`). The backend returns `slice_spacing` as the mean position delta, falling back to thickness (`volume_service.py:407-415, 498`), so the 1 mm default only applies when both are missing (rare).
- What is wrong: a missing spacing is replaced with 1 mm with no warning, which silently distorts mm measurements and sagittal/coronal proportions.
- Fix: fail or warn when `pixel_spacing` or slice spacing is absent; show "spacing unknown" in the HUD.

### N12 🟢 `initGl2` leaks a WebGL context on its early-return paths

- Where: `paxs-mpr.js:2054-2057` (`max3D` exceeded, `return`), `:2076-2079` (texture upload failed, `return`), `:2087-2090` (program link failed, `return`). The `renderCv` context is never lost; it lives until GC.
- Failure scenario: repeated series switches on a device where the texture or program fails each time can hit the ~16 live-context limit. The next viewer then fails with "too many active WebGL contexts".
- Fix: call `WEBGL_lose_context.loseContext()` on each early return (the same pattern `releaseGl2` uses).

### N13 🟢 Worker and main-thread decode disagree for float32 rescale

- Where: worker code string (`paxs-mpr.js:1626-1627`): `if (dtype === "float32") { vol = new Float32Array(buf, 0, count); }` with no rescale. Main-thread `decodeVolume` (1723-1724) also returns the raw view, but control falls through to the rescale at 1754-1756 (`vol[i] = vol[i]*slope + intercept`).
- What is wrong: for float32 data with non-identity `rescale_slope` or `rescale_intercept`, the worker path and the main-thread path give different HU values.
- Fix: apply the same rescale in both paths, or reject non-identity rescale for float32.

### N14 🟢 Telemetry reports the wrong WebGL version and a score as GB

- Where: `paxs-mpr.js:3812-3816` (`webgl2: !!caps.webgl2`, `memoryGb: caps.memory`). `probeClientCapabilities` (298-344) sets `webgl` to 0 or 30 and never sets `webgl2`. `memory` is a 0-15 points score (336), not GB.
- Downstream: `paxs-toolbar-enhance.js:542` shows "GPU: WebGL1" on WebGL2 devices. `paxs-about.js:266-268` and `:351-353` show "WebGL1 only · degraded" when an MPR session exists.
- Fix: return `webgl2` from the probe; rename `memory` as points or report `deviceMemory` in GB.

### N15 🟢 Shortcut and About text describes features that are not implemented

- Shortcut modal (`paxs-mpr.js:1044-1051`): "1 - 5 : W/L Presets", "Tab : Cycle Layouts", "Shift + Drag : Pan View", "Scroll Wheel : W/L Adjust / Zoom". Implemented: wheel changes slice (3147-3158), drag changes W/L (3098-3103), keys handled are Esc, ?/h, Space, c, r, o, b, t, arrows (1100-1138). No digit, Tab, shift, zoom or pan handling exists (grep).
- Header claim (`paxs-mpr.js:38`): "Speed Selector (5 FPS, 15 FPS, 30 FPS)". Implemented: fixed 66 ms interval (120, 1094), no selector in `buildPanel`.
- Slab projections: `projectionMode` and `slabThickness` are never set from the UI (grep: defaults only at 123-124). The MIP/MinIP/AvgIP shader path (1762-1791, 2213-2217) is unreachable.
- About (`paxs-about.js:334-337`): "Zoom: Right drag", "Pan: Shift + drag". Header (20-24): "Traps pointer / wheel / keyboard events at the capture phase", but the code traps pointer, wheel, touch and click in the bubble phase (416-431) and handles keyboard only for Escape and Tab (449-460). Other OHIF hotkeys still reach the viewer while About is open.
- Fix: implement or remove each claim; keep the About text in sync with the code.

### N16 🟢 Replay-token guard matches URLs by substring and does not check origin

- Where: `paxs-study-list-enhance.js:951-957` (`u.indexOf(PAXS_AUTH_RETRY_PATHS[i]) !== -1`), used in the fetch guard (967) and XHR guard (1006). Retry adds `Authorization: Bearer <token>` (983, 1013) to whatever origin returned 401.
- Failure scenario: a cross-origin request whose URL contains `/dicom-web/` and which returns 401 receives the bearer token on the replay. Unlikely (the app does not normally call such URLs), but it is a disclosure path.
- Fix: `new URL(url, location.href).origin === location.origin` before retrying.

### N17 🟢 Latent innerHTML of error text; `decodeURIComponent` on query strings can throw

- `paxs-mpr.js:3772` `body.innerHTML = '... MPR Workspace error: ' + msg + '</div>'`, where `msg` can be `e.message` (callers at 1262, 1547). Current messages embed only constants and encodeURIComponent'd UIDs, so no XSS today. Use `textContent`.
- `decodeURIComponent(match[1])` at `paxs-mpr.js:796`, `paxs-study-list-enhance.js:538` and `:707`. A malformed `%` in the URL throws an uncaught URIError and the workspace or modal does not open. Wrap in try/catch.

### N18 🟢 Changed scripts kept at `?v=37`, cached for 24 h

- Where: `index.html` keeps `paxs-mpr.js?v=37` and `paxs-study-list-enhance.js?v=37` although both changed. `app/main.py:161` (`DEFAULT_CACHE = "public, max-age=86400"`) and `:192` apply to non-hashed `.js` (`_cache_control_for`, 173-192).
- Failure scenario: returning users keep the old study-list script for up to 24 h, including the removed H-11 global `getContext` patch. That re-enables `preserveDrawingBuffer` on every WebGL context until the cache expires. Mixed versions are also possible (new index, old mpr.js).
- Fix: bump the query string for every changed script (`paxs-mpr.js`, `paxs-study-list-enhance.js`, and `paxs-mpr-geometry.js`, which is new). Or use content hashes.

### N19 🟢 Three.js loader polls forever if the first script never settles

- `paxs-mpr.js:211-214`: `setInterval(..., 30)` waits for `window.THREE` with no timeout. If the local script neither loads nor errors, the interval never clears.
- Fix: add a timeout (for example 15 s) and fall back to the 2D path.

### N20 🟢 Dead stale-series guard in the raw-plane fetch

- `paxs-mpr.js:2763`: `if (state.seriesUid !== (key.split('|')[0] || state.seriesUid)) return;`. `key` is built from the current series, so the comparison is always false and the guard never fires. The stale response is still written to `state.serverRaw` and passed to `cb`. Reads are key-checked (3126-3128, 3215-3219), so no wrong HU value is shown, but the guard should be removed or fixed.

### N21 🟢 Dead code: `serverImageRect` (2774-2780) is never called. `renderServerPlanes` and `drawSvgOverlays` do not use it. This is the root of N4.

### N22 🟢 `set3dUnavailable` wipes the 3D DOM and looks up the wrong element id

- `paxs-mpr.js:2783-2787` replaces `#mpr-3d-wrap` innerHTML (removes the `mpr-3d-cv` canvas and the 3D toolbar). `:2789` looks up `document.getElementById('mpr-layout')`, but the select id is `mpr-layout-select` (833). So the "(server mode)" disable never applies and `sel.value` is never reset.
- Downstream: `init3dEngine` returns early when `mpr-3d-cv` is missing (3437-3439). After a server or context-loss fallback, 3D is not restored in that session, even when split or client rendering resumes (`degradeToCoarseClient` calls `init3dEngine` at 2500 and gets nothing).
- Fix: keep the canvas in the DOM (hide it instead of replacing innerHTML) and fix the id.

---

## 4. Documentation deltas (what a user can do now)

Viewer (OHIF header, `paxs-toolbar-enhance.js`):
- Toolbar icons restyled and grouped by category (nav, measure, W/L, annotation). `aria-label` drives classification (lines 51-78); the icon table is in 91-243.
- Telemetry pill at the right of the header: processing mode (Server, GPU Client, Hybrid, Idle), with tooltip showing RTT, client score and GPU (546-584). Uses the MPR bridge `window.__getPaxsMprState` (3793-3828) and the negotiate header `X-PAXS-Processing-Mode`. Note N14 on GPU label.

3D MPR workspace (`paxs-mpr.js`):
- Launch: "⚡ 3D MPR" button inserted before "View" in each study-list row (608-628). Study UID from the `StudyInstanceUIDs`/`study` URL parameter or the first study row (644-646, 791-807). Series list from QIDO series (1231-1263); series with more than 5 instances, sorted by instance count; the largest is selected by default.
- Layouts (6 + 4-grid): `4grid`, `1x2`, `1x3`, `3d`, `axial`, `sagittal`, `coronal` (833-841, 1062-1079).
- W/L presets: Soft Tissue, Brain, Lung, Bone, Angio (825-829). Drag to change W/L (3098-3103). Wheel changes slice (3147-3158).
- Cine: toggle button; 66 ms interval (about 15 fps). No speed selector (N15).
- LOD toggle: during interaction the GPU draws at half resolution (2199-2201; LOD timer 2811-2816).
- Clinical tools (keys: C, R, O, B, T): crosshair (click to move, 3046-3055); caliper in mm via `computePhysicalLength` (3081-3083 for CTR, 2938 for ruler); elliptical ROI with mean, SD, min, max HU and area in cm² (3044-3055, 3161-3202; in server mode the stats show "sampling" until the raw plane arrives, 3210-3266); Cobb angle from two lines (3076-3079); CTR ratio from two lengths (3080-3084); clear all (934-941).
- PET/CT fusion: "Fusion" toggle lists other series in the same FoR from `/api/v1/fusion/{study}`; colormap and blend slider (944-978). See N2 for alignment.
- 3D viewport: cut planes, VRT raymarch, MIP 3D; transfer functions (bone, angio, soft tissue, MIP); ISO/anterior/superior/left camera presets (1009-1012); drag to orbit, wheel to dolly (3502-3532). See N3 for orientation.
- Processing modes: client GL (default with WebGL2), split (coarse x4 + server fine slices), server (thin client, no 3D). Fallback on server error or context loss (2131-2157, 2460-2504, 2738-2776 in server flow). Badge shows the resolved mode (462-492).
- Hotkeys: Space cine, arrows slice, C/R/O/B/T tools, Esc close, ?/H shortcuts modal (1100-1138). See N15 for claimed but absent keys.

Study list and report (`paxs-study-list-enhance.js`):
- Report modal ("Diagnostic Report" in the viewer footer, 396-402, 530-685): loads study metadata and templates, saves DRAFT or FINALIZED. Finalize is shown only to ADMIN or RADIOLOGIST (client gate 634-646). The server enforces the role (reports.py:217-220) and sends ORU on finalize (reports.py:261-263). Requires sign-in (get_current_user_any). Print opens `/api/v1/reports/print/{uid}` in a new tab (cookie auth).
- Viewer footer: attribution, Terms & Conditions alert, "Viewer Guide" link to `/paxs-viewer-guide` (exists, `app/main.py:938`). Only in viewer URLs (342-346).
- Sidebar: series cards get modality icons, first series auto-selected, "Primary" tab selected (702-807).
- User chip with name, role and Sign out in the header (1053-1105). Sign-out calls `POST /api/v1/auth/logout` and clears `paxs_token`.
- Thumbnails: unchanged logic, with an LRU of 64 blob URLs (68-90). Still index-matched (F16).

About dialog (`paxs-about.js`):
- Intercepts the header "About" menu item and shows a clinical-workstation modal (535-547): platform version, processing telemetry (mode, score, RTT), GPU renderer and WebGL level (N14), active dataset UIDs and voxel spacing from `window.__getPaxsMprState`, standards badges (DICOM 3.0, WADO-RS, WebGL2 MPR, HTJ2K Lossless), keyboard reference (N15), clinical notice. Closes on Escape, backdrop click or close button.

Viewer engine (`index.html`):
- Global WebGL `preserveDrawingBuffer` override removed (H-11). The MPR read-back canvas requests `preserveDrawingBuffer` on its own context (2044-2049). Side effect: any other canvas read-back in the OHIF bundle (for example capture/download) may now produce blank images (section 5).

APIs used by the new features (summary): `/api/v1/volume/{uid}/{info,negotiate,data,mpr/slice,tiles}`, `/api/v1/fusion/{study}`, `/dicom-web/studies/...` (QIDO and WADO-RS thumbnail), `/api/v1/reports/{study/{uid},save,print/{uid}}`, `/api/v1/templates?modality=`, `/api/v1/auth/{me,refresh,logout}`, `/paxs-viewer-guide`.

---

## 5. Open questions

1. N1: reproduce on a real series in client GL mode (512x512 with fewer than 512 slices). Confirm the sagittal and coronal panels show axial content.
2. N8: does the OHIF bundle use `XMLHttpRequest` or `fetch` for WADO-RS? This decides whether the XHR retry gap matters.
3. Does the OHIF bundle's capture/download (toolbar "Capture"/"Download") read back a canvas? If so, H-11 may have made those exports blank. Not verified; the bundle was not inspected.
4. N10: confirm the backend's i/j convention against a real oblique or sagittal series. Confirm that `slice_spacing` sign and order match the basis normal.
5. N2: what is the product intent for PET/CT fusion? The manifest says "registered: true, IDENTITY", but the viewer does not apply geometry.
6. N3: verify the 3D cut-plane orientation visually.
7. N7: confirm the loop with a Performance trace (mutations per second while the viewer is open).
8. Is `/viewer/viewer.html` (`app/static/viewer.html`, cdnjs three.js with no SRI) still deployed? Is `silent-refresh.html` (OIDC, cdnjs `es5-shim`) used? Both look like legacy files.
9. Does `about` dispatching a synthetic `pointerdown` on `document.body` (`paxs-about.js:485-487`) trigger OHIF outside-click handlers that change tool state? Not verified.
10. `paxs-study-list-enhance.js` (1121 lines, numstat +246/-66) and `paxs-mpr.js` (numstat +1778/-178) match the brief; the brief's "+312" for study-list was the `git diff --stat` total including deletions.
11. The backend commit touches 107 files (`git diff --stat e90d32c HEAD -- app/`). Only the report, volume, auth and deps paths relevant to V2b were checked. Other backend changes were not reviewed here.
