# PAXS Archive — HTTP API Reference (baseline `e90d32c`, superseded)

> 🗄️ Historical snapshot. Current reference: [../API_REFERENCE.md](../API_REFERENCE.md).

> Generated from the router source code (`app/api/v1/*.py`) by parsing every `@router.<method>` decorator and its `Depends(...)` parameters (no server run). Snapshot of commit `e90d32c`. When the server runs, interactive OpenAPI docs are served at `/api/docs`.

## How to read the *Auth* column

| Label | Meaning (see `app/api/v1/deps.py`) |
|---|---|
| ADMIN | `require_admin` — role must be `ADMIN` |
| ADMIN / RADIOLOGIST | `require_destructive_role` — role in `("ADMIN", "RADIOLOGIST")` |
| Any user (bearer) | `get_current_user` — valid `Authorization: Bearer <JWT>`, any role |
| Any user (bearer or cookie) | `get_current_user_any` — bearer JWT **or** the HttpOnly `paxs_session` refresh cookie |
| **Optional — anonymous allowed** | `get_optional_user` — never rejects; anonymous callers reach the handler with `user=None`. Several *write* routes use this — see [CODE_REVIEW.md](CODE_REVIEW_e90d32c.md) |
| **None — public** | no auth dependency at all |
| `+ inline role check` | the handler body also compares `user.role` itself |
| `+ license` | the route depends on `get_license_manager` |

**Totals:** 275 route registrations across 48 router modules.

| Auth level | Routes |
|---|---|
| Any user (bearer) | 114 |
| ADMIN | 69 |
| **None — public** | 39 |
| ADMIN / RADIOLOGIST | 24 |
| **Optional — anonymous allowed** | 23 |
| Public (rate-limited) | 3 |
| Any user (bearer or cookie) | 3 |

## Endpoints defined directly in `app/main.py`

| Method | Path | Auth | Purpose |
|---|---|---|---|
| GET | `/` | Viewer auth gate (session cookie) | Legacy dashboard `app/static/index.html` |
| GET | `/login`, `/login/` | Public | Login page; signed-in users are 303-redirected to their role landing or `?next=` |
| GET | `/logout` | Public | Clears the session cookie (does **not** revoke the server-side session row) |
| GET | `/viewer`, `/viewer/`, `/viewer/index.html`, `/viewer/viewer` | Viewer auth gate | React study list (`/studies-ui` build), or the PaxsViewer (OHIF fork) when `?StudyInstanceUIDs=` / `?study=` is present |
| MOUNT | `/viewer/*` | Gate (assets pass without a cookie) | PaxsViewer static bundle with SPA fallback to `index.html` |
| MOUNT | `/studies-ui/*` | Viewer auth gate | React + shadcn/ui study list build |
| MOUNT | `/settings/*` | Viewer auth gate | Organization settings & reporting dashboard |
| MOUNT | `/admin/*` | **Not gated** | PaxsViewer Control Panel (Electron renderer + web bridge), when `ADMIN_PANEL_ENABLED` |
| MOUNT | `/static/*` | Public | Legacy dashboard assets, logos, `uploads/` |
| GET | `/PaxsViewer-logo.png` | Public | Viewer splash logo |
| GET | `/terms` | Public | Terms & privacy markdown (`feature-plans/terms.md`) |
| GET | `/paxs-viewer-guide` | Public | Viewer user guide markdown (`paxs_viewer_explain.md`) |
| GET | `/api/docs`, `/api/openapi.json` | Public | FastAPI Swagger UI / OpenAPI schema |

## Non-HTTP listeners

| Protocol | Default bind | Enabled by | Code |
|---|---|---|---|
| DICOM DIMSE SCP — C-ECHO, C-STORE, C-FIND (Q/R + MWL), C-MOVE, C-GET, MPPS N-CREATE/N-SET, Storage Commitment N-ACTION | `0.0.0.0:10400`, AE `PAXS_SCP` | `DICOM_RECEIVER_ENABLED=true` (default) | `app/dicom/receiver.py` |
| HL7 v2 over MLLP — ADT, ORM^O01, QBP^Q22/Q23 | `0.0.0.0:2575` | `HL7_ENABLED=false` (default off) | `app/dicom/hl7.py` |
| HL7 ORU^R01 outbound to the EMR (on report finalize) | `127.0.0.1:6661` | `HL7_EMR_ENABLED=false` | `app/dicom/hl7.py`, `app/services/report_service.py` |

## Routes by module

