# PACS_Sys — BFF service (bff-svc)

> **Area analysis of PACS_Sys** for the [PAXS Archive vs PACS_Sys comparison](../../COMPARISON_PACS_Sys.md). Static, read-only analysis of the `PACS_Sys` repository at HEAD `2ebdbf4` **plus its uncommitted working tree** (2026-10-10). Nothing was built, run or modified. Secret values are never reproduced. Severity labels (🔴/🟡/🟢, RED/YELLOW, CRITICAL/MAJOR) are the analyst's; the comparison document carries the consolidated view.

Repo: `C:\Users\dody1\source\repos\PACS_Sys` (working tree as found, uncommitted work present, nothing modified).
Scope: `bff-svc/src/**` (50 files, 15,142 lines), `bff-svc/package.json`, `bff-svc/tsconfig.json`, `bff-svc/openapi.yaml` (3,034 lines), plus skimmed `bff-api-surface.md` (1,922 lines) and `hl7-oru-parsing-approach.md` (547 lines).
Method: read every file under `src/` in full. Route inventory, outbound calls, config defaults, and absence claims were checked with Grep and read-only counts. Nothing was built, run, installed, or tested. `tests/bff.test.ts` (67 cases) exists but was not executed.

Status legend:
- **Real**: BFF code that runs against live infrastructure (arc-light DICOMweb QIDO/STOW, Keycloak JWKS, Postgres, or the file store) or against BFF-owned persistent state. State-backed flows are real logic whose durability depends on `CLINICAL_STATE_MODE` (default `file`).
- **Adapter-mock**: in-memory seed data or placeholder. No external call, or the response does not reflect a real effect.
- **Planned**: documented only.
- **Absent**: no code.
- **N/A (arc-light)**: archive capability handled by dcm4chee-arc-light, not by the BFF.

---

## 1. Purpose and architecture

### 1.1 What the BFF is
- Express 4 with TypeScript (ESM, NodeNext). Entry point `src/index.ts:7-15`: `loadConfig()` → `createClinicalStateRepository` → `createClinicalStore` → `createApp` → `startHl7OruMllpListener` → `app.listen(config.port)`. `app.listen` has no host argument (`src/index.ts:13`), so it binds all interfaces. Default port 3000 (`src/config.ts:123`).
- Package `@pacs/bff-svc` 0.1.0. Dependencies: `express`, `cors`, `dotenv`, `jose`, `pg`, `zod` (`bff-svc/package.json`). **`zod` is declared but never imported** (grep: 0 imports in `src/`).
- Config loads from the **repo-root** `.env` (`src/config.ts:4-6` resolves `../../.env`). `.env` is git-ignored (`.gitignore`). `.env.example` is tracked.

### 1.2 What it does (Real unless noted)
- **Auth**: OIDC bearer JWT validation against Keycloak JWKS, plus a static dev-token mode (`src/auth.ts`).
- **RBAC** over six Keycloak realm roles (`src/clinical/accessPolicy.ts`, `src/admin/accessPolicy.ts`).
- **Study metadata** from arc-light via DICOMweb QIDO (`src/arcLightClient.ts:410-438`), normalized into patient and study rows. Study search, patient search, and study context are built on this.
- **Worklist**: per-study assignment, take-next, release, priority, and summary, stored in BFF state (`src/clinical/worklistService.ts`).
- **Reporting**: drafts, sign (preliminary, final, corrected), addendum, withdraw, and history (`src/clinical/reportService.ts`). Each signed revision produces a Basic Text DICOM SR (`src/clinical/reportSrWriter.ts`). The SR goes to arc-light via STOW only when `REPORT_SR_STORE_MODE=stow`. The default is `mock` (`src/config.ts:144`, `src/arcLightClient.ts:161-168`).
- **Attestation** for sign and addendum: the displayed patient and study identity must match the live archive (`src/clinical/routes.ts:848-878`).
- **External HIS report sources**: HL7 ORU revisions stored locally, plus REST, PDF, and iframe adapters (`src/clinical/reportSourceResolver.ts`, `src/clinical/externalReportAdapters.ts`). Precedence is set by a reporting-mode value.
- **Critical findings** with a due time and read-triggered escalation (`src/clinical/criticalFindingService.ts`).
- **Patient identity aliasing** from HL7 ADT merges (A40, A47, A34). A37 unmerge is logged only (`src/clinical/hl7AdtIngestion.ts`).
- **QC flags** and **IOCM rejections**, both local to BFF state (`src/clinical/studyGovernanceService.ts`).
- **Metadata-only share links** and in-app and email notifications (`src/clinical/shareLinkService.ts`, `src/clinical/notificationService.ts`).
- **HIS launch deep links** (in-memory, single-use) and **HIS order ingest** (`src/integration/*`).
- **HL7 MLLP TCP listener** for ORU and ADT on port 2575 (`src/clinical/hl7OruMllpListener.ts`).
- **Department operations** TAT and SLA aggregation (`src/clinical/operationsService.ts`).
- **Admin API** (`src/admin/routes.ts`), mostly backed by in-memory mocks (§2.5, §2.6, §6).
- **Prometheus-style `/metrics`** (`src/metrics.ts`) and an **audit log** (`src/audit.ts`).

### 1.3 What it does NOT do
- **Does not proxy pixel data, WADO, or thumbnails.** It returns `dicomweb_base` (`/dicom-web`) and the caller's own bearer token to the browser (`src/app.ts:333-340`, `src/clinical/clinicalStoreUtils.ts:111-119`). The thumbnail link is a URL string only (`src/dicomwebLinks.ts:51-55`). The browser fetches directly through Traefik `/dicom-web` (`infra/traefik/dynamic/routers.yml:16-21`).
- **Attaches no credentials to arc-light calls.** `storeDicomSr`, `qidoStudies`, and `qidoSeries` send only `Accept` or `Content-Type` headers (`src/arcLightClient.ts:178-185, 429-431, 460-462`).
- **No DIMSE** (C-STORE, C-FIND, C-ECHO, C-MOVE). The only DIMSE mention is an admin C-ECHO placeholder (`src/admin/arcLightAdminClient.ts:293-296`).
- **No MWL SCP and no MPPS.** Walk-in and admin MWL entries stay in an in-memory map (`src/admin/mwlAdminClient.ts:45-140`).
- **No FHIR, no IHE PIX, PDQ, or XDS-I, no multi-tenancy or site scoping, no peer review, no ward scoping.** All grep-verified. Ward values exist only on mock users (`src/admin/keycloakAdminClient.ts:12, 57`).
- **No Keycloak admin REST calls.** `src/admin/keycloakAdminClient.ts` keeps users in a `Map` (lines 25-29).
- **No arc-light admin or config API calls**, despite `ARC_CONFIG_BASE` being configured (`src/config.ts:133`). `src/admin/arcLightAdminClient.ts` contains no `fetch` (grep).
- **No scheduler or background worker.** Critical-finding escalation runs only when a list is read (`src/clinical/criticalFindingService.ts:38`). No `setInterval` or cron appears in `src/` (grep).
- **No rate limiting, no `trust proxy` setting, no security-header middleware** (grep).

### 1.4 External systems

| System | Direction and protocol | BFF code (evidence) | Status | Auth and timeout |
|---|---|---|---|---|
| dcm4chee-arc-light QIDO-RS (`/studies`, `/studies/{uid}/series`) | outbound HTTP GET | `src/arcLightClient.ts:410-469` | Real | no credentials sent; **no timeout** (`:429`, `:460`) |
| arc-light STOW-RS (`POST /studies`, multipart SR) | outbound HTTP POST | `src/arcLightClient.ts:161-196`; default mock at `:162-168`; default mode `src/config.ts:144` | Adapter-mock by default; real code path when `REPORT_SR_STORE_MODE=stow` | no credentials; **no timeout** (`:178`) |
| arc-light WADO, thumbnails, pixels | browser → Traefik `/dicom-web` | `src/dicomwebLinks.ts:51-55`; `src/app.ts:336-337` | N/A (arc-light) | edge router has **no auth middleware** (`infra/traefik/dynamic/routers.yml:16-21`) |
| arc-light MLLP (HIS order forward) | outbound TCP | `src/integration/hisMllpClient.ts:14-60`; `src/integration/hisOrderIngestService.ts:58-65` | Real only when `HIS_ORDER_FORWARD_MODE=mllp`; default `mock` (`src/config.ts:120`) | 5 s timeout; circuit breaker |
| Keycloak JWKS (`{issuer}/protocol/openid-connect/certs`) | outbound HTTP GET | `src/auth.ts:59, 96-99` | Real (oidc mode) | `jose` remote JWKS; no explicit cache setting (doc claims 1 h, `bff-api-surface.md:17`) |
| Keycloak admin REST | configured (`src/config.ts:129-132`), not called | `src/admin/keycloakAdminClient.ts` (in-memory) | Adapter-mock | n/a |
| HL7 ORU and ADT from HIS (inbound MLLP) | inbound TCP, default ON, 0.0.0.0:2575 | `src/clinical/hl7OruMllpListener.ts:15-37`; `src/config.ts:163-165` | Real (parse, state write); **unauthenticated** | none; no frame cap |
| HIS REST reports (`/reports/{acc}`, `/reports?accession=`) | outbound HTTP GET | `src/clinical/externalReportAdapters.ts:23-70` | Real when enabled (the reporting config is a mock singleton) | Basic auth uses literal `placeholder` password (`:289`); **no timeout** (`:50`) |
| HIS PDF | outbound HEAD only when `pdf_mirror_eagerly`; otherwise URL pass-through | `src/clinical/externalReportAdapters.ts:72-114` | Real HEAD; nothing is mirrored although the field is called `mirrored` | **no timeout** (`:98`) |
| HIS iframe | URL pass-through to browser with sandbox flags | `src/clinical/externalReportAdapters.ts:116-133` | N/A (browser) | n/a |
| HIS report callback | outbound HTTP POST JSON; `X-PACS-Signature` only if a secret is set | `src/integration/hisReportCallbackService.ts:19-70, 78-82` | Real when enabled (default off, `src/config.ts:159`) | 3 attempts, 5 s abort, circuit breaker; fire-and-forget, no outbox |
| HIS launch deep links | inbound mint, resolve, exchange | `src/integration/hisDeepLinkService.ts` | Real (in-memory Map) | none on public resolve |
| Prometheus HTTP API | outbound GET `/api/v1/query` | `src/admin/systemClient.ts:214-239` | Real when `METRICS_BASE_URL` set; otherwise placeholder (`:178-198`) | 5 s abort |
| Prometheus scrape of BFF | inbound GET `/metrics` | `src/metrics.ts:36-85`; `src/app.ts:61` | Real | **unauthenticated** |
| Postgres clinical state | outbound SQL | `src/clinical/clinicalStateRepository.ts:251-261, 362-458` | Real when `CLINICAL_STATE_MODE=postgres` (default `file`, `src/config.ts:69-74`) | pool max 10 |
| Postgres audit | outbound SQL | `src/audit.ts:244-411` | Real when `AUDIT_SOURCE=postgres` (default `file`, `src/config.ts:80-83`) | pool max 5; fire-and-forget writes |
| Email or SMTP | none | `src/clinical/notificationService.ts:43-74` | Adapter-mock (logs JSON to stdout, records status `sent`) | n/a |

---

## 1.5 Module map

| File or dir | Lines | Purpose |
|---|---:|---|
| `src/index.ts` | 14 | Process entry: wires repository, store, app, MLLP listener; `listen` on all interfaces |
| `src/app.ts` | 578 | Express app: global middleware, app-level routes (HIS deep link, HIS orders, `/users/me`, `/v/s/:token`, `/studies/:uid/access`, `/studies/:uid/context`, `/hanging-protocols/match`, promotions), mounts clinical and admin routers |
| `src/auth.ts` | 136 | Bearer auth: OIDC JWT via JWKS, dev-token mode, public bypass list, realm-role extraction |
| `src/config.ts` | 168 | Env to typed `BffConfig`; defaults and validation (fail-closed where stated) |
| `src/types.ts` | 34 | `Role`, `AuthUser`, Express request augmentation, error body type |
| `src/errors.ts` | 22 | `sendApiError` envelope `{error:{code,message,trace_id,detail}}` |
| `src/audit.ts` | 566 | Audit service: memory, file, and Postgres sinks; redaction; query filters |
| `src/metrics.ts` | 98 | `/metrics` text; request counters; p50 and p95 latency samples |
| `src/dicomwebLinks.ts` | 55 | Thumbnail URL on `DICOMWEB_PUBLIC_BASE` |
| `src/arcLightClient.ts` | 674 | arc-light QIDO search, study summary, series, patient derivation, STOW SR |
| `src/viewer/studyPresentation.ts` | 420 | Seeded hanging-protocol rules (5), region and priors helpers; pure logic |
| `src/clinical/routes.ts` | 1,011 | Clinical router under `/api/v1`: 30 operations (patients, studies, worklist, reports, critical flags, share links, events) |
| `src/clinical/clinicalStore.ts` | 431 | Facade (`DefaultClinicalStore`) over domain services; preferences, sessions, notes, patient identity |
| `src/clinical/clinicalStateRepository.ts` | 913 | State snapshot type; memory, file, and Postgres repositories; normalizers; alias resolution |
| `src/clinical/clinicalStoreUtils.ts` | 391 | Worklist and report helpers; idempotency keys; optimistic-concurrency assertions; token and cursor helpers |
| `src/clinical/clinicalTypes.ts` | 386 | DTO types, `ClinicalStore` interface, `SseEvent` union |
| `src/clinical/clinicalErrors.ts` | 54 | `ClinicalHttpError` factories (400, 403, 404, 409, 410, 422) |
| `src/clinical/accessPolicy.ts` | 111 | Clinical RBAC role sets and `require*` guards |
| `src/clinical/worklistService.ts` | 234 | take-next, assign, release, priority, list, summary, IOCM eligibility |
| `src/clinical/reportService.ts` | 421 | Draft, sign (SR and STOW), addendum, withdraw, HIS callback dispatch |
| `src/clinical/reportSourceResolver.ts` | 455 | Selects local or HIS report by reporting mode; builds report and history |
| `src/clinical/externalReportAdapters.ts` | 311 | HIS REST fetch and cache, PDF HEAD, iframe URL, source ordering |
| `src/clinical/reportSrWriter.ts` | 135 | Basic Text SR document builder; STOW JSON serializer |
| `src/clinical/studyGovernanceService.ts` | 774 | Study and patient search, prior lookups, IOCM reject and unreject, QC flags |
| `src/clinical/priorStudies.ts` | 135 | Relevant-prior rule: earlier, comparable modality, 5-year window, body-part tolerance |
| `src/clinical/criticalFindingService.ts` | 299 | Create, acknowledge, read-triggered escalation, visibility |
| `src/clinical/shareLinkService.ts` | 185 | Create, list, revoke, landing (records use), events for user |
| `src/clinical/notificationService.ts` | 392 | Notification plans, dedupe, in-app records, log-only email adapter |
| `src/clinical/operationsService.ts` | 333 | TAT, percentile, SLA, reader throughput, unassigned aging |
| `src/clinical/hl7OruMllpListener.ts` | 155 | TCP MLLP server; frame parser; ACK builder; ORU and ADT routing |
| `src/clinical/hl7OruIngestion.ts` | 711 | ORU^R01 parser, filters, dedupe, late-preliminary rule, revisions, redacted preview |
| `src/clinical/hl7AdtIngestion.ts` | 529 | ADT A40, A47, A34, A37 parser; patient-identity alias updates |
| `src/clinical/hl7MessageType.ts` | 24 | MSH message type and family readers |
| `src/integration/hisDeepLinkService.ts` | 121 | Single-use launch tokens, TTL, launch URL builder |
| `src/integration/hisOrderIngestService.ts` | 116 | HIS order validation; mock or MLLP forward |
| `src/integration/hisMllpClient.ts` | 109 | Outbound MLLP client with 5 s timeout |
| `src/integration/hisCircuitBreaker.ts` | 138 | In-process breaker (threshold 5, recovery 30 s); status for `/healthz` |
| `src/integration/hisReportCallbackService.ts` | 90 | Signed HTTP callback with 3 retries |
| `src/admin/routes.ts` | 751 | Admin router under `/api/v1/admin`: 72 operations (70 handler definitions, one of which generates 3 routes), role gate per handler |
| `src/admin/arcLightAdminClient.ts` | 732 | Devices and AE titles, study actions (no-op), reporting config singleton, templates, storage, retention, routing: **in-memory mocks** |
| `src/admin/systemClient.ts` | 248 | Live Prometheus health and metrics when configured; placeholders otherwise |
| `src/admin/systemDetailsClient.ts` | 188 | Versions, backups, certificates, license: **hard-coded**; backups read a log file if configured |
| `src/admin/queueAdminClient.ts` | 455 | Queue jobs: **seeded in-memory** |
| `src/admin/hl7AdminClient.ts` | 323 | HL7 application catalog (mock) and real ingested-message listing |
| `src/admin/mwlAdminClient.ts` | 225 | MWL items: **in-memory** |
| `src/admin/configAdminClient.ts` | 174 | Storage, retention, compression: **in-memory** |
| `src/admin/routingAdminClient.ts` | 189 | Routing rules: **in-memory**; test is a mock preview |
| `src/admin/keycloakAdminClient.ts` | 84 | Users and roles: **in-memory**, no Keycloak calls |
| `src/admin/accessPolicy.ts` | 22 | Admin read (admin, operator) and write (admin) guards |
| `src/admin/adminErrors.ts` | 22 | `AdminHttpError` factories |
| `openapi.yaml` | 3,034 | OpenAPI 3.0.3, v0.5.0: 99 operations, bearer security |
| `package.json`, `tsconfig.json` | — | Scripts `dev`, `build`, `start`, `typecheck` (no test script); ES2022, strict |
| `.data/` (git-ignored) | — | Local `audit-log.json` (452.8 KB, exactly 1,000 events) and `clinical-state.json` (6.8 KB) |
| `dist/` (git-ignored) | — | Local build output, not reviewed |

