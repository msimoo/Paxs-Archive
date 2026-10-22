# PACS_Sys — Admin console and clinical portal

> **Area analysis of PACS_Sys** for the [PAXS Archive vs PACS_Sys comparison](../../COMPARISON_PACS_Sys.md). Static, read-only analysis of the `PACS_Sys` repository at HEAD `2ebdbf4` **plus its uncommitted working tree** (2026-10-10). Nothing was built, run or modified. Secret values are never reproduced. Severity labels (🔴/🟡/🟢, RED/YELLOW, CRITICAL/MAJOR) are the analyst's; the comparison document carries the consolidated view.

**Repo:** `C:\Users\dody1\source\repos\PACS_Sys` (read-only pass; nothing built, installed, run or modified). `frontends/apps/admin` and `frontends/apps/clinical` were clean in git at read time.
**Method:** static reading with Read/Grep/Glob. Every claim cites file:line. Nothing was executed, no browser was driven, so items marked **(verify)** need runtime confirmation.
**Coverage:** all admin `src` pages, components and utilities; all clinical pages and the library modules that matter; shared `frontends/packages/{api-client,auth,ui,deep-link}` read where the apps depend on them; BFF read only to classify real vs adapter behaviour. Clinical `timezoneOptions.ts`, `FirstRunTour`, `HelpDrawer`, `ColumnConfigPanel`, `ReadingQueueRail`, `ReportMacros`, `compareStudies` were skimmed, not fully read.
**Intent docs skimmed:** `pacs-admin-console-screens.md`, `pacs-clinical-portal-screens.md`, `UX_DIRECTION.md`, `UX_AUDIT_2.md` (headings plus targeted lines only).
**Path convention:** `clinical/src/…` and `admin/src/…` are under `frontends/apps/`; `packages/…` is under `frontends/`; `bff-svc/src/…` is the BFF. Bare BFF module names without a `src/` segment (for example `clinical/accessPolicy.ts`, `reportService.ts`, `arcLightAdminClient.ts`) are under `bff-svc/src/`. Line numbers marked `~` are approximate; all others were checked with Read or Grep.

---

## 0. Headline facts

1. **The admin console is mostly a local MVP.** Almost every configuration and state page is served from in-memory `Map` seeds in the BFF: AE titles, report templates, reporting config, storage, retention, routing, MWL, queues, HL7 applications and users (`bff-svc/src/admin/arcLightAdminClient.ts:108-241`, `configAdminClient.ts:45-53`, `routingAdminClient.ts:42`, `mwlAdminClient.ts:45`, `queueAdminClient.ts:318`, `hl7AdminClient.ts:55`, `keycloakAdminClient.ts:25`). The real parts are the archive study search (QIDO), health/metrics probes when configured, and the file audit trail. `ADMIN_ADVANCED_MODE` is hard-wired to `local_mvp` (`bff-svc/src/config.ts:63,166`).
2. **The clinical portal reads real archive studies but writes to BFF file state.** Study lists come from the archive (`bff-svc/src/arcLightClient.ts:178`). Worklist assignment and priority, notes, share links, report drafts and signed revisions live in BFF state (`CLINICAL_STATE_MODE=file` in the dev env). The SR STOW write is **mock by default** (`REPORT_SR_STORE_MODE=mock`, `arcLightClient.ts:161-167`), yet the UI still reports "Signed … SR SOP …".
3. **🔴 Signing can sign an older autosaved draft.** `ReportAuthorPage.tsx:502` uses `draft ?? saveReportDraft(current sections)`. The sign buttons are not gated on dirty state (`:900-902`). The BFF signs the stored `draft.sections` (`reportService.ts:127`). The local mirror is then cleared (`:512`), so edits typed after the last autosave can vanish from both places.
4. **🔴 External-report URLs are not scheme-checked, and the default iframe sandbox allows script with same-origin.** Open-in-HIS, PDF and iframe templates are only checked for the `{ACC}` token (`arcLightAdminClient.ts:576,588,593`). Default sandbox flags are `allow-scripts allow-same-origin` (`:223`). The clinical app renders those values in `href` and `<iframe>` (`ReportPanel.tsx:66-70`).
5. **🔴 Anonymize and move report success without doing work.** `anonymizeStudy` returns `status:'accepted'` with the message "Study anonymization job queued." (`arcLightAdminClient.ts:370-372`). Move does the same with a hard-coded `DCM4CHEE` destination (`:374-376`; `StudiesPage.tsx:236`).
6. **🔴 (verify) The DICOMweb path has no auth at the edge.** The Traefik `dicom-web` router has only CORS and security-header middleware (`infra/traefik/dynamic/routers.yml:16-22`). Thumbnails are loaded by `<img>`, which cannot send the bearer token (`StudyThumbnail.tsx:76`; `dicomwebLinks.ts:51-55`). Archive-side auth must be checked.
7. **🟡 Tokens and PHI sit in `localStorage` with no sign-out.** The OIDC user object is stored in `localStorage` (`packages/auth/src/index.ts:19`). Neither app has a sign-out control. Report text, patient names and MRNs are persisted per user. "Reset preferences" deletes every `pacs:` key, including unsent report drafts (`packages/ui/src/ErrorBoundary.tsx:34-46,134-136`).
8. **🟡 Two places claim behaviour the code does not have.** The Users page says Keycloak is the identity source (`UsersPage.tsx:224`), but writes go to an in-memory Map (`keycloakAdminClient.ts:25-72`). Reason text typed in five confirmation dialogs is never sent (`UsersPage.tsx:95-207`, `ShareLinksPage.tsx:179-183`).
9. **🟡 Share links are not recipient-bound on the public landing.** The email check runs only when the caller supplies an email (`bff-svc/src/clinical/shareLinkService.ts:154-156`), and `/v/s/:token` is public (`auth.ts:65`, `app.ts:239-240`). The token is in the URL path (`shareLinkService.ts:77`), although the page copy says links are "without placing bearer tokens in URLs" (`ShareLinksPage.tsx:210`).
10. **Strengths:** server-side role guards are consistent (admin writes, worklist, sign, share revoke). There are no `innerHTML`, `dangerouslySetInnerHTML` or `eval` sinks in `frontends/`. CSV formula injection is guarded. Destructive actions use typed phrases and re-display the affected record. Dialogs have a focus trap with an inert root. Tables use roving tabindex. The service worker caches the shell only, never API or DICOMweb responses.

---

## 1. Architecture

| Area | Finding | Evidence |
|---|---|---|
| Stack | React ^18.3.1, Vite, TypeScript (`tsc -b`). Apps consume workspace packages through Vite aliases to package source. Dev ports 5175 (admin) and 5176 (clinical). | `frontends/apps/{admin,clinical}/package.json`; `frontends/apps/admin/vite.config.ts:14-31` |
| Routing | Hash routing with no router library. Admin matches a fixed `NAV_ITEMS` list (15 routes). Clinical parses `#/path?query` with `URL`, and matches `/patients/:mrn` and `/reports/:uid` by regex in a hand-written `renderRoute`. The clinical `renderRoute` mounts routed components without a `key`. | `admin/src/adminRouting.ts:15-35,52-55`; `clinical/src/clinicalRouting.ts:1-11`; `clinical/src/App.tsx:205-254` |
| Dead route code | `clinical/src/clinicalRouteRenderer.tsx` is an unused copy of `App.tsx` `renderRoute`. `admin/src/pages/ConfigPage.tsx` is not imported anywhere. | grep for importers: none |
| State | No global store. Each page keeps `useState`/`useEffect`. `me` is passed down as a prop. Some state is persisted to `localStorage`: clinical saved searches, reading queue, report drafts, macros, table preferences, walk-in list and FirstRunTour flag; admin PromoSlot dismissals. A small `AsyncValue` type handles per-card states on the clinical Home page. Connectivity lives in `@pacs/ui` (`connectionStore.ts`, `pendingWrites.ts`). | `clinical/src/asyncValue.ts`; `clinical/src/connectivity.ts`; `admin/src/components/PromoSlot.tsx:78` |
| API client | One class, `PacsApiClient` (`frontends/packages/api-client/src/index.ts:905`). Every call goes through `request()`, which sets `Authorization: Bearer` (`:1414-1423`), maps errors to `ApiClientError` with trace id (`:872-884`), and reports reachability through an optional observer (`:1428-1437`). Path params are `encodeURIComponent`-ed. Reads accept `AbortSignal`, and mutations accept `Idempotency-Key` on some paths. Empty query values are dropped (`:1488-1502`). | `index.ts:905-1437` |
| Auth | `resolveBrowserAuthMode` returns `'dev'` only for the exact value `dev`; anything else is `'oidc'` (`packages/auth/src/index.ts:29-31`). OIDC uses `oidc-client-ts` with response type `code`, scope `openid profile email roles`, and a `localStorage` user store (`:19`). A missing or expired token triggers `signinRedirect` with the current path as state (`:71`). Dev mode returns a static token (`:25`). Admin requires `admin` or `operator` at app level (`admin/src/App.tsx:119`). Clinical renders for any signed-in user and gates per page. No sign-out exists in either app. | `packages/auth/src/index.ts`; `admin/src/App.tsx:119`; grep for sign-out: none |
| Dev credentials in code | Hard-coded dev-token fallbacks exist in three places (`admin/src/App.tsx:13`, `clinical/src/App.tsx:28`, `packages/auth/src/index.ts:25`). Values not reproduced here. Used only in dev mode. `.env` and `.env.development` are gitignored and untracked; `.env.example` defaults to `oidc`. | `git ls-files` returned nothing for `.env*`; `.gitignore:4-5` |
| i18n, clinical | `appI18n` plus `t(key, englishSource)` from `@pacs/ui` (`packages/ui/src/i18n.ts:153-156`). Arabic catalog with about 108 keys (`clinical/src/locales/ar.ts`). Keys for sign, withdraw, escalate, acknowledge and attestation copy are deliberately excluded pending clinician review (`ar.ts:147` `KEYS_PENDING_CLINICAL_REVIEW`). Coverage is shown in Settings (`SettingsPage.tsx:35-39`). | as cited |
| i18n, admin | No console strings are translated. The admin app comments that its strings "are not yet in the catalog" (`admin/src/App.tsx:91-94`). | as cited |
| Locale and direction | Both apps call `useDocumentLocale`, which sets `lang` and `dir` on `<html>` from the user preference (`admin/src/App.tsx:95`, `clinical/src/App.tsx:153`). Both `index.html` files still hard-code `lang="en" dir="ltr"` (`admin/index.html:2`, `clinical/index.html:2`). | as cited |
| RTL CSS | No `[dir=rtl]` or `:dir(rtl)` selectors in either stylesheet (grep: 0). Physical properties remain: clinical `styles.css:560` (`border-left`), `:687` (`.drawer { inset: 0 0 0 auto }`), `:746` (`left: 0; right: 0`). Admin has 4 physical margin or padding rules. | grep |
| Accessibility utilities | `@pacs/ui/focus.ts` `useFocusTrap`: initial focus, Tab wrap, Escape, `inert` and `aria-hidden` on the app root, focus restore. Used by `Drawer.tsx` and `ConfirmationModal.tsx` (portalled, `role="dialog" aria-modal`). `LiveRegion.tsx` alternates two slots per live region so repeated text is re-announced. `announcer.ts` / `useAnnounce.ts` provide `announce()`. `toast()` announces. `AsyncBanner` is `role=status` or `role=alert`. Admin `DataTable` roving tabindex with Arrow, Home, End, Enter, Space (`admin/src/components/AdminCommon.tsx:63-98`). Clinical sortable headers are buttons with `aria-sort` (`clinical/src/pages/StudyListPage.tsx:1027-1058`). | as cited |
| Timezone | Both apps call `setRuntimeTimezone(preferences.timezone)` (`admin/src/App.tsx:70`, `clinical/src/App.tsx:117`). `@pacs/ui/datetime.ts` validates the zone and falls back to local time. The BFF validates it too (`tests/clinical-timezone.test.ts`). Gaps are listed in §4. | as cited |
| Service worker | Clinical only, registered in production only (`clinical/src/main.tsx:16-22`). Caches hashed `/assets/*` (cache-first) and `index.html` (network-first). Never caches `/api/` or `/dicom-web/`, and never intercepts non-GET (`clinical/public/sw.js:22-108`). | as cited |
| Build and tests | Root `tests/*.test.ts` (Vitest) import pure modules only. The repo has no jsdom (comments in `packages/ui/src/useExitTransition.ts` and `ui-exit-transition` say so). No component, hook or DOM behaviour is tested (§8). | `tests/` |

