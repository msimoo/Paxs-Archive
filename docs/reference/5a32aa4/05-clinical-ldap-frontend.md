# Clinical APIs, LDAP, sharing, settings and frontends — changes in 5a32aa4

> **Re-review of commit `5a32aa4`** (baseline `e90d32c`), part of the [PAXS Archive documentation](../../README.md). Static analysis, 2026-10-08; line numbers refer to `5a32aa4`. Baseline area reference: [06-clinical-ihe-ai.md](../06-clinical-ihe-ai.md), [07-web-ui-static.md](../07-web-ui-static.md), [08-study-list-ui.md](../08-study-list-ui.md). Findings are consolidated and prioritized in [CODE_REVIEW.md](../../CODE_REVIEW.md), which takes precedence where the two differ. Finding IDs here (N1, N-01, V4-01, …) are local to this file; CODE_REVIEW.md maps them to NEW-xx IDs.

- Baseline: e90d32c (old) -> HEAD 5a32aa4 (new). Repo: C:\Users\dody1\source\repos\Paxs-Archive
- Method: static reading of HEAD files plus `git diff e90d32c HEAD` per scoped file. No code was executed, no tests were run, no app was started. Anything marked "verify" was not confirmed by execution.
- Severity: 🔴 exploitable, or clinical-record integrity / PHI exposure. 🟡 significant defect, regression or gap. 🟢 hardening or low impact.
- Line numbers refer to HEAD. Repo was not modified.
- Not in V4 and not re-reviewed: app/static/viewer/index.html, viewer.js rendering internals, routing/lossy/split-merge internals (gates only), license.py, auth.py.

---

## 0. Headline

- Confirmed fixed: anonymous `/reports/save` and template writes (S-03), unauthenticated FHIR Patient/ImagingStudy (S-04), lifecycle purge and writes (S-08), the LDAP mock backdoor and the provisioned-password hash (S-06), the print-page XSS (S-09 print part), the viewer finalize role gate (F-15), share creation tenant check (03-F8), React simulated actions (D-08), and the dev proxy port (08).
- New 🔴: finalizing an existing draft signs the stored draft, not the text on screen, and records the draft author as signer (V4-01).
- New 🔴: report read and write routes have no tenant scope (V4-02).
- New 🔴: LDAP simple-bind passwords are not protected in transit. `LDAP_USE_TLS` is declared and documented but never read (V4-03).
- Still 🔴: S-09 DOM XSS in app.js inline `onclick` handlers (V4-04).
- New 🟡 regression: anonymous share-link recipients cannot load study metadata, because `GET /api/v1/studies/{uid}` now requires a bearer token or session cookie (V4-11).

---

## 1. Change summary

### 1.1 Backend, per file

| File | What changed (HEAD) |
|---|---|
| app/api/v1/reports.py | Added addenda (`POST /{id}/addendum`, destructive role), `addenda` in `GET /{id}`, `require_study_access` on `/study/{uid}` and `/print/{uid}`. `/save` now requires auth (`get_current_user_any`), takes the signer from the actor, and enforces a destructive role for finalize (reports.py:217-220). Finalize goes through `report_service.finalize_report` (ORU). Print page HTML-escapes every interpolated value (reports.py:320-343). |
| app/services/report_service.py | `add_addendum` (FINALIZED only, reports.py:127-140), `list_addenda`, `ReportAddendum` import. `update_report` remains DRAFT-only (111-112). |
| app/api/v1/fhir.py | `get_current_user_any` on Patient search/read and ImagingStudy search/read. Tenant predicate `_tenant_predicate` (68-81), applied to search (155-158, 218-219) and direct reads (196-197). `/fhir/metadata` stays anonymous (120). |
| app/api/v1/ldap.py | `POST /api/v1/ldap/login` (rate limited, 60-64). 503 when not configured (71-75), generic 401 on failure, audit on every outcome, JWT returned in the body (no cookie). |
| app/services/ldap_service.py | Fail-closed directory auth. Mock backdoor removed (14-18). `NO_LOCAL_PASSWORD_HASH` for provisioned users (38, 295). Group-to-role map (62-94, 121-134). Group lookup by `uid=` search (140-163). Role is overwritten on existing rows (287-291). |
| app/core/config.py | `LDAP_SERVER`, `LDAP_BASE_DN`, `LDAP_USER_DN_TEMPLATE`, `LDAP_GROUP_ROLE_MAP`, `LDAP_DEFAULT_ROLE`, `LDAP_TIMEOUT_SECONDS`, `LDAP_USE_TLS` declared (263-282). `LDAP_USE_TLS` is unused (V4-03). |
| app/api/v1/share.py | `POST /api/v1/share` now calls `require_study_access` (38). `GET /api/v1/share/{token}` sets a `paxs_share` cookie (72-82). `share_url` takes the request (V2-2). `DELETE` gated by destructive role (140). |
| app/services/sharing_service.py | Token `secrets.token_urlsafe(32)` (52). Origin via `resolve_public_base_url(request)` (72-79). |
| app/api/v1/settings.py | Org settings GET now auth; POST `require_admin` (133). Logo validated with Pillow (`_save_validated_logo`, 38-75): PNG/JPEG/WEBP only, 2 MiB cap, uuid filename. Templates: GET auth (164, 204), write `require_admin` (186, 223, 246). |
| app/api/v1/scripts.py | All routes `require_admin` (unchanged gates). Filesystem and exec work moved to `asyncio.to_thread`. |
| app/api/v1/dashboard.py | `/health` now `require_admin` (235-239), body extracted to `health_payload` (148-232). `/stats` auth (243-244). `/studies` auth only, no optional user (485). `/studies/{uid}` (769-772), instances (824-828) and thumbnail (851-855) use `require_study_access` or `require_series_access`. Presign uses `require_instance_access` (887-894). `DELETE /studies/{uid}` tenant-scoped (915-918). `/stats` adds `viewer_processing` telemetry (332-). `/studies` adds `patient_birth_date` and `patient_sex` (683-684). |
| app/api/v1/config.py | `GET /api/v1/config` embeds `readonly = health_payload()` (81-86) for any authenticated user. PATCH `require_admin`. |
| app/api/v1/nodes.py | Node create, update and delete are ADMIN-only inline (98, 124, 150). Ping is off the event loop (179). `/send` keeps destructive role (189), tenant scope (212), synchronous C-STORE (214-217). |
| app/api/v1/worklist.py | `WRITE_ROLES` now imported from `app.core.roles` (23). Create and update gated by `_require_write` (80-82, 135, 165). Delete ADMIN-only (197). List has no tenant predicate (102-129). |
| app/api/v1/lifecycle.py | All write routes `require_admin` (86, 118, 137, 148, 164, 175, 186). Purge was destructive, now admin. Reads still `get_current_user`. |

Unchanged in this diff (confirmed absent from `git diff --name-status`): migration.py, worklist_tasks.py, peer_review.py, critical_results.py, pix_pdq.py. Their role gates are re-checked in section 2.