Total src: 15,142 lines. The two root docs are design documents. Both describe planned behaviour as current in places (see F-52).

---

## 2. API surface

121 operations are implemented. The breakdown is 19 app-level routes, 30 clinical-router routes, and 72 admin-router routes. OpenAPI documents 99 of them, and every documented operation exists in code. The 22 undocumented operations are marked **NO** in the last column.

Auth column: **none** = no bearer required; **any** = any valid JWT with no role check; **clin-read** = admin, operator, radiologist, referring_physician, technologist, or ward_doctor; **admin-read** = admin or operator; **admin-write** = admin. Named roles are listed exactly. Gating is per handler. There is no router-level gate (grep: no `router.use` in `src/clinical/routes.ts` or `src/admin/routes.ts`).

### 2.1 Public, token-gated, and integration

| # | Method | Path | Auth / role gate | Purpose | Status | OpenAPI |
|---|---|---|---|---|---|---|
| 1 | GET | /healthz | none (`src/auth.ts:63`) | liveness; HIS circuit states if integration enabled (`src/app.ts:48-58`) | Real | yes |
| 2 | GET | /metrics | none (`src/auth.ts:64`) | Prometheus text (`src/metrics.ts:36-85`) | Real | **NO** |
| 3 | GET | /v/s/{token} | none (`src/auth.ts:65`) | share landing; metadata only; records a use (`src/clinical/shareLinkService.ts:148-171`) | Real (state); email binding optional | yes |
| 4 | POST | /api/v1/integration/his/deep-links/resolve | none (`src/auth.ts:66`); single-use token | consume HIS launch token; returns study UID, MRN, issuer (`src/app.ts:106-142`) | Real (in-memory) | yes |
| 5 | POST | /api/deep-link/exchange | none (`src/auth.ts:67`) | alias of #4 (`src/app.ts:145`) | Real (in-memory) | yes |
| 6 | POST | /api/v1/integration/his/orders | none (`src/auth.ts:68`); service bearer matched as SHA-256, timing-safe (`src/app.ts:553-575`) | HIS order ingest; mock AA by default | Adapter-mock (default); Real when `mllp` | yes |
| 7 | POST | /integration/his/orders | as #6 (`src/auth.ts:69`) | alias of #6 | Adapter-mock (default); Real when `mllp` | yes |
| 8 | POST | /api/v1/integration/his/deep-links/mint | authenticated, admin or operator; 404 unless `HIS_INTEGRATION_ENABLED` (`src/app.ts:63-73`) | mint 120 s single-use launch token (`src/config.ts:110`) | Real (in-memory) | yes |
| 9 | POST | /api/deep-link/mint | as #8 (`src/app.ts:104`) | alias of #8 | Real (in-memory) | yes |

Note on #6 and #7: `hisOrderForwardMode` defaults to `mock` (`src/config.ts:120`). An accepted order is acknowledged AA and **not persisted or forwarded** (`src/integration/hisOrderIngestService.ts:46-56`).

### 2.2 Any authenticated user, or no role check on the handler

| # | Method | Path | Auth / role gate | Purpose | Status | OpenAPI |
|---|---|---|---|---|---|---|
| 10 | GET | /api/v1/users/me | any (`src/app.ts:189-211`) | profile, prefs, feature flags (`reporting_mode` read from mock config) | Real (identity from JWT; prefs in state) | yes |
| 11 | PATCH | /api/v1/users/me/preferences | any (`src/app.ts:213-218`) | theme, density, timezone (validated IANA), ui_language | Real (state) | yes |
| 12 | GET | /api/v1/users/me/notifications | any (`src/app.ts:220-224`) | notifications for user, role, or creator | Real (state) | yes |
| 13 | GET | /api/v1/users/me/sessions | any (`src/app.ts:226-230`) | one synthetic session (`src/clinical/clinicalStoreUtils.ts:41-49`) | Adapter-mock | yes |
| 14 | DELETE | /api/v1/users/me/sessions/{id} | any (`src/app.ts:232-237`) | adds id to `revokedSessionIds`; **auth never checks it** (F-14) | Real flag, **no enforcement** | yes |
| 15 | GET | /api/v1/events | any; **no role gate** (`src/clinical/routes.ts:524-542`) | last 25 events; client uses `?once=true` (`frontends/packages/api-client/src/index.ts:1096-1097`) | Real (state); cross-user leak (F-06) | yes |
| 16 | GET | /api/v1/studies/{studyUid}/context | any; **no role gate** (`src/app.ts:353-357`) | patient identity, study, priors, report, IOCM (F-01) | Real (arc-light QIDO and state) | yes |
| 17 | GET | /api/v1/studies/{studyUid}/access | clin-read gate (`src/app.ts:298`) | returns `authorized: true` and the caller's bearer (F-16) | Real (QIDO only when patient context given) | yes |
| 18 | GET | /api/v1/hanging-protocols/match | any; **no role gate** (`src/app.ts:428-437`) | seeded protocol match | Real (pure logic) | yes |
| 19 | GET | /api/v1/admin/promotions/active | any (`src/app.ts:250-283`) | seeded HIS cross-promo (placement allowlist) | Adapter-mock | **NO** |
| 20 | POST | /api/v1/admin/promotions/{promoId}/click | any (`src/app.ts:285-292`) | audit only | Real (audit) | **NO** |

### 2.3 Clinical read (clin-read)

| # | Method | Path | Auth / role gate | Purpose | Status | OpenAPI |
|---|---|---|---|---|---|---|
| 21 | GET | /api/v1/patients/search | clin-read (`src/clinical/routes.ts:55-72`) | patient search from the first 200 archive rows (F-33) | Real | yes |
| 22 | GET | /api/v1/patients/{mrn} | clin-read (`:74-88`) | demographics and merge notice | Real | yes |
| 23 | GET | /api/v1/patients/{mrn}/studies | clin-read (`:90-110`) | studies; `status` filter cannot match (F-34) | Real | yes |
| 24 | GET | /api/v1/patients/{mrn}/notes | clin-read (`:112-126`) | notes with supersession chain | Real (state) | **NO** |
| 25 | POST | /api/v1/patients/{mrn}/notes | admin, radiologist, referring_physician, ward_doctor (`:128-138`) | create note | Real (state) | **NO** |
| 26 | GET | /api/v1/studies/search | clin-read (`:140-161`) | study search (QIDO first 200 plus BFF filters) | Real | yes |
| 27 | POST | /api/v1/studies/priors | clin-read (`:172-182`) | batch prior counts for at most 50 rows (body, no PHI in URL) | Real | **NO** |
| 28 | GET | /api/v1/critical-flags | clin-read; visible by creator, admin, or recipient role (`:245-250`; `src/clinical/criticalFindingService.ts:250-252`) | lists critical findings across all patients (F-15) | Real (state) | yes |
| 29 | POST | /api/v1/critical-flags/{id}/acknowledge | clin-read gate, then recipient role or admin (`:355-372`; `criticalFindingService.ts:254-256`) | acknowledge | Real (state) | yes |
| 30 | GET | /api/v1/reports/{studyUid} | clin-read (`:323-334`) | selected report, local or HIS source | Real (plus HIS adapters) | yes |
| 31 | GET | /api/v1/reports/{studyUid}/history | clin-read (`:464-470`) | version list | Real | yes |
| 32 | GET | /api/v1/reports/{studyUid}/attachments/{attachmentId} | clin-read (`:472-478`) | always 404 | Adapter-mock (stub) | yes |
| 33 | GET | /api/v1/share-links | clin-read; owner or admin (`:501-508`; `shareLinkService.ts:104-110`) | list (includes raw token) | Real | yes |
| 34 | DELETE | /api/v1/share-links/{id} | clin-read gate, then owner or admin (`:510-522`; `shareLinkService.ts:129`) | revoke | Real | yes |

### 2.4 QC, walk-in MWL, worklist, reports, share create

| # | Method | Path | Auth / role gate | Purpose | Status | OpenAPI |
|---|---|---|---|---|---|---|
| 35 | POST | /api/v1/studies/{studyUid}/qc-flag | admin, operator, radiologist, technologist (`:184-191`) | raise QC flag | Real (state) | yes |
| 36 | POST | /api/v1/qc-flags/{flagId}/withdraw | same QC write, plus flag author only (`:199-220`) | withdraw own pending flag | Real (state) | **NO** |
| 37 | POST | /api/v1/mwl | admin, technologist (`:222-243`) | walk-in MWL entry | Adapter-mock (in-memory) | yes |
| 38 | GET | /api/v1/worklist | admin, radiologist (`:252-266`) | worklist, scoped; summary via `?summary=true` | Real (state plus 200-row archive window) | yes |
| 39 | POST | /api/v1/worklist/take-next | admin and radiologist role (`:268-283`) | atomic pick | Real (state) | yes |
| 40 | POST | /api/v1/worklist/{studyUid}/assign | admin, radiologist; radiologist self-assign only (`:285-297`) | assign | Real (state) | yes |
| 41 | POST | /api/v1/worklist/{studyUid}/release | admin, radiologist (`:299-311`) | release | Real (state) | yes |
| 42 | POST | /api/v1/worklist/{studyUid}/priority | admin, radiologist (`:313-321`) | priority plus reason | Real (state) | yes |
| 43 | GET | /api/v1/report-templates | admin, radiologist (`:480-486`) | active templates from admin mock store | Adapter-mock | yes |
| 44 | POST | /api/v1/reports/{studyUid}/draft | admin, radiologist; reporting mode must allow authoring (`:374-382`; `:981-984`) | save draft (versioned) | Real (state) | yes |
| 45 | POST | /api/v1/reports/{studyUid}/sign | admin, radiologist; attestation against live archive; author or admin (`:384-415`; `reportService.ts:113`) | sign, write SR; STOW only in `stow` mode | Real (state); SR STOW Adapter-mock by default | yes |
| 46 | POST | /api/v1/reports/{studyUid}/addendum | admin, radiologist (`:417-447`) | addendum on final, corrected, or amended | Real (state); SR STOW as #45 | yes |
| 47 | POST | /api/v1/reports/{studyUid}/withdraw | admin, radiologist; **no author check** (`:449-462`) | withdraw current report (local only) | Real (state) | yes |
| 48 | POST | /api/v1/reports/{studyUid}/critical-flag | admin, radiologist (`:336-353`) | create critical finding | Real (state) | yes |
| 49 | POST | /api/v1/share-links | admin, radiologist, referring_physician (`:488-499`) | create metadata-only share | Real (state) | yes |

### 2.5 Admin read (admin-read)