---

## 2. Admin console screens

Reading the table: "Real" means the BFF reaches the archive or a real file. "Adapter/local" means BFF in-memory or file state standing in for the upstream. "Placeholder" means the code says so.

| Screen (route) | What it does | Backend calls | Real vs adapter (`ADMIN_ADVANCED_MODE=local_mvp`) | Evidence |
|---|---|---|---|---|
| Dashboard (`/`) | Health pill, storage bar, activity counters, dependency health, attention list, PromoSlot | `GET /api/v1/admin/system/health`, `/system/metrics`; `GET /api/v1/admin/promotions/active?placement=pacs.admin_dashboard` | **Adapter.** Probes call Prometheus only if `METRICS_BASE_URL` is set. Otherwise the code returns `degraded`/`unknown` placeholders (`bff-svc/src/admin/systemClient.ts:170-190`). Promo is an in-BFF seed (`bff-svc/src/app.ts:262-283`). | `DashboardPage.tsx`; `systemClient.ts:37-65,170-190` |
| Devices & AEs (`/devices`) | AE list and detail. "Run C-ECHO placeholder". Edit is a stub. | `GET /admin/devices`, `GET /admin/aetitles`, `POST /admin/aetitles/:aet/test` | **Adapter/placeholder.** AE map is in memory (`arcLightAdminClient.ts:108`). Devices are derived from the AE map (`:245-257`). C-ECHO returns `status:'planned'` (`:293-296`). The Edit button only sets a message (`DevicesPage.tsx:54`). BFF create, update and delete routes exist (`routes.ts:123-150`) but the UI does not use them. | `DevicesPage.tsx:54`; `arcLightAdminClient.ts:108,245,293` |
| Operations (`/operations`) | Read-only turnaround, SLA by priority, throughput per reader, unassigned backlog. Window 24h, 7d or 30d. | `GET /admin/operations?window=` | **Local.** Computed by `ClinicalOperationsService` over the clinical state repository (`routes.ts:87`), so it reflects local state, not archive-verified data. | `OperationsPage.tsx:37-55`; `routes.ts:87` |
| Studies (`/studies`) | Search, detail drawer with series and metadata, reject and restore at study or series scope with reason code, anonymize, move. | `GET /admin/studies/search`, `GET /admin/studies/:uid`, `POST …/reject`, `…/unreject`, `…/anonymize`, `…/move` | **Mixed.** Search is **real** (archive QIDO through `searchStudyRows`, `bff-svc/src/clinical/studyGovernanceService.ts:132-140`). Reject and restore are a **local IOCM overlay**. **Anonymize is a no-op that returns "accepted"** (`arcLightAdminClient.ts:370-372`). **Move is a no-op** with hard-coded `DCM4CHEE` (`StudiesPage.tsx:236`, `arcLightAdminClient.ts:374-376`). | `StudiesPage.tsx:199-237`; `arcLightAdminClient.ts:370-376` |
| Queues (`/queues`) | Queue summary, job list with status tabs and resource filter, job detail (attempts, payload preview, error trace, admin actions), retry and discard. Rejected HL7 resubmit, link and discard. QC backlog reject or leave intact. | `GET /admin/queues`, `/queues/:q`, `/queues/:q/:job`; `POST …/retry`; `DELETE …/:job`; `GET /admin/rejected-hl7`; `POST /admin/rejected-hl7/:id/{resubmit,link,discard}`; `GET /admin/qc-backlog`; `POST /admin/qc-flags/:id/dismiss`; `POST /admin/studies/:uid/reject` | **Local.** Queue state is in BFF memory (`queueAdminClient.ts:318`; `arcLightAdminClient.ts:379`). QC backlog originates from clinical QC flags in BFF state. Each panel has its own error via `Promise.allSettled` (`QueuesPage.tsx`, `reload`). | `QueuesPage.tsx:29-112`; `queueAdminClient.ts:318` |
| HL7 applications (`/hl7`) | Listener catalog CRUD (name, facility, host, port, accepted ADT/ORM/ORU, enabled). Message log with filters, ACK and DLQ markers, identity-change display, dead-letter bridge. | `GET/POST/PUT/DELETE /admin/hl7-applications[/:id]`; `GET …/:id/messages` | **Local.** Catalog is a BFF `Map` (`hl7AdminClient.ts:55`). The page itself says "Deterministic local-dev catalog" (`Hl7ApplicationsPage.tsx:283`). No live listener. | `Hl7ApplicationsPage.tsx:235,283`; `hl7AdminClient.ts:55` |
| Users & roles (`/users`) | Search, create user (validated, admin role requires typed phrase), grant or revoke roles, disable and re-enable, role catalog. | `GET /admin/users`; `POST /admin/users`; `PATCH /admin/users/:id`; `DELETE /admin/users/:id`; `GET /admin/roles` | **Mock.** In-memory `Map` seeded with dev users (`keycloakAdminClient.ts:25-28`), with create, patch and disable on the same map (`:47-72`). No Keycloak call. The page header says Keycloak is the identity source (`UsersPage.tsx:224`). Reason input is not sent (see §6, M2). | `UsersPage.tsx`; `keycloakAdminClient.ts:25-72` |
| Report templates (`/templates`) | Template CRUD: modality, body part, language, seven sections, enabled. Filter. | `GET/POST/PUT/DELETE /admin/report-templates[/:id]` | **Local.** Map seed (`arcLightAdminClient.ts:148`). The clinical template picker reads this catalog. | `ReportTemplatesPage.tsx`; `arcLightAdminClient.ts:148` |
| Storage (`/storage`) | Storage descriptors: type, tier, path, region, access key ID, default, read-only, enabled, write-only secret. | `GET/POST/PUT /admin/storage[/:id]` | **Local.** Descriptor records only (`configAdminClient.ts:45`). Secret is write-only and the UI shows `secret_set` (`StoragePage.tsx:182`). Not wired to real stores (verify secret handling in BFF). | `StoragePage.tsx`; `configAdminClient.ts:45` |
| Retention (`/retention`) | Retention rules (keep days, modality, legal-hold flag) and lossless compression rules (after N days, JPEG-LS, J2K or RLE). | `GET/POST/PUT/DELETE /admin/retention-rules`; `/admin/compression-rules` | **Local.** Map seeds (`configAdminClient.ts:49-53`). Nothing in the admin path shows rules being executed (open question). The page says legal-hold management is a later step. | `RetentionPage.tsx`; `configAdminClient.ts:49-53` |
| Routing (`/routing`) | Priority-ordered rules. WHEN conditions with regex validation. THEN forward, anonymize plus forward, or tag. Quiet period. Enable. Move up or down. Preview and test against a study UID. | `GET/POST/PUT/PATCH/DELETE /admin/routing-rules[/:id]`; `POST …/:id/test?study_uid=` | **Local.** Map seed (`routingAdminClient.ts:42`). Preview and test run on the **saved** rule (`RoutingPage.tsx:143,154`). Forwarding is not evidenced in the admin path. | `RoutingPage.tsx`; `routingValidation.ts` |
| System (`/system`) | Versions, backups, certificates, license with redacted key. Backup run, restore test, rotate certificate and license update are disabled placeholders. | `GET /admin/system/versions`, `/backups`, `/certificates`, `/license` | **Static seed.** Hard-coded records (`systemDetailsClient.ts:64,147,157`). One real local log-file read (`:86`). | `SystemPage.tsx`; `systemDetailsClient.ts` |
| MWL (`/mwl`) | Scheduled, arrived and cancelled items. Admin manual create. Status change. | `GET/POST /admin/mwl`; `PUT /admin/mwl/:id` | **Local.** Map (`mwlAdminClient.ts:45`). Clinical walk-ins land in the same queue (`MwlPage.tsx` header). Not a live modality worklist. | `MwlPage.tsx`; `mwlAdminClient.ts:45` |
| Audit log (`/audit`) | Filters (user, action, resource, MRN, study UID, date range or custom), cursor paging, CSV export of the loaded page, event detail with a redacted payload and deep links. | `GET /admin/audit-log?…` | **Real-ish.** Written by the BFF `audit()` function to a file in dev (`AUDIT_SOURCE=file`). The page shows the source. Redaction was not verified. | `AuditPage.tsx`; `auditUtils.ts` |
| Reporting (`/reporting`) | Source mode (in-app, external, hybrid). HL7 ORU listener settings. HIS REST (bearer or basic). PDF URL template. Iframe embed with sandbox flags. Outbound-on-sign. Test probes. | `GET/PUT /admin/reporting-config`; `POST …/test-hl7`, `…/test-his-rest` | **Local config, deterministic probes.** Config is a module-level `let` (`arcLightAdminClient.ts:205`). Validation exists (`:571-610`) but has the scheme gap in §6 C3. The page says probes do not call a live HIS. | `ReportingPage.tsx`; `arcLightAdminClient.ts:205,571-610` |
| Chrome (every page) | Quick search (Ctrl or Cmd+K): three calls per debounced keystroke, 180 ms (study search, AE list cached once, user list). Health drawer (30 s poll while visible). Notifications drawer derived from health and metrics. | `GET /admin/system/health|metrics`; `GET /admin/studies/search`; `GET /admin/aetitles`; `GET /admin/users` | Mixed, as above. | `AdminChrome.tsx:117-133,157-168,282-309`; `chromeUtils.ts:18,76-120` |