Also changed, outside V4 but gates checked for S-08: routing.py (writes admin, 88/130/150; evaluate and queue run destructive 164/189/201), lossy_compression.py (writes destructive 88/131/158/172), split_merge.py (destructive 48/84).

### 1.2 Routes touched in V4 (METHOD path | auth at HEAD | notes)

Reports (`/api/v1/reports`):
- POST `` | get_current_user (reports.py:82) | draft create; `ReportIn.radiologist` accepted from body (41), passed to service (report_service.py:88).
- GET `` | get_current_user (101) | no tenant filter, `status` and `study_uid` filters.
- GET `/{id}` | get_current_user (109) | includes `addenda` (114-115). No tenant check.
- PATCH `/{id}` | get_current_user (121) | `ReportPatch.radiologist` accepted (48). DRAFT only via service (111-112).
- POST `/{id}/finalize` | require_destructive_role (138) | ORU via finalize_report.
- POST `/{id}/resend` | require_destructive_role (154) | FINALIZED only, HL7 enabled.
- GET `/study/{uid}` | get_current_user_any + require_study_access (181-184) | was anonymous.
- POST `/save` | get_current_user_any (197); finalize role gate (217-220); no `require_study_access` | was get_optional_user.
- POST `/{id}/addendum` | require_destructive_role (279) | new; FINALIZED only; `text` 1..20000 chars (271).
- GET `/print/{uid}` | get_current_user_any + require_study_access (302-304) | was anonymous; HTML.

Settings / templates:
- GET `/api/v1/settings/organization` | get_current_user_any (99) | was anonymous.
- POST `/api/v1/settings/organization` | require_admin (133) | logo via `_save_validated_logo` (138).
- GET `/api/v1/templates`, GET `/{id}` | get_current_user_any (164, 204) | was anonymous.
- POST, PATCH, DELETE `/api/v1/templates[/{id}]` | require_admin (186, 223, 246) | was anonymous or optional user.

LDAP:
- POST `/api/v1/ldap/login` | rate limit (63); no user dependency | 503 / 401 / 200.

Share:
- POST `/api/v1/share` | get_current_user (36) + require_study_access (38) | tenant-scoped now; `expiry_days` 1..365 (29).
- GET `/api/v1/share/{token}` | anonymous (63) | returns patient name and ID, study, series. Sets `paxs_share` (httponly, SameSite=Lax, Secure per AUTH_COOKIE_SECURE, max-age clamped to 1 day, deps.py:153-162).
- GET `/api/v1/share/{token}/qr.png` | anonymous (128).
- DELETE `/api/v1/share/{token}` | require_destructive_role (140) | no tenant check (V4-22).

Dashboard / studies:
- GET `/api/v1/health/live`, `/health/ready` | anonymous (127, 133).
- GET `/api/v1/health` | require_admin (236) | was anonymous.
- GET `/api/v1/stats` | get_current_user_any (243-244) | was anonymous; archive-wide counts (V4-20).
- GET `/api/v1/studies` | get_current_user_any (485) | tenant-scoped (507-520).
- GET `/api/v1/studies/{uid}` | get_current_user_any (770) + require_study_access (772) | was anonymous. Share recipients now blocked (V4-11).
- GET `/api/v1/studies/{uid}/series/{s}/instances` and `/thumbnail.png` | get_current_user_any + require_series_access (824-828, 851-855) | was anonymous.
- GET `/api/v1/storage/presign/...` | get_current_user (889-890) + require_instance_access (898) | was anonymous.
- DELETE `/api/v1/studies/{uid}` | require_destructive_role (916) + require_study_access (918) | tenant-scoped; does not check for FINALIZED reports (V4-09).
- GET `/api/v1/config` | get_current_user (56) | returns `readonly` health block to all users (V4-10).

FHIR (`/fhir`):
- GET `/Patient` (141, auth 148, tenant 155-158), `/Patient/{id}` (183-185, tenant check 196-197), `/ImagingStudy` (202, auth 208, tenant 218-219), `/ImagingStudy/{uid}` (273-275, require_study_access). `/metadata` anonymous (120).

Nodes:
- GET `/api/v1/nodes` (90), ping (163) get_current_user. Create, update and delete ADMIN inline (98, 124, 150). `/send` destructive (189).

Scripts: all routes require_admin (36, 45, 52, 63, 75, 85).

### 1.3 Report states and transitions

- States: DRAFT, FINALIZED (`VALID_STATUSES`, report_service.py:34). Alias: `SIGNED` is accepted by `/save` and treated as FINALIZED (reports.py:214).
- DRAFT -> FINALIZED: `finalize_report` sets status, `finalized_at`, and `radiologist` only if empty (report_service.py:324-327). Triggered by `/finalize` (destructive) or `/save` with status FINALIZED/SIGNED (destructive gate at reports.py:217).
- FINALIZED -> DRAFT: no route. `update_report` (111-112) and `add_addendum` (133) enforce the state.
- Addenda: POST `/{id}/addendum` (destructive, FINALIZED only), append-only through the API (no edit or delete route). Not included in ORU (report_service.py:127-140 and build_oru_message 169-251 do not read addenda), not in the print template (reports.py:345-530), not in any UI (no reference to addenda in app/static or study-list-ui/src).
- oru_status: PENDING (finalized with HL7 disabled, report_service.py:339), SENT or FAILED (`_deliver`, 303-313). `resend_report` (350-363) re-sends for FINALIZED reports when `HL7_EMR_ENABLED`.
- Delivery is synchronous MLLP over plain TCP (report_service.py:257-292, socket at 272, timeout 5 s). A timeout returns ok=false, so a delivered message can be recorded as FAILED.

### 1.4 LDAP configuration and flow

Settings (config.py:263-282; .env.example:197-206):
- `LDAP_ENABLED=false`; `LDAP_SERVER` (e.g. `ldaps://dc01...` or `ldap://dc01:389`, config.py:273); `LDAP_BASE_DN`; `LDAP_USER_DN_TEMPLATE=uid={username},{base_dn}`; `LDAP_GROUP_ROLE_MAP` (format `CN=Radiologists=ADMIN;CN=Doctors=PHYSICIAN`); `LDAP_DEFAULT_ROLE` (empty = deny); `LDAP_TIMEOUT_SECONDS=5`; `LDAP_USE_TLS=false` (unused, V4-03).
- Runtime-editable through `/api/v1/config` PATCH (settings_service.py:64-94): LDAP_ENABLED, LDAP_SERVER, LDAP_BASE_DN, LDAP_USER_DN_TEMPLATE, LDAP_GROUP_ROLE_MAP, LDAP_DEFAULT_ROLE. Readable by every authenticated user through GET (V4-10).
- `ldap3` is commented out in requirements.txt:35. Without it the login is denied with a warning (ldap_service.py:188-194).