| # | Method | Path | Auth / role gate | Purpose | Status | OpenAPI |
|---|---|---|---|---|---|---|
| 50 | GET | /api/v1/admin/system/health | admin-read (`src/admin/routes.ts:60-65`) | component health: Prometheus if configured | Real if `METRICS_BASE_URL`, else placeholder | yes |
| 51 | GET | /api/v1/admin/system/metrics | admin-read (`:67-72`) | ingest, storage, performance from Prometheus | Real if configured, else placeholder | yes |
| 52 | GET | /api/v1/admin/system/versions | admin-read (`:104`) | hard-coded versions and digests (`systemDetailsClient.ts:57-67`) | Adapter-mock | **NO** |
| 53 | GET | /api/v1/admin/system/backups | admin-read (`:105`) | backup log if `BACKUP_LOG_PATH`, else stub reporting verified (F-18) | Real if configured; else Adapter-mock | **NO** |
| 54 | GET | /api/v1/admin/system/certificates | admin-read (`:106`) | hard-coded certificates (F-18) | Adapter-mock | **NO** |
| 55 | GET | /api/v1/admin/system/license | admin-read (`:107`) | hard-coded license (F-18) | Adapter-mock | **NO** |
| 56 | GET | /api/v1/admin/operations | admin-read (`:87-102`) | TAT and SLA aggregation over 200 archive rows (F-33) | Real (partial) | **NO** |
| 57 | GET | /api/v1/admin/devices | admin-read (`:109-114`) | device list from AE seeds | Adapter-mock | yes |
| 58 | GET | /api/v1/admin/aetitles | admin-read (`:116-121`) | AE titles | Adapter-mock | yes |
| 59 | GET | /api/v1/admin/aetitles/{aet} | admin-read (`:130-135`) | AE detail | Adapter-mock | yes |
| 60 | POST | /api/v1/admin/aetitles/{aet}/test | admin-read gate (`:152-157`) | C-ECHO placeholder (`arcLightAdminClient.ts:293-296`) | Adapter-mock | yes |
| 61 | GET | /api/v1/admin/studies/search | admin-read (`:159-164`) | study search including rejected, plus local IOCM | Real | yes |
| 62 | GET | /api/v1/admin/studies/{studyUid} | admin-read (`:166-171`) | study detail and series | Real | yes |
| 63 | GET | /api/v1/admin/queues | admin-read (`:203-208`) | queue summary (seeded jobs) | Adapter-mock | yes |
| 64 | GET | /api/v1/admin/queues/{queue} | admin-read (`:225-231`) | queue jobs | Adapter-mock | yes |
| 65 | GET | /api/v1/admin/queues/{queue}/{jobId} | admin-read (`:233-238`) | job detail | Adapter-mock | yes |
| 66 | GET | /api/v1/admin/rejected-hl7 | admin-read (`:254-259`) | dead-letter list (seeded) | Adapter-mock | yes |
| 67 | GET | /api/v1/admin/hl7-applications | admin-read (`:270-275`) | HL7 application catalog | Adapter-mock | yes |
| 68 | GET | /api/v1/admin/hl7-applications/{id}/messages | admin-read (`:298-304`) | real ingested HL7 records plus mock DLQ | Real plus Adapter-mock | yes |
| 69 | GET | /api/v1/admin/qc-backlog | admin-read (`:210-215`) | pending QC flags | Real | yes |
| 70 | GET | /api/v1/admin/users | admin-read (`:306-311`) | user directory with emails; **operator-readable** (F-17) | Adapter-mock | yes |
| 71 | GET | /api/v1/admin/users/{id} | admin-read (`:320-325`) | user detail | Adapter-mock | **NO** |
| 72 | GET | /api/v1/admin/roles | admin-read (`:341-345`) | static role catalog | Real (static) | yes |
| 73 | GET | /api/v1/admin/audit-log | admin-read (`:347-351`) | audit query over the sink | Real | yes |
| 74 | GET | /api/v1/admin/reporting-config | admin-read (`:353-358`) | reporting config singleton (non-persistent) | Adapter-mock | yes |
| 75 | GET | /api/v1/admin/report-templates | admin-read (`:367-372`) | templates | Adapter-mock | yes |
| 76 | GET | /api/v1/admin/storage | admin-read (`:452`) | storage descriptors | Adapter-mock | yes |
| 77 | GET | /api/v1/admin/retention-rules | admin-read (`:456`) | retention rules | Adapter-mock | yes |
| 78 | GET | /api/v1/admin/compression-rules | admin-read (`:461`) | compression rules | Adapter-mock | **NO** |
| 79 | GET | /api/v1/admin/routing-rules | admin-read (`:466`) | routing rules | Adapter-mock | yes |
| 80 | POST | /api/v1/admin/routing-rules/{id}/test | admin-read gate (`:471`) | mock preview | Adapter-mock | yes |
| 81 | GET | /api/v1/admin/mwl | admin-read (`:473`) | MWL items | Adapter-mock | yes |

### 2.6 Admin write (admin-write)

| # | Method | Path | Auth / role gate | Purpose | Status | OpenAPI |
|---|---|---|---|---|---|---|
| 82 | POST | /api/v1/admin/aetitles | admin-write (`:123-128`) | create AE | Adapter-mock | yes |
| 83 | PUT | /api/v1/admin/aetitles/{aet} | admin-write (`:137-143`) | update AE | Adapter-mock | yes |
| 84 | DELETE | /api/v1/admin/aetitles/{aet} | admin-write (`:145-150`) | delete AE (DCM4CHEE protected) | Adapter-mock | yes |
| 85 | POST | /api/v1/admin/studies/{studyUid}/reject | admin-write (`:173-179`) | local IOCM reject (no DICOM change) | Real (local) | yes |
| 86 | POST | /api/v1/admin/studies/{studyUid}/unreject | admin-write (`:181-187`) | local IOCM rescind | Real (local) | yes |
| 87 | POST | /api/v1/admin/studies/{studyUid}/anonymize | admin-write (`:189-194`) | **returns `accepted`, does nothing** (F-09) | Adapter-mock (no-op) | yes |
| 88 | POST | /api/v1/admin/studies/{studyUid}/move | admin-write (`:196-201`) | **returns `accepted`, does nothing** (F-09) | Adapter-mock (no-op) | yes |
| 89 | POST | /api/v1/admin/queues/{queue}/{jobId}/retry | admin-write (`:240-245`) | retry seeded job | Adapter-mock | yes |
| 90 | DELETE | /api/v1/admin/queues/{queue}/{jobId} | admin-write (`:247-252`) | discard seeded job | Adapter-mock | yes |
| 91 | POST | /api/v1/admin/qc-flags/{flagId}/dismiss | admin-write (`:217-223`) | dismiss QC flag | Real (state) | yes |
| 92 | POST | /api/v1/admin/rejected-hl7/{id}/resubmit | admin-write (`:261-268`) | mock DLQ action | Adapter-mock | **NO** |
| 93 | POST | /api/v1/admin/rejected-hl7/{id}/link | admin-write (`:261-268`) | mock DLQ action | Adapter-mock | **NO** |
| 94 | POST | /api/v1/admin/rejected-hl7/{id}/discard | admin-write (`:261-268`) | mock DLQ action | Adapter-mock | **NO** |
| 95 | POST | /api/v1/admin/hl7-applications | admin-write (`:277-282`) | create HL7 app | Adapter-mock | yes |
| 96 | PUT | /api/v1/admin/hl7-applications/{id} | admin-write (`:284-289`) | update HL7 app | Adapter-mock | yes |
| 97 | DELETE | /api/v1/admin/hl7-applications/{id} | admin-write (`:291-296`) | delete HL7 app | Adapter-mock | yes |
| 98 | POST | /api/v1/admin/users | admin-write (`:313-318`) | create user; roles unvalidated (F-19) | Adapter-mock | yes |
| 99 | PATCH | /api/v1/admin/users/{id} | admin-write (`:327-332`) | patch user (spreads raw body, F-19) | Adapter-mock | **NO** |
| 100 | DELETE | /api/v1/admin/users/{id} | admin-write (`:334-339`) | disable user | Adapter-mock | **NO** |
| 101 | PUT | /api/v1/admin/reporting-config | admin-write (`:360-365`) | update reporting config (validated) | Adapter-mock | yes |
| 102 | POST | /api/v1/admin/reporting-config/ingest-hl7 | admin-write (`:425-443`) | ADT or ORU ingest through the real parser | Real (state) | yes |
| 103 | POST | /api/v1/admin/reporting-config/test-hl7 | admin-write (`:395-423`) | synthetic ORU probe into real state when HL7 enabled (F-27) | Real (state) when enabled; placeholder otherwise | yes |
| 104 | POST | /api/v1/admin/reporting-config/test-his-rest | admin-write (`:445-450`) | placeholder probe | Adapter-mock | yes |
| 105 | POST | /api/v1/admin/report-templates | admin-write (`:374-379`) | create template | Adapter-mock | yes |
| 106 | PUT | /api/v1/admin/report-templates/{id} | admin-write (`:381-386`) | update template | Adapter-mock | yes |
| 107 | DELETE | /api/v1/admin/report-templates/{id} | admin-write (`:388-393`) | delete template | Adapter-mock | yes |
| 108 | POST | /api/v1/admin/storage | admin-write (`:453`) | create storage descriptor | Adapter-mock | yes |
| 109 | PUT | /api/v1/admin/storage/{id} | admin-write (`:454`) | update storage descriptor | Adapter-mock | yes |
| 110 | POST | /api/v1/admin/retention-rules | admin-write (`:457`) | create retention rule | Adapter-mock | yes |
| 111 | PUT | /api/v1/admin/retention-rules/{id} | admin-write (`:458`) | update retention rule | Adapter-mock | yes |
| 112 | DELETE | /api/v1/admin/retention-rules/{id} | admin-write (`:459`) | delete retention rule | Adapter-mock | yes |
| 113 | POST | /api/v1/admin/compression-rules | admin-write (`:462`) | create compression rule | Adapter-mock | **NO** |
| 114 | PUT | /api/v1/admin/compression-rules/{id} | admin-write (`:463`) | update compression rule | Adapter-mock | **NO** |
| 115 | DELETE | /api/v1/admin/compression-rules/{id} | admin-write (`:464`) | delete compression rule | Adapter-mock | **NO** |
| 116 | POST | /api/v1/admin/routing-rules | admin-write (`:467`) | create routing rule | Adapter-mock | yes |
| 117 | PUT | /api/v1/admin/routing-rules/{id} | admin-write (`:468`) | update routing rule | Adapter-mock | yes |
| 118 | PATCH | /api/v1/admin/routing-rules/{id} | admin-write (`:469`) | reorder priority | Adapter-mock | yes |
| 119 | DELETE | /api/v1/admin/routing-rules/{id} | admin-write (`:470`) | delete routing rule | Adapter-mock | yes |
| 120 | POST | /api/v1/admin/mwl | admin-write (`:474-486`) | admin manual MWL | Adapter-mock | yes |
| 121 | PUT | /api/v1/admin/mwl/{id} | admin-write (`:487`) | update MWL; can rewrite `created_by_*` (F-31) | Adapter-mock | yes |

Documentation drift (not code defects): `bff-api-surface.md` describes `/api/v1/users/me/saved-views` (§6.2), `/api/v1/webhooks/arc-light/study-stable` (§14.2), and a `study.arrived` SSE event (§13.1). None of them exists in code or in OpenAPI. The full drift list is F-52.

---

## 3. Security model

### 3.1 Authentication

**OIDC mode (default)**
- Bearer header only: `Authorization: Bearer <token>` (`src/auth.ts:76-82`). Query-string tokens are not accepted.
- Verification: `jose` `jwtVerify(token, jwks, { issuer, audience })` (`src/auth.ts:96-99`). JWKS URL `${keycloakIssuer}/protocol/openid-connect/certs` (`src/auth.ts:59`). Signature, `iss`, `aud`, `exp`, and `nbf` are verified by the library. There is no explicit `algorithms` allowlist, no `azp` or `typ` check, and no JWKS cache setting.
- Defaults: issuer `http://localhost:8081/realms/pacs` and audience `pacs-bff` (`src/config.ts:125-126`). The default issuer is plain HTTP and must be overridden for any real deployment.
- Identity: `sub`, `preferred_username`, `name`, `email` from claims (`src/auth.ts:101-110`). Roles come **only** from `realm_access.roles`, filtered to the six known roles (`src/auth.ts:118-136`). Client roles are ignored.
- Unknown realm roles are dropped silently, so a user with only unknown roles gets `roles: []`. Several routes accept that user anyway (F-01).

**Dev mode**
- Enabled only when `BFF_AUTH_MODE` is exactly `dev` (`src/config.ts:124`). Any other value falls back to `oidc`. This is fail-closed, and `tests/auth-defaults.test.ts:14-17` asserts it.
- In dev mode, the bearer string is looked up in a static table of six users. Each token equals its user key (`src/auth.ts:7-56, 84-93`). Anyone who knows those strings gets that role.
- **No guard exists.** There is no `NODE_ENV` check, no startup refusal, and no bind-address restriction (`src/index.ts:13`). The startup log prints only the mode (`src/index.ts:14`).
- The root `.env` (git-ignored, loaded by `src/config.ts:4-6`) sets `BFF_AUTH_MODE` to the dev value (`.env:3`). `.env.example:5` and `.env.development:5` set `oidc`.
- The prod compose file `infra/docker-compose.prod.yml` defines no BFF service at all (§9 Q2).

**Public bypass list** (`src/auth.ts:62-73`; exact `request.path` equality, except the `/v/s/` prefix). Routes behind it: `/healthz`, `/metrics`, `/v/s/*`, `/api/v1/integration/his/deep-links/resolve`, `/api/deep-link/exchange`, `/api/v1/integration/his/orders`, `/integration/his/orders`. Trailing-slash or case variants do not match the bypass, so they fail closed.

**HIS order service bearer** (separate from JWT): the bearer is compared as SHA-256 hex with `HIS_ORDER_INGEST_BEARER_SHA256` using `crypto.timingSafeEqual` (`src/app.ts:553-568`). If unset, only dev mode accepts the literal `dev-his-integration` (`src/app.ts:570-572`). Otherwise the request gets 401 (`:574`). This is fail-closed and well done (§8).

**No server-side sessions.** Authentication is JWT-only. `DELETE /api/v1/users/me/sessions/{id}` writes a flag that nothing reads (F-14).

**Token forwarding to the browser.** `/api/v1/studies/{uid}/access` and `take-next` return the caller's own JWT as `bearer_for_dicomweb` (`src/app.ts:337`, `src/clinical/clinicalStoreUtils.ts:116`). The access object says it expires in five minutes, but the token lives until its own `exp` (F-16, F-24).

### 3.2 RBAC

Role sets (`src/clinical/accessPolicy.ts:5-21`, `src/admin/accessPolicy.ts:5-11`):

| Capability | Allowed roles | Evidence |
|---|---|---|
| Clinical read | admin, operator, radiologist, referring_physician, technologist, ward_doctor | `accessPolicy.ts:5-6, 23-25` |
| Worklist read and write | admin, radiologist | `accessPolicy.ts:14-15` |
| Take-next | admin and radiologist, plus an explicit radiologist role check | `src/clinical/routes.ts:272` |
| Walk-in MWL write | admin, technologist | `accessPolicy.ts:16` |
| Report author (draft, sign, addendum, withdraw, templates, critical-flag create) | admin, radiologist | `accessPolicy.ts:17-18` |
| Share create | admin, radiologist, referring_physician | `accessPolicy.ts:19` |
| Patient note write | admin, radiologist, referring_physician, ward_doctor | `accessPolicy.ts:20` |
| QC write | admin, operator, radiologist, technologist | `accessPolicy.ts:21` |
| Admin read | admin, operator | `src/admin/accessPolicy.ts:5-7` |
| Admin write | admin | `src/admin/accessPolicy.ts:9-11` |
| HIS deep-link mint | admin, operator | `src/app.ts:70` |

Object-level checks inside services:
- Share revoke and share list: owner or admin (`src/clinical/shareLinkService.ts:129`, `:104-110`).
- Sign: draft author or admin (`src/clinical/reportService.ts:113`).
- Assign and release: a radiologist may self-assign and cannot take another reader's study. Admin may override (`src/clinical/worklistService.ts:129-135, 167-169`).
- Critical acknowledge: recipient role or admin (`src/clinical/criticalFindingService.ts:254-256`).
- Critical visibility: creator, admin, or recipient role (`:250-252`).
- QC withdraw: flag author only (`src/clinical/studyGovernanceService.ts:560-562`).

Enforcement is handler-level only, with **no router-level gate** (grep). The app-level routes `/studies/{uid}/context` and `/hanging-protocols/match`, plus `/events`, carry only authentication (F-01, F-06). `ward_doctor` has no ward scoping. `referring_physician` can read every patient and every critical finding addressed to its role (F-15).

### 3.3 Tenant and site scoping
None. No tenant or site key exists in state or in queries. Facility and application filters for HL7 are per-deployment settings (`src/clinical/hl7OruIngestion.ts:73-123`). The temp-MRN prefix `TEMP-SITE-` is configuration only (`src/config.ts:109`). There is no care-relationship or break-glass check, so any clinical-read role can read any patient (`src/clinical/routes.ts:74-88`).