---

## 3. Clinical portal screens

| Screen | What it does | Backend calls | Real vs mock | Evidence |
|---|---|---|---|---|
| Home (role dashboard) | Role-specific metric cards, each with its own loading, ready or error state. Role action tiles. Critical-finding acknowledgement behind a confirmation. Walk-in MWL form for technologist or admin. Recent walk-ins. | `listCriticalFindings`, `searchStudies` (counts), `listShareLinks`, `searchPatients`, `getWorklistSummary`, `getWorklist` (STAT or urgent; in progress), `acknowledgeCriticalFinding`, `createWalkInMwl` | Counts from **real** archive QIDO. Worklist, findings and share counts from **BFF state**. Walk-in goes to the local MWL queue. | `HomePage.tsx:115-170,222-232,267` |
| Worklist (`/worklist`) | Queue with thumbnails, flags, queue age, priority, status, assignee. Search with 300 ms debounce (`WorklistPage.tsx:65`). Modality, priority, status, scope (admin can select all). Saved searches. Multi-select. Take next (radiologist). Assign and release, single and bulk. STAT escalation with reason, single and bulk, with undo. Open viewer and report. Polls every 45 s while visible and stages new rows behind a "Load updates" prompt. | `GET /worklist`, `GET /worklist/summary`, `POST /worklist/take-next`, `POST /worklist/:uid/assign`, `…/release`, `…/priority`; `POST /studies/priors` (chips) | Rows from **archive**. Assignment and priority are **BFF state**. Server requires radiologist or admin for writes (`bff-svc/src/clinical/accessPolicy.ts:37-39,71-75`). Take next is radiologist-only on the server (`clinical/routes.ts:272`). | `WorklistPage.tsx:65,69,396,436-456,465-535,640-830` |
| Study list (`/studies`) | URL-backed filters (name, MRN, study UID, accession, modality, status, sort, source AE, body part, date range). 10, 25 or 50 rows. Cursor paging. Column configuration and density. Saved searches. Multi-select. Open 1 to 3 viewers, or a comparison pair. Create share links (permissions, expiry, optional email, reason). CSV export of the loaded page. Print. | `GET /studies/search`; `POST /shares`; viewer deep links to `VIEWER_BASE` | Rows from **archive**. Share links are **BFF state**. | `StudyListPage.tsx:220-233,297-327,420-520,560-660,1221-1230` |
| Patient search (`/patients`) | MRN or name search, 350 ms debounce, load more. | `GET /patients/search` | Archive-derived patient search (BFF). | `PatientSearchPage.tsx:14-57` |
| Patient detail (`/patients/:mrn`) | Banner, studies grouped by date, reports tab, notes tab with create and supersede, compare-with-prior, study drawer, merge-notice redirect. | `GET /patients/:mrn`, `GET /patients/:mrn/studies`, `GET|POST /patients/:mrn/notes` | Studies archive-derived. Notes are **BFF state** only. | `PatientDetailPage.tsx:50-80,133`; `App.tsx:208-225` |
| Report author (`/reports/:uid`) | Identity banner. Sections. Autosave: 5 s idle, intended 30 s ceiling. Local mirror and recovery prompt. Templates, "Insert normal", text macros. Dictation per section. Sign preliminary, final or corrected with identity attestation. "Sign and read next" from the queue. Addendum. Withdraw (typed phrase). Critical-finding communications and acknowledgements. Print. Reading-queue rail. | `GET /studies/:uid/context`, `GET /reports/:uid`, `GET /reports/:uid/history`, `POST /reports/:uid/draft`, `…/sign`, `…/addendum`, `…/withdraw`, critical-finding endpoints; templates from admin catalog | Draft, sign and revisions are **BFF state**. **SR STOW is mock by default** (`arcLightClient.ts:161-167`). Dictation is **browser Web Speech**. Template catalog is the **admin Map**. | `ReportAuthorPage.tsx:100-660,780-935` |
| Report history (`/report-history`) | Studies that have reports (up to 100), current report, version list. | `GET /studies/search`; `GET /reports/:uid`; `GET /reports/:uid/history` | Archive study list plus local report state. | `ReportHistoryPage.tsx:17-50` |
| Share links (`/shares`) | Study picker with debounce, optional email, expiry 24 h, 7 d or 30 d, reason. Link copied on create, with Undo that revokes. Active, revoked or all filter. Revoke behind confirmation. Study UID validation. | `GET /shares`, `POST /shares`, revoke endpoint, `GET /studies/search` | **BFF state.** Link is `SHARE_LINK_BASE_URL/v/s/<token>` (`shareLinkService.ts:77`). Landing is public (`auth.ts:65`). | `ShareLinksPage.tsx:94-210,139`; `shareLinkService.ts:32-80,148-166` |
| Settings (`/settings`) | Density, appearance, UI language (Arabic coverage shown), default landing page, timezone picker with warning for a stored invalid zone, notification list, session list with revoke. | `GET /users/me/sessions`, `GET /users/me/notifications`, `PATCH /users/me/preferences`, `DELETE /users/me/sessions/:id` | BFF state. No sign-out control. | `SettingsPage.tsx:40-140` |
| Study drawer and report panel | Study facts. Open viewer. Compare with prior. Shortcuts to report author and share. Flag for QC with reason and undo. Report panel: status, source, precedence, HIS link, PDF link, **iframe**. | `POST /studies/:uid/qc-flag`; withdraw own QC flag; `GET /reports/:uid` | QC flags are BFF state. Report source comes from BFF external adapters. | `StudyDrawer.tsx`; `ReportPanel.tsx:66-70`; `bff-svc/src/clinical/externalReportAdapters.ts` |
| Priors | "N priors" chip per row, nearest prior, compare-with-prior viewer URL, compare pair from the study list. | `POST /studies/priors` | Archive-derived (BFF prior-study logic). | `WorklistPage.tsx` (`PriorsChip`); `compareStudies.ts`; `bff-svc/src/clinical/priorStudies.ts` |
| Dictation | Push to talk per section. Interim text shown beside the button, only final text inserted. Spoken punctuation. Stops on unmount. | none on server | **Browser Web Speech API.** The code comment says Chrome sends audio to Google (`speechInput.ts:236-243`). No disclosure found in `DictationButton.tsx`. | `DictationButton.tsx`; `speechInput.ts` |
| Macros and templates | Macro expansion on delimiter keys. Macro list per user. "Normal for this modality" inferred from usage. Template select. | none on server (macros in `localStorage`) | Macros: **browser-local**. Templates: **admin catalog**. | `reportMacros.ts:241-251`; `ReportAuthorPage.tsx:448-450,822` |
| Notifications | List in Settings. Critical findings on Home and the report page. | `GET /users/me/notifications`; critical-finding endpoints | BFF state. | `SettingsPage.tsx` (`loadSettings`) |
| Help, tour, connection | `?` keyboard reference. First-run tour (per-user flag). Connection indicator. Replay of queued report-draft writes only. Signing is explicitly not queued. | none | Local. | `HelpDrawer.tsx`; `FirstRunTour.tsx:23-33`; `connectivity.ts`; `shortcuts.ts` |

---

## 4. UX quality

**Loading, error and empty states**
- Good: Home uses per-card `AsyncValue` with skeletons and one aggregate retry (`HomePage.tsx:~333-380`). Admin Queues isolates panel failures (`QueuesPage.tsx`, `allSettled`). Worklist shows an empty state with a one-click fix when scope is "My studies" (`WorklistPage.tsx`, empty-state block).
- Gap: Patient detail keeps the previous patient's data while the next one loads, and shows the error banner over it (§6 M9).