Flow (ldap.py:60-107):
1. Rate limit (63). 503 if `is_ldap_configured()` is false (71-75).
2. `authenticate_ldap_user` (ldap_service.py:166-242): empty password refused (174); simple bind with DN from template (203-211); group lookup (`_search_groups`, 225); `resolve_role` (226), first mapped group wins, else `LDAP_DEFAULT_ROLE` or deny (227-233).
3. Failure: audit `login_failed` (79) and generic 401 (80).
4. Success: `sync_ldap_user_to_db` (ldap.py:83). New row gets `NO_LOCAL_PASSWORD_HASH` (ldap_service.py:293-300). Existing row: role and full_name overwritten, password hash untouched (287-291).
5. `is_active` check (89-91), then commit and audit `login_success` (93-94). Response is a bearer JWT and `auth_provider`. No refresh cookie is set.
6. No UI path: login.html has no LDAP branch, and app/static has no call to `/api/v1/ldap/login`. Cookie-gated pages (main.py:384-400) redirect LDAP sessions to /login (V4-12, open question 8).

### 1.5 React study-list: new and changed API calls

All calls read the bearer token from `localStorage('paxs_token')`. None handle 401 (08 finding still open). Base path comes from `appBasePath()` (api.ts:18-29).

| Call (api.ts) | Server route | Used by | Behaviour |
|---|---|---|---|
| fetchStudies (42-66) | GET /api/v1/studies | use-studies | throws on !ok |
| deleteStudy (68-74) | DELETE /api/v1/studies/{uid} | DeleteModal.tsx:28 | throws on !ok, real delete |
| fetchStudyDetail (111-118) | GET /api/v1/studies/{uid} | StudyDetailDrawer.tsx:55 | throws on !ok; keys match dashboard.py:788-820 |
| fetchStudyReport (130-138) | GET /api/v1/reports/study/{uid} | StudyDetailDrawer.tsx:56 | returns null on any !ok (V4-19) |
| fetchNodes (155-162) | GET /api/v1/nodes | ForwardModal.tsx:30 | enabled nodes only |
| forwardStudy (169-189) | POST /api/v1/nodes/{ae}/send, body `{study_uids:[uid]}` | ForwardModal.tsx:54 | returns body unused (V4-14) |
| createShare (200-220) | POST /api/v1/share, body `{study_uid, expiry_days}` | ShareModal.tsx:33 | runs on modal open and on expiry change (V4-15) |
| StudyListPage: GET /api/v1/auth/me (59), POST /api/v1/auth/logout (69) | cookie and bearer | header chip, logout | logout redirects to absolute `/login` (76) |

Viewer link: `getViewerUrl` (76-78) uses `appBaseUrl()` and encodes the UID. Drawer "Diagnostic Report" and the header button call `window.paxsOpenReportModal` (StudyDetailDrawer.tsx:119, 220, 228; StudyListPage.tsx:129), which is defined by paxs-study-list-enhance.js:687.

Other React changes: PacsViewerDashboard.tsx deleted (no remaining imports, grep). Vite dev proxy 8000 (vite.config.ts:21-25). `select.tsx` throws in dev on empty SelectItem value (H17, 112-122). `types/study.ts` adds `patient_birth_date`, `patient_sex`. No `dangerouslySetInnerHTML` or raw HTML sinks in study-list-ui/src (grep).

### 1.6 Legacy static frontends

- app/static/app.js: `viewerUrl` now prefixes `PAXS_BASE_PATH` (326-337). `CFG_PANELS` adds `viewer` (655). Boot calls `/api/v1/health` before login (4390) and after (4354), now admin-only (V4-21). `viewerLink` and inline `onclick` unchanged (V4-04).
- app/static/index.html: adds a Viewer settings panel (tab button 766, panel 800) that loads `/api/v1/config` (app.js:752) and PATCHes admin-only.
- app/static/studies-ui/index.html: rebuilt shell (script order and asset hashes; whole-file diff, likely line endings).
- app/static/admin/admin-bridge.js: 401 redirect carries `PAXS_BASE_PATH` (40).
- app/static/viewer.js: back-button redirect now carries `PAXS_BASE_PATH` (274). Share branch (118-125) calls `/api/v1/share/${token}` unencoded (120) and then `/api/v1/studies/{uid}` (139) (V4-11, V4-23).
- app/static/viewer/paxs-study-list-enhance.js: report modal (540-686). Save and Finalize post `/api/v1/reports/save` (651-680). Print opens `/api/v1/reports/print/{uid}` (647-649). Textareas and labels set via textContent or static strings (no XSS sink in the new code).

---

## 2. Prior findings status