### 3.4 Audit logging
- **Where**: every `audit()` call goes to the configured sink and also to `console.info(JSON.stringify(event))` (`src/audit.ts:174-188`). Sink selection: `memory`, `file` (default, `src/config.ts:80-83`; file `bff-svc/.data/audit-log.json`, `src/config.ts:67`), or `postgres`.
- **Fields**: `id`, `action`, `user {id, username, roles}`, `resource`, `trace_id`, `at`, `ip`, `detail` (redacted).
- **Redaction**: keys matching `token`, `*_token`, `password`, `secret`, `credential`, `bearer`, `authorization`, `raw_payload`, `raw_hl7`, `full_hl7_payload`, `raw_dicom_json`, `full_dicom_json` are replaced by `[redacted]` (`src/audit.ts:549-566`), to a depth of 4 (`:533-547`). The share-link access event passes `token` (`src/app.ts:241`), and the `token` key is redacted. **Checked: the raw share token does not reach the audit trail.**
- **Retention cap**: memory and file keep the newest **1,000** events (`src/audit.ts:199-200`; file load `:228`). Verified by count: the local `.data/audit-log.json` has exactly 1,000 entries spanning 2026-08-16 to 2026-08-21 (dates only). See F-08.
- **Write path**: the file sink rewrites the whole file synchronously on every event (`src/audit.ts:216, 234-241`). The Postgres sink is fire-and-forget (`:266-272`): a failed insert only logs a warning.
- **PHI in audit detail**: MRN, names, DOB, and sex appear in `patient.read`, `report.attestation.confirmed`, and `study.access.granted` details (`src/clinical/routes.ts:81-86, 391-396`; `src/app.ts:328-332`). See F-26.
- **Who can read**: admin and operator (`src/admin/routes.ts:347-351`). Query filters match on `detail` keys (`src/audit.ts:457-466`).
- **Client IP**: `request.ip` with no `trust proxy` (`src/app.ts:42`; `src/audit.ts:447-449`). Behind the Traefik hop, `ip` is the proxy address (F-43).

### 3.5 Input validation
- JSON bodies: `express.json()` with the default 100 KB limit, strict mode (`src/app.ts:36`). This runs before auth.
- Query and body validation is hand-written (`src/clinical/routes.ts:641-726`; `src/app.ts:540-546`; `src/admin/routes.ts:650-745`). It covers enums, `limit` 1 to 200, `YYYY-MM-DD` dates (`src/arcLightClient.ts:660-663`), base64url JSON cursors (`src/clinical/clinicalStoreUtils.ts:250-259`), and free-text length caps (`:206-210`). **zod is unused.**
- QIDO parameters are built with `URLSearchParams` (`src/arcLightClient.ts:417-420`), so parameter injection is blocked. Study UIDs in paths use `encodeURIComponent` (`:448`). **DICOM wildcard characters `*` and `?` are not stripped from exact-match fields** (`PatientID`, `AccessionNumber`, `IssuerOfPatientID`: `:385-387, 393-394`), so `patient_mrn=*` or an accession containing `*` widens the match (F-12).
- HL7: enum-validated status map (`src/clinical/hl7OruIngestion.ts:17-25, 280-284`); section keys whitelisted (`:315-316`); ADT events whitelisted (`src/clinical/hl7AdtIngestion.ts:12, 222-225`). **No size limit on frames or messages** (F-22).
- SQL: parameterized throughout (`src/audit.ts:284-311, 327, 358-374`; `src/clinical/clinicalStateRepository.ts:437-458`). The clinical schema name is validated (`:890-896`). The **audit schema name is interpolated without validation** (`src/audit.ts:321, 333`). It is environment-controlled only.
- Output: JSON only, with no HTML rendering in the BFF. External URLs (`pdf_url`, `iframe_url`, attachment `url`) pass through with **no scheme validation** (`src/clinical/externalReportAdapters.ts:83, 127, 230-236`). Safety depends on frontend escaping and admin validation (F-28).

### 3.6 Rate limiting, CORS, headers
- **Rate limiting: none** (grep). The public share landing performs a state write per hit (F-13).
- **CORS**: `app.use(cors())` with no options gives `Access-Control-Allow-Origin: *` on every response (`src/app.ts:35`). At the edge, Traefik sets ACAO to `https://pacs.local` with `Access-Control-Allow-Credentials: true` for the `bff-api` router (`infra/traefik/dynamic/routers.yml:104-112`, applied by the router at `:7-12`) (F-50).
- **Security headers**: none in the BFF. Traefik's `security-headers` middleware (HSTS, nosniff, SAMEORIGIN, referrer policy) applies at the edge (`infra/traefik/dynamic/routers.yml:114-127`). `x-powered-by` is not disabled (grep).

### 3.7 Secrets handling
- Env values come from `dotenv`, loaded from the repo-root `.env` (git-ignored). `.env.example` (tracked) names these secret-type keys: `KEYCLOAK_ADMIN_CLIENT_SECRET`, `HIS_ORDER_INGEST_BEARER_SHA256`, `HIS_REPORT_CALLBACK_HMAC_SECRET`, `CLINICAL_STATE_POSTGRES_URL`, `AUDIT_POSTGRES_URL`. Their values were not read or reproduced.
- Good: the HIS order service bearer is stored only as SHA-256 (`src/app.ts:560-562`). Storage secrets are reduced to a `secret_set` boolean (`src/admin/arcLightAdminClient.ts:514-517, 660`). Reporting `token` and `password` are never persisted, only `*_set` flags (`:459-486`).
- Bad: the HIS REST basic-auth password is the literal `placeholder` (`src/clinical/externalReportAdapters.ts:289`; F-29). The Keycloak admin secret is loaded into config (`src/config.ts:132`) and unused (F-19).
- Persisted plaintext: clinical state holds PHI and share-link tokens in plaintext (`src/clinical/clinicalStateRepository.ts:199`; `src/clinical/shareLinkService.ts:71, 77`). The audit log holds PHI in plaintext JSON. Both live under `bff-svc/.data/`, which is git-ignored (`src/config.ts:67-68`).

### 3.8 Share-link design
- **Not signed.** The token is `crypto.randomBytes(24)` base64url, 192 bits (`src/clinical/clinicalStoreUtils.ts:373-375`). The URL is `${SHARE_LINK_BASE_URL}/v/s/${token}` (`src/clinical/shareLinkService.ts:72-77`). There is no HMAC, JWT, or signature.
- **Stored in plaintext** in `shareLinksById` (`src/clinical/clinicalStateRepository.ts:199`). Anyone who can read the state file or store can use every live link.
- **Expiry** is 24h, 7d, or 30d only (`src/clinical/clinicalStoreUtils.ts:231-236`). Revoke by owner or admin (`src/clinical/shareLinkService.ts:112-146`). There is no maximum number of uses. Each landing appends to `used_at` (`:159`).
- **Permissions**: `view_images` and `download_dicom` are rejected (`:35-40`). The landing returns metadata only, never report content (`:161-169`). The doc's viewer-JWT exchange and redirect (`bff-api-surface.md:1052-1077`) are **planned, not implemented** (`viewer_url: null`, `shareLinkService.ts:167`).
- **Recipient binding**: an external link with an email is checked only if the caller supplies `?email=` (`src/clinical/shareLinkService.ts:154-156`). The doc requires the recipient to enter the email (`bff-api-surface.md:1070`). Not enforced (F-11).

### 3.9 Idempotency and concurrency
- `Idempotency-Key` (at most 200 chars, `src/clinical/clinicalStoreUtils.ts:290-295`) is accepted on worklist, report, share, critical, and QC mutations. Results are cached forever per (user, resource, key) in state maps (`src/clinical/clinicalStateRepository.ts:186-200`).
- Optimistic concurrency, **all optional**: `expected_updated_at` (worklist, share revoke), `expected_draft_version` (sign), `base_version` (draft), `expected_report_id` (addendum, withdraw) (`src/clinical/clinicalStoreUtils.ts:297-347`). If omitted, the last writer wins.
- Serialization: memory and file modes use an **in-process promise queue** (`src/clinical/clinicalStateRepository.ts:296-315`). File mode is unsafe with more than one BFF process. Postgres uses `BEGIN` and `SELECT … FOR UPDATE` on **one row for the whole state** (`:386-403, 437-444`), so every clinical write in the deployment is serialized.
- Take-next is atomic. Candidate selection and assignment both run inside the state lock, and the `assigned_to === null` filter rejects a double pick (`src/clinical/worklistService.ts:75-104`).

---

## 4. Clinical domain

### 4.1 Worklist (per study; `WorklistState`, `src/clinical/clinicalStateRepository.ts:29-37`)
- Initial status comes from the archive row (`unread`). No state record exists until the first mutation (`src/clinical/clinicalStoreUtils.ts:377-387`).
- take-next: `unread` and unassigned becomes `in_progress`, assigned to the caller. Ordered by priority rank, then acquisition time (`src/clinical/worklistService.ts:80-95`). Radiologist role required (`src/clinical/routes.ts:272`).
- assign: becomes `in_progress` with the assignee (`worklistService.ts:137-139`).
- release: becomes `unread`, unassigned (`:171-173`). Only the current assignee or admin may release.
- priority: `stat`, `urgent`, or `routine` with a required reason. Status is unchanged (`:204`).
- sign (any status): worklist status becomes `preliminary` (`markStudyReported`, `src/clinical/clinicalStoreUtils.ts:121-135`). Both branches of its ternary return `'preliminary'` (F-21).
- withdraw: becomes `in_progress`, assigned to the withdrawing user (`src/clinical/clinicalStoreUtils.ts:137-151`).
- IOCM reject: unassigned, `unread` (`src/clinical/studyGovernanceService.ts:639-649`).
- The default list hides `preliminary` unless requested (`src/clinical/worklistService.ts:218`).
- Universe: the first 200 archive rows only (`src/clinical/clinicalStoreUtils.ts:25-27`; F-33).

### 4.2 Reports (per study; `reportRevisionsByStudy`, append-only; current = last)
- **Draft**: one per (author, study). `version` increments on each save. Sections are trimmed to 20,000 chars; extra-section labels to 80 and bodies to 10,000 (`src/clinical/clinicalStoreUtils.ts:188-210`; `src/clinical/reportService.ts:59-92`).
- **Sign** (`src/clinical/reportService.ts:94-192`; route `src/clinical/routes.ts:384-415`):
  - An existing draft is required, and the draft author or an admin must sign (`reportService.ts:109-115`).
  - Optional `expected_draft_version` (`:116`).
  - The attestation must match the live archive on study UID, accession, patient name, MRN, DOB, and issuer (`routes.ts:848-878`).
  - Status rules (`src/clinical/reportService.ts:401-421`): `corrected` requires a current revision of final, corrected, or amended. A `preliminary` or `final` sign is refused when the current revision is final, corrected, or amended.
  - SR is built (`:123-133`), then sent by STOW (`:134`) only in `stow` mode.
- **SR document** (`src/clinical/reportSrWriter.ts`): Basic Text SR (SOP `1.2.840.10008.5.1.4.1.1.88.11`). Sections are CONTAINS/TEXT items. Status is `VERIFIED` for final, corrected, and amended, and `UNVERIFIED` for preliminary (`:46, 73`). UIDs are deterministic `2.25.` plus the decimal form of a SHA-256 (`:125-128`). **No verifying observer** (F-23).
- **Addendum**: requires a current revision of final, corrected, or amended (`reportService.ts:213-219`). The new revision has status `amended`, with the body placed in the addendum section (`:228`).
- **Withdraw**: a new revision with status `withdrawn` and `stow_status: 'withdrawn_local'` (`reportService.ts:330`). The archive copy is **not** changed. The current report must not already be withdrawn.
- **History**: list of revisions with `is_current` (`src/clinical/clinicalStoreUtils.ts:163-176`).
- **STOW status recorded**: `source_meta.stow_status` is `mocked` by default (`src/arcLightClient.ts:162-168`).

**HIS precedence** (`src/clinical/reportSourceResolver.ts:372-390`; authoring gate `src/clinical/routes.ts:981-984`):

| Mode | Selected report | In-app authoring |
|---|---|---|
| `in_app` | local revision | allowed |
| `external_his` | HIS source (first enabled among HL7, REST, PDF, iframe; `externalReportAdapters.ts:135-142`) | blocked (422) |
| `hybrid_his_wins` | HIS if available, else local | blocked (422) |
| `hybrid_in_app_wins` | local if a local revision exists, else HIS | allowed |

The reporting mode is a **module-level singleton** (`src/admin/arcLightAdminClient.ts:205-230`) that resets to `in_app` on every restart.

External sources:
- HL7 ORU: stored per accession and per study (`src/clinical/hl7OruIngestion.ts:166-230`; lookup `src/clinical/reportSourceResolver.ts:446-456`). **No patient cross-check** (F-03).
- REST: placeholder adapter with cache (`src/clinical/externalReportAdapters.ts:23-70`).
- PDF: URL pass-through with an accession template (`:72-114`).
- iframe: URL pass-through with sandbox flags (`:116-133`).
- Placeholder text is injected into every external report as an extra section (`src/clinical/reportSourceResolver.ts:223`).

### 4.3 Critical findings (`src/clinical/criticalFindingService.ts`)
- Create: severity `critical`, `urgent`, or `incidental`. Recipient `referring_physician` or `ward_doctor`. Due time is now plus `CRITICAL_FINDING_ESCALATION_MINUTES` (default 30, `src/config.ts:89`).
- Status is derived: `escalated` if due and unacknowledged, `acknowledged` after acknowledgement, otherwise `open` (`:213-222`).
- **Escalation runs only inside `listCriticalFindings`** (`:38`, `:183-210`). No timer exists (F-07).
- Acknowledge: recipient role or admin; note at most 2,000 chars; one-time (`:147-174`).
- Notifications: in-app to the recipient role, and to the creator on acknowledgement or escalation.

### 4.4 Patient identity (ADT, `src/clinical/hl7AdtIngestion.ts`)
- A40, A47, and A34 create an alias from the deprecated identifier (MRG-1) to the canonical identifier (PID-3). Chains are followed (`:116-171`; `src/clinical/clinicalStateRepository.ts:828-877`).
- A37 is logged only, with "no automatic data reversal" (`:93-114`).
- Same-identity merges are ignored (`:131-163`).
- Aliases redirect every later lookup of the deprecated MRN to the canonical patient (`src/clinical/routes.ts:547-602`).
- **Accepted from any sender** on the listener, even when ORU is disabled (F-02).

### 4.5 QC flags and IOCM (`src/clinical/studyGovernanceService.ts`)
- QC flag reasons: `poor_positioning`, `motion_artifact`, `wrong_protocol`, `duplicate`, `other` (note required for `other`). Status goes `pending` to `dismissed` (admin), `rejected` (automatic on IOCM reject), or `dismissed` (author withdrawal).
- IOCM reject (scope study, series, or instance) is recorded as local state. Decorated rows show `status: 'rejected'` and are hidden by default (`:603-616`). **DICOM objects are not changed** (`src/arcLightClient.ts:419-424`; F-10).

### 4.6 Share links, notifications, events
- Share link states: active, then used (each landing), then revoked, or expired by time. Notifications are created for internal recipients (in-app) and for external recipients (email plan, then log adapter).
- Email adapter: `LogNotificationAdapter` prints recipient email and message to stdout and marks the record `sent` (`src/clinical/notificationService.ts:54-73`). `CLINICAL_NOTIFICATION_MODE=disabled` yields `skipped` (`:31-41`; default `log`, `src/config.ts:149`).
- Events: ring buffer of 200 (`src/clinical/clinicalStoreUtils.ts:281-284`). Read returns the last 25 matching events (`src/clinical/shareLinkService.ts:173-184`). The client only polls `?once=true`. The streaming branch sends one heartbeat and **never ends or keeps the socket alive** (`src/clinical/routes.ts:524-542`). The doc's 25 s heartbeat and `Last-Event-ID` are absent (`bff-api-surface.md:1724`).