**Dirty-form guards**
- Admin: `guardSelectionChange` prompts on row change in five editors: HL7 (`Hl7ApplicationsPage.tsx:~150-170`), Routing, Retention, Storage and MWL. It does not cover:
  - Report templates: row click (`ReportTemplatesPage.tsx:184`, `selectTemplate` at `:68`).
  - Any "New" or "Add" button: HL7 `startNew` (`Hl7ApplicationsPage.tsx:237`), Routing `startNew` (`RoutingPage.tsx:232`), Retention (`RetentionPage.tsx:220,367`), Storage (`StoragePage.tsx:154`), Templates (`ReportTemplatesPage.tsx:158`).
  - The Reporting page, which has no route guard.
  - The Users create form (cancel discards silently).
- Admin has no `beforeunload` or hashchange guard. Navigating away from a dirty admin form loses the edits. `useDirtyGuard` from `@pacs/ui` is used only by `ReportAuthorPage` (`ReportAuthorPage.tsx:261-263`).
- Clinical: `useDirtyGuard` (beforeunload, in-app nav and hashchange) covers the report author. Whitespace-only edits do not arm it (`reportDraftStore.ts` `areSnapshotsEqual`). Settings, the patient-detail note box, the walk-in form and the share form have no guard.

**Keyboard**
- Admin: Ctrl or Cmd+K focuses quick search, which is a combobox with `aria-activedescendant` and a flat option list (`AdminChrome.tsx:117-133,250-275`). Escape closes drawers. `DataTable` supports Arrow, Home, End, Enter and Space (`AdminCommon.tsx:73-78`).
- Clinical: row Enter opens the detail drawer, Shift+Enter or `o` opens the viewer (`StudyListPage.tsx:1221-1230`). Paging keys ignore form controls (`StudyListPage.tsx:630`, `handlePagingKeyboardShortcut`). Patient tabs use arrow keys. `?` opens the reference (`shortcuts.ts`).
- Defect: the row handler also receives keys pressed on nested buttons and checkboxes (§6 M15).

**Focus management**
- Drawer and ConfirmationModal: focus moves in, Tab is trapped, focus returns on close, app root is `inert` (`packages/ui/src/focus.ts`).
- Gaps: the clinical attestation dialog is hand-rolled with no trap, no Escape and no focus restore (`ReportAuthorPage.tsx:1004-1005`). Neither app moves focus or updates `document.title` on route change (both `index.html` titles are static). No skip link exists in either app.

**Announcer**
- Good: `LiveRegionHost` alternates slots so repeated messages are re-announced. Row selection in the admin `DataTable` announces "Row N of M selected". `toast()` announces. `AsyncBanner` uses `role=alert` for errors and `role=status` otherwise.

**Narrow viewport**
- Admin: viewport modes `narrow` below 1024, `collapsed` below 1280 (`chromeUtils.ts:39-43`). The narrow-width banner only shows on configuration routes (`NARROW_FRIENDLY_ROUTES`, `chromeUtils.ts:61`).
- Clinical: breakpoints at 1023 and 767 (`styles.css:1051,1094,1140`). Touch targets enlarged for coarse pointers.

**Timezone**
- Display uses the preference zone (`formatDateTime`, `ui/datetime.ts`). Inputs do not. `toLocalDateTimeInput` uses the browser's zone, and `fromLocalDateTimeInput` parses in the browser's zone (`ui/datetime.ts`). Walk-in and MWL scheduled times, and the audit custom window (`auditUtils.ts` `toIsoString`), are therefore interpreted in a different zone from the one displayed (§6 M11).
- The clinical "today" used for dashboard counts is the **UTC** date (`HomePage.tsx:39-41`). The walk-in form's date uses the local zone (`:63-65`).
- Date and time display has no zone label, so a clinician cannot tell which zone a timestamp is in.

**Health and staleness**
- Admin: "checked N min ago" next to the status pill (`chromeUtils.ts:25-37`), with 30 s polling while visible (`HEALTH_POLL_MS`, `:18`).
- Clinical: worklist polls every 45 s while visible. New rows are staged rather than reordering under the cursor (`WorklistPage.tsx:69`; `applyPending` at `:312`).

---

## 5. Security

**Token storage**
- OIDC user object, including the access token, is in `localStorage` (`packages/auth/src/index.ts:19`). Any script running on the origin can read it. No CSP exists anywhere in the repo (grep). Traefik `security-headers` sets nosniff, frame SAMEORIGIN, HSTS and referrer policy, but no CSP (`infra/traefik/dynamic/routers.yml`).
- No sign-out, no idle lock, no silent renew. When the token expires the app redirects to login.
- CSRF is not a concern for the apps: every request uses an `Authorization` header and no cookies are used.

**XSS sinks**
- No `innerHTML`, `dangerouslySetInnerHTML`, `eval` or `new Function` under `frontends/` (grep, excluding `node_modules`). React escapes text.
- Server-supplied URLs are placed in `href` and `src` without scheme checks:
  - `ReportPanel.tsx:66-67` (`open_in_his_url`, PDF).
  - `ReportPanel.tsx:70` (`<iframe src={iframeUrl} sandbox={iframeSandbox}>`).
  - `ReportAuthorPage.tsx:787-788`.
  - `PromoSlot.tsx:161` (`src`) and `:177` (`href`, with `rel="noopener noreferrer"`).
- Link hygiene is otherwise good: `target="_blank"` with `rel="noreferrer"` on report and viewer links (`ReportPanel.tsx:66-67`, `StudyDrawer.tsx:98-99`, `StudiesPage.tsx:307`).
- The BFF's scheme check covers only the HIS REST base URL and the outbound REST URL (`arcLightAdminClient.ts:580,606,617-626`). Open-in-HIS, PDF and iframe templates are not checked (§6 C3).

**Destructive-action confirmations**
- Admin: typed phrases on reject (`REJECT`, `StudiesPage.tsx:199`), anonymize (`ANONYMIZE`), move (`MOVE`), delete (`DELETE`), discard (`DISCARD`), grant admin (`GRANT ADMIN`), remove or disable last admin (`REMOVE LAST ADMIN`, `DISABLE LAST ADMIN`), disable (`DISABLE`). Last-admin guard (`userAdminUtils.ts` `readLastAdminWarning`). Reason prompts (but see M2).
- Admin without a typed phrase: QC backlog "Reject study" (`QueuesPage.tsx:251-258`), storage default and read-only toggles (`StoragePage.tsx:177-179`), MWL status change to cancelled (`MwlPage.tsx:120`), routing rule save (`RoutingPage.tsx:~86-130`).
- Clinical: attestation dialog re-displays the patient identity before sign and addendum (`ReportAuthorPage.tsx:1004-1040`). Withdraw requires `WITHDRAW` (`ReportAuthorPage.tsx:568-600`). Acknowledge requires confirmation (`HomePage.tsx:~215-225`). STAT escalation requires a reason, and the reason is sent (`WorklistPage.tsx:476-485`). Share revoke requires confirmation (`ShareLinksPage.tsx:169-183`). QC flags and share creation have undo, not confirmation.

**Role-based UI gating versus server enforcement**
- Admin: UI `canWrite = roles.includes('admin')` (`admin/src/adminRouteRenderer.tsx:27`). BFF: reads need `admin` or `operator` (`bff-svc/src/admin/accessPolicy.ts:1-20`). Every admin write route checks `requireAdminWriteAccess` (verified for POST, PUT, PATCH and DELETE across `bff-svc/src/admin/routes.ts`). The UI and BFF agree.
- Clinical: worklist writes need radiologist or admin (`clinical/accessPolicy.ts:71-75`). Take next needs radiologist (`clinical/routes.ts:272`). Sign and draft save are author-only, with admin override (`reportService.ts:113-116`). Acknowledge is limited to the recipient role or admin (`criticalFindingService.ts:157`). Share revoke is owner or admin (`shareLinkService.ts:130`). Release is assignee or admin (`worklistService.ts:168`).
- Mismatch: the worklist shows Assign, Release and Escalate buttons to any role that can open the page. Non-radiologist roles get a server 403 (§6 m7).
- Server-side gap: the public share landing returns study UID and issuer for any valid token (`shareLinkService.ts:166-170`).

**Secrets in the UI**
- Write-only fields are `type="password"`: storage secret (`StoragePage.tsx:182`), HIS bearer token and password (`ReportingPage.tsx:69-70`). The UI shows presence flags only (`token_set`, `password_set`, `secret_set`). BFF redaction was not verified.

**PHI in URLs, history and logs**
- MRN appears in the path (`api-client/src/index.ts:969` `getPatient`; hash route `#/patients/{mrn}`).
- MRN appears in hash query strings: `buildRouteForFilters` (`StudyListPage.tsx:220-233`, `patient_mrn=`); global search (`AppChrome.tsx:63`, `?q=`).
- Viewer deep links carry `patientMrn` as a query parameter (`packages/deep-link/src/index.ts:101-102`) from `WorklistPage.tsx:795,812`, `StudyDrawer.tsx:40-44`, `ReportAuthorPage.tsx:147-150`, `StudyListPage.tsx:480-500`. The admin app's viewer links omit MRN (`StudiesPage.tsx`, `AuditPage.tsx`).
- Admin HL7 message filters send `patient_mrn` as a query parameter on every keystroke (`Hl7ApplicationsPage.tsx:118,375`).

**PHI at rest in `localStorage`**
- Report draft text per study: `reportDraftStore.ts:73`.
- Reading queue with patient names: `readingQueue.ts:179`.
- Walk-in entries with patient names and MRNs: `HomePage.tsx:189`.
- Saved searches with MRN filters: `savedSearches.ts:94`.
- Table preferences: `studyTablePreferences.ts:296`.
- Pending writes queue: `packages/ui/src/pendingWrites.ts:276`.
- None of these is cleared on sign-out, because there is no sign-out. Per-user keys prevent cross-user reads on a shared workstation, but not after the same user's session ends.