| ID | Status | Evidence (HEAD) | Notes |
|---|---|---|---|
| S-03 | FIXED (new defects in V4-01, V4-17) | `/save` requires auth (reports.py:197); finalize role gate (217-220); FINALIZED not overwritten (report_service.py:111-112; reports.py:249-255). Org POST `require_admin` (settings.py:133). Templates writes `require_admin` (186, 223, 246). Upload restricted to validated logo (settings.py:38-75). | Anonymous and any-extension upload gone. |
| S-06 | PARTIAL | Backdoor removed: `domainpass` and `ldap_*` no longer present in app (ldap_service.py:14-18). `LDAP_SERVER` declared (config.py:273). Provisioned hash replaced (ldap_service.py:38, 295). Role overwrite still open (ldap_service.py:287-291). | V4-12, V4-03 (TLS), V4-13. |
| S-08 | PARTIAL | Lifecycle writes admin (lifecycle.py:86, 118, 137, 148, 164, 175, 186). Routing writes admin (routing.py:88, 130, 150). Node writes ADMIN inline (nodes.py:98, 124, 150). Lossy writes destructive only (lossy_compression.py:88, 131, 158, 172). Split and merge destructive (split_merge.py:48, 84). | See item list below. |
| S-08 (item: migration folder) | OPEN (high) | migration.py:31-32 (`folder_path`, `delete_after_import`), 55-60 `get_current_user`, 64-71 passes straight through. No path allowlist. | Any logged-in user can import from any server path and delete originals after import. |
| S-08 (item: worklist-task assign and stat) | OPEN | worklist_tasks.py:69-84 `get_current_user` (unchanged file). | |
| S-08 (item: peer review assign and score) | OPEN | peer_review.py:65-69, 117-122, 134-139 `get_current_user` (unchanged). | |
| S-08 (item: critical-result acknowledge) | OPEN | critical_results.py:95-100 `get_current_user` (unchanged). | |
| S-08 (item: PIX identity writes) | OPEN for POST `/identities` (pix_pdq.py:204-208 `get_current_user`). AA create, AA delete and identity delete already `require_admin` (109-113, 185-189, 273-278). | | |
| S-09 | OPEN (XSS). Print part FIXED. | app.js:399-401 `onclick="openForward('...','${esc(...)}')"`; 1467 same pattern; viewerLink 342 and 398, 1490 use raw UID; esc at 103-106 does not defeat JS-string context (V4-04). Print escaping: reports.py:320-343. | Residual reports.py:515 (V4-28). |
| F-15 | FIXED (new defect) | Role gate reports.py:217-220; finalize via `finalize_report` with ORU (260-267). | Finalize-on-draft integrity defect: V4-01. |
| S-04 (FHIR part) | FIXED | fhir.py:141-150, 183-199, 202-226, 273-279 auth; tenant predicate 68-81 and checks 155-158, 196-197, 218-219. | `/fhir/metadata` anonymous by design (120). |
| 03-F8 / F13 (share and images) | FIXED | Share creation tenant-scoped (share.py:38). Share-scoped DICOMweb and series reads (deps.py:226-313) with `DICOMWEB_PUBLIC` default False (config.py:427). | Recipient metadata regression: V4-11. |
| D-08 (React simulated actions, fake URL, fabricated demographics and report text) | FIXED for the listed items | api.ts:68-220 real calls; DeleteModal.tsx:28-30; ForwardModal.tsx:54-56 (but see V4-14); ShareModal.tsx:33 (server URL and QR); StudyDetailDrawer.tsx:79-81, 101-103 use real data or "Unknown"/"N/A". | New residuals: V4-14, V4-15, V4-16, V4-19. |
| 08: null `patient_name` crash | OPEN | `study.patient_name.replace` at StudyTable.tsx:102, 233; DeleteModal.tsx:53; ForwardModal.tsx:79; ShareModal.tsx:74; StudyDetailDrawer.tsx:95; StudyListPage.tsx:135. Backend returns the raw value (dashboard.py:680) and the model is nullable (models.py:207). Type says `string` (types/study.ts:3). | Crashes table rows and modals. |
| 08: sort key `patient_name` | OPEN | Frontend sends `sortKey="patient_name"` (StudyTable.tsx:172). Backend matches `"patient"` (dashboard.py:604), so falls through to date sort. | |
| 08: Vite proxy port | FIXED | vite.config.ts:21-25 uses 8000. | |
| 08: token in localStorage, no 401 handling | OPEN | api.ts:59, 69, 112, 131, 156, 174, 205 read `paxs_token`. StudyListPage.tsx:55-65 same. No refresh or logout on 401. | |
| 08: TS strict off | OPEN | tsconfig.app.json has no `strict` key (verified by grep and read). | |
| 08: header "Diagnostic Report" opens studies[0] | FIXED | StudyListPage.tsx:124-131 bound to `selectedStudy`, disabled when none (132). | |
| 06 F-10: signer name client-supplied | PARTIAL | `/save` ignores body radiologist (reports.py:232-233). Still accepted by POST `/api/v1/reports` (reports.py:41, 80-95; report_service.py:88) and PATCH `/api/v1/reports/{id}` (reports.py:48, 118-133; report_service.py:113). Finalize keeps the stored signer (report_service.py:327). | V4-01 is the same root cause. |

Other prior items re-checked:
- Upload validation: FIXED for the logo (V4-27 notes).
- Migration, worklist_tasks, peer_review, critical_results, pix_pdq: unchanged in diff. Role gates as listed above.

---

## 3. New findings

Order: 🔴, then 🟡, then 🟢.

### 🔴 V4-01 Finalize on an existing draft signs stored content under the draft author's name

- Location: reports.py:245-251 (existing-report branch), 260-267 (finalize); report_service.py:323-327.
- Quote (reports.py:249): `if not wants_final:` wraps `report_service.update_report(db, report, fields)`. For FINALIZED or SIGNED requests the submitted findings, impression and radiologist are never written.
- Quote (report_service.py:327): `report.radiologist = report.radiologist or performed_by`.
- What is wrong: the UI sends the edited findings with status FINALIZED (paxs-study-list-enhance.js:651-657). When a draft already exists, the server finalizes the stored row and discards the payload. The signer is the stored `radiologist`, which was set to the draft author (reports.py:236-244 or 228-234). The UI then alerts "finalized and signed successfully" (676). The response returns stored text, but the client ignores it.
- Failure scenario: Radiologist A saves a draft. Radiologist B opens the editor, changes Findings, and clicks "Finalize & Sign" without a prior Save Draft. The server signs A's draft text, sends that text in ORU, and the printed signature block shows A. B is told the edited text was signed. The same happens if A edits and B finalizes.
- Fix: for wants_final, apply `fields` and call `update_report` before finalizing, in the same transaction. In `finalize_report`, set `radiologist` to the actor (or record a separate `finalized_by`). Return the stored findings to the client and show them before signing. Add a test: draft by A, edit and finalize by B, assert the finalized text and signer are B's.

### 🔴 V4-02 Report read and write routes have no tenant scope

- Location: reports.py:98-115 (list, get), 118-133 (PATCH), 136-161 (finalize, resend), 193-267 (`/save`, esp. 223), 274-297 (addendum). Service: report_service.py:95-106.
- Quote (reports.py:101): `user: User = Depends(get_current_user)`. Only `/study/{uid}` (184) and `/print/{uid}` (304) call `require_study_access`.
- What is wrong: `list_reports` filters only by status and study UID. `get_report` takes an integer ID. `save_report` calls the unscoped `get_study_report_metadata` (223) and then creates or updates a report for any study UID. Finalize and resend check only the role.
- Failure scenario (when VNA tenants are used, see open question 1): a TECHNOLOGIST in tenant A calls `GET /api/v1/reports` and receives findings, impression and patient_id for all tenants. IDs are sequential, so `GET /api/v1/reports/{id}` enumerates them. A RADIOLOGIST in tenant A calls `POST /api/v1/reports/save` with tenant B's study UID, finalizes it, and triggers an ORU to the EMR for tenant B's patient. The legacy dashboard already calls `GET /api/v1/reports?study_uid=` (app.js:480), so the same leak exists there.
- Fix: resolve the report's study with `require_study_access` on every report route (404 when out of tenant), and filter `list_reports` by tenant (join Study). Add `tests/test_tenant_isolation`-style cases for each route. Mirror the existing pattern at reports.py:184.

### 🔴 V4-03 LDAP passwords are not protected in transit; TLS setting is dead

- Location: ldap_service.py:196-211 (Server, Connection, bind); config.py:273, 282; .env.example:197-206.
- Quote (ldap_service.py:200): `use_ssl=str(settings.LDAP_SERVER).lower().startswith("ldaps://")`. No `start_tls()` call and no `tls=` argument on the Server or Connection.
- Quote (config.py:282): `LDAP_USE_TLS: bool = False  # startTLS on ldap:// (ldaps:// implies TLS)`. Grep for `LDAP_USE_TLS` finds only config.py:282 and .env.example:206. It is never read.
- What is wrong: the simple bind sends the user's password in cleartext for any `ldap://` URL, and the documented `LDAP_USE_TLS` flag does nothing. For `ldaps://` the code sets no CA file and no `CERT_REQUIRED`, so certificate verification depends on ldap3 defaults. Verify the default in the pinned ldap3 version; if it is CERT_NONE, a network attacker can impersonate the DC and capture passwords.
- Failure scenario: an operator follows config.py:273 and uses `ldap://dc01:389` with `LDAP_USE_TLS=true`, believing StartTLS is on. Every login's password crosses the network in cleartext.
- Fix: when `LDAP_USE_TLS` is set, call `conn.start_tls()` before bind and fail closed if it fails. Refuse `ldap://` without StartTLS. Pass `ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_file=...)` with a new `LDAP_CA_CERT_FILE` setting. Tests in tests/test_ldap.py mock Server and Connection and do not assert either.