### 4.7 Operations and SLA (`src/clinical/operationsService.ts`)
- TAT is acquisition to the earliest signature across revisions (`:301-332`). Output: median, nearest-rank p90, six buckets, SLA rows per priority, reader medians, and unassigned aging that ignores the window.
- Targets from config: stat 60, urgent 240, routine 1,440 minutes (`src/config.ts:103-107`). Negative TAT is dropped (`src/clinical/operationsService.ts:138-147`).
- Input is the first 200 archive rows (F-33). The comment says 500 (`:306-308`).

### 4.8 Relevant priors and hanging protocols
- Priors (`src/clinical/priorStudies.ts`): earlier study only; same modality or CR/DX; within 5 years (`:53`); body part compared only when both sides have it (`:89-94`).
- Hanging protocols (`src/viewer/studyPresentation.ts:190-277`): five seeded rules with `source: 'seeded_local'`, keyed by region (`:348-371`). Region matching uses normalized text (`:306-309`).

### 4.9 Persistence and data model
- Snapshot keys (`src/clinical/clinicalStateRepository.ts:176-204`): `preferencesByUser`, `patientNotesByMrn`, `patientIdentityAliasesByIdentity`, `studyRejectionsByStudy`, `qcFlagsByStudy`, `criticalFindingsById`, `revokedSessionIds`, `worklistStatesByStudy`, ten idempotency maps (lines 186-193 and 200-201), `draftsById`, `reportRevisionsByStudy`, `hl7MessagesByControlId`, `externalHl7RevisionsByAccession`, `externalHl7RevisionsByStudy`, `shareLinksById`, `notificationsById`, and `events` (ring of 200).
- Modes (`src/clinical/clinicalStateRepository.ts:232-249`): `memory` (lost on restart); `file` (default, `bff-svc/.data/clinical-state.json`); `postgres` (`<schema>.clinical_state_snapshots`, scope `default`, JSONB; default schema `pacs_clinical`, `:418-435`).
- Every read and update JSON-clones the **entire** snapshot (`src/clinical/clinicalStateRepository.ts:496-498`). Postgres rewrites the whole JSONB row on every mutation (`:446-458`). Growth is unbounded for idempotency maps, HL7 messages, notifications, and external revisions (F-40).
- Data quality: no migrations and no schema versioning beyond `schemaVersion: 1`. Normalizers cast rather than validate (`:461-494`).

---

## 5. Integrations

### 5.1 arc-light DICOMweb
- **QIDO-RS**: `GET {ARC_DICOMWEB_BASE}/studies` with `includefield=all`, `limit`, `offset`. `showRejected` is deliberately not sent because the archive returns 400 for it (`src/arcLightClient.ts:421-424`). Series via `/studies/{uid}/series` (`:440-469`). Patient, study, and modality normalization (`:471-584`). `ModalitiesInStudy` is read across all values (`:549-584`).
- **STOW-RS**: `POST {base}/studies`, `multipart/related`, body `application/dicom+json` built by `serializeDicomJsonForStow` (`src/arcLightClient.ts:170-195`). Mock unless `REPORT_SR_STORE_MODE=stow`. **No auth header and no timeout.**
- **WADO, pixels, thumbnails**: handled by arc-light through the browser. The BFF emits `${DICOMWEB_PUBLIC_BASE}/studies/{uid}/thumbnail?viewport=96,96` (`src/dicomwebLinks.ts:51-55`). The Traefik `dicom-web` router has no auth middleware (`infra/traefik/dynamic/routers.yml:16-21`) (F-10).
- **Admin and config API**: `ARC_CONFIG_BASE` is configured (`src/config.ts:133`) but not called.

### 5.2 HL7 ORU and ADT MLLP listener (inbound, default ON)
- **Start**: `startHl7OruMllpListener` runs if `HL7_ORU_MLLP_ENABLED !== 'false'` (`src/config.ts:163`). Host defaults to `0.0.0.0` (`:164`), port to 2575 (`:165`). The HIS doc says 2576 (`bff-api-surface.md:1731`).
- **Framing**: MLLP start `0x0B`, end `0x1C 0x0D` (`src/clinical/hl7OruMllpListener.ts:7-9, 89-119, 121-124`). Frames are concatenated in an unbounded buffer (`:42-45`). Each `data` event rescans a partial frame from its start (`:111-119`). Frames are processed concurrently, so ACKs can return out of order (`:47-57`).
- **Routing**: by MSH-9 family. ADT always goes to the ADT path (`:69-72`). Everything else goes to ORU. ORU is gated by `hl7_enabled` in the reporting config, which defaults to `false` (`src/admin/arcLightAdminClient.ts:208`). Until an admin enables it, ORU messages get AR (`src/clinical/hl7OruMllpListener.ts:76-79`).
- **ACK**: `MSH|^~\&|PACS_BFF|PACS|HIS|MAIN|<ts>||ACK^R01|ACK-<cid>-<ts>|P|2.5.1` and `MSA|<AA|AE|AR>|<cid>|<text, at most 180 chars, pipes replaced>` (`:133-144`). AA on success, AR on parse failure or a thrown error. The error path ACKs with `error.message` (`:52-55`).
- **ACK control-ID bug** (F-37): the fallback control ID is read from `fields[10]`, which is MSH-11 (processing ID), not MSH-10.
- **ORU parse** (`src/clinical/hl7OruIngestion.ts:245-350`): requires `ORU^R01`, MSH-10, an OBR, and at least one OBX. Status comes from OBR-25 (fallback OBX-11) and maps I or P to preliminary, F to final, C to corrected, A to amended, X or D to withdrawn. OBX segments are sorted by set ID. Sections are mapped by label heuristics (`:577-595`); other labels become extra sections. ED and RP values are replaced by placeholders (`:606-621`). Reporting physician comes from OBR-32 or an OBX label (`:327-328, 569-575`). Language comes from the OBX language hint or script detection (`:649-670`). Observed time comes from OBR-22 or OBR-7, falling back to MSH-7 (`:331`).
- **ORU dedupe**: by MSH-10 only, with no content hash (`src/clinical/hl7OruIngestion.ts:127-140`). The doc's step 2 specifies a SHA-256 of the raw bytes (`hl7-oru-parsing-approach.md:395`).
- **ORU precedence**: a late preliminary after a final, corrected, or amended revision is ignored AA (`:168-189`). Other revisions append (`:213-219`).
- **Addendum heuristic**: if the incoming addendum is under 30% of the current body, it is merged into the current addendum (`:366-383`).
- **Study link**: `resolveStudyUid` queries QIDO by accession with limit 1, and returns null on any error (`:385-394`). Unmatched accessions are stored and ACKed AA with a null study (`:216-219`).
- **No patient cross-check** (F-03). The doc requires one (`hl7-oru-parsing-approach.md:425-426`).
- **Stored**: `raw_preview` (first 12 lines, PID and OBX-5 values and NTE redacted; `src/clinical/hl7OruIngestion.ts:457-487`) and `patient_mrn`. The raw payload is **not** stored, so the doc's resubmit path (`hl7-oru-parsing-approach.md:450`) is not possible.
- **ADT parse** (`src/clinical/hl7AdtIngestion.ts:202-257`): canonical MRN from PID-3, deprecated from MRG-1, both required (`:236-238`). Assigning authority from CX-4.

### 5.3 HIS order ingest (inbound HTTP, optional outbound MLLP)
- Accepts ORM, OML, SIU, and ADT families (`src/integration/hisOrderIngestService.ts:34`). Control ID from MSH-10 (`:81-92`, index 9, correct).
- `mock`: AA with text "Order accepted (mock MLLP forward mode)" (`:46-56`). `mllp`: forward through the circuit breaker to `HIS_ARC_MLLP_HOST:PORT` (`:58-65`).
- Response is 202 with `ack_payload`. A rejection is 400 (`src/app.ts:175-183`).

### 5.4 HIS REST, PDF, iframe (outbound when enabled)
- REST: `GET {base}/reports/{acc}`, falling back to `GET {base}/reports?accession=` (`src/clinical/externalReportAdapters.ts:41-57`). Cache keyed by base and accession with TTL (`:21, 64-68`). **No timeout** (`:50`). Basic auth uses a literal placeholder password (`:289`). Response normalization is defensive (`:144-237`), but attachment URLs are not scheme-checked (`:230-236`).
- PDF: template with `{ACC}`. Eager mode issues a HEAD request (`:83, 97-110`). `mirrored: true` is set on HEAD success, although nothing is mirrored.
- iframe: template with `{ACC}` and sandbox flags from config (`:116-133`).
- Validation for these settings lives in the reporting config, which is an in-memory singleton (`src/admin/arcLightAdminClient.ts:459-493, 571-609`).

### 5.5 HIS report callback (outbound, optional)
- Enabled only when `HIS_INTEGRATION_ENABLED`, `HIS_REPORT_CALLBACK_ENABLED`, and a base URL are all set (`src/integration/hisReportCallbackService.ts:22-25`).
- JSON body with `event_type`, `report_id`, `study_instance_uid`, `accession_number`, `report_status`, and `actor` (`:5-17`). Headers `X-PACS-Timestamp`, plus optional `X-PACS-Signature: sha256=<hex>` over `timestamp.body` (`:29-30, 78-90`).
- Three attempts with a 5 s abort, wrapped in the breaker (`:26-69`). Dispatched with `void` after the signing response (`src/clinical/reportService.ts:163-189, 264-290, 350-376`). **No outbox**: a crash or an open breaker loses the callback (F-25).

### 5.6 Keycloak
- JWKS fetched on first verification through `jose` (`src/auth.ts:59`). The JWT is checked for signature, issuer, audience, exp, and nbf. Roles come from `realm_access`.
- Admin REST is configured (`src/config.ts:129-132`) and **not called** (F-19).

### 5.7 Prometheus and metrics
- Outbound, when `METRICS_BASE_URL` is set: 6 health instant queries, 1 certificate query, and 13 metric queries (`src/admin/systemClient.ts:60-67, 98, 124-140`). 5 s abort each. Any failure yields `unknown` or `degraded`.
- Inbound: `/metrics` exposes `bff_requests_total`, `bff_request_errors_total`, `bff_request_latency_p50_ms` and `p95_ms`, `bff_audit_event_backlog` (total in the audit sink), and process heap, RSS, and uptime (`src/metrics.ts:53-82`). Unauthenticated (`src/app.ts:61`). Not routed by Traefik. The routers in `infra/traefik/dynamic/routers.yml` cover only `/api`, `/dicom-web`, `/auth`, `/admin`, `/clinical`, `/viewer`, and `/`.

### 5.8 Postgres
- Clinical state: one table and one row holding a JSONB snapshot (see §4.9). Pool max 10, idle timeout 30 s (`src/clinical/clinicalStateRepository.ts:251-258`).
- Audit: table `<schema>.audit_event` with indexes on `at` and `action` (`src/audit.ts:378-410`). Pool max 5, connection timeout 5 s (`:252-257`). Inserts are fire-and-forget (`:266-272`). List returns an empty result with "temporarily unavailable" on error (`:344-353`).

---

## 6. Capability checklist

| Capability | Status | Evidence (file:line) | Notes |
|---|---|---|---|
| DIMSE services | N/A-handled-by-arc-light (BFF: Absent; admin C-ECHO is an Adapter-mock placeholder) | `src/admin/arcLightAdminClient.ts:293-296` | BFF has no C-STORE, C-FIND, C-MOVE, or C-ECHO client. The admin test returns a planned placeholder. |
| DICOMweb QIDO, WADO, STOW | QIDO: Real. WADO: N/A-handled-by-arc-light. STOW: Adapter-mock by default | QIDO `src/arcLightClient.ts:410-438`; STOW `:161-196` (mock `:162-168`); thumbnails `src/dicomwebLinks.ts:51-55`; default `src/config.ts:144` | The STOW code path is real but off by default. No archive credentials are attached. Pixel access depends entirely on edge and archive auth (F-10). |
| Worklist (MWL, MPPS) | MWL: Adapter-mock. MPPS: Absent | `src/admin/mwlAdminClient.ts:45-140`; `src/admin/arcLightAdminClient.ts:552` (`createMwlPlaceholder` throws 501, unused) | Walk-in MWL is in-memory only, not sent to the archive. No MPPS handling anywhere. |
| Reading worklist | Real | `src/clinical/worklistService.ts:48-223`; `src/clinical/routes.ts:252-321` | take-next is atomic. Universe capped at 200 archive rows (F-33). |
| Reporting (draft, sign, addendum, SR) | Real (SR generation and state). SR STOW Adapter-mock by default | `src/clinical/reportService.ts:59-379`; `src/clinical/reportSrWriter.ts:39-77`; `src/config.ts:144` | Sign requires attestation against the live archive. The SR has no verifying observer (F-23). |
| Report templates | Adapter-mock | `src/admin/arcLightAdminClient.ts:148-203, 425-448`; `src/clinical/routes.ts:480-486` | Seed templates only. The clinical route reads the admin mock store. |
| Critical results | Real (state), with read-triggered escalation | `src/clinical/criticalFindingService.ts:38, 183-210`; no scheduler (grep) | Escalation fires only on a read (F-07). Notifications are in-app only. |
| Peer review | Absent | grep: no matches | No second-reader or discrepancy workflow. |
| Sharing | Real (metadata and report status only) | `src/clinical/shareLinkService.ts:35-40, 148-171` | No image or DICOM sharing (refused at `:35-40`). No signed URLs. Email binding optional (F-11). |
| Notifications | In-app: Real. Email: Adapter-mock (log only, reports `sent`) | `src/clinical/notificationService.ts:189-240` (in-app); `:43-74` (log) | Email is printed to stdout, not delivered (F-26). |
| HL7 ADT, ORM, ORU | ADT: Real (merge alias; A37 logged only). ORU: Real (parse, store; no MRN check). ORM, OML, SIU: Adapter-mock | ADT `src/clinical/hl7AdtIngestion.ts:33-200`; ORU `src/clinical/hl7OruIngestion.ts:50-243`; orders `src/integration/hisOrderIngestService.ts:46-65` | ORU listener is default-on (`src/config.ts:163`), unauthenticated (F-02), with no MRN cross-check (F-03). |
| FHIR | Absent | grep: no matches | None. |
| IHE PIX, PDQ, XDS-I | Absent | grep: no matches | The ADT alias is a local map (`src/clinical/hl7AdtIngestion.ts:165`), not PIX. |
| Users and roles admin | Adapter-mock | `src/admin/keycloakAdminClient.ts:25-85` | No Keycloak admin REST calls. Roles unvalidated (F-19). |
| Devices and AE admin | Adapter-mock | `src/admin/arcLightAdminClient.ts:108-135, 271-296` | Seed AEs and in-memory CRUD. No arc-light config calls. |
| Queues admin | Adapter-mock | `src/admin/queueAdminClient.ts:78-314, 364-408` | Seeded jobs. Retry and discard change local seed data only. |
| Audit trail | Real | `src/audit.ts:163-188, 195-266, 533-566` | 1,000-event window (F-08). Postgres sink is fire-and-forget (F-08). PHI in detail (F-26). |
| Config admin | Adapter-mock | `src/admin/configAdminClient.ts:45-161`; `src/admin/arcLightAdminClient.ts:205-230, 459-493` | Reporting config is a non-persistent singleton. `ADMIN_ADVANCED_MODE` is hard-wired to `local_mvp` (`src/config.ts:166`). |
| License | Adapter-mock | `src/admin/systemDetailsClient.ts:161-179` | Fixed "Production" license and support dates, presented as real (F-18). |
| Monitoring and metrics | Real (BFF `/metrics`, unauthenticated). Real Prometheus queries when `METRICS_BASE_URL` is set | `src/metrics.ts:36-85`; `src/admin/systemClient.ts:55-116, 214-239`; placeholder `:178-198` | The metrics endpoint is not routed by Traefik (`infra/traefik/dynamic/routers.yml`). |
| Multi-tenancy | Absent | `src/clinical/hl7OruIngestion.ts:73-123`; `src/config.ts:109` | Only deployment-level facility filters and the temp-MRN prefix. |
| Authentication | Real (OIDC JWT). Dev bypass present | `src/auth.ts:58-116`; dev `:84-93`; config fail-closed `src/config.ts:124`; service bearer `src/app.ts:553-575` | Dev mode has no guard (F-05). JWT `azp` and `typ` are not checked (§3.1). |
| Authorization (RBAC) | Real but incomplete | `src/clinical/accessPolicy.ts:5-111`; `src/admin/accessPolicy.ts:5-23`; gaps `src/app.ts:353-357, 428-437`; `src/clinical/routes.ts:524-542` | Two app-level routes and `/events` lack a role gate (F-01, F-06). Role-only scoping with no care relationship (F-15). |