**Dictation**
- Browser speech recognition, which the code says may send audio to a third party in Chrome (`speechInput.ts:236-243`). No consent or disclosure is shown in the UI (grep for disclosure text in `DictationButton.tsx`: none).

**Promo feed**
- Placement is allowlisted in both the client (`PromoSlot.tsx`, `ALLOWED_PLACEMENTS`) and the BFF (`app.ts:263-266`). The content is a seed cross-promotion for a hospital information system, rendered on the admin login and loading screens (`admin/src/App.tsx:97-98`). This is a product decision to confirm.

---

## 6. Code review findings

Severity: 🔴 = clinical-safety, security or data-loss risk; 🟡 = wrong or misleading behaviour, or a notable gap; 🟢 = cleanup or minor. "Verify" means the claim rests on code reading only. Code snippets are condensed; the quoted identifiers and strings are verbatim.

### 🔴 Critical

**C1. Signing can sign a stale autosaved draft, and the local mirror is erased.** `clinical/src/pages/ReportAuthorPage.tsx:502` and `:512`; `:900-902`; `bff-svc/src/clinical/reportService.ts:127`.
```ts
const saved = draft ?? await apiClient.saveReportDraft(studyUid, { sections, extra_sections: [], language });
...
clearLocalDraft(studyUid, me.id);
```
- Issue: `draft` is set only by autosave or manual save. Once one save has happened, `draft` is non-null, so the current editor text is never saved before signing. The sign buttons are enabled regardless of `isDirty`. The BFF signs `draft.sections` from the stored draft, and the request sends only `draft_id` and `expected_draft_version`.
- Failure: a radiologist autosaves a report, types another sentence, and clicks "Sign final" within the 5 s idle window. The signed revision omits the new sentence. The sign then clears the local mirror, so the sentence is gone from both places.
- Fix: before signing, always persist the current `sections` and sign the returned draft. Alternatively disable sign while `isDirty`, or send a content hash that the BFF checks against the stored draft.

**C2. The UI reports a signed SR as stored when the STOW write was mocked.** `clinical/src/pages/ReportAuthorPage.tsx:517`; `bff-svc/src/arcLightClient.ts:161-167`.
```ts
// arcLightClient.ts
if (config.reportSrStoreMode !== 'stow') {
  return { status: 'mocked', sop_instance_uid: document.sopInstanceUid, message: 'DICOM SR STOW-RS is in mock mode for local development.' };
}
// ReportAuthorPage.tsx
toast(`Signed ${signed.status} report. SR SOP ${signed.sop_instance_uid}.`);
```
- Issue: the `status:'mocked'` result is ignored. The toast presents a SOP UID as if it were archived.
- Failure: in a deployment with `REPORT_SR_STORE_MODE=mock`, radiologists believe the signed report reached the archive. It did not. `.env.example:122` ships with `mock`.
- Fix: surface the STOW status in the sign response and in the UI. Refuse "archived" wording unless the status is stored. Make mock mode visible.

**C3. External-report URL templates are not scheme-checked, and the default iframe sandbox allows script with same-origin.** `bff-svc/src/admin/arcLightAdminClient.ts:576,588,593-594,223,146`; `bff-svc/src/clinical/externalReportAdapters.ts:78-83,122-135`; `clinical/src/components/ReportPanel.tsx:66-70`; `clinical/src/pages/ReportAuthorPage.tsx:787-788`.
```ts
if (config.open_in_his_url_template) validateAccTemplate(config.open_in_his_url_template, 'open_in_his_url_template');
...
if (config.iframe_enabled) { ... validateAccTemplate(config.iframe_url_template, 'iframe_url_template'); validateSandboxFlags(config.iframe_sandbox_flags); }
```
```tsx
<iframe className="report-iframe" title="External HIS report" src={iframeUrl} sandbox={iframeSandbox} referrerPolicy="no-referrer" />
```
- Issue: `validateAccTemplate` only checks for `{ACC}`. Nothing rejects `javascript:` or `data:` schemes. The default sandbox `allow-scripts allow-same-origin` (`:223`) is accepted by the allowlist (`:146`). It effectively removes isolation.
- Failure: an administrator enters a `javascript:` URL with `{ACC}` in the iframe template. Clinical users who open the report run script in the clinical origin, where the OIDC token sits in `localStorage` (§5). React 18 does not block `javascript:` in `href`.
- Fix: require `https:` (or `http:`) for all three templates in the BFF and the UI. Drop `allow-scripts` combined with `allow-same-origin` from the allowlist or the default. Consider a CSP that disallows `javascript:`. (Verify with a test.)

**C4. Anonymize and move report success while doing nothing.** `bff-svc/src/admin/arcLightAdminClient.ts:370-376`; `frontends/apps/admin/src/pages/StudiesPage.tsx:219-224,232,236`.
```ts
export async function anonymizeStudy(studyUid: string) {
  return { status: 'accepted', message: 'Study anonymization job queued.', job_id: `job-anon-${hashId(studyUid)}` };
}
```
- Issue: no job exists. The move copy says "queued for move to AE DCM4CHEE in the local MVP workflow" and hard-codes the destination (`StudiesPage.tsx:236`). The anonymize confirmation says downstream metadata "may change after processing", which is not true.
- Failure: an operator anonymizes or moves a study, sees "queued", and believes PHI has been removed or the study has been sent. Neither happened.
- Fix: return `status:'not_implemented'` and disable the buttons in the UI, or implement the job. Remove the hard-coded destination and make the target an explicit, validated input.

**C5. (verify) The DICOMweb route is unauthenticated at the edge, and thumbnails rely on it.** `infra/traefik/dynamic/routers.yml:16-22`; `clinical/src/components/StudyThumbnail.tsx:76`; `bff-svc/src/dicomwebLinks.ts:51-55`.
- Issue: the `dicom-web` router has no forward-auth middleware. The thumbnail URL is a plain `${dicomwebPublicBase}/studies/{uid}/thumbnail?viewport=…` and is loaded by `<img>`, which cannot send the bearer token. That only works if the archive accepts anonymous GETs on this path, which would also expose instances.
- Failure: if dcm4chee accepts anonymous requests, anyone who can reach `pacs.local` can fetch study thumbnails and possibly other DICOMweb resources without logging in.
- Fix: put forward-auth on `/dicom-web` (or serve thumbnails through the BFF with the bearer token), and confirm the archive's auth configuration.

### 🟡 Major

**M1. Share-link recipient binding is optional on the public landing, and the token travels in the URL.** `bff-svc/src/clinical/shareLinkService.ts:77,154-156`; `bff-svc/src/app.ts:239-240`; `bff-svc/src/auth.ts:65`; `clinical/src/pages/ShareLinksPage.tsx:210`.
```ts
if (link.recipient_email && email && link.recipient_email.toLowerCase() !== email.toLowerCase()) { throw clinicalForbidden(...); }
```
- Issue: the check runs only when `email` is supplied in the query. A link created for a named recipient works for anyone who omits the parameter. The landing returns study UID, issuer and permission flags. Page copy promises "without placing bearer tokens in URLs", but the token is the URL path segment.
- Failure: a link sent to one recipient is forwarded and used by a second person with no check.
- Fix: enforce the email match when `recipient_email` is set. Correct the copy, or move the token to a header or POST body. Add tests.

**M2. Reasons typed in five confirmation dialogs are discarded.** `frontends/apps/admin/src/pages/UsersPage.tsx:95-97,152-154,180-183,205-207`; `frontends/apps/clinical/src/pages/ShareLinksPage.tsx:179-183`.
```tsx
reason: { label: 'Reason for creating this account', placeholder: '…', required: false },
...
onConfirm: () => { setConfirmation(null); ... apiClient.createAdminUser({ ... }) }
```
- Issue: `ConfirmationModal` passes `{ reason }` to `onConfirm` (`packages/ui/src/ConfirmationModal.tsx:52,182`), but these callbacks ignore the argument. The API methods take no reason (`api-client/src/index.ts:1265-1274`). The reason prompts say "(optional)".
- Failure: an administrator records why an account was granted admin. The audit log has no reason.
- Fix: pass `input.reason` through to the API and the audit record, as the clinical STAT escalation does (`WorklistPage.tsx:478-483`).

**M3. Users & Roles is an in-memory mock labelled as Keycloak-backed.** `bff-svc/src/admin/keycloakAdminClient.ts:25-72`; `frontends/apps/admin/src/adminRouting.ts:22`; `frontends/apps/admin/src/pages/UsersPage.tsx:224`.
- Issue: create, patch and disable change a Map. The description "Keycloak-backed PACS users" and "Keycloak remains the identity source" are false for this build.
- Failure: an administrator removes a role or disables an account and believes access is revoked. Sign-in still works because Keycloak was never changed.
- Fix: make the page say "local MVP" until the Keycloak adapter exists, or implement it. Block the grant and revoke actions in that mode.

**M4. The autosave ceiling never fires during continuous typing.** `clinical/src/pages/ReportAuthorPage.tsx:324-331`.
```ts
const idle = window.setTimeout(run, AUTOSAVE_IDLE_MS);
const ceiling = window.setTimeout(run, AUTOSAVE_MAX_MS);
return () => { cancelled = true; window.clearTimeout(idle); window.clearTimeout(ceiling); };
// deps: [isDirty, snapshot, studyUid, me.id, persistDraft, markClean]
```
- Issue: `snapshot` changes on every keystroke, so the effect cleans up and re-arms both timers each time. The comment at `:~276-280` promises "never more than 30 s after the first unsaved keystroke".
- Failure: a long uninterrupted dictation is not saved to the server until a 5 s pause. A crash or network drop mid-dictation loses server-side text. The local mirror is written on each change, which limits the damage on the same machine.
- Fix: keep a `firstDirtyAt` ref. Arm the ceiling only once, and do not clear it on subsequent keystrokes.