### 🔴 V4-04 S-09 still open: inline onclick handlers with server text (DOM XSS in admin dashboard)

- Location: app.js:103-106 (`esc`), 342 (`viewerLink`), 398-401 (row buttons), 1467 (report badge), 1490 (worklist buttons).
- Quote (app.js:399): `onclick="openForward('${encodeURIComponent(s.study_uid)}', '${esc(s.patient_name || "Patient")}')"` (same pattern at 400-401 and 1467). Quote (app.js:342): `onclick="location.href=viewerUrl('${studyUid}')"`, with raw `studyUid`.
- What is wrong: `esc()` HTML-escapes `'` to `&#39;`. Inside a double-quoted HTML attribute the browser decodes entities before the JavaScript runs, so the escaped quote becomes a real quote inside the handler's JS string literal. Escaping for HTML does not protect a JS string context. Patient names come from DICOM headers, which can arrive over C-STORE from modalities or other nodes.
- Failure scenario: a received study with a crafted PatientName reaches the admin study list. Clicking its row button runs script in the admin's session. The admin's bearer token is in localStorage (app.js:22, 52), so it can be read.
- Fix: stop placing server text in inline handlers. Put the UID in `data-*` attributes (HTML-escaped) and bind handlers with `addEventListener` or delegation, then read `dataset` in the handler. Apply the same to `viewerLink` and the worklist rows. Add a test with a PatientName containing quotes and angle brackets.

### 🟡 V4-05 ORU builder does not escape CR or LF; segment injection and raw newlines