---

## 7. Code review findings

Severity: 🔴 blocking (security, patient safety, data loss, or compliance). 🟡 important. 🟢 nit. Items marked "pending verification" depend on configuration outside this repo.

### 🔴 F-01: Missing RBAC on `GET /api/v1/studies/{studyUid}/context` and `GET /api/v1/hanging-protocols/match`
- `src/app.ts:353-357`:
  ```ts
  app.get('/api/v1/studies/:studyUid/context', async (request, response) => {
    const user = request.authUser!;
  ```
- Issue: neither handler checks a role. Both are registered in `app.ts`, not in the role-gated clinical router. The auth middleware only authenticates (`src/auth.ts:95-111`).
- Failure: a valid JWT with no recognised role (`roles: []` after filtering, `src/auth.ts:118-136`) reads patient name, MRN, DOB, sex, study description, the report (`clinicalStore.getReport`, `app.ts:376`), priors, and IOCM state. The same user is refused on `/studies/{uid}/access` (`app.ts:298-304`), so the two routes disagree.
- Fix: add the clinical-read guard at the top of both handlers. Add a test asserting 403 for a token with unknown roles.

### 🔴 F-02: HL7 MLLP listener is unauthenticated, default-on, bound to all interfaces, and accepts ADT without a gate
- `src/config.ts:163-164`:
  ```ts
  hl7OruMllpEnabled: env.HL7_ORU_MLLP_ENABLED === 'false' ? false : true,
  hl7OruMllpHost: env.HL7_ORU_MLLP_HOST?.trim() || '0.0.0.0',
  ```
- `src/clinical/hl7OruMllpListener.ts:69-72`:
  ```ts
  if (messageFamily === 'ADT') {
    const ingest = await clinicalStore.ingestHl7AdtMessage(payload, { source: 'listener' });
  ```
- Issue: no TLS, no IP allowlist, no sending-application allowlist on the TCP path, and no switch that disables ADT. ORU is gated by `hl7_enabled`, but ADT is not.
- Failure: any host that can reach port 2575 sends an A40 with `MRG-1 = <victim MRN>` and `PID-3 = <attacker MRN>`. The alias then redirects every lookup of the victim MRN to the attacker's patient (`src/clinical/routes.ts:74-88`; `src/clinical/clinicalStateRepository.ts:828-847`). Notes, reports, and worklist state then attach to the wrong patient.
- Fix: default off, or bind `127.0.0.1` and require a VPN or mTLS sidecar. Gate ADT behind the same enable flag and a sending-application allowlist. Refuse merges to a canonical identity that is not present in the archive. Require a second confirmation for merges.

### 🔴 F-03: No patient cross-check between ORU PID-3 and the archive study
- `src/clinical/hl7OruIngestion.ts:125`:
  ```ts
  const studyInstanceUid = await resolveStudyUid(config, parsed.accessionNumber);
  ```
  `resolveStudyUid` (`:385-394`) resolves by accession only. The revision stores `patient_mrn: parsed.patientMrn` (`:198`), but nothing compares it with `getStudySummary(...).patientId`.
- Spec: `hl7-oru-parsing-approach.md:426` requires "MRN matches … Mismatch → quarantine, alert admin. Never auto-correct."
- Failure: an ORU for patient B carrying patient A's accession is stored and served as A's study report (`src/clinical/reportSourceResolver.ts:446-456`), with B's findings. This is a wrong-patient report in a clinical record.
- Fix: before storing, compare normalized MRN and issuer with the archive study. On mismatch, reply AR, quarantine the message, and raise an admin alert.

### 🔴 F-04: File-backed clinical state silently resets to empty on any read failure, then overwrites the file
- `src/clinical/clinicalStateRepository.ts:342-353`:
  ```ts
  const raw = await readFile(this.filePath, 'utf8');
  return normalizeClinicalStateSnapshot(JSON.parse(raw));
  } catch (error) {
  ...
  console.warn(`Unable to read clinical state from ${this.filePath}; starting with an empty snapshot.`, error);
  return createEmptyClinicalStateSnapshot();
  ```
  and `:356-358` writes with `writeFile(this.filePath, JSON.stringify(next, null, 2), 'utf8')`, which is non-atomic: no temp file and no fsync.
- Failure: a crash or power loss during the write leaves truncated JSON. The next start logs a warning and runs on an empty state. The next mutation (any note, sign, or claim) writes that empty state over the file. Notes, reports, worklist assignments, share links, critical findings, and idempotency records are all lost, with no error reported to the operator.
- Fix: fail closed on parse errors (refuse to start or refuse mutations, and alert). Write to `file.tmp`, fsync, then rename. Keep a rotated backup. Use a process lock if more than one instance is ever allowed.

### 🔴 F-05: Dev-auth mode is a deployment footgun and listens on all interfaces
- `src/auth.ts:84-93`: a request with `Authorization: Bearer dev-admin` is admitted as admin. The static string equals the user key (`src/auth.ts:16-22`).
- `src/index.ts:13`: `app.listen(config.port, ...)` has no host, so it binds all interfaces.
- `.env:3`: this repo's git-ignored working `.env` sets dev mode, and `src/config.ts:4-6` loads it.
- Failure: copying this `.env` to a shared host gives unauthenticated admin access to anyone on the network.
- Fix: refuse to start in dev mode unless `NODE_ENV=development` **and** the host is `127.0.0.1`. Print a loud warning. Keep dev users out of production builds.

### 🔴 F-06: Events feed leaks cross-user data (recipient emails, study UIDs of signed reports) to any authenticated user
- `src/clinical/shareLinkService.ts:173-184`:
  ```ts
  return snapshot.events
    .filter((event) => {
      if (event.type === 'worklist.updated' || event.type === 'notification.created') {
        return ...;
      }
      return true;
    })
  ```
  So `report.upserted` (study UID and status), `share_link.used` (`by` = external recipient email, `src/clinical/shareLinkService.ts:160`), and `admin.alert` all pass through for everyone. Emit sites: `src/clinical/reportService.ts:153, 254, 340` and `src/clinical/shareLinkService.ts:160`.
- `GET /api/v1/events` has no role gate (`src/clinical/routes.ts:524-528`).
- Failure: any token, including one with no recognised role, polls `/api/v1/events?once=true` and receives the recipient email of each external share access and the study UID of each signed report.
- Fix: filter every event type by the caller's study and role authorization. Remove email from `share_link.used`. Gate the route by clinical read.

### 🔴 F-07: Critical-finding escalation never fires unless someone lists findings
- `src/clinical/criticalFindingService.ts:38` calls `syncEscalations()` only inside `listCriticalFindings`. `:183-210` marks overdue findings escalated and notifies. Grep: no `setInterval`, cron, or worker exists in `bff-svc/src`.
- Failure: a critical result is created, nobody lists critical findings, and the referring physician is never escalated. Audit and notification happen only when a page loads.
- Fix: run `syncEscalations` on a timer (for example every 30 to 60 s) and on every write. Alert when escalation lags. Make the due time a worklist property, not a side effect of a read.

### 🔴 F-08: Audit trail keeps only 1,000 events; the default sink is that file; Postgres writes are fire-and-forget
- `src/audit.ts:199-200`:
  ```ts
  this.events.unshift(event);
  if (this.events.length > 1000) this.events.pop();
  ```
  Default sink `AUDIT_SOURCE=file` (`src/config.ts:80-83`) has the same cap (`src/audit.ts:228`). Postgres inserts are `void`-dispatched (`src/audit.ts:266-272`), so a failed write is only a console warning. Verified: the local `bff-svc/.data/audit-log.json` holds exactly 1,000 events covering about five days (dates only).
- Failure: an access review or incident query for an earlier date returns nothing. A clinical-access audit trail that rolls over within days fails retention requirements. Postgres outages silently drop audit records.
- Fix: write to an append-only store (Postgres or a log sink) with time-based retention. Stop rewriting the whole file on each event (`src/audit.ts:234-241`). Await Postgres audit writes, or buffer and retry them.

### 🔴 F-09: Admin "anonymize" and "move" return `accepted` and do nothing
- `src/admin/arcLightAdminClient.ts:370-372`:
  ```ts
  export async function anonymizeStudy(studyUid: string): Promise<AdminActionResponse> {
    return { status: 'accepted', message: 'Study anonymization job queued.', job_id: `job-anon-${hashId(studyUid)}` };
  ```
  `moveStudy` has the same shape (`:374-376`). The route writes an `admin.study.action` audit line as if the operation happened (`src/admin/routes.ts:189-201`).
- Failure: an operator anonymizes or moves a study. The UI reports "job queued", the audit log agrees, and nothing happens. For a research export this is a privacy failure. For a move it is a lost study.
- Fix: return 501 until implemented, as `createMwlPlaceholder` already does (`src/admin/arcLightAdminClient.ts:552`). Never report an unperformed action as `accepted`.

### 🔴 F-10: IOCM "reject" is local state only, so DICOM objects stay retrievable (pending verification of arc-light auth)
- `src/clinical/studyGovernanceService.ts:364-421` writes a local record. `src/arcLightClient.ts:419-424` says "Rejected-object filtering happens downstream in StudyGovernanceService."
- `src/app.ts:336-337` hands the caller's bearer and the `/dicom-web` base to the browser. The Traefik `dicom-web` router has no auth middleware (`infra/traefik/dynamic/routers.yml:16-21`). The BFF attaches no credentials to arc-light (`src/arcLightClient.ts:429-431`).
- Failure: a study an admin has "rejected" is hidden from BFF lists, but a direct QIDO or WADO call through `/dicom-web` still returns it. Whether that succeeds depends on arc-light's own authorization, which is not in this repo (pending verification).
- Fix: make rejection a DICOM-level action in arc-light, or enforce it at the edge. Require authentication at Traefik with `forwardAuth` to the BFF or to Keycloak introspection. Verify arc-light's configured authorization before relying on either.

### 🟡 F-11: Share-link recipient binding is optional, and the email travels in a query string
- `src/clinical/shareLinkService.ts:154-156`:
  ```ts
  if (link.recipient_email && email && link.recipient_email.toLowerCase() !== email.toLowerCase()) {
  ```
- `src/app.ts:240`: the email is read from the URL query (`typeof request.query.email === 'string' ? request.query.email : undefined`).
- Spec: `bff-api-surface.md:1070` says the recipient must enter the email.
- Failure: a forwarded link opens without the email. The email also lands in access logs and browser history.
- Fix: require the email for external links. Send it in a POST body, never a query string.

### 🟡 F-12: Wildcard characters in exact-match QIDO fields
- `src/arcLightClient.ts:385-387, 393-394`: `PatientID: query.patient_mrn`, `AccessionNumber: query.accession`, and in the `q` path `PatientID: q` and `AccessionNumber: q`. No `*` or `?` stripping.
- Failure: `/patients/*` or `/studies/search?accession=*` returns arbitrary patients or studies. This is not an authorization bypass by itself (any clinical role may search), but it defeats the intent of exact lookup.
- Fix: reject or escape DICOM wildcard characters in exact-match fields.

### 🟡 F-13: Unauthenticated share landing writes state on every hit, with unbounded growth
- `src/clinical/shareLinkService.ts:149-160`: `repository.update(...)`, then `link.used_at.push(accessedAt)` and `emit(...)`. Reached via `GET /v/s/{token}` with no auth (`src/auth.ts:65`).
- Failure: an attacker who guesses or leaks a token hammers the endpoint. Each hit takes the global state lock and rewrites the whole snapshot. `used_at` grows without limit per link.
- Fix: rate-limit the public routes. Record use with a counter and a capped history. Move use recording off the request path.

### 🟡 F-14: Session revocation has no effect on access
- `src/clinical/clinicalStore.ts:198-214` appends to `revokedSessionIds`. `listUserSessions` hides the synthetic session if revoked (`:192-196`). Authentication (`src/auth.ts`) never reads that list. Grep: `revokedSessionIds` is used only in `clinicalStore.ts` and the repository.
- Failure: the user revokes "their session" and remains fully authenticated until the JWT expires.
- Fix: implement token revocation (a deny-list keyed by `jti` or `sub` plus `iat`), or remove the endpoint and the UI control.

### 🟡 F-15: Role-wide visibility and read scoping leak across patients
- `src/clinical/criticalFindingService.ts:250-252`:
  ```ts
  return user.roles.includes('admin') || record.created_by.id === user.id || user.roles.includes(record.recipient_role);
  ```
  Every `referring_physician` sees every critical finding addressed to referring physicians, for every patient. There is no referral or care-relationship check. The same applies to patient search and study search (`src/clinical/routes.ts:55-72, 140-161`).
- Fix: add a relationship model (referral or ward assignment) and scope lists by it, or document role-wide access as intentional for this deployment.

### 🟡 F-16: Access endpoint reports `authorized: true` without checking anything, and returns the caller's bearer
- `src/app.ts:306-340` returns `authorized: true` when there is no patient context, without calling arc-light. It always includes `bearer_for_dicomweb: user.bearerToken` (`:337`) and an `expires_at` five minutes out (`:338`) that does not match the token's real `exp`.
- Failure: the field name says "authorized" but nothing was authorized. Clients may cache the bearer and the false expiry.
- Fix: rename or remove the `authorized` field. Return the token's real `exp`. Do not return the bearer if a shorter-lived, scoped credential can be issued.

### 🟡 F-17: Operator role reads the user directory and the audit log
- `src/admin/accessPolicy.ts:5-7`: `hasAdminReadAccess` includes `operator`. The routes `/users` (`src/admin/routes.ts:306-311`, emails) and `/audit-log` (`:347-351`, PHI in detail) are therefore operator-readable.
- Fix: restrict `/users` and `/audit-log` to admin, or add a separate audit-reader role.

### 🟡 F-18: Fabricated or stubbed system pages presented as live
- `src/admin/systemDetailsClient.ts:139-151`: with no `BACKUP_LOG_PATH`, the response is `last_backup_verified: true` and `last_backup_size_bytes: 47_000_000_000`, with fixed timestamps (`:144`).
- `:153-158`: certificate `cert-pacs-local` has `not_after: '2026-06-08T00:00:00.000Z'` (line 155). Today is 2026-10-10 per the environment, so the stub already shows an expired certificate, and it is always shown.
- `:161-179`: license tier "Production" and support to 2027, hard-coded.
- `src/admin/systemClient.ts:111`: `tls_configured: config.metricsBaseUrl ? true : false` infers TLS from Prometheus configuration.
- Fix: return "unknown" or 501 when no source is configured. Never show a backup as verified without a log.