**M5. Applying a template overwrites every section with no confirmation and no undo.** `clinical/src/pages/ReportAuthorPage.tsx:448-450`, `:822`.
```ts
const applyTemplate = (template: ReportTemplate) => {
  setSections(template.sections);
```
- Issue: this replaces all seven sections, including typed findings and impression. The local mirror is rewritten with the new text, so the old text is gone. Browser undo is lost on programmatic value changes.
- Failure: a radiologist with a half-written findings section clicks "Insert normal" or picks a template and loses it.
- Fix: confirm when any section is non-empty. Merge per section, or insert at the caret rather than replace.

**M6. The attestation dialog has no focus trap, Escape handler or focus return.** `clinical/src/pages/ReportAuthorPage.tsx:1004-1005` (hand-rolled `modal-backdrop` / `modal-card`).
- Issue: focus is not moved into the dialog, Tab escapes behind it, and Escape does nothing. The shared `ConfirmationModal` does all three (`packages/ui/src/ConfirmationModal.tsx`, `focus.ts`).
- Failure: a keyboard or screen-reader user cannot reliably reach or dismiss the identity attestation, which is the step that legally commits a signature.
- Fix: replace with `Drawer`-style or `ConfirmationModal`-style dialog, or wire `useFocusTrap`.

**M7. The popup-block check is always positive.** `clinical/src/pages/StudyListPage.tsx:505-519`.
```ts
const opened = window.open(buildViewerUrl('/viewer', {...}), '_blank', 'noopener,noreferrer');
if (!opened) blocked.push(study);
...
setError(`Your browser blocked ${blocked.length} of ${toOpen.length} viewer tabs. …`);
```
- Issue: with `noopener`, `window.open` returns `null` by specification, so every study is counted as blocked. The error appears even when the tabs opened, and the success status is never set.
- Failure: a user opens three viewers, all of which open, and sees "Your browser blocked 3 of 3 viewer tabs. Allow pop-ups…".
- Fix: drop `noopener` (set `opener = null` after opening), or detect blocking differently. Add a test for the message path.

**M8. Bulk actions stop partway and leave the table stale.** `clinical/src/pages/WorklistPage.tsx:436-456`.
```ts
for (const item of selected) { await action(item.study_instance_uid); }
clearSelection();
await load('refresh');
...
} catch (bulkError) { setError(readErrorMessage(bulkError)); setStatus(''); }
```
- Issue: on the first failure the loop stops. Earlier items were already applied on the server, but there is no reload, the selection is kept, and the error does not say which items succeeded.
- Failure: 10 studies are selected for assign. Study 4 is taken by someone else. Studies 1 to 3 are now assigned to the user, the table shows the old state, and the user is told only "Conflict".
- Fix: run per item with `allSettled`. Reload in a `finally`. Report succeeded and failed counts, and keep only failed items selected.

**M9. Patient detail keeps the previous patient's data when the MRN changes.** `clinical/src/pages/PatientDetailPage.tsx:31-33,50-52,78-80`; `clinical/src/App.tsx:210-213`.
- Issue: the component is not keyed and never clears `patient`, `studies` or `notes` at the start of the effect. A route change from one `/patients/:mrn` to another (merge redirect, browser Back or Forward, typed hash) reuses the instance. While the new patient loads, the old patient's banner, studies and note draft are shown. If the new load fails, the old data stays on screen under the error.
- Failure: a clinician adds a note while looking at patient B's URL but patient A's banner. The note is saved with `patient.mrn` from the stale state (`:133`).
- Fix: add `key={mrn}` on the route element and reset state at the start of the effect. Gate note save on the loaded patient matching the URL.

**M10. The study row for the patient drawer fabricates `source_ae` and `priority`.** `clinical/src/clinicalHelpers.ts:286-300`; `clinical/src/components/StudyDrawer.tsx:94`.
```ts
return { ...study, patient: {...}, source_ae: 'DCM4CHEE', priority: 'routine', read_at: null };
```
- Issue: `PatientStudyRow` has neither field (`api-client/src/index.ts:289-310`), so the spread is overwritten with constants. The drawer then shows "Source AE: DCM4CHEE" for every study.
- Failure: a clinician checks where a study came from and is shown the wrong AE.
- Fix: fetch the real source AE, or omit the row when it is not known and show "unknown".

**M11. Timezone handling is inconsistent across inputs, "today" and display.** `clinical/src/pages/HomePage.tsx:39-41` (UTC "today") and `:63-65` (local date); `packages/ui/src/datetime.ts` (`toLocalDateTimeInput`, `fromLocalDateTimeInput`, browser zone); `admin/src/auditUtils.ts` (`toIsoString`, browser zone); `formatDateTime` (preference zone, no zone label).
- Issue: a user whose preference is Africa/Khartoum on a UTC workstation types 09:00 into "Scheduled at", which is saved as 09:00 UTC and displayed as 11:00. Between 00:00 and 02:00 local time the dashboard "today" counts use yesterday's date.
- Failure: a walk-in scheduled for the wrong time, or dashboard "acquired today" numbers that are wrong in the early morning.
- Fix: use the preference zone for every input conversion and for "today". Show the zone in timestamps, or in the field label.

**M12. PHI in URL paths, query strings, hashes and viewer links.** Evidence is in §5 ("PHI in URLs"). Key lines: `packages/api-client/src/index.ts:969`; `clinical/src/components/AppChrome.tsx:63`; `clinical/src/pages/StudyListPage.tsx:220-233`; `clinical/src/pages/WorklistPage.tsx:795,812`; `packages/deep-link/src/index.ts:101-102`; `admin/src/pages/Hl7ApplicationsPage.tsx:118`.
- Issue: MRN ends up in browser history, the address bar, referrers (where not blocked), server access logs and viewer logs.
- Fix: keep MRN in POST bodies or headers, or use an opaque internal patient ID in the URL. For the viewer, pass the study UID only, as the admin app already does.

**M13. PHI persisted in `localStorage` with no sign-out; the OIDC token lives there too.** See §5 "PHI at rest". Evidence: `packages/auth/src/index.ts:19`; `clinical/src/reportDraftStore.ts:73`; `clinical/src/readingQueue.ts:179`; `clinical/src/pages/HomePage.tsx:189`; `clinical/src/savedSearches.ts:94`; `packages/ui/src/pendingWrites.ts:276`.
- Issue: on a shared reading-room workstation, the previous user's session and patient-identifying data remain until expiry or manual clearing. Per-user keys do not help after the user has gone.
- Fix: add sign-out that clears all app-owned keys and the OIDC store. Move drafts and queues to IndexedDB with a TTL, or to the server. Consider `sessionStorage` for the OIDC user.

**M14. "Reset preferences" deletes unsent report drafts.** `packages/ui/src/ErrorBoundary.tsx:34-46,134-136`; `packages/ui/src/pendingWrites.ts:276` (key `pacs:pending-writes:`); `clinical/src/reportDraftStore.ts:51-73` (key `pacs:clinical:report-draft:`).
```ts
const APP_STORAGE_KEY_PATTERN = /^pacs[.:-]/i;
... storage.removeItem(key) ...
```
- Issue: the reset removes every app key, including the only local copy of unsent report text and the replay queue for it. The copy says "Preferences cleared".
- Failure: a render error occurs while a radiologist has an unsaved report. They click "Reset preferences" and lose the mirror and the queued draft.
- Fix: exclude draft and pending-write keys from the reset, or warn and offer to save first.

**M15. Row keyboard handler hijacks Enter and Space from nested controls.** `admin/src/components/AdminCommon.tsx:73-78` (used by `QueueJobsTable.tsx` for View details, Retry and Discard); `clinical/src/pages/StudyListPage.tsx:1221-1230` (Open viewer, Open report, Pin and the select checkbox).
```ts
const onRowKeyDown = (event, index) => {
  ...
  if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); selectRow(index); }
};
```
- Issue: the handler is on `<tr>` and has no `event.target === event.currentTarget` check. Enter and Space on a focused nested button are default-prevented, so the button's click does not fire and the row is selected instead. QueueJobsTable's stop-propagation is for mouse clicks only.
- Failure: a keyboard user tabs to "Retry" on a failed job and presses Enter. The job is not retried and the detail panel opens. Equivalent on study rows: Enter on "Open report" selects the row.
- Fix: check `event.target === event.currentTarget` before handling row keys, or handle only keys whose target is the row.

**M16. Admin dirty-state coverage is partial, and admin has no route guard.** See §4. Citations: `admin/src/pages/Hl7ApplicationsPage.tsx:237`; `RoutingPage.tsx:232`; `RetentionPage.tsx:220,367`; `StoragePage.tsx:154`; `ReportTemplatesPage.tsx:68,158,184`; `ReportingPage.tsx` (save and discard; no route guard). `packages/ui/src/useDirtyGuard.ts` is not used in admin.
- Issue: "New" and "Add" buttons and row clicks in these editors silently discard edits. Navigating to another admin page discards a dirty Reporting form.
- Fix: route every selection and "New" action through `guardSelectionChange`. Wire `useDirtyGuard` in `AdminApp` for navigation.

**M17. Numeric inputs fall back to the previous value, so the field cannot be cleared.** `admin/src/pages/RetentionPage.tsx:235` and `:383`; `admin/src/pages/ReportingPage.tsx` (HL7 listener port and HIS cache TTL, `Number(event.target.value) || config.…`).
```tsx
<input type="number" ... value={String(draft.keep_days)} onChange={(event) => setDraft({ ...draft, keep_days: Number(event.target.value) || draft.keep_days })} />
```
- Issue: clearing the field produces `Number('') === 0`, which is falsy, so the old value is kept and re-rendered. The user cannot clear the field, and typing a new value appends to the old one. This is the same silent-fallback pattern that the routing quiet period fixed (`routingValidation.ts`, G7).
- Fix: keep the input as a string, validate on blur or save, and show an error as the routing page does.