- Location: report_service.py:40-47 (`_ESC_MAP`, `_esc`), 233 (OBX-5), 251 (segment join).
- Quote (report_service.py:40): `_ESC_MAP = {"\\": "\\E\\", "|": "\\F\\", "^": "\\S\\", "&": "\\T\\", "~": "\\R\\"}`. CR and LF are not in the map.
- What is wrong: the HL7 segment terminator is CR, and the message is `"\r".join(segments)` (251). A CR in findings splits the message into extra segments. LF is not a segment terminator in HL7 v2, but many parsers treat it as one, and multi-line findings (textarea LF) are sent raw. HL7 escapes line breaks as `\.br\` (or `\X0D\`).
- Failure scenario: a TECHNOLOGIST (who may draft, reports.py:82 and 197) saves findings containing CR followed by `OBR|...`. A RADIOLOGIST finalizes. The ORU carries injected segments, which the EMR may act on. Separately, normal multi-line findings may be parsed incorrectly by the EMR.
- Fix: escape `\r` and `\n` as `\.br\` (or `\X0D\`) and strip other control characters in `_esc`. Add unit tests for `build_oru_message` with CR, LF and `|` inside findings.

### 🟡 V4-06 Double-click or concurrent finalize can send two ORU messages

- Location: report_service.py:317-341 (check, then `_deliver`, commit at 341); reports.py:260-263; paxs-study-list-enhance.js:643-646 (no in-flight guard).
- What is wrong: `finalize_report` reads the row without a lock, checks `already_sent`, sends MLLP, and only then commits (341). Two threads can both see DRAFT or not-SENT and both send. Control IDs are random (report_service.py:187), so the EMR sees two distinct messages. The docstring at reports.py:208-210 promises idempotency, which holds only for sequential calls.
- Failure scenario: a double-click on "Finalize & Sign" sends two POST `/save` requests while the first MLLP send (5 s timeout) is still running. Both send ORU.
- Fix: take a row lock (`SELECT ... FOR UPDATE`) or do a conditional UPDATE to a `SENDING` state and commit it before the socket call. Disable the finalize button while the request is in flight. Return 409 to concurrent callers.

### 🟡 V4-07 Print page shows a fake FINALIZED report when none exists; drafts are not marked

- Location: reports.py:310-336 (`rpt = data.get("report") or {}`, fallbacks at 333-336), 515 (status cell), 518 (signature), 520 (date). Print button: paxs-study-list-enhance.js:647-649.
- Quote (reports.py:515): `{rpt.get('status', 'FINALIZED')}`. Quote (reports.py:333): `findings = esc(rpt.get("findings") or "No findings recorded.")`.
- What is wrong: when the study has no report, `rpt` is `{}`, so the page prints "Report Status: FINALIZED", "No findings recorded.", and a signature block "Authorized Radiologist / Consultant Radiologist". The print button works from the editor before anything is saved (paxs-study-list-enhance.js:647). For a DRAFT, the page shows "DRAFT" in the status cell, but the date falls back to `created_at` (336) and nothing watermarks the page as unsigned.
- Failure scenario: a radiologist clicks Print before drafting. The printout looks like a signed report with a finalized status.
- Fix: return 404 or an explicit "NO REPORT" page when there is no report. Add a DRAFT watermark, and label the date as created or finalized accordingly. Escape the status value.

### 🟡 V4-08 Addenda are not shown, printed, or sent; finalize and addendum are not audited

- Location: reports.py:274-297 (addendum), 114-115 (GET includes addenda), 345-530 (print template, no addenda), report_service.py:127-152 (add and list), 169-251 (ORU builder, no addenda). No `add_audit` call in reports.py or report_service.py (grep). Comment at report_service.py:125 says the original "stays intact and auditable".
- What is wrong: an addendum is written, but no UI reads it (grep: no `addend` in app/static or study-list-ui/src), printed reports omit it, and the EMR never receives it. Finalize, addendum and resend write no AuditLog row. The only record is `radiologist`, which V4-01 can overwrite in practice.
- Failure scenario: a RADIOLOGIST adds a correction to a signed report. The printed copy and the EMR still show the original findings, and no audit entry records who finalized or amended.
- Fix: include addenda in print and in an ORU correction (or NTE segments). Add UI to view and add addenda. Write AuditLog rows for finalize, addendum, resend and ORU outcome, with actor and timestamp.

### 🟡 V4-09 Deleting a study cascades to signed reports and addenda

- Location: dashboard.py:914-918 (`delete_study`; no RadiologyReport check, grep shows none in that route), models.py:1432 (`studies.id` FK `ondelete="CASCADE"` on `reports.study_id`), models.py:1483 (`reports.id` FK `ondelete="CASCADE"` on addenda).
- What is wrong: a RADIOLOGIST (destructive) can delete a study with a FINALIZED report, which removes the signed report, its addenda and its ORU delivery state. No audit row records the report deletion.
- Failure scenario: a radiologist deletes a studied patient's study to clear the worklist and erases the signed record and its amendments.
- Fix: refuse delete when a FINALIZED report exists, or require ADMIN and write an audit row, or archive the report instead of cascading.

### 🟡 V4-10 `/api/v1/config` exposes the admin-only health block and LDAP settings to any user

- Location: config.py:55-56 (`get_current_user`), 81-86 (`readonly = await health_payload()` returned to every caller). dashboard.py:235-236 (`/api/v1/health` is `require_admin`). settings_service.py:64-94 (LDAP keys in the editable set, so they are returned as values).
- What is wrong: the same payload that is now admin-only at `/api/v1/health` is returned by `/api/v1/config` to TECHNOLOGIST or PHYSICIAN accounts. It includes DICOM AE title, host and port (dashboard.py:175-180), HL7 host and port (185-189), the TLS certificate path (181-184), the S3 endpoint and bucket (190-196), and the service matrix. Current values of `LDAP_SERVER`, `LDAP_BASE_DN` and `LDAP_GROUP_ROLE_MAP` (group-to-role mapping, including admin group DNs) are also returned.
- Failure scenario: a TECHNOLOGIST reads the admin health block and the LDAP admin group mapping, then maps those group names into an attack plan against directory accounts.
- Fix: return `readonly` only when `user.role == "ADMIN"`, or drop it for non-admins. Hide LDAP_* value fields from non-admin responses.

### 🟡 V4-11 Regression: anonymous share-link recipients cannot load study metadata

- Location: viewer.js:118-125 (share branch), 139 (`api(/api/v1/studies/${encodeURIComponent(studyUid)})`), 130-134 (catch shows "Failed to load study"). dashboard.py:768-772 (`get_current_user_any` plus `require_study_access` with no share scope). deps.py:104-116 (`get_current_user_any` accepts bearer or `paxs_session` only, not `paxs_share`).
- What is wrong: at HEAD the share viewer obtains `study_uid` from `/api/v1/share/{token}` and then calls `/api/v1/studies/{uid}`. That route no longer accepts the share cookie, so it returns 401, `detail.patient` is undefined, and the viewer falls into the catch block. The same applies to `/series/{s}/instances` and `/thumbnail.png` (dashboard.py:824-828, 851-855). Only DICOMweb and volume routes accept share scope (deps.py:226-313). Code-read only, not executed.
- Failure scenario: a patient receives a QR code, opens it without an account, and sees "Failed to load study".
- Fix: pass share scope to `require_study_access` and `require_series_access` on these routes (`get_share_scope(request)`, as in deps.py:227-270). Add a test for an anonymous share request to `/api/v1/studies/{uid}`.

### 🟡 V4-12 LDAP login overwrites the role of existing local accounts, including disabled ones

- Location: ldap_service.py:287-291 (`user.role = role` on an existing row, password hash untouched). ldap.py:82-91 (sync before the `is_active` check), 43-57 (`_audit` commits, line 55), 89-91 (refusal path).
- What is wrong: `sync_ldap_user_to_db` adopts any row with the same username, regardless of whether it is a directory-managed account. The role changes, and the local password still works because the hash is untouched. For a disabled account, the sync flushes the role change, then the `is_active` refusal calls `_audit`, which commits the session. The refused login still persists the role change.
- Failure scenario: local admin `dr.x` exists. A directory account with the same uid maps to PHYSICIAN, so the local admin is downgraded and can still log in with the local password. Separately, an admin disables an account; an LDAP attempt with that name silently changes its role, and when re-enabled it has the directory role.
- Fix: refuse to adopt rows whose `password_hash != NO_LOCAL_PASSWORD_HASH`, or require an explicit directory-managed flag. Check `is_active` before sync. Do not commit role changes on refusal paths (use a separate audit session).

### 🟡 V4-13 LDAP group discovery drops the groups it finds; tests bypass the production path

- Location: ldap_service.py:140-163. Quote (155-160): `member_of = getattr(result, "memberOf", None)` then `groups.extend(...)`. Filter at 150-154 (`(|(member=...)(uid=...))`).
- What is wrong: for results of the `member=` search, the code reads `memberOf` of the result entries, which are the matching group objects. It never adds the matched group's DN or CN. For directories without an overlay that exposes `memberOf` on the user, lookup returns nothing, so role resolves to the default or denies. The default `uid=` filter (config.py:275) also does not match AD `sAMAccountName`. tests/test_ldap.py:52-55 makes `bind()` populate `entries` with `memberOf`, which real ldap3 does not do (entries are empty until a search). The `search()` mock (58-59) returns True and sets nothing. The production search branch is therefore untested.
- Failure scenario: a valid directory user with a mapped group is denied (or gets only the default role) on a non-overlay OpenLDAP or on AD.
- Fix: include `entry_dn` and CN of matching groups in the result. Make the search mock populate `entries` and assert the path. Support a `sAMAccountName` or UPN template for AD and document it.

### 🟡 V4-14 ForwardModal reports "Queued" whatever the server returned

- Location: ForwardModal.tsx:54-56 (`await forwardStudy(...)` then `onToast('Queued for forwarding to ...')`). api.ts:169-189 (returns body verbatim, unused). nodes.py:213-236 (synchronous C-STORE; returns 200 with `ok:false` and `failed>0`; 404 only when nothing was attempted).
- What is wrong: the transfer is synchronous, so "Queued" is inaccurate. Failures return HTTP 200 with `ok:false` and a `reason`, and the modal still shows success. api.ts:164-167 says "never assume delivered" but the caller does.
- Failure scenario: C-STORE to a node fails (`failed:1`). The radiologist sees "Queued for forwarding" and assumes the study reached the destination.
- Fix: read `ok`, `sent` and `failed` from the response. Show "Sent N, failed M" or the reason. Reword "Queued" unless the backend really queues.

### 🟡 V4-15 ShareModal creates a public patient link on open and on every expiry change

- Location: ShareModal.tsx:27-44 (effect on `[studyUid, isOpen, expiryDays]`, calls `createShare` at 33). api.ts:200-220. share.py:33-59 (creates the share, writes AuditLog "SHARED" at 44-48, returns `qr_base64`). No revoke in the UI; DELETE exists at share.py:138-144 (destructive role).
- What is wrong: each open creates a new share row and public URL with the default 1-day expiry. Each change of the dropdown creates another. The effect cancels aborted fetches client-side, but the server still processes the request, so aborted requests leave rows. There is no revoke when the modal closes.
- Failure scenario: a user opens Share to look at the options, closes it, and leaves an active patient link. Changing the expiry three times leaves three live links.
- Fix: create the link only on an explicit "Create link" click. Revoke the link on close, or show existing links and let the user revoke.

### 🟡 V4-16 DeleteModal success leaves a stale row

- Location: StudyListPage.tsx:97-99 (`handleDeleteConfirm` only shows a toast). StudyListPage.tsx:29-43 destructures from `useStudies()` but not `refresh`. use-studies.ts:89 exposes `refresh: loadData`. DeleteModal.tsx:22-23 says the caller refetches; DeleteModal.tsx:28-29 calls `onConfirm` after the server confirms.
- What is wrong: the deleted study stays in the table until a manual refresh. A second click on the stale row returns 404, shown as "Failed to delete study (404)".
- Fix: call `refresh()` in `handleDeleteConfirm` (add `refresh` to the destructuring).

### 🟡 V4-17 Print escapes branding HTML, so default header and footer show as literal markup (regression)

- Location: reports.py:322-331 (header and footer wrapped in `esc`), report_service.py:381-382 (default `header_html_text` and `footer_html_text` are HTML: `<div style='text-align:center;'><h2>...`).
- What is wrong: the change is deliberate in the comment (313-319), but the built-in defaults are HTML. Every printed report now shows raw tags as text in the header and footer. Admins who entered HTML branding see the same result.
- Fix: keep plain-text branding (escape) and render a fixed template for the header, or run an allowlist sanitizer on branding HTML. Update the default values to plain text.

### 🟡 V4-18 Worklist listing has no tenant scope

- Location: worklist.py:102-129 (`list_steps`, joins Patient, no tenant predicate). The grep for `tenant` in worklist.py returns nothing.
- What is wrong: any authenticated user lists every scheduled procedure with patient name, ID and accession across tenants. C04 scope is applied to studies and FHIR but not here.
- Fix: join Patient and filter `Patient.tenant_id` with `tenant_scope(user)`, following the FHIR pattern (fhir.py:68-81).

### 🟡 V4-19 `fetchStudyReport` hides every error; drawer then says "no clinical text recorded"

- Location: api.ts:130-137 (`if (!res.ok) return null`). StudyDetailDrawer.tsx:56 (`.catch(() => null)`), 215-219 (message "This report has no clinical text recorded yet").
- What is wrong: a 401, 403 (tenant), 404 or 500 looks identical to "no report text". The drawer shows a clinical-looking statement when the actual state is unknown.
- Fix: throw on !ok (except 404 for "no report") and show the error in the drawer.

### 🟢 V4-20 `/api/v1/stats` gives any authenticated user archive-wide counts

- Location: dashboard.py:243-244 (auth only). Counts for all patients, studies, users, shares, audit events and reports (251-326). Tenant totals at 327-330. `reader_counts(db, None, include_all=True)` at 309.
- What is wrong: tenant-level totals and counts cross tenant boundaries. Low PHI content.
- Fix: scope counts by `tenant_scope(user)`, or restrict the endpoint to ADMIN.

### 🟢 V4-21 `/api/v1/health` is now admin-only; non-admin public base URL and docs break

- Location: dashboard.py:235-236. app.js:4390 (pre-login call, `.catch` swallows it), 4354 (after login, guarded by `health.ok`), 335-336 (fallback to origin plus prefix). Docs: deploy/REVERSE-PROXY.md:200, 209 and deploy/LOAD-BALANCING.md:40 use `/api/v1/health` with curl.
- What is wrong: the public `PAXS_PUBLIC_BASE_URL` is no longer populated for guests and non-admins, so viewer deep-links use the origin fallback. Docs and monitoring examples now get 401/403. Not a security defect; a behaviour change.
- Fix: keep `/api/v1/health` for admins and add a public, minimal `/api/v1/health/public` with `public_base_url` only. Update the docs.

### 🟢 V4-22 Share hygiene

- `DELETE /api/v1/share/{token}` (share.py:138-144) has no tenant check. Any RADIOLOGIST can revoke another tenant's links.
- `get_share_info` increments `accessed_count` on every GET, including link previews (share.py:67).
- Token is in the query string (`/viewer?share=`, deps.py:164-175; main.py:377-380 bypasses the session gate on any `share=`). Tokens land in access logs and `Referer` headers.
- `share_url` derives the origin from `X-Forwarded-Host` or `Host` when `PUBLIC_BASE_URL` is unset (sharing_service.py:72-79; config.py:535-590).
- Fix: tenant-scope delete. Count accesses on first load only. Move the token out of the query (for example POST exchange, cookie-only). Require `PUBLIC_BASE_URL` in production.

### 🟢 V4-23 viewer.js does not encode the share token in the path

- Location: viewer.js:120: `api(`/api/v1/share/${shareToken}`)`.
- What is wrong: `shareToken` is raw from `location.search`. A crafted `share=../studies/<uid>` makes the browser resolve another GET path, sent with the viewer user's bearer token (api.ts-equivalent `api()` in app.js:63-78 attaches it). The response is only rendered as viewer metadata; no exfiltration channel found.
- Fix: `encodeURIComponent(shareToken)` and validate the token format client-side.

### 🟢 V4-24 LDAP hardening

- Filter built from raw username (ldap_service.py:152: `(|(member={_user_dn(username)})(uid={username}))`). Escape per RFC 4515. Exploitation needs a DN that resolves at bind, so impact is low.
- `conn.unbind()` is never called after a successful bind (ldap_service.py:203-225). One socket per login, left to GC.
- `LDAPLoginRequest.username` has no length bound (ldap.py:38-40).
- `resolve_role` returns the first mapped group (ldap_service.py:121-134). With several mapped groups, precedence follows directory order. Choose the highest role instead.

### 🟢 V4-25 Races that yield 500 instead of 409

- `create_report`: concurrent creates pass the `existing` check (report_service.py:72-78), then the unique `study_id` constraint raises IntegrityError (models.py:1432). The handler catches only ValueError (reports.py:236-244), so it returns 500.
- LDAP first login with the same username concurrently: the unique username constraint (models.py:109) raises IntegrityError in `sync_ldap_user_to_db` (ldap_service.py:293-300). Unhandled.
- `create_script` checks then writes (scripts.py:89-98), a TOCTOU that is admin-only.

### 🟢 V4-26 Scripts: source endpoint skips the name regex; admin code execution by design

- `GET /api/v1/scripts/{name}/source` (scripts.py:74-80) does not validate `name` with `_NAME_RE` (applied on create at 87). Path reads are limited to `*.py` under the scripts directory and admin-only.
- `PUT /api/v1/scripts/{name}` (83-102) writes arbitrary Python and calls `reload_scripts`. Admin code execution is the documented feature. Record this as an accepted risk in the docs.

### 🟢 V4-27 Logo upload: whole-body read before the cap; no cleanup; relies on Pillow limits

- Location: settings.py:138 (`await logo_file.read()` loads the whole body before `_save_validated_logo` checks the 2 MiB cap at 49-53). settings.py:72-73 (files accumulate; old logos never removed). Pillow default `MAX_IMAGE_PIXELS` is the only pixel-bomb guard (verify the version).
- What is wrong: validation is good (decode via Pillow `verify`, format allowlist, uuid name, no SVG/HTML, nosniff from global headers). Gaps: memory use before the cap, no retention cleanup, EXIF retained.
- Fix: stream-read with a size limit, delete the previous logo on replace, strip metadata by re-encoding.

### 🟢 V4-28 Minor output escaping

- reports.py:515 `{rpt.get('status', 'FINALIZED')}` is raw HTML (DB-sourced; low risk).
- app/static/settings/index.html:738-739 renders `header_html_text` and `footer_html_text` with `innerHTML` in the live preview (admin-only input).

### 🟢 V4-29 Frontend and FHIR path details

- StudyListPage.tsx:76 `window.location.href = '/login'` ignores the mount prefix (V2-3). Same for `/#nodes` and `/#workflow` (StudyListPage.tsx:144, 152, 160).
- Thumbnail URLs are absolute paths (dashboard.py:814), so they ignore the mount prefix. Drawer `<img>` tags (StudyDetailDrawer.tsx:246-247) cannot send the bearer header, so they work only with the session cookie.
- fhir.py:28 `SERVER_BASE` is computed at import from `resolve_public_base_url()` with no request, so bundles point at the configured or loopback origin.