### 🟡 F-19: Keycloak admin mock accepts arbitrary roles, which becomes privilege escalation when wired up
- `src/admin/keycloakAdminClient.ts:47-70`: `roles: input.roles?.length ? input.roles : ['operator']` (line 55); `patchAdminUser` spreads the raw input (`:67`). No role validation.
- Failure: once wired to Keycloak without validation, an admin (or a bug) can write unknown roles or elevate privileges by patch.
- Fix: validate roles against the `Role` union before any write. Treat this mock as a non-production adapter.

### 🟡 F-20: Admin mock writes return success codes
- The mocked writes are listed in §2.6 (rows 82–121). Examples: `src/admin/routes.ts:123-128` (`createAeTitle` returns 201 with in-memory data), `:474-486` (MWL create), `:313-318` (user create).
- Fix: return 501 for mock writes, or mark the mock explicitly in the response (`status: 'planned'`), consistently.

### 🟡 F-21: `markStudyReported` ignores its status argument, so a final sign hides the study from the worklist
- `src/clinical/clinicalStoreUtils.ts:129`:
  ```ts
  worklistState.status = status === 'preliminary' ? 'preliminary' : 'preliminary';
  ```
  Called from `src/clinical/reportService.ts:152` with `'final'` on a final sign. The default worklist filter hides `preliminary` (`src/clinical/worklistService.ts:218`).
- Failure: a finalized study drops off the default worklist and shows as "preliminary". The redundant ternary suggests a copy-paste error.
- Fix: decide the intended worklist state for final reports. Map `final` to its own state, or set `status` explicitly and remove the ternary.

### 🟡 F-22: HL7 listener DoS and out-of-order ACKs
- `src/clinical/hl7OruMllpListener.ts:42-45`: `socket.on('data', (chunk) => { buffer = Buffer.concat([buffer, chunk]);`. There is no cap, no socket timeout, and no connection limit.
- `:111-119` rescans from the frame start on every chunk, so a large or slow frame costs O(n²).
- `:47-57`: `void processFrame(...).then((ackPayload) => socket.write(...))` runs frames concurrently. ACKs are written in completion order, not arrival order. MLLP senders that pipeline expect in-order ACKs.
- Failure: one peer sends a multi-gigabyte frame without a terminator and exhausts memory. A slow ORU and a fast ADT on the same socket reorder the ACKs and confuse the sender.
- Fix: cap frame size (for example 1 MB) and close on overflow. Add an idle timeout and a connection limit. Process frames sequentially per socket, or queue ACKs in order.

### 🟡 F-23: DICOM SR "VERIFIED" without a verifying observer
- `src/clinical/reportSrWriter.ts:33` declares `author: { id: string; name: string }`. The returned dataset (`:48-76`) never references it. The SR carries `0040A493 VERIFIED` (`:73`) but no Verifying Observer sequence or name.
- Failure: the archive holds a "verified" report that does not say who verified it. This weakens traceability for any SR consumer.
- Fix: add the verifying observer sequence and name, and the verification date-time, from the authenticated user.

### 🟡 F-24: Access and take-next responses carry the caller's full JWT as a DICOMweb credential
- `src/app.ts:336-338`; `src/clinical/clinicalStoreUtils.ts:111-119`.
- Failure: the browser holds a broad-scope token for direct archive calls. Any token leak is a full API leak, and the five-minute label is false.
- Fix: exchange for a short-lived, study-scoped DICOMweb credential, or let the edge enforce study scope.

### 🟡 F-25: HIS report callback is unsigned without a secret, and has no durable delivery
- `src/integration/hisReportCallbackService.ts:78-82`:
  ```ts
  const normalizedSecret = secret?.trim();
  if (!normalizedSecret) {
    return null;
  }
  ```
  Then no `X-PACS-Signature` header is sent (`:44-45`).
- `src/clinical/reportService.ts:163-189`: `void getStudySummary(...).then(...)`. Failures are logged to audit and dropped.
- Failure: the HIS cannot authenticate the callback. A restart or an open breaker drops the event permanently.
- Fix: require the secret when callbacks are enabled. Persist an outbox row in the same transaction as the sign, then deliver with retry.

### 🟡 F-26: PHI in logs, audit detail, and HL7 previews
- `src/clinical/notificationService.ts:54-66` prints `recipient_email` and `message` to stdout on every notification (`console.info`).
- `src/audit.ts:187` prints every audit event, detail included, to stdout.
- `src/clinical/routes.ts:391-396` places the MRN, display name, DOB, and issuer in the attestation snapshot audit detail.
- `src/clinical/hl7AdtIngestion.ts:475-485` redacts only `PID` and `MRG` in `raw_preview`. `NK1`, `IN1`, and `PV1` pass through. `src/clinical/hl7OruIngestion.ts:469-487` keeps `PV1`, `ORC`, and similar segments.
- Fix: log IDs, not content. Redact PV1, NK1, and IN1 in previews. Reduce the attestation audit detail to a hash of the snapshot.

### 🟡 F-27: `test-hl7` writes a synthetic report into live HL7 state
- `src/admin/routes.ts:404-413` builds an ORU for accession `PROBE-yyyymmdd` and calls `clinicalStore.ingestHl7OruMessage` with `source: 'admin_probe'` when `hl7_enabled` is on. The record persists in `externalHl7RevisionsByAccession` (`src/clinical/hl7OruIngestion.ts:214`).
- Fix: run probes in dry-run mode, or write them to a separate namespace that reads exclude.

### 🟡 F-28: External URLs reach the browser without scheme validation, and the default iframe sandbox defeats the sandbox
- `src/clinical/externalReportAdapters.ts:83, 127`: `template.replaceAll('{ACC}', ...)` produces `pdf_url` and `iframe_url`. `:230-236` passes HIS `attachments[].url` through.
- `src/admin/arcLightAdminClient.ts:586-595`: `validateAccTemplate` checks only for `{ACC}`. Only the REST and outbound URLs are validated as http(s) (`:578-580, 605-606`).
- `src/admin/arcLightAdminClient.ts:223`: default `iframe_sandbox_flags: 'allow-scripts allow-same-origin'`. `:628-634` permits that combination.
- Failure: an admin, or a compromised HIS, sets `javascript:` in a template, which renders as a link. Or an iframe with scripts and same-origin reads the app's storage and bearer.
- Fix: validate all external URLs as `https:` with an allowlisted host. Forbid `allow-scripts` together with `allow-same-origin` in validation. Default to an empty sandbox.

### 🟡 F-29: HIS REST basic auth sends a literal placeholder password
- `src/clinical/externalReportAdapters.ts:288-291`:
  ```ts
  headers.Authorization = `Basic ${Buffer.from(`${reportingConfig.his_rest_username}:placeholder`).toString('base64')}`;
  ```
- Failure: a correctly configured basic-auth HIS returns 401, which the adapter silently treats as "no report" (`:54-56`).
- Fix: store the password as a secret reference, not a literal. Surface 401 as a configuration error.

### 🟡 F-30: Errors are swallowed in report and HL7 paths, and mapped to a misleading status
- `src/clinical/reportSourceResolver.ts:152-154`: `} catch { return null; }` wraps the whole HIS path.
- `src/clinical/hl7OruIngestion.ts:391-393`: `resolveStudyUid` returns `null` on any QIDO error. The report is then stored with no study link and ACKed AA (`:216-219`), with no alert.
- `src/clinical/routes.ts:1011`:
  ```ts
  sendApiError(response, 502, 'arc_light_unreachable', 'Unable to query arc-light.', trace, { reason: error instanceof Error ? error.message : 'unknown' });
  ```
  Any unexpected error, including a bug, is reported as an arc-light outage, with the raw message attached.
- Failure: a HIS outage looks like "no report exists". An ORU whose study lookup failed is accepted and never linked, and the operator gets no signal.
- Fix: distinguish upstream from internal errors. Return 503 for upstream outages and 500 for bugs. Retry ORU linking, and quarantine unlinked messages.

### 🟡 F-31: Admin MWL update can rewrite creator provenance
- `src/admin/mwlAdminClient.ts:135-136` lets `created_by_user_id` and `created_by_name` be overwritten by `PUT /api/v1/admin/mwl/{id}`. The route passes the raw body (`src/admin/routes.ts:487`).
- Fix: make creator fields immutable after create.

### 🟡 F-32: Report withdraw has no ownership check
- `src/clinical/routes.ts:449-462` gates only on `requireReportAuthorAccess`. `src/clinical/reportService.ts:295-344` does not compare the withdrawing user with the report author. Any radiologist can withdraw any final report.
- Fix: require the author, an admin, or a documented second-reader reason.

### 🟡 F-33: Worklist, take-next, operations, and study search see only the first 200 archive rows
- `src/clinical/clinicalStoreUtils.ts:25-27`: `buildWorklistStudySearchQuery` hard-codes `limit: 200`. `src/clinical/studyGovernanceService.ts:117-119` passes `limit: 200` with `cursor: undefined` every time. `src/clinical/worklistService.ts:74`, `src/clinical/operationsService.ts:306-308`, and the study and patient search paths all build on it.
- The operations comment (`src/clinical/operationsService.ts:306-308`) says "500 rather than the worklist's 200", but the code hard-codes 200.
- Failure: on an archive with more than 200 studies, take-next can return "nothing to read" while unread studies exist beyond the window, and the department dashboard under-reports the backlog. Which rows fall in the window depends on QIDO's default ordering (§9 Q8).
- Fix: page through QIDO with an offset until it is exhausted, or push status and modality filters down to the archive. If a cap is kept, return an explicit truncation flag.

### 🟡 F-34: Study status filter on search and patient studies can never match
- `src/arcLightClient.ts:491`: `status: 'arrived',` for every archive row. `src/clinical/studyGovernanceService.ts:603-616`: `decorateStudyRow` changes status only to `'rejected'`, and `shouldIncludeClinicalStudy` filters on `requestedStatuses.includes(row.status)`.
- Failure: `/studies/search?status=final` or `/patients/{mrn}/studies?status=preliminary` returns an empty list. Only `arrived` and `rejected` can match.
- Fix: join study rows to worklist and report state before filtering, or drop the status filter from study search.

### 🟡 F-35: Blocking network I/O inside the state lock during sign
- `src/clinical/reportService.ts:104-157`: `this.repository.update(async (state) => { ... const study = await getStudySummary(this.config, studyUid); ... const stow = await storeDicomSr(this.config, sr); ... })`.
- `src/clinical/clinicalStateRepository.ts:386-403, 437-444`: in Postgres mode the `FOR UPDATE` row lock is held across these calls. In memory and file modes the queue blocks all clinical writes.
- Failure: a slow or hung arc-light (no timeout, `src/arcLightClient.ts:178`) stalls every clinical write in the deployment. A successful STOW followed by a failed state commit leaves an orphan SR in the archive.
- Fix: do the archive calls before taking the lock, then commit state with the SR UID. Use compensation or an outbox for the orphan case.

### 🟡 F-36: No timeouts on arc-light and HIS REST or PDF calls
- `src/arcLightClient.ts:178-185, 429-431, 460-462`; `src/clinical/externalReportAdapters.ts:50, 98`. None sets an `AbortSignal`. Only the HIS callback, the MLLP client, and the Prometheus client have timeouts.
- Failure: a hung upstream holds the request, and the lock (F-35), indefinitely.
- Fix: set an explicit `AbortSignal.timeout(...)` on every outbound call. Put arc-light behind the same circuit breaker used for HIS.

### 🟡 F-37: ACK fallback reads the processing ID (MSH-11) instead of the control ID (MSH-10)
- `src/clinical/hl7OruMllpListener.ts:126-131`:
  ```ts
  const fields = msh.split('|');
  return fields[10]?.trim() || 'UNKNOWN';
  ```
  With `split('|')`, index 9 is MSH-10 (message control ID) and index 10 is MSH-11. The correct index is confirmed by `src/clinical/hl7OruIngestion.ts:263` (`readField(mshFields, 9)`) and `src/integration/hisOrderIngestService.ts:90` (`fields[9]`).
- Failure: the NACK for a disabled ORU, or for a thrown error, carries processing ID `P` as its control ID. The sender cannot correlate it and retries or duplicates.
- Fix: use `fields[9]`. Add a unit test on a sample MSH.

### 🟡 F-38: ADT preview reports an unmerge event that was never applied
- `src/clinical/hl7AdtIngestion.ts:330-343` (`buildPreview`) sets `event: parsed.eventCode === 'A37' ? 'unmerge' : 'merge'`. A37 is reported as ignored and not applied (`:93-114`), so the preview claims an unmerge event that never happened.
- Fix: report A37 as `ignored` with `applied: false`, and keep the event name consistent with the stored record.

### 🟡 F-39: Full-state clone and write on every request
- `src/clinical/clinicalStateRepository.ts:300, 304-307, 375-380, 392-393`: every `read()` and `update()` runs `cloneState` (`JSON.parse(JSON.stringify(...))`, `:496-498`). Postgres `update` writes the whole row (`:446-458`).
- Failure: latency grows with total history, not with the request. The per-request write in `GET /v/s/{token}` (F-13) makes it worse.
- Fix: move to per-entity tables, or at least split the snapshot into per-patient and per-study documents.

### 🟡 F-40: Idempotency maps, history maps, and caches grow without bound
- `src/clinical/clinicalStateRepository.ts:186-193` and `:200-201`: ten idempotency maps, never evicted. `:199`: `shareLinksById`. `:196-198`: HL7 message and revision maps. `src/clinical/shareLinkService.ts:159`: `used_at` growth. `src/clinical/externalReportAdapters.ts:21`: `restCache` Map, with expired entries never removed. `src/integration/hisDeepLinkService.ts:35, 91-98`: token map, purged only on mint.
- Fix: TTL eviction on idempotency keys (for example 24 h), bounded arrays, and an LRU limit on the cache.

### 🟢 F-41: No `NODE_ENV` guard; default Express error handler
- No `NODE_ENV` reference in `src/` (grep). No four-argument error middleware in `src/app.ts`. Malformed JSON bodies therefore reach Express's default handler, which in non-production renders a stack trace. The body parser runs before auth (`src/app.ts:36`), so the response is unauthenticated.
- Fix: add an error handler that returns the standard envelope, and call `app.disable('x-powered-by')`.

### 🟢 F-42: `/healthz` exposes internal failure messages
- `src/app.ts:48-58` returns `getAllHisCircuitBreakerStatuses()` unauthenticated when HIS is enabled. `src/integration/hisCircuitBreaker.ts:62-65` stores `last_failure_message`, which can contain host and port text from `ECONNREFUSED`.
- Fix: return only breaker state on `/healthz`. Move details behind admin.

### 🟢 F-43: No `trust proxy` configured
- `src/app.ts:42` and `src/audit.ts:447-449` record `request.ip`. Behind Traefik this is the proxy address, so audit IP attribution is wrong. `app.set('trust proxy', ...)` is absent.
- Fix: set `trust proxy` to the Traefik network only.