**M18. Routing "Preview before save" tests the saved rule, and forward rules can be saved without a destination.** `admin/src/pages/RoutingPage.tsx:190` (copy), `:143` and `:154` (`testAdminRoutingRule(selected.id, …)`); save payload `destination_ae: … || null` (`:~96-102`); `admin/src/routingValidation.ts` `canSaveRoutingRule` (no destination check).
- Issue: preview cannot test unsaved edits, so the copy is misleading. A forward or anonymize-forward rule with an empty destination is accepted by the UI and sent as `null`.
- Failure: an operator edits a rule, previews it, sees the old count, and saves. A new forward rule with no destination is saved and starts matching production traffic.
- Fix: make preview take the draft payload, or change the copy. Require a destination for forward actions, and validate it against the AE list.

**M19. Confirmation severity is inconsistent for the same class of action.** `admin/src/pages/QueuesPage.tsx:251-258` (reject from QC backlog, `tone: 'warning'`, no phrase) versus `admin/src/pages/StudiesPage.tsx:199` (reject, `requiredPhrase: 'REJECT'`). Storage default and read-only toggles (`StoragePage.tsx:177-179`) and MWL cancel (`MwlPage.tsx:120`) save with no confirmation.
- Issue: the QC path rejects the study and hides it from clinical search, with no typed phrase. Changing the default ingest store or cancelling a scanner worklist item has similar operational impact and no confirmation.
- Fix: use one reject flow with a typed phrase, and confirm operational toggles.

**M20. Hl7 "Link" uses `patient_hint` as a patient MRN.** `admin/src/pages/QueuesPage.tsx:214`.
```tsx
requestAction({ ... message: `Message ${item.id} will be linked using the available patient/accession hints…` }, () => apiClient.linkAdminRejectedHl7(item.id, { patient_mrn: item.patient_hint ?? undefined }), ...)
```
- Issue: the hint is free text from the HL7 sender. It is not validated and may be a name or a non-MRN identifier, yet it is sent as `patient_mrn`.
- Failure: a rejected message is linked to the wrong patient because the hint matched an unrelated MRN.
- Fix: show the hint and the candidate patient for confirmation, and require a typed MRN.

**M21. Several list loads have no request sequencing, and some filters fire a request per keystroke.** `admin/src/pages/QueuesPage.tsx` `reload` (no abort, `:29-112`) and filter input (`:313`, `updateFilters` per keystroke, which reloads three endpoints); `admin/src/pages/StudiesPage.tsx` `search`; `admin/src/pages/Hl7ApplicationsPage.tsx:118,375` (per keystroke); `clinical/src/pages/StudyListPage.tsx:297-327` (no sequence guard); `clinical/src/pages/PatientSearchPage.tsx:39-57` (`loadMore` uses a stale `results.items` and has no guard against a changed query); `clinical/src/pages/ShareLinksPage.tsx:94-110` (abort controller created but not passed to `searchStudies`).
- Issue: an earlier slow response can overwrite a later one. Per-keystroke requests multiply load. The worklist already has the right pattern (`WorklistPage.tsx:~151-158`, sequence counter plus abort).
- Fix: adopt the worklist pattern everywhere, and debounce text filters.

**M22. Dictation sends audio to a third party in Chrome, with no disclosure.** `clinical/src/speechInput.ts:236-243` (comment), `clinical/src/components/DictationButton.tsx`.
- Issue: clinical dictation can leave the deployment through the browser's cloud speech service, and the UI does not say so. The code comment says this is a real consideration for clinical deployment.
- Fix: disclose in the UI, let the deployment disable the feature, or use a local or BFF-hosted engine. (Verify the engine Chrome uses in the target environment.)

**M23. Public share landing and link tokens are logged and stored in plain text.** `bff-svc/src/clinical/shareLinkService.ts:70-80` (`token = randomToken()`, stored in `state.shareLinksById`, returned in list responses). Verify whether `listShareLinks` returns `token` to the owner, and whether the state file is protected.
- Fix: store only a hash of the token, and return the URL only once at creation.

**M24. No focus management and no per-route title on navigation; no skip link.** `admin/index.html:10` and `clinical/index.html:10` are static titles. Neither `App.tsx` moves focus on `hashchange`.
- Issue: screen-reader and keyboard users are not told that the page changed.
- Fix: update `document.title` and move focus to the page heading on route change. Add a skip link to `main`.

**M25. RTL is not implemented beyond the document attributes.** Evidence in §1 (no `[dir=rtl]` rules; physical properties at `clinical/src/styles.css:560,687,746`). Admin strings are not translated (`admin/src/App.tsx:91-94`).
- Issue: Arabic users get a mirrored text flow but physical left and right layout (accent borders, drawer side, dropdown offsets).
- Fix: convert to logical properties (`border-inline-start`, `inset-inline-end`) and add RTL-specific overrides. Translate the admin console, or state that it is English-only.

### 🟢 Minor

- **m1.** `admin/src/pages/AuditPage.tsx:93`: `rowLabel="Audit event\" onRowClick=…` contains a stray backslash, so the row announcement reads "Audit event\ 1 of N selected." JSX strings do not process escapes.
- **m2.** `admin/src/pages/OperationsPage.tsx:54`: `const [window, setWindow] = …` shadows the global `window`. Compiles today; a future `window.setTimeout` would fail to type-check or misbehave.
- **m3.** Dead code: `admin/src/pages/ConfigPage.tsx` (not imported), `clinical/src/clinicalRouteRenderer.tsx` (duplicate of `App.tsx` `renderRoute`). Delete or wire one.
- **m4.** Duplicate sign-in redirect: the admin login and loading screens render `PromoSlot`, whose request calls `getBearerToken()` and starts a second `signinRedirect` (`admin/src/App.tsx:97-98`; `packages/api-client/src/index.ts:1414-1419`; `packages/auth/src/index.ts`). Benign, but unnecessary.
- **m5.** `packages/ui/src/useExitTransition.ts`: `setPhase((current) => { … timer.current = setTimeout(…) })` schedules a side effect inside a state updater. Under React StrictMode in development it can schedule two timers, so `onClose` may run twice.
- **m6.** `clinical/src/pages/ShareLinksPage.tsx:94-110`: the study-picker `AbortController` is never passed to the request, so `abort()` does nothing.
- **m7.** `clinical/src/pages/ShareLinksPage.tsx:139`: share permissions are hard-coded (`view_images: false, download_dicom: false`). The study list lets the user choose. Inconsistent.
- **m8.** `clinical/src/pages/StudyListPage.tsx` share form: a blank reason is replaced with "Clinical collaboration from study list". The screen doc says the reason is "required (for audit)" (`pacs-clinical-portal-screens.md:742-757`). The implementation falls back to a canned reason.
- **m9.** `clinical/src/pages/WorklistPage.tsx` (legacy-action-icons group, ~`:785-830`): Assign, Release and Escalate are shown to every role that can open the page. Non-radiologists get a server 403 on write. Also the "Viewer" link duplicates the viewer icon in the same row (`:793` and `:810`).
- **m10.** `admin/src/pages/UsersPage.tsx` (role toggle buttons): the label flips between "Grant X" and "Revoke X" while `aria-pressed` changes. Toggle buttons should keep a constant label.
- **m11.** `admin/src/components/AdminCommon.tsx:82-98` and `clinical/src/pages/StudyListPage.tsx:1174-1175`: tables use `role="grid"` and `gridcell`, but rows do not have `role="row"`. ARIA grid is incomplete.
- **m12.** `clinical/src/pages/AppChrome.tsx`: `me.feature_flags.reporting_mode` is shown raw (for example `in_app`) in the user card.
- **m13.** `admin/src/pages/Hl7ApplicationsPage.tsx` `buildApplicationPayload`: port is `Number(draft.port)` with no range check. Non-numeric input becomes `NaN` and serializes as `null`.
- **m14.** `clinical/src/reportDraftStore.ts`: local drafts keyed per study are never garbage-collected if the report is never signed or the study leaves the worklist.
- **m15.** `admin/src/routingValidation.ts` `isValidRegex`: uses the JavaScript `RegExp` engine, while the archive's routing engine is not known. A pattern valid in one engine may fail in the other (verify).
- **m16.** `admin/src/pages/MwlPage.tsx` `useEffect` on `[selected]` and `ConfigPage` filter effects: minor, but `ConfigPage` recreates its filtered array each render (dead code, see m3).

---

## 7. Strengths