---

## 4. Documentation deltas

Report lifecycle (replace the old single-save description in docs/reference/06-clinical-ihe-ai.md):
- States DRAFT and FINALIZED; `SIGNED` is an alias on `/save`. One-way: no route returns to DRAFT.
- Create: POST `/api/v1/reports` (any authenticated user; `radiologist` accepted from body, see V4-01) or POST `/api/v1/reports/save` (any authenticated user; 404 unknown study; 409 when report exists in conflict paths).
- Edit: PATCH `/api/v1/reports/{id}` and `/save` with DRAFT (DRAFT only; 409 otherwise).
- Finalize: POST `/api/v1/reports/{id}/finalize`, or `/save` with FINALIZED or SIGNED (ADMIN or RADIOLOGIST; with an existing draft the submitted text is ignored, V4-01).
- Addendum: POST `/api/v1/reports/{id}/addendum` (ADMIN or RADIOLOGIST; FINALIZED only; 1-20000 chars; append-only; not sent to EMR; not printed; not in UI).
- Resend: POST `/api/v1/reports/{id}/resend` (ADMIN or RADIOLOGIST; FINALIZED; HL7 enabled).
- Reads: GET `/api/v1/reports` (no tenant, V4-02), GET `/api/v1/reports/{id}` (includes addenda), GET `/api/v1/reports/study/{uid}` and `/print/{uid}` (tenant-scoped, HTML print).
- oru_status: PENDING, SENT, FAILED. MLLP over plain TCP.