### 🟢 F-44: Placeholder literals on production paths
- `src/app.ts:570`: `dev-his-integration`, accepted only in dev mode. Acceptable, but still a fixed string.
- `src/clinical/externalReportAdapters.ts:289`: `placeholder` (F-29).
- `src/clinical/reportSourceResolver.ts:223`: every external report gets "Deterministic local-development placeholder until live HIS adapters are wired." as an extra section.
- Fix: gate placeholder text behind dev mode, or fail the adapter.

### 🟢 F-45: Keyed maps built from external input use plain objects
- `src/clinical/hl7OruIngestion.ts:128, 166, 214`: `state.hl7MessagesByControlId[parsed.messageControlId]` and `state.externalHl7RevisionsByAccession[parsed.accessionNumber]` are plain objects keyed by HL7 fields. `src/clinical/clinicalStore.ts:272` does the same with patient MRN. `src/clinical/hl7AdtIngestion.ts:364` does it for alias keys.
- By inspection (not run): an accession or control ID of `__proto__` reads `Object.prototype`, so the revision spread throws and the message is ACKed AR. Nothing persists corruptly, because the snapshot round-trips through JSON.
- Fix: use `Map` or `Object.create(null)` for collections keyed from external input.

### 🟢 F-46: Dead code, unused dependency, and a duplicated helper
- `zod` is declared in `bff-svc/package.json` dependencies and never imported (grep: 0 matches).
- `src/admin/arcLightAdminClient.ts:362-368` (`rejectStudy`, `unrejectStudy`) are unused; routes use `studyGovernanceService`.
- `src/admin/arcLightAdminClient.ts:552` (`createMwlPlaceholder`) is unused.
- `src/clinical/reportSourceResolver.ts:344` (`buildExternalNarrativeSections`) is unused (its only grep hit is the definition).
- `sanitizeFileSegment` is duplicated in `src/clinical/reportSourceResolver.ts:442` and `src/clinical/externalReportAdapters.ts:309`.

### 🟢 F-47: Client-supplied attestation time, audited before the sign succeeds
- `src/clinical/routes.ts:391-396` writes `report.attestation.confirmed` before `signReportDraft` runs. `src/clinical/routes.ts:880-899` accepts any valid `confirmed_at` with no bound against server time.
- Fix: stamp the server time, and audit after success.

### 🟢 F-48: Addendum merge heuristic can silently fold content
- `src/clinical/hl7OruIngestion.ts:366-383`: when the incoming addendum is under 30% of the current body length, it is appended to the current addendum instead of replacing it. The 30% threshold is a magic constant.
- Fix: surface the merge as a warning in the response and in `warnings`.

### 🟢 F-49: Non-cryptographic identifiers
- `src/clinical/hl7OruIngestion.ts:699-701` (`randomId` uses `Math.random`), `src/clinical/studyGovernanceService.ts:383, 463` (IOCM and QC IDs), and `src/admin/mwlAdminClient.ts:205` (temp MRN suffix). These are identifiers, not secrets, but collisions and guessing are avoidable.
- Fix: use `crypto.randomUUID()`.

### 🟢 F-50: CORS wildcard on the BFF
- `src/app.ts:35` `app.use(cors())` returns `Access-Control-Allow-Origin: *` for every route. Traefik overrides this for `pacs.local` (`infra/traefik/dynamic/routers.yml:104-112`). Direct access to the BFF (dev, or a misrouted request) is open to any origin that holds a valid bearer.
- Fix: pass an explicit origin allowlist to `cors()`.

### 🟢 F-51: Share-link idempotency returns the cached link even when the payload differs
- `src/clinical/shareLinkService.ts:45-52`: a cache hit ignores the new `permissions`, `expires_in`, and `recipient`.
- Fix: include a hash of the request body in the cache key, or return 409.

### 🟢 F-52: Documentation and implementation drift
From `bff-api-surface.md`, which describes planned behaviour as current:
- §1.2 says "Public endpoints: none" (`bff-api-surface.md:16`). Code has seven public routes (§3.1).
- §1.2 says the JWKS cache is 1 hour (`bff-api-surface.md:17`). The code sets no cache option.
- §6.2 saved views, §14.2 arc-light study-stable webhook, and the §13.1 `study.arrived` event are absent from code and from `openapi.yaml`.
- §8.3 requires the external recipient's email (`bff-api-surface.md:1070`) and describes a viewer-JWT redirect (`:1052-1077`). Neither is implemented (F-11, §3.8).
- §13.1 documents a 25 s heartbeat and `Last-Event-ID` (`bff-api-surface.md:1724`). The code sends one heartbeat and keeps no stream (`src/clinical/routes.ts:524-542`).
- §14.1 gives the MLLP port as "typically 2576" (`bff-api-surface.md:1731`). The BFF listener default is 2575 (`src/config.ts:165`). The admin HL7 catalog shows both 2575 and 2576 (`src/admin/hl7AdminClient.ts:60, 69`).

From `hl7-oru-parsing-approach.md`, which describes planned design:
- §10.3 step 2: SHA-256 of raw bytes as the dedupe key (`hl7-oru-parsing-approach.md:395`). The code dedupes by MSH-10 only (§5.2).
- §10.3 step 3: store the raw payload. The code stores only a redacted preview (`src/clinical/hl7OruIngestion.ts:457-487`), so the resubmit action in §12 (`hl7-oru-parsing-approach.md:450`) is impossible.
- §11.2: MRN cross-check (F-03; `hl7-oru-parsing-approach.md:426`).
- §12: dead-letter table and admin resubmit, link, or discard. The admin implementation is an in-memory mock (`src/admin/arcLightAdminClient.ts:142-144, 414-419`).

OpenAPI drift: the 22 implemented-but-undocumented operations, marked **NO** in §2. They are `/metrics`; `/api/v1/admin/promotions/active`; `/api/v1/admin/promotions/{promoId}/click`; `/api/v1/patients/{mrn}/notes` (GET and POST); `/api/v1/studies/priors`; `/api/v1/qc-flags/{flagId}/withdraw`; `/api/v1/admin/system/{versions,backups,certificates,license}`; `/api/v1/admin/operations`; `/api/v1/admin/users/{id}` (GET, PATCH, DELETE); `/api/v1/admin/rejected-hl7/{id}/{resubmit,link,discard}`; and `/api/v1/admin/compression-rules` (GET, POST, PUT, DELETE).

### 7.12 Tests
- `tests/bff.test.ts` has 67 cases covering health, auth rejection, the dev-user profile, study access and context issuer checks, priors, hanging protocols, HIS deep links and order ingest, report-callback failure, patient search, worklist, report lifecycle and attestation, HL7 precedence, share links, notifications, and critical findings.
- **Not covered**:
  - The MLLP listener and ORU or ADT parsing over TCP (grep of `tests/` for `startHl7OruMllpListener`, `processFrame`, `ingestHl7AdtPayload`, and `ingestHl7OruPayload`: no hits).
  - 403 behaviour on `/studies/{uid}/context`, `/hanging-protocols/match`, and `/events`. The seven `expect(403)` assertions target deep-link mint, worklist, admin health, admin AE create, admin HL7 app create, admin queue delete, and walk-in MWL. None targets those three routes.
  - Session revocation's effect on access, `/v/s` email enforcement, and the ACK control-ID path.
- `tests/auth-defaults.test.ts` covers config defaults only.
- Not executed (per the rules of this review).

---

## 8. Strengths

1. **Fail-closed auth defaults.** `BFF_AUTH_MODE` is `oidc` unless it is exactly `dev` (`src/config.ts:124`), and this is tested (`tests/auth-defaults.test.ts:14-17`). The HIS order bearer is stored as SHA-256 and compared with `timingSafeEqual` (`src/app.ts:560-566`). It is refused when unconfigured in OIDC mode (`:574`).
2. **Strict JWT verification via `jose`**, with issuer and audience checks and remote JWKS (`src/auth.ts:95-111`). No hand-rolled crypto.
3. **Positive identity attestation for sign and addendum**, checked against the live archive on study UID, accession, patient name, MRN, DOB, and issuer. A mismatch returns 409 with the mismatched fields listed (`src/clinical/routes.ts:848-878`). This is a strong clinical-safety control.
4. **Atomic take-next** inside the state lock, with the candidate filter re-evaluated on fresh state (`src/clinical/worklistService.ts:75-104`).
5. **Optimistic-concurrency preconditions** available on every clinical mutation (`expected_updated_at`, `expected_draft_version`, `expected_report_id`, `base_version`) with clear 409 bodies (`src/clinical/clinicalStoreUtils.ts:297-347`). `Idempotency-Key` support on mutations.
6. **Share-link tokens** are 192-bit random (`src/clinical/clinicalStoreUtils.ts:373-375`). Image and DICOM sharing is explicitly refused until a scoped exchange exists (`src/clinical/shareLinkService.ts:35-40`; `src/clinical/clinicalErrors.ts:53-55`).
7. **Parameterized SQL and encoded QIDO parameters**, so search inputs have no injection surface (`src/arcLightClient.ts:417-420`; `src/audit.ts:284-311`).
8. **Audit redaction by key** for tokens, passwords, bearer values, and raw payloads (`src/audit.ts:549-566`). The share-link token is redacted in practice.
9. **HL7 raw-preview redaction** of PID and OBX-5 values and NTE (`src/clinical/hl7OruIngestion.ts:469-487`), and ADT PID and MRG redaction (`src/clinical/hl7AdtIngestion.ts:475-485`).
10. **Postgres row lock for cross-process serialization** (`src/clinical/clinicalStateRepository.ts:437-444`) and validated clinical schema identifiers (`:890-896`). The file mode is honestly limited to one process by its in-process queue.
11. **Documented domain rules with reasons**: priors comparability with CR/DX equivalence and body-part tolerance (`src/clinical/priorStudies.ts:1-38`); the `ModalitiesInStudy` fix (`src/arcLightClient.ts:549-584`); the hanging-protocol matching fix (`src/viewer/studyPresentation.ts:316-347`); TAT measured to first signature (`src/clinical/operationsService.ts:18-24`).
12. **Honest adapter labelling** in several places: `source: 'seeded_local'` on protocols (`src/viewer/studyPresentation.ts:198`); explicit disabled and log notification adapters (`src/clinical/notificationService.ts:31-74`); `secret_set` flags instead of secrets (`src/admin/arcLightAdminClient.ts:514-517`).
13. **Circuit breaker and timeouts on HIS paths** (`src/integration/hisCircuitBreaker.ts`; `src/integration/hisMllpClient.ts:20`; `src/integration/hisReportCallbackService.ts:37`).
14. **Consistent admin write gating**: every admin write handler calls `requireAdminWriteAccess`, verified by reading all 751 lines of `src/admin/routes.ts`.
15. **Bounded inputs where it counts**: 100 KB JSON limit, `limit` 1 to 200, and prior lookups capped at 50 with an explicit refusal instead of silent truncation (`src/clinical/routes.ts:690-711`).
16. **Good breadth of clinical tests**: 67 cases in `tests/bff.test.ts`, including the precedence matrix and the sign attestation flow.

---

## 9. Open questions

1. **DICOMweb access control.** Does arc-light enforce authentication or authorization on QIDO, WADO, and STOW? The BFF sends no credentials, and the Traefik `dicom-web` router has no auth middleware (`infra/traefik/dynamic/routers.yml:16-21`). The answer decides whether F-10 is blocking. **Highest priority.**
2. **Where does the BFF run in production?** `infra/docker-compose.prod.yml` has no BFF or arc-light service (services: db-sidecar, ldap-sidecar, minio x5, traefik, haproxy, prometheus, grafana). Which deployment file is authoritative, and does it set `BFF_AUTH_MODE` at all?
3. **IOCM semantics.** Is a rejection meant to remove images from access (DICOM-level), or only to hide them in the BFF UI? The code and comments (`src/arcLightClient.ts:419-424`) describe the latter.
4. **Is STOW intended for production** (`REPORT_SR_STORE_MODE=stow`), and who owns the SR verifier metadata (F-23)?
5. **Should the operator role read the audit log and the user directory?** (F-17)
6. **Critical-finding escalation**: is an external scheduler planned (worker, cron, or the archive), or must the BFF own it? (F-07)
7. **Patient-level access control**: is role-only access (any clinical role reads any patient, F-15) accepted for this deployment, or is a care-relationship or break-glass model planned?
8. **Archive ordering and size**: what order does arc-light return QIDO results in, and what study count is expected? This decides how far the 200-row window (F-33) reaches.
9. **Session revocation**: what should "revoke session" mean (token deny-list, forced logout, or remove the control)? (F-14)
10. **Share-link policy**: must external recipients always enter their email (`bff-api-surface.md:1070`)? Should `/v/s/*` be routed through Traefik? Only `/api` routes to the BFF (`infra/traefik/dynamic/routers.yml:7-12`), so the public landing may be unreachable from the browser domain.
11. **HIS MRN policy**: should ORU reports be refused on MRN mismatch (doc §11.2), or accepted with an alert? (F-03)
12. **Audit retention requirement**: what retention period is required, and which store is the system of record? (F-08)
13. **Keycloak token contents**: does the access token carry `aud: pacs-bff` (this needs an audience mapper), and what lifetime is configured? The BFF relies on `aud`, and the doc claims a 1-hour JWKS cache.
14. **ORU listener network placement**: which network can reach 2575? If it is only the HIS VLAN, F-02 is less severe, but ADT still lacks a gate.
15. **HL7 raw retention**: is storing the raw ORU payload intended (for resubmit), given the PHI exposure? (§5.2)
16. **Admin mock data**: are the seeded queues, AE titles, users (including the "samira.admin" actor names), and the backup and license stubs fixtures to be removed before any demonstration? (F-18)
17. **Multi-replica deployment**: is a single BFF instance the only supported topology? With file or memory state and an in-process queue, the BFF cannot scale horizontally.
18. **Walk-in and admin MWL**: should entries reach the archive or a real MWL SCP? Today they do not (§1.3).
19. **Email delivery**: is a real SMTP or notification service planned, or is the log adapter the intended production behaviour?

---

## Verification log (for the reviewer)
- Read every file in `bff-svc/src/` in full (50 files, 15,142 lines).
- Route inventory: Grep and shell extraction from `src/app.ts` (19 handlers), `src/clinical/routes.ts` (30), and `src/admin/routes.ts` (70 handler lines; 72 operations including the 3-route loop). Total 121, matching the normalized inventory. Compared against `openapi.yaml` (99 operations): every documented operation exists in code; 22 implemented operations are undocumented.
- Outbound calls: Grep for `fetch(`, `net.createServer`, `net.Socket`, `new Pool(`, `createRemoteJWKSet`. Timeouts: Grep for `setTimeout`, `AbortSignal`, `AbortController`.
- Absence checks by Grep: `setInterval`, `trust proxy`, rate limiting, helmet, FHIR, IHE, PIX, PDQ, XDS, DIMSE, MPPS, peer review, ward scoping, `NODE_ENV`, zod import, router-level `use`, `revokedSessionIds` consumers.
- Config and env: key names and non-secret mode values only (`.env.example`, `.env.development`, root `.env`). Secret-type values were not read or reproduced.
- Local data: counted audit entries in `bff-svc/.data/audit-log.json` (1,000) and read only the first and last `at` dates. `clinical-state.json` content was not read.
- Tests: counted and grepped `tests/bff.test.ts` (67 cases) and `tests/auth-defaults.test.ts`. Not run.
- Not verified: runtime behaviour, arc-light authorization, actual HIS endpoints, Keycloak realm configuration, Traefik behaviour under load, and the `dist/` build.