- **Server-side authorization is consistent and matches the UI.** Admin writes all check `requireAdminWriteAccess` (`bff-svc/src/admin/routes.ts`, `accessPolicy.ts:1-20`). Clinical worklist writes, take-next, sign (author only), acknowledge (recipient role), share revoke (owner or admin) and release (assignee or admin) are enforced on the server (`clinical/accessPolicy.ts:71-75`, `reportService.ts:113-116`, `criticalFindingService.ts:157`, `shareLinkService.ts:130`, `worklistService.ts:168`).
- **No HTML injection sinks** in any frontend (grep for `innerHTML`, `dangerouslySetInnerHTML`, `eval`, `new Function` returned nothing).
- **Bearer-token API design.** One request chokepoint sets `Authorization`, so no cookies are used and CSRF is not an issue. Path params are encoded (`index.ts:1414-1437`).
- **CSV export is safe.** `escapeCsvValue` quotes RFC 4180 fields and prefixes `=`, `+`, `-`, `@`, tab or CR with an apostrophe. Audit and study exports use it. A UTF-8 BOM is included for Excel (`packages/ui/src/csv.ts:12-16,~24-40`).
- **Destructive actions are well designed.** Typed phrases on the highest-impact admin actions. Re-display of the affected record (`details`). Last-admin guard (`userAdminUtils.ts`). Attestation of patient identity before sign and addendum. Undo for STAT and QC flags. Reason fields for STAT escalation that are actually sent.
- **Clinical Home is a good model for partial failure.** `AsyncValue` gives each card loading, ready or error. A "zero" is never shown for a failed request (`asyncValue.ts`, `HomePage.tsx`).
- **Worklist polling is careful.** Background polls stage new rows behind a prompt rather than reordering under the cursor. Abort on superseded requests. Selection preserved across refresh (`WorklistPage.tsx:~140-260`; sequence guard and abort in `load`).
- **Offline model is honest.** Only idempotent report-draft saves are queued. Signing and other writes are explicitly excluded (`connectivity.ts`). Pending writes are namespaced per user.
- **Idempotency keys** on draft save, sign, share create and worklist mutations (`api-client`, `reportService.ts`, `shareLinkService.ts:32`).
- **Accessibility groundwork is real.** Focus trap with inert root, focus restore, live regions with slot alternation, `aria-sort`, `aria-invalid` with `aria-describedby`, roving tabindex tables, keyboard shortcuts with a reference that lists only real bindings (`clinical/src/shortcuts.ts` header).
- **Dirty-form thinking is present.** Admin selection guard with a clear warning. Clinical report author with `beforeunload`, nav and hashchange guards. Whitespace-only edits do not count as changes.
- **Timezone is validated end to end** (client picker, BFF, `setRuntimeTimezone` fallback), with tests (`tests/clinical-timezone.test.ts`).
- **Honest local-MVP labelling** in many places (C-ECHO "placeholder", backup and certificate actions "placeholder", reporting probes "deterministic", HL7 "local-dev catalog"). Several earlier defects called out in `UX_AUDIT_2.md` were fixed (copy link, walk-in form, create user form, allergies rendered, CSV export, drawer semantics).
- **Service worker** caches the shell only; no API or DICOMweb responses, and no non-GET interception (`clinical/public/sw.js:1-22`).
- **Promo placement allowlist** enforced in both client and BFF, with dismissals keyed per promo ID (`PromoSlot.tsx`, `app.ts:263-266`).
- **Viewer links use `rel="noreferrer"`**, and the iframe uses `referrerPolicy="no-referrer"`.
- **Test suite is wide for pure logic**: 10 admin and 17 clinical test files cover validation, routing, sorting, dirty tracking, CSV, dictation text, macros, saved searches, reading queue and timezone.

---

## 8. Open questions

1. **Archive auth on `/dicom-web` (C5).** Does dcm4chee accept anonymous GETs for `/studies/{uid}/thumbnail` and instance retrieval? If yes, what is the intended public exposure?
2. **Signed-report storage (C2).** Is `REPORT_SR_STORE_MODE=mock` acceptable for any pilot? Should radiologists see an explicit "not archived" state?
3. **Sign semantics (C1).** Should signing always sign the latest editor text (client flushes first), or the last saved version (current BFF behaviour)? Which should the attestation text promise?
4. **External-report templates (C3).** Which schemes are intended? Is `iframe` embedding of HIS content a requirement, and if so what sandbox is acceptable?
5. **Anonymize and move (C4).** Is there a plan for real jobs, or should these actions be removed from the console until they exist?
6. **Users and roles (M3).** Is Keycloak integration in scope for the pilot? If not, should this page be disabled or labelled in production builds?
7. **Share-link model (M1, M23).** Are links meant to be recipient-bound? Who is allowed to view the landing page, and is token-in-path acceptable?
8. **Retention and routing execution.** Does anything in the BFF actually run retention, compression or routing rules? The admin path shows only stored rules.
9. **Dictation (M22).** Is cloud speech acceptable under the deployment's privacy policy? Is a consent step required?
10. **Audit redaction.** Who redacts the audit `detail` payload, and where is it tested? `AuditPage` displays it as "redacted detail payload".
11. **Clinical priority writes.** Should the UI hide Assign, Release and Escalate from roles the server rejects (m9)?
12. **Admin Operations.** Is it intended to reflect archive truth or local state? The page does not say which.
13. **Promotion feed.** Is a cross-product promotion appropriate on the admin login and loading screens of a clinical system?
14. **Routing regex engine (m15).** Which engine evaluates routing conditions in the archive, and does the UI validation match it?
15. **Session lifecycle.** Is there a planned sign-out, idle timeout and token-storage change? Clinical shared workstations need all three.

---

## Appendix A. Root tests that cover admin-* and clinical-* code

Tests are pure-function unit tests. None mounts a component, and none exercises a hook or the DOM.

**Admin (10 files)**
- `admin-audit-utils`: default 7-day query, filter trimming and cursor, ISO custom windows, MRN and study UID extraction from detail payloads, resource fallback.
- `admin-chrome-utils`: viewport modes, AE quick-search filter, health and operational notification derivation.
- `admin-confirmation-utils`: required-phrase rules and prompt copy (shared primitive).
- `admin-narrow-viewport`: breakpoints, narrow-friendly route list, unknown-route default.
- `admin-operations`: window validation, turnaround from acquisition to first signature, percentiles, SLA breach boundary, reader attribution, unassigned backlog, duration formatting.
- `admin-promo-slot`: placement denylist, dismissal persistence (per ID, idempotent, bounded, junk-tolerant).
- `admin-quick-search-combobox`: option flattening, ids, active-option movement with wrap.
- `admin-routing`: route labels, audit-event to route mapping.
- `admin-routing-validation`: blank conditions, regex validity, save gate, quiet-period parsing, match-count copy and broad-match threshold.
- `admin-user-admin`: create-user validation (username charset, duplicate, roles required), role toggle, grant and revoke copy, last-admin warnings, audit CSV escaping and filename.

**Clinical (17 files)**
- `clinical-compare-utils`: nearest prior, compare-with-prior viewer URL.
- `clinical-dashboard-state`: async state zero versus failure, walk-in validation, per-user walk-in key, health staleness label (imports `HomePage` and admin `chromeUtils`).
- `clinical-deadline`: escalation deadline framing, past-due, absolute and relative formatting.
- `clinical-dictation`: text insertion at caret, spoken punctuation, speech engine interface and error messages.
- `clinical-prior-studies`: relevant-prior rules, client matches server (imports `bff-svc/src/clinical/priorStudies`), summary, nearest-prior choice.
- `clinical-reading-queue`: enter and move through queue, drop and clamp, expiry, per-user key, junk tolerance.
- `clinical-report-draft`: dirty tracking (whitespace-insensitive), per-user and per-study keys, save-state copy.
- `clinical-report-macros`: trigger rules, expansion on delimiter, normal-for-modality inference, favourites order, persistence.
- `clinical-saved-searches`: store keys (worklist key unchanged), per-user, upsert, cap, undo index, study-list save gate.
- `clinical-shortcuts`: reference groups, `?` shortcut matching.
- `clinical-state-repository`: BFF Postgres and file repository adapter and config (imports `bff-svc`).
- `clinical-status-tone`: colour ramp rules for clinical state.
- `clinical-study-list`: age at exam, sortable columns, share email validation, bulk viewer cap (imports `StudyListPage`).
- `clinical-study-rows`: preview column excluded from export, modality glyphs, outstanding-row weight.
- `clinical-study-table-preferences`: column model, locked columns, reordering, widths, stored-preference repair, per-user key.
- `clinical-timezone`: timezone validation client and BFF agreement, picker filtering (imports `bff-svc/src/clinical/clinicalStoreUtils.js`).
- `clinical-worklist`: queue-age tone, clock injection, saved-search key, pending-update copy, abort detection (imports `WorklistPage`, `api-client`).

**Shared UI (for context):** `ui-announcer`, `ui-connection`, `ui-csv`, `ui-dirty-guard`, `ui-error-boundary`, `ui-exit-transition`, `ui-i18n`, `ui-toast-store`.

**Coverage gaps the tests leave.** None of these would catch:
- C1 (sign uses stale draft) or C2 (mocked SR toast).
- C3 (scheme check): `tests/bff.test.ts:1234-1247, 2202-2218, 2272-2273` only use `https://` templates and the default sandbox as a valid value. There is no negative test.
- M1 (share email bypass), M2 (dropped reasons), M4 (autosave ceiling), M5 (template overwrite), M7 (popup check), M8 (bulk partial failure), M9 (stale patient), M10 (fabricated source AE), M11 (timezone inputs), M14 (reset deletes drafts), M15 (row key handling), M17 (numeric fallback), M18 (routing preview).

---

## Appendix B. Intent versus implementation (from the four docs, briefly)

| Intent | Source | Implementation |
|---|---|---|
| Anonymize modal with Basic, Research or Custom profile | `pacs-admin-console-screens.md:221` | One button, no profile, no-op (`arcLightAdminClient.ts:370-372`) |
| C-ECHO result with round-trip time and negotiated syntax | `pacs-admin-console-screens.md:326` | Placeholder `status:'planned'` (`arcLightAdminClient.ts:293-296`) |
| Share reason required and audited | `pacs-clinical-portal-screens.md:742-757` | Default fallback text; revoke reason discarded (`StudyListPage.tsx` share; `ShareLinksPage.tsx:179-183`) |
| Users are Keycloak-backed | `adminRouting.ts:22`; `UsersPage.tsx:224` | In-memory Map (`keycloakAdminClient.ts:25`) |
| Create user, no demo account | `UX_AUDIT_2.md` G4 | Implemented, but the reason is not sent (M2) |
| CSV export with honest scope | `UX_AUDIT_2.md` G5 | Implemented, page-scoped and labelled |
| Copy share link | `UX_AUDIT_2.md` H1 | Implemented (`ShareLinksPage.tsx` `copyLink`) |
| Allergies visible | `UX_AUDIT_2.md` H7 | Implemented (`PatientDetailPage.tsx:181-190`) |
| Dirty guard across six admin editors | `UX_AUDIT_2.md` F5 | Partial: five editors on selection only (M16) |
| `lang` and `dir` from user preference | `UX_AUDIT_2.md` I4 | Set at runtime; static `index.html` still `lang="en"` |
| Thumbnails from `/studies/{uid}/thumbnail` | `UX_DIRECTION.md` 2.1 | Implemented; depends on unauthenticated DICOMweb (C5) |