LDAP login flow and settings (docs/reference/04-auth-admin-ops.md):
- Endpoint: POST `/api/v1/ldap/login`, rate limited; 503 when not configured; generic 401; success returns a bearer JWT, no cookie.
- Settings: as in section 1.4. `LDAP_USE_TLS` is documented but not implemented (V4-03). `ldap3` must be installed (requirements.txt:35 commented).
- Provisioning: new rows get `NO_LOCAL_PASSWORD_HASH`. Existing rows have their role overwritten (V4-12).
- Groups: first mapped group wins; default role or deny. `uid=` search only (V4-13).

Upload rules (settings):
- Only the organization logo is accepted, through POST `/api/v1/settings/organization` (ADMIN). PNG, JPEG or WEBP, decoded and verified by Pillow. Maximum 2 MiB. Saved as `/static/uploads/logo_<32-hex>.<png|jpg|webp>`. SVG, HTML, GIF and other formats are rejected (422). The client filename is ignored.
- No other upload endpoints exist in app/api/v1 (grep). The license upload at license.py:110 is separate and not reviewed here.

Share flow:
- Create: POST `/api/v1/share` (bearer or cookie via `get_current_user`; bearer only in practice; tenant-scoped; `expiry_days` 1..365, default `SHARE_EXPIRY_DAYS`). Returns token (urlsafe 32 bytes), `url` (from `resolve_public_base_url`), `qr_base64`, `expires_at`. AuditLog "SHARED".
- Open: GET `/api/v1/share/{token}` anonymous. Sets `paxs_share` (httponly, SameSite=Lax, Secure per AUTH_COOKIE_SECURE, max-age up to 1 day). Returns patient name and ID, study, series.
- Viewer: `/viewer?share=...` bypasses the session gate (main.py:377-380). DICOMweb and volume reads are scoped to that study when `DICOMWEB_PUBLIC=false` (default). Study metadata endpoints do not accept share scope (V4-11).
- Revoke: DELETE `/api/v1/share/{token}` (ADMIN or RADIOLOGIST; no tenant check, V4-22).

React UI actions now wired to APIs (docs/reference/08-study-list-ui.md):
- Delete: DELETE `/api/v1/studies/{uid}` (destructive). List not refreshed (V4-16).
- Forward: GET `/api/v1/nodes` then POST `/api/v1/nodes/{ae}/send` (destructive). Shows "Queued" regardless (V4-14).
- Share: POST `/api/v1/share` on open and on expiry change (V4-15). Link and QR from server.
- Drawer: GET `/api/v1/studies/{uid}` and GET `/api/v1/reports/study/{uid}`. DOB, sex and report text are real or explicitly "Unknown" or "N/A".
- Header and drawer "Diagnostic Report": `window.paxsOpenReportModal(uid)` from paxs-study-list-enhance.js; header button is bound to the selected study (H16).

---

## 5. Open questions

1. Are VNA tenants (tenant_id on Study and Patient) enabled in production? V4-02 and V4-18 are 🔴 and 🟡 only if they are.
2. Is `DICOMWEB_PUBLIC` false in production (config default at config.py:427)? Share scoping and V4-11 assume it.
3. Is LDAP used in production? Which directory (OpenLDAP with memberOf overlay, AD, other)? Is LDAPS CA pinned? Verify the ldap3 `Tls` default validate mode in the pinned version (V4-03).
4. Is the HL7 MLLP channel to the EMR protected by TLS or a private network? The code sends plain TCP (report_service.py:272).
5. Should addenda be sent to the EMR (ORU correction) and shown in print? Is an addendum expected from RADIOLOGIST only, or also ADMIN?
6. Is the removal of anonymous `/api/v1/studies/{uid}` (dashboard.py:768-772) intended? Share recipients break (V4-11). Was there a plan for share-scoped metadata?
7. Is the finalize path meant to accept edits in the same call (the UI sends them), or should finalize only use stored content? V4-01 assumes the former.
8. LDAP sessions have no `paxs_session` cookie (ldap.py returns only the JWT). Are LDAP users expected to use the browser UI, and if so, how do they pass the cookie gate (main.py:384-400) and the print window (`window.open`, which cannot send headers)?
9. Should `/api/v1/config` return the `readonly` health block and LDAP values to non-admins (V4-10)?
10. Should `/api/v1/reports` (list) and `POST /api/v1/reports` be limited to a radiologist role, not any authenticated user?
11. Is a synchronous C-STORE the intended behaviour behind the "Queued" wording (V4-14)?
12. What does the team expect for `delete_study` on a FINALIZED report (V4-09)?