### `admin_panel.py` — PaxsViewer Control Panel web bridge — REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/admin-panel/actions/{action}` | ADMIN | [`panel_action`](../../app/api/v1/admin_panel.py#L53) |  |
| GET | `/api/v1/admin-panel/config` | Any user (bearer) | [`panel_get_config`](../../app/api/v1/admin_panel.py#L39) |  |
| POST | `/api/v1/admin-panel/config` | ADMIN | [`panel_save_config`](../../app/api/v1/admin_panel.py#L44) |  |
| GET | `/api/v1/admin-panel/logs` | Any user (bearer) | [`panel_logs`](../../app/api/v1/admin_panel.py#L34) |  |
| GET | `/api/v1/admin-panel/status` | Any user (bearer) | [`panel_status`](../../app/api/v1/admin_panel.py#L28) |  |

### `ai.py` — PAXS Clinical AI & Diagnostic Intelligence Endpoints.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/ai/infer` | Any user (bearer) + license | [`run_ai_inference`](../../app/api/v1/ai.py#L91) | Runs automated clinical AI computer vision inference on a volumetric series. |
| GET | `/api/v1/ai/mask/{series_instance_uid}` | Any user (bearer) | [`get_segmentation_mask`](../../app/api/v1/ai.py#L119) | Returns raw binary 3D segmentation mask (Uint8Array) for 3D overlay rendering. |
| GET | `/api/v1/ai/models` | Any user (bearer) + license | [`get_available_ai_models`](../../app/api/v1/ai.py#L35) | Returns the list of clinical AI models available under the active license. |
| POST | `/api/v1/ai/sr` | Any user (bearer) + license | [`generate_and_save_sr`](../../app/api/v1/ai.py#L139) | Generates and permanently archives a DICOM Structured Report (TID 1500). |

### `auth.py` — Authentication: register (first user becomes ADMIN), login, refresh, me.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/auth/login` | Public (rate-limited) | [`login`](../../app/api/v1/auth.py#L219) |  |
| POST | `/api/v1/auth/logout` | **None — public** | [`logout`](../../app/api/v1/auth.py#L335) | Revoke the session row behind the cookie and clear it. |
| GET | `/api/v1/auth/me` | Any user (bearer or cookie) | [`me`](../../app/api/v1/auth.py#L353) |  |
| POST | `/api/v1/auth/refresh` | Public (rate-limited) | [`refresh`](../../app/api/v1/auth.py#L279) | Rotate the refresh token and issue a fresh short-lived access token. |
| POST | `/api/v1/auth/register` | Public (rate-limited) | [`register`](../../app/api/v1/auth.py#L172) |  |

### `clinical_protocols.py` — Clinical Protocols Library REST API (roadmap #31).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/clinical-protocols` | Any user (bearer) | [`list_protocols`](../../app/api/v1/clinical_protocols.py#L90) |  |
| POST | `/api/v1/clinical-protocols` | ADMIN / RADIOLOGIST | [`create_protocol`](../../app/api/v1/clinical_protocols.py#L80) |  |
| GET | `/api/v1/clinical-protocols/match` | Any user (bearer) | [`match_protocol`](../../app/api/v1/clinical_protocols.py#L97) | Best matching protocol for a study context (or null). |
| GET | `/api/v1/clinical-protocols/suggest` | Any user (bearer) | [`suggest_protocols`](../../app/api/v1/clinical_protocols.py#L113) | Ranked candidate protocols for the study context. |
| DELETE | `/api/v1/clinical-protocols/{protocol_id}` | ADMIN / RADIOLOGIST | [`delete_protocol`](../../app/api/v1/clinical_protocols.py#L150) |  |
| GET | `/api/v1/clinical-protocols/{protocol_id}` | Any user (bearer) | [`get_protocol`](../../app/api/v1/clinical_protocols.py#L128) |  |
| PATCH | `/api/v1/clinical-protocols/{protocol_id}` | ADMIN / RADIOLOGIST | [`update_protocol`](../../app/api/v1/clinical_protocols.py#L137) |  |

### `commitments.py` — Storage Commitment API — archive guarantee status.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/commitments` | Any user (bearer) | [`list_commitments`](../../app/api/v1/commitments.py#L55) | List storage commitment transactions, newest first (optional filter). |

### `config.py` — Admin Settings API — read and edit the server configuration.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/config` | Any user (bearer) | [`get_config`](../../app/api/v1/config.py#L47) |  |
| PATCH | `/api/v1/config` | ADMIN | [`patch_config`](../../app/api/v1/config.py#L77) |  |

### `critical_results.py` — Critical Results Tracking REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/critical-results` | Any user (bearer) | [`list_critical_results`](../../app/api/v1/critical_results.py#L120) | Query critical results with filters. |
| POST | `/api/v1/critical-results` | Any user (bearer) | [`create_critical_result`](../../app/api/v1/critical_results.py#L66) | Flag a critical or urgent radiology finding on a study. |
| GET | `/api/v1/critical-results/study/{study_uid}` | Any user (bearer) | [`get_study_critical_results`](../../app/api/v1/critical_results.py#L135) | List all critical findings associated with a specific study. |
| GET | `/api/v1/critical-results/{result_id}` | Any user (bearer) | [`get_critical_result`](../../app/api/v1/critical_results.py#L148) | Get details of a specific critical result. |
| POST | `/api/v1/critical-results/{result_id}/acknowledge` | Any user (bearer) | [`acknowledge_critical_result`](../../app/api/v1/critical_results.py#L96) | Formally acknowledge receipt of a critical finding. |

### `dashboard.py` — Human-facing REST API for the web dashboard: stats, study browsing,

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/audit` | ADMIN / RADIOLOGIST | [`list_audit`](../../app/api/v1/dashboard.py#L387) | Recent audit-trail events (tamper-evident SHA-256 fingerprints). |
| GET | `/api/v1/health` | **None — public** | [`health`](../../app/api/v1/dashboard.py#L144) |  |
| GET | `/api/v1/health/live` | **None — public** | [`health_live`](../../app/api/v1/dashboard.py#L123) | Liveness probe (HAProxy httpchk) — the process is up. |
| GET | `/api/v1/health/ready` | **None — public** | [`health_ready`](../../app/api/v1/dashboard.py#L129) | Readiness probe — the node can serve traffic (DB reachable). |
| GET | `/api/v1/stats` | **None — public** | [`stats`](../../app/api/v1/dashboard.py#L224) |  |
| GET | `/api/v1/storage/presign/{study_uid}/{series_uid}/{sop_uid}` | Any user (bearer) | [`presign_instance`](../../app/api/v1/dashboard.py#L805) | Short-lived direct-download URL for one instance. |
| GET | `/api/v1/studies` | **Optional — anonymous allowed** | [`list_studies`](../../app/api/v1/dashboard.py#L416) |  |
| DELETE | `/api/v1/studies/{study_uid}` | ADMIN / RADIOLOGIST | [`delete_study`](../../app/api/v1/dashboard.py#L835) |  |
| GET | `/api/v1/studies/{study_uid}` | **None — public** | [`study_detail`](../../app/api/v1/dashboard.py#L698) |  |
| GET | `/api/v1/studies/{study_uid}/series/{series_uid}/instances` | **None — public** | [`series_instances`](../../app/api/v1/dashboard.py#L752) |  |
| GET | `/api/v1/studies/{study_uid}/series/{series_uid}/thumbnail.png` | **None — public** | [`series_thumbnail`](../../app/api/v1/dashboard.py#L774) |  |
| GET | `/api/v1/triage` | Any user (bearer or cookie) | [`list_triage`](../../app/api/v1/dashboard.py#L644) | Unreviewed triage findings across the archive (emergency queue). |
| POST | `/api/v1/triage/{flag_id}/review` | Any user (bearer or cookie) | [`review_triage`](../../app/api/v1/dashboard.py#L685) | Mark a triage finding as reviewed (seen by a clinician). |

### `database.py` — Database administration API — Settings → Database tab.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/db/backup` | ADMIN | [`db_backup`](../../app/api/v1/database.py#L212) |  |
| GET | `/api/v1/db/backups` | ADMIN | [`db_backups`](../../app/api/v1/database.py#L218) |  |
| DELETE | `/api/v1/db/backups/{name}` | ADMIN | [`db_backup_delete`](../../app/api/v1/database.py#L234) |  |
| GET | `/api/v1/db/backups/{name}` | ADMIN | [`db_backup_download`](../../app/api/v1/database.py#L223) |  |
| POST | `/api/v1/db/configure` | ADMIN | [`db_configure`](../../app/api/v1/database.py#L96) |  |
| POST | `/api/v1/db/init` | ADMIN | [`db_init`](../../app/api/v1/database.py#L123) | Initialize a database (new install) or migrate it to head. |
| GET | `/api/v1/db/status` | Any user (bearer) | [`db_status`](../../app/api/v1/database.py#L78) |  |
| POST | `/api/v1/db/test` | ADMIN | [`db_test`](../../app/api/v1/database.py#L85) |  |

### `db_panel.py` — Admin → DB Pool panel API (read-replica / connection-pool dashboard).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/db/perf` | ADMIN / RADIOLOGIST | [`perf`](../../app/api/v1/db_panel.py#L37) | Live pool stats, MySQL counters, replica status and slow-query state. |
| GET | `/api/v1/db/perf/explain` | ADMIN / RADIOLOGIST | [`explain`](../../app/api/v1/db_panel.py#L54) |  |
| POST | `/api/v1/db/perf/replica` | ADMIN / RADIOLOGIST | [`toggle_replica`](../../app/api/v1/db_panel.py#L63) |  |
| GET | `/api/v1/db/perf/slow-queries` | ADMIN / RADIOLOGIST | [`list_slow_queries`](../../app/api/v1/db_panel.py#L49) |  |
| POST | `/api/v1/db/perf/slow-query` | ADMIN / RADIOLOGIST | [`toggle_slow_query`](../../app/api/v1/db_panel.py#L43) |  |

### `dicomdir.py` — DICOMDIR Media Export REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/export/dicomdir` | Any user (bearer) | [`export_dicomdir_package`](../../app/api/v1/dicomdir.py#L34) | Generate and download a standard DICOMDIR export ZIP archive (contains DICOMDIR file + IMAGES/). |
| POST | `/api/v1/export/dicomdir/preview` | Any user (bearer) | [`preview_dicomdir_structure`](../../app/api/v1/dicomdir.py#L62) | Preview the DICOMDIR record hierarchy structure (PATIENT -> STUDY -> SERIES -> IMAGE). |

### `dicomweb.py` — DICOMweb (PS3.18) REST API:

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/dicom-web/instances` | **Optional — anonymous allowed** | [`qido_instances`](../../app/api/v1/dicomweb.py#L280) |  |
| GET | `/dicom-web/series` | **Optional — anonymous allowed** | [`qido_series`](../../app/api/v1/dicomweb.py#L258) |  |
| GET | `/dicom-web/studies` | **Optional — anonymous allowed** | [`qido_studies`](../../app/api/v1/dicomweb.py#L198) |  |
| POST | `/dicom-web/studies` | **Optional — anonymous allowed** | [`stow_studies`](../../app/api/v1/dicomweb.py#L806) | STOW-RS: ingest DICOM instances from a multipart/related POST. |
| GET | `/dicom-web/studies/{study_uid}` | **None — public** | [`wado_study`](../../app/api/v1/dicomweb.py#L360) |  |
| POST | `/dicom-web/studies/{study_uid}` | **Optional — anonymous allowed** | [`stow_studies`](../../app/api/v1/dicomweb.py#L806) | STOW-RS: ingest DICOM instances from a multipart/related POST. |
| GET | `/dicom-web/studies/{study_uid}/instances` | **Optional — anonymous allowed** | [`qido_instances`](../../app/api/v1/dicomweb.py#L280) |  |
| GET | `/dicom-web/studies/{study_uid}/metadata` | **None — public** | [`wado_study_metadata`](../../app/api/v1/dicomweb.py#L328) |  |
| GET | `/dicom-web/studies/{study_uid}/series` | **Optional — anonymous allowed** | [`qido_series`](../../app/api/v1/dicomweb.py#L258) |  |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}` | **None — public** | [`wado_series`](../../app/api/v1/dicomweb.py#L377) |  |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances` | **Optional — anonymous allowed** | [`qido_instances`](../../app/api/v1/dicomweb.py#L280) |  |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}` | **None — public** | [`wado_instance`](../../app/api/v1/dicomweb.py#L404) |  |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/bulk/{tag_path}` | **None — public** | [`wado_bulk_data`](../../app/api/v1/dicomweb.py#L736) | WADO-RS Bulk Data Retrieval (PS 3.18) for specific attributes (PixelData, OverlayData). |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/frames/{frame_list}` | **None — public** | [`wado_frame`](../../app/api/v1/dicomweb.py#L594) | WADO-RS Frame Retrieval (PS 3.18) for single-frame or multi-frame stacks. |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/metadata` | **None — public** | [`wado_instance_metadata`](../../app/api/v1/dicomweb.py#L416) |  |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/rendered` | **None — public** | [`wado_rendered`](../../app/api/v1/dicomweb.py#L426) |  |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/instances/{sop_uid}/thumbnail` | **None — public** | [`wado_thumbnail`](../../app/api/v1/dicomweb.py#L513) | WADO-RS rendered thumbnail (PaxsViewer/PAXs ``thumbnailRendering: 'wadors'`` |
| GET | `/dicom-web/studies/{study_uid}/series/{series_uid}/metadata` | **None — public** | [`wado_series_metadata`](../../app/api/v1/dicomweb.py#L340) | WADO-RS series-level metadata (PaxsViewer v3.13+ fetches this to build display sets). |
| GET | `/dicom-web/wado` | **None — public** | [`wado_uri`](../../app/api/v1/dicomweb.py#L137) | WADO-URI: retrieve one instance (or its rendered image) by UID query |

### `dose.py` — Radiation Dose Monitoring (RDSR) REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/dose/analytics` | Any user (bearer) | [`get_dose_analytics`](../../app/api/v1/dose.py#L74) | Get archive-wide radiation dose statistics. |
| GET | `/api/v1/dose/patient/{patient_id}` | Any user (bearer) | [`get_patient_cumulative_dose`](../../app/api/v1/dose.py#L63) | Calculate cumulative lifetime radiation exposure metrics for a patient across all studies. |
| GET | `/api/v1/dose/report/{sop_uid}` | Any user (bearer) | [`get_dose_report`](../../app/api/v1/dose.py#L84) | Get details of a specific RDSR report by SOPInstanceUID. |
| GET | `/api/v1/dose/study/{study_uid}` | Any user (bearer) | [`get_study_dose`](../../app/api/v1/dose.py#L52) | Get all Radiation Dose Structured Reports for a study. |

### `export.py` — Research export: download a study as de-identified DICOM (ZIP).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/export/{study_uid}/anonymized` | Any user (bearer) | [`export_anonymized`](../../app/api/v1/export.py#L28) |  |

### `fhir.py` — FHIR R4 read gateway (HL7/FHIR integration).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/fhir/ImagingStudy` | **None — public** | [`search_imagingstudy`](../../app/api/v1/fhir.py#L171) |  |
| GET | `/fhir/ImagingStudy/{study_uid}` | **None — public** | [`read_imagingstudy`](../../app/api/v1/fhir.py#L235) |  |
| GET | `/fhir/Patient` | **None — public** | [`search_patient`](../../app/api/v1/fhir.py#L125) |  |
| GET | `/fhir/Patient/{patient_id}` | **None — public** | [`read_patient`](../../app/api/v1/fhir.py#L159) |  |
| GET | `/fhir/metadata` | **None — public** | [`capability_statement`](../../app/api/v1/fhir.py#L104) |  |

### `fusion.py` — Fusion (roadmap #29) REST API — Frame-of-Reference aligned series pairs.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/fusion/{study_uid}` | **Optional — anonymous allowed** | [`fusion_manifest`](../../app/api/v1/fusion.py#L30) | FoR groups + aligned series pairs + per-series geometry for a study. |

### `gsps.py` — GSPS (Presentation State) REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/gsps/instance/{sop_uid}/presentations` | **None — public** | [`find_presentations_for_instance`](../../app/api/v1/gsps.py#L61) | Find all GSPS objects that reference a given image instance. |
| GET | `/api/v1/gsps/study/{study_uid}` | **None — public** | [`list_gsps_for_study`](../../app/api/v1/gsps.py#L54) | List all GSPS objects belonging to a study. |
| GET | `/api/v1/gsps/{sop_uid}` | **None — public** | [`get_gsps`](../../app/api/v1/gsps.py#L45) | Retrieve a single GSPS by its SOP Instance UID. |

### `hanging_protocols.py` — Hanging Protocol REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/hanging-protocols` | Any user (bearer) | [`list_hanging_protocols`](../../app/api/v1/hanging_protocols.py#L79) | List all hanging protocols. |
| POST | `/api/v1/hanging-protocols` | Any user (bearer) | [`create_hanging_protocol`](../../app/api/v1/hanging_protocols.py#L63) | Create a new hanging protocol layout rule. |
| GET | `/api/v1/hanging-protocols/match` | Any user (bearer) | [`match_hanging_protocol`](../../app/api/v1/hanging_protocols.py#L89) | Find best matching hanging protocol layout for current study context. |
| DELETE | `/api/v1/hanging-protocols/{protocol_id}` | Any user (bearer) | [`delete_hanging_protocol`](../../app/api/v1/hanging_protocols.py#L126) | Delete a hanging protocol. |
| GET | `/api/v1/hanging-protocols/{protocol_id}` | Any user (bearer) | [`get_hanging_protocol`](../../app/api/v1/hanging_protocols.py#L113) | Get details of a specific hanging protocol. |

### `ian.py` — Instance Availability Notification (IAN) REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/ian/notifications` | Any user (bearer) | [`list_ian_notifications`](../../app/api/v1/ian.py#L80) | List IAN notification logs. |
| GET | `/api/v1/ian/notifications/{notif_id}` | Any user (bearer) | [`get_ian_notification`](../../app/api/v1/ian.py#L94) | Get details of a specific IAN notification record. |
| POST | `/api/v1/ian/notify` | Any user (bearer) | [`trigger_ian_notification`](../../app/api/v1/ian.py#L54) | Trigger N-CREATE Instance Availability Notification for a study. |

### `kin.py` — Key Image Notes (KIN / KO) REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/kin` | Any user (bearer) | [`create_key_image_note`](../../app/api/v1/kin.py#L55) | Create and bookmark key images with notes (creates a DICOM KO document). |
| GET | `/api/v1/kin/instance/{sop_uid}/notes` | Any user (bearer) | [`find_kin_for_instance`](../../app/api/v1/kin.py#L93) | Find Key Image Notes that reference a specific image instance. |
| GET | `/api/v1/kin/study/{study_uid}` | Any user (bearer) | [`list_kin_for_study`](../../app/api/v1/kin.py#L82) | List all Key Image Notes in a study. |
| GET | `/api/v1/kin/{sop_uid}` | Any user (bearer) | [`get_key_image_note`](../../app/api/v1/kin.py#L104) | Get details of a specific Key Image Note by its SOPInstanceUID. |

### `ldap.py` — LDAP / Active Directory Authentication REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/ldap/login` | **None — public** | [`ldap_login`](../../app/api/v1/ldap.py#L30) | Authenticate enterprise user via LDAP / Active Directory and issue PAXS JWT token. |

### `license.py` — PAXS Licensing API Endpoints.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/license` | **None — public** + license | [`get_license_status`](../../app/api/v1/license.py#L50) | Returns current server license status, edition, features, and expiration. |
| POST | `/api/v1/license/activate` | ADMIN + license | [`activate_license_base64`](../../app/api/v1/license.py#L82) | Activates a license by providing base64-encoded license content. |
| POST | `/api/v1/license/activate-online` | ADMIN + license | [`activate_license_online`](../../app/api/v1/license.py#L136) | Activates a license key online against the vendor activation portal. |
| GET | `/api/v1/license/fingerprint` | **None — public** | [`get_server_fingerprint`](../../app/api/v1/license.py#L58) | Returns the host machine hardware fingerprint and system identifiers. |
| POST | `/api/v1/license/reload` | ADMIN + license | [`reload_license`](../../app/api/v1/license.py#L67) | Forces the server to re-read and verify paxs.lic from disk. |
| GET | `/api/v1/license/status` | **None — public** + license | [`get_license_status`](../../app/api/v1/license.py#L50) | Returns current server license status, edition, features, and expiration. |
| POST | `/api/v1/license/upload` | ADMIN + license | [`upload_license_file`](../../app/api/v1/license.py#L108) | Uploads and activates a .lic license file directly. |
| POST | `/api/v1/license/validate-online` | ADMIN + license | [`validate_license_online`](../../app/api/v1/license.py#L158) | Manually triggers an online license heartbeat validation. |

### `lifecycle.py` — Study Lifecycle Management REST API (item 14).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/lifecycle/rules` | Any user (bearer) | [`list_rules`](../../app/api/v1/lifecycle.py#L96) |  |
| POST | `/api/v1/lifecycle/rules` | Any user (bearer) | [`create_rule`](../../app/api/v1/lifecycle.py#L80) |  |
| DELETE | `/api/v1/lifecycle/rules/{rule_id}` | Any user (bearer) | [`delete_rule`](../../app/api/v1/lifecycle.py#L132) |  |
| GET | `/api/v1/lifecycle/rules/{rule_id}` | Any user (bearer) | [`get_rule`](../../app/api/v1/lifecycle.py#L103) |  |
| PUT | `/api/v1/lifecycle/rules/{rule_id}` | Any user (bearer) | [`update_rule`](../../app/api/v1/lifecycle.py#L112) |  |
| POST | `/api/v1/lifecycle/run` | Any user (bearer) | [`run_lifecycle`](../../app/api/v1/lifecycle.py#L142) | Evaluate enabled lifecycle rules now (or a single rule by id). |
| GET | `/api/v1/lifecycle/studies` | Any user (bearer) | [`list_studies`](../../app/api/v1/lifecycle.py#L192) | List studies by lifecycle state (default: the cold tier). |
| POST | `/api/v1/lifecycle/studies/{study_uid}/archive` | Any user (bearer) | [`archive_one`](../../app/api/v1/lifecycle.py#L159) |  |
| POST | `/api/v1/lifecycle/studies/{study_uid}/purge` | ADMIN / RADIOLOGIST | [`purge_one`](../../app/api/v1/lifecycle.py#L181) |  |
| POST | `/api/v1/lifecycle/studies/{study_uid}/restore` | Any user (bearer) | [`restore_one`](../../app/api/v1/lifecycle.py#L170) |  |

### `lossy_compression.py` — Lossy Compression Engine REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/compression/lossy/rules` | Any user (bearer) | [`list_lossy_rules`](../../app/api/v1/lossy_compression.py#L91) | List all lossy compression policy rules. |
| POST | `/api/v1/compression/lossy/rules` | Any user (bearer) | [`create_lossy_rule`](../../app/api/v1/lossy_compression.py#L75) | Create a new lossy compression policy rule. |
| DELETE | `/api/v1/compression/lossy/rules/{rule_id}` | Any user (bearer) | [`delete_lossy_rule`](../../app/api/v1/lossy_compression.py#L135) | Delete a lossy compression rule. |
| GET | `/api/v1/compression/lossy/rules/{rule_id}` | Any user (bearer) | [`get_lossy_rule`](../../app/api/v1/lossy_compression.py#L101) | Get details of a specific lossy compression rule. |
| PUT | `/api/v1/compression/lossy/rules/{rule_id}` | Any user (bearer) | [`update_lossy_rule`](../../app/api/v1/lossy_compression.py#L114) | Update a lossy compression rule. |
| POST | `/api/v1/compression/lossy/run` | Any user (bearer) | [`trigger_lossy_compression_run`](../../app/api/v1/lossy_compression.py#L149) | Trigger an on-demand lossy compression evaluation & transcoding run. |

### `metrics.py` — Prometheus /metrics endpoint (hand-rolled text exposition, zero new deps).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/metrics` | **None — public** | [`metrics`](../../app/api/v1/metrics.py#L88) | Prometheus text-format metrics (scraped every 15s by default). |

### `migration.py` — Migration REST API endpoints.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/migration/cmove` | Any user (bearer) | [`start_cmove_migration`](../../app/api/v1/migration.py#L81) | Trigger a batch C-MOVE migration job pulling studies from a remote DICOM Node. |
| POST | `/api/v1/migration/folder` | Any user (bearer) | [`start_folder_migration`](../../app/api/v1/migration.py#L56) | Trigger a background migration job scanning and importing a local folder. |
| GET | `/api/v1/migration/jobs` | Any user (bearer) | [`list_migration_jobs`](../../app/api/v1/migration.py#L110) | List all migration jobs. |
| GET | `/api/v1/migration/jobs/{job_id}` | Any user (bearer) | [`get_migration_job`](../../app/api/v1/migration.py#L121) | Get status & metrics for a specific migration job. |

### `mpps.py` — MPPS (Modality Performed Procedure Step) API — live exam state.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/mpps` | Any user (bearer) | [`list_performed`](../../app/api/v1/mpps.py#L57) | List performed procedure steps, newest first (optional status filter). |

### `nodes.py` — DICOM node registry management API — register AE destinations so C-MOVE

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/nodes` | Any user (bearer) | [`list_nodes`](../../app/api/v1/nodes.py#L89) |  |
| POST | `/api/v1/nodes` | Any user (bearer) + inline role check | [`create_node`](../../app/api/v1/nodes.py#L96) |  |
| DELETE | `/api/v1/nodes/{ae_title}` | Any user (bearer) + inline role check | [`delete_node`](../../app/api/v1/nodes.py#L148) |  |
| PUT | `/api/v1/nodes/{ae_title}` | Any user (bearer) + inline role check | [`update_node`](../../app/api/v1/nodes.py#L122) |  |
| POST | `/api/v1/nodes/{ae_title}/find` | ADMIN / RADIOLOGIST | [`find_on_node`](../../app/api/v1/nodes.py#L238) | C-FIND SCU trigger: query a remote node (Q/R information model) and |
| POST | `/api/v1/nodes/{ae_title}/ping` | Any user (bearer) | [`ping_node`](../../app/api/v1/nodes.py#L162) | C-ECHO (verification) against the registered node — checks reachability |
| POST | `/api/v1/nodes/{ae_title}/pull` | ADMIN / RADIOLOGIST | [`pull_from_node`](../../app/api/v1/nodes.py#L276) | C-GET SCU trigger: pull matching instances from a remote node and |
| POST | `/api/v1/nodes/{ae_title}/send` | ADMIN / RADIOLOGIST | [`send_to_node`](../../app/api/v1/nodes.py#L185) | C-STORE SCU trigger: forward matching instances to a registered node. |
| POST | `/api/v1/nodes/{ae_title}/web/query` | ADMIN / RADIOLOGIST | [`web_query_node`](../../app/api/v1/nodes.py#L346) | QIDO-RS query against a node's DICOMweb service (C-FIND's REST twin). |
| POST | `/api/v1/nodes/{ae_title}/web/retrieve` | ADMIN / RADIOLOGIST | [`web_retrieve_node`](../../app/api/v1/nodes.py#L380) | WADO-RS retrieve from a node's DICOMweb service (C-GET's REST twin). |
| POST | `/api/v1/nodes/{ae_title}/web/upload` | ADMIN / RADIOLOGIST | [`web_upload_node`](../../app/api/v1/nodes.py#L477) | STOW-RS upload — send matching local instances to a node's DICOMweb |

### `peer_review.py` — Peer Review / QA REST API — RADPEER quality assurance endpoints.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/peer-review` | Any user (bearer) | [`list_cases`](../../app/api/v1/peer_review.py#L84) | List peer review cases with optional status/reviewer filter. |
| POST | `/api/v1/peer-review` | Any user (bearer) | [`create_case`](../../app/api/v1/peer_review.py#L66) | Create a new peer review case for a study. |
| GET | `/api/v1/peer-review/summary` | Any user (bearer) | [`score_summary`](../../app/api/v1/peer_review.py#L96) | Get aggregate RADPEER score statistics. |
| GET | `/api/v1/peer-review/{case_id}` | Any user (bearer) | [`get_case`](../../app/api/v1/peer_review.py#L105) | Get a single peer review case by ID. |
| POST | `/api/v1/peer-review/{case_id}/assign` | Any user (bearer) | [`assign_reviewer`](../../app/api/v1/peer_review.py#L118) | Assign a reviewer to a peer review case. |
| POST | `/api/v1/peer-review/{case_id}/score` | Any user (bearer) | [`submit_score`](../../app/api/v1/peer_review.py#L135) | Submit a RADPEER score (1-4) for a peer review case. |

### `pix_pdq.py` — IHE PIX/PDQ REST API - Patient Identity Cross-referencing & Demographics Query.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/pix/assigning-authorities` | Any user (bearer) | [`list_assigning_authorities`](../../app/api/v1/pix_pdq.py#L143) | List all registered Assigning Authorities. |
| POST | `/api/v1/pix/assigning-authorities` | ADMIN | [`create_assigning_authority`](../../app/api/v1/pix_pdq.py#L110) | Register a new Assigning Authority (Admin only). |
| DELETE | `/api/v1/pix/assigning-authorities/{oid}` | ADMIN | [`delete_assigning_authority`](../../app/api/v1/pix_pdq.py#L186) | Delete an Assigning Authority (Admin only). |
| GET | `/api/v1/pix/assigning-authorities/{oid}` | Any user (bearer) | [`get_assigning_authority`](../../app/api/v1/pix_pdq.py#L163) | Get Assigning Authority details by OID. |
| POST | `/api/v1/pix/identities` | Any user (bearer) | [`create_patient_identity`](../../app/api/v1/pix_pdq.py#L205) | Create or update a patient identity mapping. |
| GET | `/api/v1/pix/patients/{patient_id}/identities` | Any user (bearer) | [`list_patient_identities`](../../app/api/v1/pix_pdq.py#L243) | Get all identities for a patient across all assigning authorities. |
| DELETE | `/api/v1/pix/patients/{patient_id}/identities/{aa_oid}` | ADMIN | [`delete_patient_identity`](../../app/api/v1/pix_pdq.py#L274) | Delete a patient identity mapping (Admin only). |
| GET | `/api/v1/pix/pdq/query` | Any user (bearer) | [`pdq_query`](../../app/api/v1/pix_pdq.py#L337) | PDQ Query: Find patient demographics across all assigning authorities. |
| GET | `/api/v1/pix/query` | Any user (bearer) | [`pix_query`](../../app/api/v1/pix_pdq.py#L293) | PIX Query: Find patient identifiers across all assigning authorities. |

### `prefetch.py` — Prefetching Engine REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/prefetch/jobs` | Any user (bearer) | [`list_prefetch_jobs`](../../app/api/v1/prefetch.py#L204) | List prefetch job execution logs. |
| GET | `/api/v1/prefetch/rules` | Any user (bearer) | [`list_prefetch_rules`](../../app/api/v1/prefetch.py#L123) | List all active prefetch rules. |
| POST | `/api/v1/prefetch/rules` | Any user (bearer) | [`create_prefetch_rule`](../../app/api/v1/prefetch.py#L106) | Create a new automated prefetching rule. |
| DELETE | `/api/v1/prefetch/rules/{rule_id}` | Any user (bearer) | [`delete_prefetch_rule`](../../app/api/v1/prefetch.py#L167) | Delete a prefetch rule. |
| GET | `/api/v1/prefetch/rules/{rule_id}` | Any user (bearer) | [`get_prefetch_rule`](../../app/api/v1/prefetch.py#L133) | Get details of a specific prefetch rule. |
| PUT | `/api/v1/prefetch/rules/{rule_id}` | Any user (bearer) | [`update_prefetch_rule`](../../app/api/v1/prefetch.py#L146) | Update a prefetch rule. |
| POST | `/api/v1/prefetch/trigger` | Any user (bearer) | [`trigger_prefetch`](../../app/api/v1/prefetch.py#L181) | Trigger manual prefetch evaluation for a patient. |

### `priors.py` — Prior Study Linking REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/priors/patient/{patient_id}` | Any user (bearer) | [`get_priors_for_patient`](../../app/api/v1/priors.py#L53) | Find all prior imaging studies for a patient. |
| GET | `/api/v1/priors/study/{study_uid}` | Any user (bearer) | [`get_priors_for_study`](../../app/api/v1/priors.py#L42) | Find relevant prior imaging studies for a given study. |

### `quota.py` — Storage Quota Management API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/quotas` | ADMIN | [`list_quotas`](../../app/api/v1/quota.py#L94) | List all quota rules with current usage. |
| POST | `/api/v1/quotas` | ADMIN | [`create_quota`](../../app/api/v1/quota.py#L109) | Create a new storage quota rule. |
| GET | `/api/v1/quotas/status` | ADMIN | [`quota_status`](../../app/api/v1/quota.py#L184) | Current quota status (usage vs limits) for monitoring dashboards. |
| GET | `/api/v1/quotas/status/backend/{backend_name}` | ADMIN | [`backend_quota_status`](../../app/api/v1/quota.py#L202) | Quota status for a specific storage backend. |
| GET | `/api/v1/quotas/status/tenant/{tenant_id}` | ADMIN | [`tenant_quota_status`](../../app/api/v1/quota.py#L194) | Quota status for a specific tenant. |
| DELETE | `/api/v1/quotas/{quota_id}` | ADMIN | [`delete_quota`](../../app/api/v1/quota.py#L173) |  |
| GET | `/api/v1/quotas/{quota_id}` | ADMIN | [`get_quota`](../../app/api/v1/quota.py#L146) |  |
| PUT | `/api/v1/quotas/{quota_id}` | ADMIN | [`update_quota`](../../app/api/v1/quota.py#L156) |  |

### `redis_panel.py` — Redis administration and cache control API — Settings → Redis tab.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/redis/configure` | ADMIN | [`configure_redis`](../../app/api/v1/redis_panel.py#L82) | Persist new Redis settings and immediately re-connect active pool. |
| POST | `/api/v1/redis/flush` | ADMIN | [`flush_cache`](../../app/api/v1/redis_panel.py#L125) | Flush cache keys according to selected target. |
| GET | `/api/v1/redis/info` | ADMIN | [`get_raw_info`](../../app/api/v1/redis_panel.py#L142) | Retrieve raw Redis server INFO sections. |
| DELETE | `/api/v1/redis/key` | ADMIN | [`delete_key`](../../app/api/v1/redis_panel.py#L118) | Delete a specific key from RAM and Redis. |
| GET | `/api/v1/redis/key` | **Optional — anonymous allowed** | [`get_key`](../../app/api/v1/redis_panel.py#L106) | View details and content preview of a specific key. |
| GET | `/api/v1/redis/keys` | **Optional — anonymous allowed** | [`list_keys`](../../app/api/v1/redis_panel.py#L96) | Search and list keys from Redis and Tier-1 RAM cache. |
| POST | `/api/v1/redis/ping` | **Optional — anonymous allowed** | [`ping`](../../app/api/v1/redis_panel.py#L136) | Measure real-time PING round-trip latency against Redis. |
| POST | `/api/v1/redis/server/install` | ADMIN | [`install_server`](../../app/api/v1/redis_panel.py#L180) | Download & set up portable Redis for Windows in .tools/redis/. |
| POST | `/api/v1/redis/server/restart` | ADMIN | [`restart_server`](../../app/api/v1/redis_panel.py#L171) | Restart Redis server. |
| POST | `/api/v1/redis/server/start` | ADMIN | [`start_server`](../../app/api/v1/redis_panel.py#L155) | Start Redis / Memurai server or Windows service. |
| POST | `/api/v1/redis/server/stop` | ADMIN | [`stop_server`](../../app/api/v1/redis_panel.py#L164) | Stop Redis / Memurai server or Windows service. |
| GET | `/api/v1/redis/status` | **Optional — anonymous allowed** | [`get_status`](../../app/api/v1/redis_panel.py#L64) | Retrieve full Redis metrics, memory usage, key stats, install state and L1 RAM status. |
| POST | `/api/v1/redis/test` | ADMIN | [`test_connection`](../../app/api/v1/redis_panel.py#L73) | Probe Redis server at specified host/port/auth without modifying state. |

### `replication.py` — DR replication admin API (roadmap #49).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/replication` | ADMIN | [`replication_status`](../../app/api/v1/replication.py#L28) |  |
| GET | `/api/v1/replication/records` | ADMIN | [`replication_records`](../../app/api/v1/replication.py#L33) |  |
| POST | `/api/v1/replication/studies/{study_uid}` | ADMIN | [`replicate_study`](../../app/api/v1/replication.py#L48) |  |
| POST | `/api/v1/replication/sweep` | ADMIN | [`replication_sweep`](../../app/api/v1/replication.py#L38) |  |

### `reports.py` — Radiology reports REST API (Phase E2 — HL7 ORU write-back to the EMR).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/reports` | Any user (bearer) | [`list_reports`](../../app/api/v1/reports.py#L81) |  |
| POST | `/api/v1/reports` | Any user (bearer) | [`create_report`](../../app/api/v1/reports.py#L63) | Draft a report for a study (one report per study). |
| GET | `/api/v1/reports/print/{study_instance_uid}` | **None — public** | [`print_report`](../../app/api/v1/reports.py#L228) | Generate and render a clean, professional printable HTML report. |
| POST | `/api/v1/reports/save` | **Optional — anonymous allowed** | [`save_report`](../../app/api/v1/reports.py#L174) | Create or update a diagnostic report (Save Draft / Finalize Report). |
| GET | `/api/v1/reports/study/{study_instance_uid}` | **None — public** | [`get_study_report`](../../app/api/v1/reports.py#L161) | Check if report exists for study UID; if new, returns pre-filled DICOM metadata. |
| GET | `/api/v1/reports/{report_id}` | Any user (bearer) | [`get_report`](../../app/api/v1/reports.py#L91) |  |
| PATCH | `/api/v1/reports/{report_id}` | Any user (bearer) | [`update_report`](../../app/api/v1/reports.py#L100) |  |
| POST | `/api/v1/reports/{report_id}/finalize` | ADMIN / RADIOLOGIST | [`finalize_report`](../../app/api/v1/reports.py#L118) | Lock the report FINALIZED and deliver ORU^R01 to the EMR (when enabled). |
| POST | `/api/v1/reports/{report_id}/resend` | ADMIN / RADIOLOGIST | [`resend_report`](../../app/api/v1/reports.py#L134) | Re-deliver ORU^R01 for a finalized report whose delivery failed. |

### `routing.py` — Auto-routing Rules REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/routing/evaluate/{study_uid}` | Any user (bearer) | [`evaluate_study_routing`](../../app/api/v1/routing.py#L157) | Manually evaluate auto-routing rules for a study and enqueue dispatch. |
| GET | `/api/v1/routing/queue` | Any user (bearer) | [`list_routing_queue`](../../app/api/v1/routing.py#L170) | List Smart Router send-queue items (optionally by status). |
| POST | `/api/v1/routing/queue/run` | ADMIN / RADIOLOGIST | [`run_routing_queue`](../../app/api/v1/routing.py#L195) | Trigger a Smart Router queue sweep immediately (background thread). |
| POST | `/api/v1/routing/queue/{queue_id}/retry` | ADMIN / RADIOLOGIST | [`retry_routing_queue_item`](../../app/api/v1/routing.py#L182) | Re-queue a FAILED Smart Router dispatch for immediate retry. |
| GET | `/api/v1/routing/rules` | Any user (bearer) | [`list_routing_rules`](../../app/api/v1/routing.py#L99) | List all auto-routing rules ordered by priority. |
| POST | `/api/v1/routing/rules` | Any user (bearer) | [`create_routing_rule`](../../app/api/v1/routing.py#L81) | Create a new auto-routing rule. |
| DELETE | `/api/v1/routing/rules/{rule_id}` | Any user (bearer) | [`delete_routing_rule`](../../app/api/v1/routing.py#L143) | Delete an auto-routing rule. |
| GET | `/api/v1/routing/rules/{rule_id}` | Any user (bearer) | [`get_routing_rule`](../../app/api/v1/routing.py#L109) | Get details of a specific routing rule. |
| PUT | `/api/v1/routing/rules/{rule_id}` | Any user (bearer) | [`update_routing_rule`](../../app/api/v1/routing.py#L122) | Update an auto-routing rule. |

### `scripts.py` — Custom server scripts API (roadmap #43) — manage runtime Python scripts.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/scripts` | ADMIN | [`list_scripts`](../../app/api/v1/scripts.py#L35) |  |
| POST | `/api/v1/scripts/reload` | ADMIN | [`reload_scripts`](../../app/api/v1/scripts.py#L42) |  |
| PUT | `/api/v1/scripts/{name}` | ADMIN | [`create_script`](../../app/api/v1/scripts.py#L75) | Create a script file from the request body (the UI sends a template). |
| POST | `/api/v1/scripts/{name}/run` | ADMIN | [`run_script`](../../app/api/v1/scripts.py#L58) |  |
| GET | `/api/v1/scripts/{name}/source` | ADMIN | [`script_source`](../../app/api/v1/scripts.py#L67) |  |
| POST | `/api/v1/scripts/{name}/toggle` | ADMIN | [`toggle_script`](../../app/api/v1/scripts.py#L48) |  |

### `settings.py` — Organization Settings & Report Templates REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/settings/organization` | **None — public** | [`get_org_settings`](../../app/api/v1/settings.py#L50) | Fetch current organization settings. |
| POST | `/api/v1/settings/organization` | **Optional — anonymous allowed** | [`update_org_settings`](../../app/api/v1/settings.py#L74) | Update organization settings and handle logo file upload. |
| GET | `/api/v1/templates` | **None — public** | [`list_templates`](../../app/api/v1/settings.py#L119) | List report templates, optionally filtered by modality. |
| POST | `/api/v1/templates` | **Optional — anonymous allowed** | [`create_template`](../../app/api/v1/settings.py#L138) | Create a new report template. |
| DELETE | `/api/v1/templates/{template_id}` | **Optional — anonymous allowed** | [`delete_template`](../../app/api/v1/settings.py#L197) |  |
| GET | `/api/v1/templates/{template_id}` | **None — public** | [`get_template`](../../app/api/v1/settings.py#L158) |  |
| PATCH | `/api/v1/templates/{template_id}` | **Optional — anonymous allowed** | [`update_template`](../../app/api/v1/settings.py#L173) |  |

### `share.py` — Patient share links (Zero-CD): create/list/delete temporary links + QR.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/share` | Any user (bearer) | [`create_share_endpoint`](../../app/api/v1/share.py#L28) |  |
| DELETE | `/api/v1/share/{token}` | ADMIN / RADIOLOGIST | [`delete_share`](../../app/api/v1/share.py#L115) |  |
| GET | `/api/v1/share/{token}` | **None — public** | [`get_share_info`](../../app/api/v1/share.py#L54) |  |
| GET | `/api/v1/share/{token}/qr.png` | **None — public** | [`share_qr`](../../app/api/v1/share.py#L104) |  |

### `split_merge.py` — Study Split and Merge REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| POST | `/api/v1/studies/merge` | Any user (bearer) | [`merge_studies`](../../app/api/v1/split_merge.py#L77) | Merge source studies into a target study. |
| POST | `/api/v1/studies/split` | Any user (bearer) | [`split_study`](../../app/api/v1/split_merge.py#L41) | Split specific series from a study into a new study. |

### `storage.py` — Multi-storage admin API — configure named storage backends (local + S3/MinIO).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/storage/backends` | ADMIN | [`list_backends`](../../app/api/v1/storage.py#L116) |  |
| POST | `/api/v1/storage/backends` | ADMIN | [`create_backend`](../../app/api/v1/storage.py#L137) |  |
| DELETE | `/api/v1/storage/backends/{name}` | ADMIN | [`delete_backend`](../../app/api/v1/storage.py#L243) |  |
| PATCH | `/api/v1/storage/backends/{name}` | ADMIN | [`patch_backend`](../../app/api/v1/storage.py#L177) |  |
| POST | `/api/v1/storage/backends/{name}/test` | ADMIN | [`test_backend`](../../app/api/v1/storage.py#L209) |  |
| POST | `/api/v1/storage/studies/{study_uid}/move` | ADMIN | [`move_study`](../../app/api/v1/storage.py#L268) | Relocate a study's objects (instances + thumbnails) to another backend. |

### `tenants.py` — VNA (Vendor Neutral Archive) multi-tenancy REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/tenants` | ADMIN / RADIOLOGIST | [`list_tenants`](../../app/api/v1/tenants.py#L93) |  |
| POST | `/api/v1/tenants` | ADMIN | [`create_tenant`](../../app/api/v1/tenants.py#L100) |  |
| GET | `/api/v1/tenants/context` | Any user (bearer) | [`tenant_context`](../../app/api/v1/tenants.py#L259) | The caller's tenant scope — surfaced by the UI to show isolation state. |
| DELETE | `/api/v1/tenants/{tenant_id}` | ADMIN | [`delete_tenant`](../../app/api/v1/tenants.py#L154) |  |
| PATCH | `/api/v1/tenants/{tenant_id}` | ADMIN | [`update_tenant`](../../app/api/v1/tenants.py#L128) |  |
| GET | `/api/v1/tenants/{tenant_id}/aes` | ADMIN / RADIOLOGIST | [`list_ae_titles`](../../app/api/v1/tenants.py#L174) |  |
| POST | `/api/v1/tenants/{tenant_id}/aes` | ADMIN | [`map_ae_title`](../../app/api/v1/tenants.py#L188) |  |
| DELETE | `/api/v1/tenants/{tenant_id}/aes/{ae_title}` | ADMIN | [`unmap_ae_title`](../../app/api/v1/tenants.py#L209) |  |
| POST | `/api/v1/tenants/{tenant_id}/users` | ADMIN | [`assign_user`](../../app/api/v1/tenants.py#L228) | Assign a user to this tenant — their REST/QIDO browsing becomes scoped. |
| DELETE | `/api/v1/tenants/{tenant_id}/users/{username}` | ADMIN | [`unassign_user`](../../app/api/v1/tenants.py#L244) | Remove a user's tenant assignment (back to the legacy unscoped scope). |

### `users.py` — User management — Admin console → Users tab.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/users` | ADMIN | [`list_users`](../../app/api/v1/users.py#L86) |  |
| POST | `/api/v1/users` | ADMIN | [`create_user`](../../app/api/v1/users.py#L94) |  |
| GET | `/api/v1/users/auth-audit` | ADMIN | [`list_auth_audit`](../../app/api/v1/users.py#L217) | Auth audit trail (admin console → Auth Audit tab). |
| GET | `/api/v1/users/locked` | ADMIN | [`list_locked`](../../app/api/v1/users.py#L254) | Accounts currently locked out by the brute-force guard. |
| GET | `/api/v1/users/sessions` | ADMIN | [`list_sessions`](../../app/api/v1/users.py#L119) | List active login sessions (admin console → Sessions tab). |
| POST | `/api/v1/users/sessions/revoke-user/{user_id}` | ADMIN | [`revoke_user_sessions`](../../app/api/v1/users.py#L182) | Revoke every active session of one user (per-user sign-out). |
| POST | `/api/v1/users/sessions/{session_id}/revoke` | ADMIN | [`revoke_session`](../../app/api/v1/users.py#L154) | Revoke one login session (per-session sign-out). |
| DELETE | `/api/v1/users/{user_id}` | ADMIN | [`delete_user`](../../app/api/v1/users.py#L357) |  |
| PATCH | `/api/v1/users/{user_id}` | ADMIN | [`patch_user`](../../app/api/v1/users.py#L310) |  |
| POST | `/api/v1/users/{user_id}/reset-lockout` | ADMIN | [`reset_lockout`](../../app/api/v1/users.py#L284) | Unlock an account locked by the brute-force guard (per-account reset). |
| POST | `/api/v1/users/{user_id}/reset-password` | ADMIN | [`reset_password`](../../app/api/v1/users.py#L346) |  |

### `volume.py` — 2D MPR backend prep (roadmap #26) — volume metadata + raw volume streaming.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/volume/{series_uid}/data` | **Optional — anonymous allowed** | [`volume_data_endpoint`](../../app/api/v1/volume.py#L81) | Stream the raw voxel block of a series (optionally a sub-range). |
| GET | `/api/v1/volume/{series_uid}/info` | **Optional — anonymous allowed** | [`volume_info_endpoint`](../../app/api/v1/volume.py#L66) | Volume geometry/encoding metadata for MPR reconstruction. |
| GET | `/api/v1/volume/{series_uid}/mip` | **Optional — anonymous allowed** | [`volume_mip_endpoint`](../../app/api/v1/volume.py#L235) | 3D VRT / MIP (roadmap #27) — server-side maximum-intensity projection. |
| GET | `/api/v1/volume/{series_uid}/mpr/slice` | **Optional — anonymous allowed** | [`volume_mpr_slice_endpoint`](../../app/api/v1/volume.py#L340) | Server-side MPR slice reslicing — thin-client fallback for WebGL MPR. |

### `worklist.py` — Modality Worklist management API — schedule and manage the scheduled

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/worklist` | Any user (bearer) | [`list_steps`](../../app/api/v1/worklist.py#L102) |  |
| POST | `/api/v1/worklist` | Any user (bearer) | [`create_step`](../../app/api/v1/worklist.py#L132) |  |
| DELETE | `/api/v1/worklist/{step_id}` | Any user (bearer) + inline role check | [`delete_step`](../../app/api/v1/worklist.py#L194) |  |
| PATCH | `/api/v1/worklist/{step_id}` | Any user (bearer) | [`update_step`](../../app/api/v1/worklist.py#L161) |  |

### `worklist_tasks.py` — Radiologist reading worklist REST API (roadmap #30).

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/worklist-tasks` | Any user (bearer) | [`list_tasks`](../../app/api/v1/worklist_tasks.py#L54) | List studies in a given reader state. |
| GET | `/api/v1/worklist-tasks/counts` | Any user (bearer) | [`task_counts`](../../app/api/v1/worklist_tasks.py#L47) | Per-state reader task counts (VNA-scoped). |
| POST | `/api/v1/worklist-tasks/{study_uid}/assign` | Any user (bearer) | [`assign`](../../app/api/v1/worklist_tasks.py#L82) | Assign (or clear) the radiologist reading this study. |
| POST | `/api/v1/worklist-tasks/{study_uid}/read` | Any user (bearer) | [`mark_read`](../../app/api/v1/worklist_tasks.py#L98) | Mark a study as read by the current user. |
| POST | `/api/v1/worklist-tasks/{study_uid}/stat` | Any user (bearer) | [`set_stat`](../../app/api/v1/worklist_tasks.py#L70) | Manually flag / unflag a study as STAT (urgent). |
| POST | `/api/v1/worklist-tasks/{study_uid}/unread` | Any user (bearer) | [`mark_unread`](../../app/api/v1/worklist_tasks.py#L109) | Move a study back into the UNREAD pile. |

### `xdsi.py` — Cross-Enterprise Document Sharing for Imaging (XDS-I.b) REST API.

| Method | Path | Auth | Handler | Summary |
|---|---|---|---|---|
| GET | `/api/v1/xdsi/documents` | Any user (bearer) | [`query_documents`](../../app/api/v1/xdsi.py#L104) | FindDocuments [ITI-18] ebXML Registry Query. |
| GET | `/api/v1/xdsi/documents/{entry_uuid}` | Any user (bearer) | [`retrieve_document`](../../app/api/v1/xdsi.py#L119) | RetrieveDocumentSet [ITI-43] Repository Retrieve. |
| GET | `/api/v1/xdsi/documents/{entry_uuid}/ebxml` | Any user (bearer) | [`get_ebxml_metadata`](../../app/api/v1/xdsi.py#L133) | Download ebXML SubmitObjectsRequest metadata XML string. |
| POST | `/api/v1/xdsi/register` | Any user (bearer) | [`register_document`](../../app/api/v1/xdsi.py#L67) | Provide and Register Document Set-b [ITI-41 / RAD-68]. |

