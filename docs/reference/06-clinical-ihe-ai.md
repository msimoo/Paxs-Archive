# Clinical workflow: reports, worklists, MPPS, critical results, peer review, dose, IAN, FHIR, PIX/PDQ, XDS-I, AI

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/05-clinical-ldap-frontend.md](5a32aa4/05-clinical-ldap-frontend.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

No code was executed and no packages were installed.

**Evidence tags used below**
- `[V]` verified by reading the cited code (or by a grep/`find_spec` check noted in the text)
- `[H]` high-confidence inference from library semantics or standards. Not executed, because SQLAlchemy and pynetdicom are not installed in this environment.
- `[U]` unverified. Listed in section 7.

**Severity legend:** 🔴 blocking (security, data loss, or patient-safety defect; fix before release) · 🟡 important · 🟢 nit

---

## 0. Headline findings

1. 🔴 **The checkout cannot import.** `app/services/storage/` (the backend package that `storage_service.py` imports) is excluded by `.gitignore:9` (`storage/`) and is absent from git and the working tree. `storage_service` is imported by `ai_service.py` (used by `ai.py`), `xdsi_service.py` (used by `xdsi.py`), and `dicomweb.py`, all of which `main.py` mounts. `[V]` (see F-08)
2. 🔴 **Anonymous report edits and finalization.** `POST /api/v1/reports/save` has no authentication. It can overwrite FINALIZED reports and finalize drafts, bypassing the ADMIN/RADIOLOGIST gate, and it takes the signer's name from the request body. `[V]` (F-01)
3. 🔴 **Unauthenticated FHIR PHI, enabled by default.** All `/fhir/*` routes lack auth, and `FHIR_ENABLED` defaults to `True`. `GET /fhir/Patient?name=%25` enumerates patients. `[V]` (F-03)
4. 🔴 **PIX A40 merge deletes data.** `handle_adt_feed_patient_merge` reassigns studies and identities, then deletes the source patient. ORM cascade on the still-populated collections deletes them instead of moving them. `[H]` (F-07)
5. 🔴 **The "AI" is a Hounsfield threshold with canned conclusions.** Fixed confidences, RadLex/SNOMED codes, and "No acute intracranial hemorrhage ... within normal limits" are returned when a threshold count is not met. `[V]` for the code; clinical validity `[H]` (F-09)

Other 🔴 items: reading-worklist actions are not tenant-scoped and have no role gate (F-05); peer-review scoring has no role gate and COMPLETED cases can be reopened (F-06); the print page interpolates patient and report text unescaped (F-04); report PHI endpoints are unauthenticated (F-02); and cross-scope anonymous writes in `settings.py` feed the print-page XSS (X-01).

---

## 1. Files covered

| Path | Lines | Purpose |
|---|---|---|
| app/api/v1/reports.py | 433 | Radiology report CRUD, finalize/resend (ORU), `/save` draft, `/print` HTML |
| app/services/report_service.py | 506 | Report lifecycle, HL7 ORU^R01 builder, MLLP send, org settings, templates, study metadata |
| app/api/v1/worklist.py | 204 | MWL (Scheduled Procedure Step) REST CRUD |
| app/services/worklist_service.py | 215 | MWL C-FIND query building, response serialization, C-STORE step matcher |
| app/api/v1/worklist_tasks.py | 116 | Reader task lists (UNREAD/DICTATED/READ/STAT) and actions |
| app/services/reading_worklist_service.py | 336 | Reader state derivation, counts, STAT/assign/read actions |
| app/api/v1/mpps.py | 73 | MPPS read-only list |
| app/services/mpps_service.py | 234 | MPPS N-CREATE/N-SET handlers (DIMSE, sync) |
| app/api/v1/critical_results.py | 157 | Critical result create/ack/query |
| app/services/critical_results_service.py | 171 | Critical result lifecycle, audit, webhook/script fire |
| app/api/v1/peer_review.py | 148 | RADPEER peer-review case API |
| app/services/peer_review_service.py | 175 | Peer-review create/assign/score, summary |
| app/api/v1/clinical_protocols.py | 155 | Clinical protocol library CRUD and matching |
| app/services/clinical_protocol_service.py | 178 | Protocol CRUD, best-match scoring |
| app/api/v1/dose.py | 94 | RDSR dose query API |
| app/services/dose_service.py | 219 | RDSR parsing, indexing, cumulative dose |
| app/api/v1/ian.py | 104 | IAN notify and log API |
| app/services/ian_service.py | 237 | IAN N-CREATE builder and DIMSE send |
| app/api/v1/fhir.py | 248 | FHIR R4 read gateway (Patient, ImagingStudy) |
| app/api/v1/pix_pdq.py | 389 | PIX/PDQ REST: assigning authorities, identities, queries |
| app/services/pix_pdq_service.py | 447 | PIX/PDQ logic and HL7 ADT PIX feed (A04/A08/A40) |
| app/api/v1/xdsi.py | 153 | XDS-I.b register, query, retrieve, ebXML |
| app/services/xdsi_service.py | 184 | XDS-I.b document registry and ebXML generator |
| app/api/v1/ai.py | 163 | Clinical AI: models, infer, mask, SR |
| app/services/ai_service.py | 460 | HU-threshold "inference", mask cache, DICOM SR writer, dead HL7 dispatcher |
| app/api/v1/deps.py (peek, full read) | 120 | Auth dependencies (bearer, optional, cookie-capable, role gates) |

Peeks (only to resolve names or confirm behavior): `app/db/models.py` (targeted greps and reads of relevant classes), `app/core/config.py` (defaults), `app/db/session.py` (`get_db` auto-commit, autoflush/expire settings), `app/main.py` (router mounting, middleware, CSP), `app/dicom/hl7.py:439-461` (PIX dispatch), `app/services/tenant_service.py:83-95`, `app/services/audit_service.py:16-24`, `app/services/storage_service.py` (imports and key helpers), `app/services/volume_service.py` (HU rescale, lines 537-570), `app/core/licensing/__init__.py` and `deps.py`, `app/core/security.py:31-39`, and `app/api/v1/settings.py:73-207` (write routes that feed F-04).

Totals: 25 in-scope files, about 5,800 lines, plus `deps.py`, read in full.

---

## 2. API endpoint reference

Auth legend (from `app/api/v1/deps.py`):
- **bearer** = `get_current_user` (lines 46-63). Requires an `Authorization: Bearer` token. Rejects inactive users.
- **optional** = `get_optional_user` (lines 66-86). Never rejects. Returns `None` for anonymous callers. Used as an auth gate only when the handler checks the result.
- **cookie-capable** = `get_current_user_any` / `require_destructive_role` (108-114) / `require_admin` (117-120). Accept the bearer token or the HttpOnly session cookie.
- Role constants: `DESTRUCTIVE_ROLES = ("ADMIN", "RADIOLOGIST")` (deps.py:105). `WRITE_ROLES = ("ADMIN", "TECHNOLOGIST")` (worklist.py:29).

Mounting: all routers are included in `app/main.py:507-553`. The FHIR router is included only when `settings.FHIR_ENABLED` (`main.py:554-556`). `config.py:187` defaults that to `True`.

### 2.1 Reports (`/api/v1/reports`, reports.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 1 | POST | /api/v1/reports | bearer (any role) | ReportIn {study_instance_uid, clinical_history?, findings?, impression?, radiologist?} | 201 report | reports.py:62-77 |
| 2 | GET | /api/v1/reports | bearer | status?, study_uid? | list | reports.py:80-87 |
| 3 | GET | /api/v1/reports/{report_id} | bearer | int | report | reports.py:90-96 |
| 4 | PATCH | /api/v1/reports/{report_id} | bearer (any role, not author-checked) | ReportPatch {clinical_history?, findings?, impression?, radiologist?} | report | reports.py:99-114 |
| 5 | POST | /api/v1/reports/{report_id}/finalize | cookie-capable, ADMIN or RADIOLOGIST | none | {ok, report_id, status, delivery} | reports.py:117-130 |
| 6 | POST | /api/v1/reports/{report_id}/resend | cookie-capable, ADMIN or RADIOLOGIST | none | {ok, report_id, delivery} | reports.py:133-142 |
| 7 | GET | /api/v1/reports/study/{study_instance_uid} | **NONE** | path | report + DICOM metadata (PHI) | reports.py:160-167 |
| 8 | POST | /api/v1/reports/save | **optional (never enforced)** | SaveReportIn {study_instance_uid, clinical_history?, findings?, impression?, radiologist?, status="DRAFT"} | report | reports.py:173-224 |
| 9 | GET | /api/v1/reports/print/{study_instance_uid} | **NONE** | path | text/html printable report (PHI) | reports.py:227-432 (handler 227-236, HTML 247-431) |

### 2.2 Modality worklist (`/api/v1/worklist`, worklist.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 10 | GET | /api/v1/worklist | bearer | status?, station?, modality?, search? | list of steps with patient | worklist.py:101-128 |
| 11 | POST | /api/v1/worklist | bearer + ADMIN/TECHNOLOGIST (`_require_write`) | WorklistIn | 201 step | worklist.py:131-157 |
| 12 | PATCH | /api/v1/worklist/{step_id} | bearer + ADMIN/TECHNOLOGIST | WorklistUpdate {status?, scheduled_station_aet?, start_date?, start_time?, modality?} | step | worklist.py:160-190 |
| 13 | DELETE | /api/v1/worklist/{step_id} | bearer + ADMIN only (inline check) | none | 204 | worklist.py:193-204 |

### 2.3 Reader task list (`/api/v1/worklist-tasks`, worklist_tasks.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 14 | GET | /api/v1/worklist-tasks/counts | bearer (tenant-scoped counts) | none | per-state counts | worklist_tasks.py:46-50 |
| 15 | GET | /api/v1/worklist-tasks | bearer (tenant-scoped) | state (UNREAD\|DICTATED\|READ\|STAT), radiologist?, page>=1, limit 1-100 | paged items | worklist_tasks.py:53-66 |
| 16 | POST | /api/v1/worklist-tasks/{study_uid}/stat | bearer, **no role, no tenant check** | {on: bool} | {ok, study_uid, stat} | worklist_tasks.py:69-78 |
| 17 | POST | /api/v1/worklist-tasks/{study_uid}/assign | bearer, **no role, no tenant check** | {radiologist: str \| null (max 128)} | {ok, study_uid, assigned_radiologist} | worklist_tasks.py:81-94 |
| 18 | POST | /api/v1/worklist-tasks/{study_uid}/read | bearer, **no tenant check** | none | {ok, study_uid} | worklist_tasks.py:97-105 |
| 19 | POST | /api/v1/worklist-tasks/{study_uid}/unread | bearer, **no tenant check** | none | {ok, study_uid} | worklist_tasks.py:108-116 |

### 2.4 MPPS (`/api/v1/mpps`, mpps.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 20 | GET | /api/v1/mpps | bearer | status? (not validated) | last 100 performed steps, with patient PHI | mpps.py:56-73 |

Writes happen only through the DICOM N-CREATE/N-SET SCP (`app/dicom/receiver.py`, out of scope). No HTTP write path exists.

### 2.5 Critical results (`/api/v1/critical-results`, critical_results.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 21 | POST | /api/v1/critical-results | bearer, any role | CreateCriticalResultRequest {study_instance_uid, description, finding_level="CRITICAL_RED" (not validated), series?, sop?, patient_id?, communicated_to?, communication_channel?} | 201 | critical_results.py:65-92 |
| 22 | POST | /api/v1/critical-results/{result_id}/acknowledge | bearer, **any role** | {notes?} | 200 record | critical_results.py:95-116 |
| 23 | GET | /api/v1/critical-results | bearer, not tenant-scoped | status?, finding_level?, patient_id? | list (max 100) | critical_results.py:119-131 |
| 24 | GET | /api/v1/critical-results/study/{study_uid} | bearer | path | list | critical_results.py:134-144 |
| 25 | GET | /api/v1/critical-results/{result_id} | bearer | int | record | critical_results.py:147-157 |

### 2.6 Peer review (`/api/v1/peer-review`, peer_review.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 26 | POST | /api/v1/peer-review | bearer, **no role gate** | CreateCaseRequest {study_instance_uid, original_reader (client-supplied), patient_id?, modality?, body_part_examined?} | 201 case | peer_review.py:65-80 |
| 27 | GET | /api/v1/peer-review | bearer | status?, reviewer? | list (max 100) | peer_review.py:83-92 |
| 28 | GET | /api/v1/peer-review/summary | bearer | none | {total_completed, avg_score, significant_discrepancies} | peer_review.py:95-101 |
| 29 | GET | /api/v1/peer-review/{case_id} | bearer | int | case | peer_review.py:104-114 |
| 30 | POST | /api/v1/peer-review/{case_id}/assign | bearer, **no role gate, no status guard** | {reviewer: str} | case | peer_review.py:117-131 |
| 31 | POST | /api/v1/peer-review/{case_id}/score | bearer, **no role gate, no assignment check** | {score: 1-4, comments?} | case | peer_review.py:134-148 |

### 2.7 Clinical protocols (`/api/v1/clinical-protocols`, clinical_protocols.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 32 | POST | /api/v1/clinical-protocols | cookie-capable, ADMIN or RADIOLOGIST | ProtocolRequest | 201 | clinical_protocols.py:79-86 |
| 33 | GET | /api/v1/clinical-protocols | bearer | none | list ordered by priority | clinical_protocols.py:89-93 |
| 34 | GET | /api/v1/clinical-protocols/match | bearer | study_description?, modality?, body_part? | {match} | clinical_protocols.py:96-109 |
| 35 | GET | /api/v1/clinical-protocols/suggest | bearer | same + limit 1-50 | {items} | clinical_protocols.py:112-124 |
| 36 | GET | /api/v1/clinical-protocols/{protocol_id} | bearer | int | protocol | clinical_protocols.py:127-133 |
| 37 | PATCH | /api/v1/clinical-protocols/{protocol_id} | cookie-capable, ADMIN or RADIOLOGIST | ProtocolUpdate (exclude_unset) | protocol | clinical_protocols.py:136-146 |
| 38 | DELETE | /api/v1/clinical-protocols/{protocol_id} | cookie-capable, ADMIN or RADIOLOGIST | none | {message} | clinical_protocols.py:149-155 |

### 2.8 Dose / RDSR (`/api/v1/dose`, dose.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 39 | GET | /api/v1/dose/study/{study_uid} | bearer, not tenant-scoped | path | list of RDSR rows | dose.py:51-59 |
| 40 | GET | /api/v1/dose/patient/{patient_id} | bearer, not tenant-scoped | path | cumulative totals | dose.py:62-70 |
| 41 | GET | /api/v1/dose/analytics | bearer, archive-wide | none | aggregates | dose.py:73-80 |
| 42 | GET | /api/v1/dose/report/{sop_uid} | bearer | path | RDSR row incl. server `file_path` | dose.py:83-94 |

### 2.9 IAN (`/api/v1/ian`, ian.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 43 | POST | /api/v1/ian/notify | bearer, **any role**; sends outbound DIMSE | TriggerIANRequest {study_instance_uid, target_ae_title?} | {message, notifications[]} | ian.py:53-76 |
| 44 | GET | /api/v1/ian/notifications | bearer | study_instance_uid?, status? | log (max 100) | ian.py:79-90 |
| 45 | GET | /api/v1/ian/notifications/{notif_id} | bearer | int | record | ian.py:93-104 |

### 2.10 FHIR R4 (`/fhir`, fhir.py; mounted when `FHIR_ENABLED`, default True)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 46 | GET | /fhir/metadata | **NONE** | none | CapabilityStatement | fhir.py:103-121 |
| 47 | GET | /fhir/Patient | **NONE** | _id?, identifier?, name? (LIKE %name%), _count 1-500 | searchset Bundle (PHI) | fhir.py:124-155 |
| 48 | GET | /fhir/Patient/{patient_id} | **NONE** | numeric internal id | Patient (PHI) | fhir.py:158-167 |
| 49 | GET | /fhir/ImagingStudy | **NONE** | patient? (MRN or Patient/{id}), accession?, _count | searchset Bundle | fhir.py:170-231 |
| 50 | GET | /fhir/ImagingStudy/{study_uid} | **NONE** | path | ImagingStudy | fhir.py:234-248 |

### 2.11 PIX/PDQ (`/api/v1/pix`, pix_pdq.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 51 | POST | /api/v1/pix/assigning-authorities | cookie-capable, ADMIN | AssigningAuthorityCreate | 201 AA | pix_pdq.py:109-139 |
| 52 | GET | /api/v1/pix/assigning-authorities | bearer | none | list | pix_pdq.py:142-159 |
| 53 | GET | /api/v1/pix/assigning-authorities/{oid} | bearer | oid | AA | pix_pdq.py:162-182 |
| 54 | DELETE | /api/v1/pix/assigning-authorities/{oid} | cookie-capable, ADMIN | oid | 204 | pix_pdq.py:185-200 |
| 55 | POST | /api/v1/pix/identities | bearer, **any role** | PatientIdentityCreate {patient_id:int, assigning_authority_oid, identifier, id_type="MR", is_primary} | 201 identity | pix_pdq.py:204-239 |
| 56 | GET | /api/v1/pix/patients/{patient_id}/identities | bearer | int | list | pix_pdq.py:242-270 |
| 57 | DELETE | /api/v1/pix/patients/{patient_id}/identities/{aa_oid} | cookie-capable, ADMIN | path | 204 | pix_pdq.py:273-288 |
| 58 | GET | /api/v1/pix/query | bearer | (AA oid + identifier) OR (patient_name / birth_date / sex); else 422 | identity list | pix_pdq.py:292-332 |
| 59 | GET | /api/v1/pix/pdq/query | bearer | same, plus optional AA filter | demographics (name, DOB, sex, phone, identities) | pix_pdq.py:336-389 |

All PIX/PDQ routes require authentication. Verified: none is unauthenticated.

### 2.12 XDS-I.b (`/api/v1/xdsi`, xdsi.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 60 | POST | /api/v1/xdsi/register | bearer, any role | RegisterDocumentRequest {patient_id, document_title, content_base64?, content_text?, mime_type (free text, default application/dicom), study_instance_uid?, class_code, type_code, format_code} | 201 document | xdsi.py:66-100 |
| 61 | GET | /api/v1/xdsi/documents | bearer, not tenant-scoped | patient_id?, class_code?, status="APPROVED" | list (max 100) | xdsi.py:103-115 |
| 62 | GET | /api/v1/xdsi/documents/{entry_uuid} | bearer | entry_uuid or unique_id | raw bytes, Content-Type = stored `mime_type` | xdsi.py:118-129 |
| 63 | GET | /api/v1/xdsi/documents/{entry_uuid}/ebxml | bearer | same | text/xml SubmitObjectsRequest | xdsi.py:132-153 |

### 2.13 Clinical AI (`/api/v1/ai`, ai.py)

| # | Method | Full path | Auth | Input | Output | Handler |
|---|---|---|---|---|---|---|
| 64 | GET | /api/v1/ai/models | bearer | none | model list with per-feature `licensed` flags | ai.py:34-87 |
| 65 | POST | /api/v1/ai/infer | bearer + `lic.is_active` only (no per-model feature check) | InferenceRequest {series_instance_uid, model_type (free text), confidence_threshold 0.1-0.99 (unused)} | {success, data} | ai.py:90-115 |
| 66 | GET | /api/v1/ai/mask/{series_instance_uid} | bearer (no license check) | path | application/octet-stream uint8 mask | ai.py:118-135 |
| 67 | POST | /api/v1/ai/sr | bearer + `lic.is_active` only | GenerateSRRequest {study_instance_uid, series_instance_uid, findings[], summary} | {success, sr_instance} | ai.py:138-163 |

Total in scope: 67 routes.

### 2.14 Write-authorization matrix (in-scope writes)

| Operation | Who can do it today | Expected (per code comments or docstrings) |
|---|---|---|
| Create/update/finalize report draft | Anyone with a token (create, PATCH). **Anonymous** via `/save`. | Radiologist; finalize = ADMIN/RADIOLOGIST (reports.py:5-6) |
| Finalize / resend report | ADMIN, RADIOLOGIST (also via `/save` status=FINALIZED, anonymous) | Same |
| Set STAT, assign, read, unread a study | Any authenticated user, any tenant | Tenant-scoped (reading_worklist_service.py:16-18) |
| Create/ack critical result | Any authenticated user | Recipient / physician (not specified in code) |
| Create/assign/score peer review | Any authenticated user | RADPEER QA reviewer (not specified in code) |
| Create/update/delete clinical protocol | ADMIN, RADIOLOGIST | Library "curated via the Admin → Protocols panel" (clinical_protocol_service.py:6-9) |
| PIX identity create/update | Any authenticated user | Feed owner / admin (not specified) |
| PIX identity delete, AA create/delete | ADMIN | ADMIN (pix_pdq.py:17) |
| Register XDS document | Any authenticated user | Not specified |
| Trigger IAN notification | Any authenticated user | Not specified |
| AI infer / SR write | Any authenticated user with an active license | Per-model license (ai.py:44-80, unused at check time) |

---

## 3. Service reference

### 3.1 report_service.py (report lifecycle, ORU, org settings, templates, study metadata)

**Purpose:** DRAFT → FINALIZED report lifecycle, HL7 v2.3 ORU^R01 build, MLLP send to the EMR, org settings, report templates, and the study metadata used by the print page. Sync and async halves: CRUD is async (`AsyncSession`); finalize and resend are sync on `SyncSessionLocal`, run through `asyncio.to_thread`.

**Key functions**
- `create_report(session, data: dict, performed_by: str) -> RadiologyReport` (57-92). Raises `ValueError("Study not found")` or `ValueError("A report already exists for this study")`.
- `get_report(session, report_id) -> RadiologyReport | None` (95-96)
- `list_reports(session, status=None, study_uid=None) -> list` (99-106)
- `update_report(session, report, data: dict) -> RadiologyReport` (109-117). DRAFT only (111-112).
- `build_oru_message(report, study, patient=None) -> str` (134-216)
- `send_oru_to_emr(message, host=None, port=None) -> dict` (222-257). Uses `settings.HL7_EMR_HOST/PORT`, so no user-controlled destination (no SSRF).
- `finalize_report(report_id: int, performed_by: str="report") -> dict` (282-312). **Sync.** Note: the call in `ai_service.py:453` uses a different signature (see F-34).
- `resend_report(report_id: int) -> dict` (315-328)
- `get_or_create_org_settings(session)` (337-354), `update_org_settings(session, data, logo_path=None)` (357-366)
- `list_report_templates(session, modality=None)` (369-403, auto-seeds 3 templates when empty), `create_report_template` (406-417), `get_report_template` (420-421), `update_report_template` (424-435), `delete_report_template` (438-444)
- `get_study_report_metadata(session, study_uid) -> dict` (447-505). Raises `ValueError("Study not found")`.

**Models touched:** `RadiologyReport` (`reports`), `Study`, `Patient`, `Series`, `OrganizationSettings`, `ReportTemplate`.

**State machine (RadiologyReport.status):**
- DRAFT → FINALIZED: `finalize_report` (289-292), `save_report` FINALIZED or SIGNED (reports.py:199-201, 217-220). "SIGNED" is an alias for FINALIZED.
- FINALIZED → DRAFT: not possible.
- FINALIZED → content edits: **not blocked on the `/save` path** (F-01). Blocked on PATCH (`update_report`, 111-112).
- There is no PRELIMINARY or AMENDED state. `VALID_STATUSES = ("DRAFT", "FINALIZED")` (line 34).

**State machine (oru_status, model default PENDING):**
- PENDING → SENT (`_deliver`, 268-270) or FAILED (276-277).
- With `HL7_EMR_ENABLED` False, `finalize_report` sets PENDING (304-305).
- Idempotency: if already FINALIZED and SENT, returns SENT without resending (294-298).
- FAILED → SENT via `resend_report` (only when `HL7_EMR_ENABLED`, 323-325).

**External integration:** HL7 v2.3 ORU^R01 over MLLP (`0x0B ... 0x1C 0x0D`) to the EMR (`report_service.py:222-257`). Socket timeout 5 s. Any ACK containing `MSA|AE` or `MSA|AR` is a failure. Any other ACK, or an empty ACK, counts as delivered (best effort).

### 3.2 worklist_service.py (MWL query and C-STORE matcher)

**Purpose:** DICOM Modality Worklist (C-FIND) query-to-SQL translation, response serialization, and matching of incoming C-STOREs to open worklist steps.

**Key functions**
- `query_filters(query: Dataset) -> dict` (57-75). Non-empty keys are match filters; empty keys are return keys.
- `build_statement(filters: dict)` (88-111). Wildcard `*` becomes LIKE (101-102). ScheduledStationAETitle and Modality treat NULL as a wildcard (103-106). CANCELED steps are hidden unless the modality asked for that status (109-110).
- `requested_keys(query) -> set[str]` (114-124)
- `serialize_sps(step, patient, requested) -> Dataset` (127-161)
- `find_matching_step(session, patient_id: int, meta: dict)` (164-208). Async. Order: accession (no patient check, 179-188), then patient plus modality, oldest first (192-208).
- `complete_step(step, study_uid)` (211-215). Sets COMPLETED, links the study, sets `performed_at`.

**Models touched:** `ScheduledProcedureStep` (`scheduled_procedure_steps`), `Patient`.

**State machine (ScheduledProcedureStep.status):**
- SCHEDULED → IN PROGRESS: MPPS N-CREATE (`mpps_service.py:173-175`)
- IN PROGRESS → SCHEDULED: MPPS N-SET DISCONTINUED (`mpps_service.py:227-231`)
- SCHEDULED or IN PROGRESS → COMPLETED: MPPS N-SET COMPLETED (`mpps_service.py:222-225`) or the C-STORE fallback (`find_matching_step` then `complete_step`)
- Any → CANCELED or any other value: REST PATCH (`worklist.py:170-177`), **no transition guard**
- COMPLETED → SCHEDULED or IN PROGRESS: REST PATCH only (no guard)

### 3.3 reading_worklist_service.py (reader task lists)

**Purpose:** Derives reader state (UNREAD, DICTATED, READ) from report status and `read_at`. STAT is an overlay (manual flag, pending critical result, or unreviewed HIGH triage). Provides the assign, read, and STAT actions.

**Key functions**
- `reader_counts(session, user, include_all=None) -> dict` (90-150)
- `list_reader_tasks(session, state, user, page=1, limit=25, radiologist=None) -> dict` (153-274)
- `_task_state(s, rep) -> str` (281-286). FINALIZED → READ. DRAFT → DICTATED. Otherwise READ if `read_at` else UNREAD.
- `set_stat(session, study_uid, on)` (293-300)
- `assign_radiologist(session, study_uid, radiologist)` (303-311)
- `mark_read(session, study_uid, performed_by)` (314-325)
- `mark_unread(session, study_uid)` (328-336)

**Models touched:** `Study` (`stat_flag`, `assigned_radiologist`, `read_at`, `read_by`, `tenant_id`), `Series`, `Instance`, `RadiologyReport`, `CriticalResult`, `TriageFlag`.

**State (derived, not stored):** UNREAD, DICTATED, READ, plus STAT overlay.
- `mark_unread` clears `read_at` but cannot move a FINALIZED or DRAFT study out of READ or DICTATED (`_task_state`, 281-286).

**Integrations:** `query_service.study_aggregates` (229), `tenant_service.scope_from_user` (97, 161).

### 3.4 mpps_service.py (MPPS N-CREATE and N-SET, DIMSE handlers)

**Purpose:** Persists Performed Procedure Steps from the modality over DIMSE. Drives the worklist state machine. Runs on pynetdicom threads on a sync session.

**Key functions**
- `_str(ds, kw) -> str | None` (35-40)
- `_link_scheduled_step(session, ds, patient, modality)` (43-100). Priority: SPS ID from the echoed sequence (52-66), then accession (70-80, no patient check), then patient plus modality (82-99).
- `_get_or_create_patient(session, ds) -> Patient | None` (103-136). Savepoint on insert (115-123). Backfills non-HIS patients (128-135).
- `create_step(session, sop_instance_uid, ds) -> None` (139-178). Commits.
- `update_step(session, sop_instance_uid, ds) -> bool` (181-234). Returns False for an unknown SOP instance.

**Models touched:** `PerformedProcedureStep` (`performed_procedure_steps`, `sop_instance_uid` unique), `ScheduledProcedureStep`, `Patient`.

**State machine (PerformedProcedureStep.status):** set from the modality with no guard. N-CREATE sets it to IN PROGRESS or the echoed value (164). N-SET sets any value (195-197). See F-16.

### 3.5 critical_results_service.py

**Purpose:** Records critical and urgent findings, audits them, fires webhook and custom scripts, and records formal acknowledgement.

**Key functions**
- `create_critical_result(session, study_instance_uid, description, reported_by, finding_level="CRITICAL_RED", series_instance_uid=None, sop_instance_uid=None, patient_id=None, communicated_to=None, communication_channel=None) -> CriticalResult` (22-98). Backfills `patient_id` from the study when omitted (36-45). Flushes (60). Audits (63). Fires webhook (72) and script (82). Returns; the route commits (`critical_results.py:85`).
- `acknowledge_critical_result(session, result_id, acknowledged_by, notes=None) -> CriticalResult` (101-137). Idempotent if already ACKNOWLEDGED (116-117).
- `get_critical_result(session, result_id)` (140-148)
- `list_critical_results(session, study_instance_uid=None, patient_id=None, status=None, finding_level=None, limit=100)` (151-171)

**Models touched:** `CriticalResult` (`critical_results`), `Study`, `AuditLog`.

**State machine (CriticalResult.status):** PENDING_ACK → ACKNOWLEDGED. The model comment (`models.py:835`) also lists ESCALATED and EXPIRED. Nothing in code sets either (`[V]` grep). See F-21.

**Integrations:** `webhook_service.fire` (sync, daemon thread per URL, `webhook_service.py:53-65`). `scripting_service.fire` (sync, threads, `scripting_service.py:308-320`). Both fire before the route commits.

### 3.6 peer_review_service.py (RADPEER QA)

**Purpose:** Peer-review cases: create, assign a reviewer, submit a 1-4 score, aggregate summary.

**Key functions**
- `create_case(session, *, study_instance_uid, original_reader, patient_id=None, modality=None, body_part_examined=None) -> PeerReviewCase` (22-59). Tries to auto-fill from the study (32-38) using `patient_id_str` and `modalities_in_study`, which do not exist on `Study` (`[V]` grep of models.py), so auto-fill never happens.
- `assign_reviewer(session, *, case_id, reviewer) -> PeerReviewCase` (62-89)
- `submit_score(session, *, case_id, score, comments=None, reviewer=None) -> PeerReviewCase` (92-126). Accepts `reviewer`, but the route never passes it (`peer_review.py:143-145`).
- `list_cases(...)` (129-142), `get_case` (145-147), `get_score_summary(session)` (150-175)

**Models touched:** `PeerReviewCase`, `Study`, `AuditLog`.

**State machine (PeerReviewCase.status):**
- PENDING (create, line 47) → ASSIGNED (assign, line 79)
- ASSIGNED → ASSIGNED (reassign to another reviewer; allowed)
- COMPLETED → ASSIGNED: **allowed**, because `assign_reviewer` has no status guard (77-81)
- PENDING or ASSIGNED → COMPLETED (score, line 113). Scoring a COMPLETED case is rejected (107-108).
- Scoring an unassigned case is allowed (no reviewer check).

### 3.7 clinical_protocol_service.py

**Purpose:** Clinical protocol library CRUD and best-match scoring for a study context (description substring +60, modality +40, body part +30, priority tie-break).

**Key functions**
- `create_protocol(session, data) -> ClinicalProtocol` (31-46). Raises ValueError on duplicate name (41-42).
- `get_protocol`, `list_protocols` (49-66)
- `update_protocol(session, protocol_id, data) -> ClinicalProtocol | None` (69-99). The duplicate check runs after `setattr` (see F-22).
- `delete_protocol` (102-107)
- `match_best_protocol(session, study_description, modality, body_part, include_disabled=False)` (121-148)
- `suggest_matches(session, ...) -> list[dict]` (151-178)

**Models touched:** `ClinicalProtocol` (`name` unique, models.py:1355).

**State:** `enabled` flag only. No workflow state.

### 3.8 dose_service.py (RDSR)

**Purpose:** Parses DICOM Radiation Dose Structured Reports (SOP 1.2.840.10008.5.1.4.1.1.88.67), indexes the totals, and computes per-patient cumulative dose.

**Key functions**
- `parse_rdsr(ds: Dataset) -> ParsedRDSRContent` (40-94). Recursive traversal of ContentSequence. Overwrites totals on each match (see F-23).
- `index_dose_report(session, meta, ds, file_path) -> RadiationDoseReport | None` (97-163). Upsert by `sop_instance_uid`. Audit on insert (154-160).
- `get_dose_for_study(session, study_uid)` (166-175), `get_patient_cumulative_dose(session, patient_id)` (178-201), `get_dose_analytics(session)` (204-219)

**Models touched:** `RadiationDoseReport` (`sop_instance_uid` unique), `AuditLog`.

**Integration:** called from the C-STORE indexing path at `app/services/indexing_service.py:324-329` (out of scope).

### 3.9 ian_service.py (IAN)

**Purpose:** Builds an IAN N-CREATE dataset for a study (series and instance references, `InstanceAvailability=ONLINE`) and sends it to each enabled registered DICOM node.

**Key functions**
- `build_ian_dataset(study_instance_uid, series_structure, availability="ONLINE") -> Dataset` (35-63)
- `send_ian_notification_sync(host, port, ae_title, ian_ds, our_ae_title="PAXS_SCU") -> dict` (66-104). Pynetdicom. Keyword names at line 90-95 are unverified (see U-2).
- `notify_study_available(session, study_instance_uid, target_ae_title=None) -> list[InstanceAvailabilityNotification]` (107-221)
- `list_notifications(session, study_instance_uid=None, status=None, limit=100)` (224-237)

**Models touched:** `Study`, `Series`, `Instance`, `DicomNode` (`ae_title` unique), `InstanceAvailabilityNotification`, `AuditLog`.

**State machine (InstanceAvailabilityNotification.status):** PENDING (row created, 186) → SENT (205-213) or FAILED (214-216). No retry.

**Integration:** outbound DICOM N-CREATE to nodes resolved from the DB only (`ian_service.py:143-167`). No user-controlled host, so no SSRF.

### 3.10 pix_pdq_service.py (PIX/PDQ and ADT PIX feed)

**Purpose:** Assigning authorities (AA), patient identities per AA, PIX query (cross-reference by identifier), PDQ query (demographics), and the HL7 ADT PIX feed (A04/A08 registration, A40 merge). Sync session.

**Key functions**
- `create_assigning_authority(...)` (54-71). Unused by routes (the route inlines the insert).
- `upsert_patient_identity(patient_id, assigning_authority_oid, identifier, id_type="MR", is_primary=True) -> PatientIdentity` (88-136). Opens its own `SyncSessionLocal` (99).
- `delete_patient_identity(patient_id, aa_oid) -> bool` (139-157)
- `pix_query_by_identifier(aa_oid, identifier) -> list[PatientIdentityResult]` (160-187)
- `pix_query_by_demographics(patient_name=None, birth_date=None, sex=None)` (190-213)
- `pdq_query_by_identifier(aa_oid, identifier)` (236-260), `pdq_query_by_demographics(patient_name, birth_date, sex, aa_oid)` (263-301)
- `_build_demographics(session, patient_id)` (304-320)
- `handle_adt_feed_patient_registration(data: dict) -> Patient | None` (323-381). Called from `hl7.py:460`.
- `handle_adt_feed_patient_merge(data: dict) -> bool` (384-430). Called from `hl7.py:458`.

**Models touched:** `AssigningAuthority` (`oid` unique), `PatientIdentity` (unique index on (assigning_authority_id, identifier), models.py:313), `Patient` (`patient_id` unique, models.py:198), `Study`, `ScheduledProcedureStep`.

**State:** no workflow state. Identity rows are upserted or deleted.

**Integration:** HL7 v2 listener (`hl7.py:455-466`, `HL7_ENABLED` default False, config.py:175). QBP^Q22/Q23 handlers exist in `hl7.py`, outside this scope.

### 3.11 xdsi_service.py (XDS-I.b)

**Purpose:** Provide and Register Document Set-b (ITI-41 / RAD-68) into an in-archive registry, FindDocuments (ITI-18), RetrieveDocumentSet (ITI-43), and ebXML SubmitObjectsRequest generation.

**Key functions**
- `generate_ebxml_metadata(entry_uuid, unique_id, patient_id, document_title, mime_type, class_code, format_code, availability_status) -> str` (29-89). Built with `xml.etree.ElementTree`. Values are attributes, which ET escapes (see section 5, "XML injection").
- `register_document(session, patient_id, document_title, content_bytes, ...) -> XDSDocument` (92-146)
- `query_documents(session, patient_id=None, class_code=None, availability_status="APPROVED", limit=100)` (149-165)
- `get_document_by_uuid(session, entry_uuid)` (168-173). Matches entry_uuid or unique_id.
- `retrieve_document(session, entry_uuid) -> (XDSDocument, bytes)` (176-184)

**Models touched:** `XDSDocument` (`entry_uuid`, `unique_id` unique), `AuditLog`.

**State machine (XDSDocument.availability_status):** APPROVED (set at 127). DEPRECATED is accepted only as a filter description (xdsi.py:107). No code sets it `[V]`.

**Integrations:** storage via `storage_service.save_bytes` / `read_bytes` (pass-through; see F-26).

### 3.12 ai_service.py (AI inference, mask cache, DICOM SR, dead HL7 dispatcher)

**Purpose:** Runs HU-threshold "models" over a loaded CT or MR volume, returns findings and a 3-D uint8 mask (cached in process memory), writes a DICOM Comprehensive SR instance into the study, and holds a dead HL7 ORU dispatcher.

**Key functions**
- `run_inference(db, series_uid, model_type="brain_hemorrhage", confidence_threshold=0.5) -> dict` (44-238). Cache keyed by `series_uid:model_type:threshold` (58-60, 232-235). Loads the volume through `volume_service.load_volume_array` in a thread (79). All numeric work runs on the event loop (84-211).
- `get_cached_mask(series_uid) -> np.ndarray | None` (241-246). Returns the mask for whichever run last touched that series, regardless of model.
- `generate_dicom_sr(db, study_uid, series_uid, findings, summary, performed_by=...) -> dict` (253-403). Commits (394). `series_uid` and `performed_by` are never used (see F-33).
- `dispatch_ai_hl7_report(db, study_uid, findings, summary, performed_by=...) -> dict` (410-460). **No callers** `[V]` grep. Broken (F-34).

**Models touched:** `Study`, `Patient`, `Series` (new SR series, `SeriesNumber 9001`), `Instance` (new SR instance), `RadiologyReport` (via dead dispatcher). No audit call.

**Integrations:** in-process numpy and pydicom. Storage via `storage_service.save_bytes` (371). **No outbound HTTP, no external AI endpoint, no URL input, so no SSRF surface** `[V]` (grep for httpx, requests, aiohttp, urllib, socket in scope returns none in this module).

**Caches:** `_AI_CACHE` (module global dict, max 32 entries, FIFO eviction of one key per insert, lines 31-33 and 232-235). Each result holds a full-volume uint8 mask.

### 3.13 fhir.py (no service module)

The FHIR router builds resources directly from ORM rows and calls `query_service.series_instance_counts` (fhir.py:215, 247). No state machine or external integration beyond the Patient and Study tables.

---

## 4. Workflows

### (a) Order arrives → MWL entry → modality queries MWL → MPPS IN PROGRESS / COMPLETED

1. **Order in.** Two paths. (i) HL7 `ORM^O01`: `hl7.py:450-454` calls `_upsert_order` (out of scope). (ii) Manual REST: `POST /api/v1/worklist` (`worklist.py:131-157`). It requires ADMIN or TECHNOLOGIST (`_require_write`, line 134). It validates status (135-136), resolves or creates the patient (`_get_patient`, 84-98; 404 if the MRN is unknown and no name is given), generates `sps_id = "SPS-" + 8 hex` (141), and inserts with status SCHEDULED (default, 43).
2. **Modality C-FIND (MWL SCP; caller is `app/dicom/receiver.py:192-200`, out of scope).** `query_filters` (worklist_service.py:57-75) extracts match keys. `build_statement` (88-111) applies them. Wildcard `*` uses LIKE (101-102). NULL station or modality matches any (103-106). CANCELED is hidden unless requested (109-110). `serialize_sps` (127-161) builds the response, with the sequence always included (142).
3. **MPPS N-CREATE (IN PROGRESS).** Called from `app/dicom/receiver.py:348`. `mpps_service.create_step` (139-178): resolves or creates the patient (`_get_or_create_patient`, 103-136). Links an open step via `_link_scheduled_step` (43-100): (1) SPS ID echo (52-66), (2) accession, no patient check (70-80), (3) patient plus modality, oldest first (82-99). Upserts `PerformedProcedureStep` keyed by SOP instance UID (153-160). Sets status from the modality (164). Moves a SCHEDULED step to IN PROGRESS (173-175). Commits (178).
4. **MPPS N-SET COMPLETED.** `update_step` (181-234): sets status, end date and time (195-199). Links the performed series (203-211). If COMPLETED and the step is SCHEDULED or IN PROGRESS, calls `worklist_service.complete_step` (222-225). Commits (233).
5. **MPPS N-SET DISCONTINUED.** Releases an IN PROGRESS step back to SCHEDULED (227-231).
6. **C-STORE fallback.** `worklist_service.find_matching_step` (164-208) closes an open step when the study arrives (accession first, then patient plus modality). Called from `app/services/indexing_service.py:119-121` (out of scope).
7. **Read.** `GET /api/v1/mpps` (`mpps.py:56-73`) lists the last 100 performed steps with patient data. `GET /api/v1/worklist` (`worklist.py:101-128`) lists steps.

### (b) Radiologist reading worklist: claim / assign / release

1. **List.** `GET /api/v1/worklist-tasks?state=…` (`worklist_tasks.py:53-66`) → `list_reader_tasks` (`reading_worklist_service.py:153-274`). Tenant scope via `scope_from_user` (161-162). State filter (177-187). Pagination (205-206).
2. **Counts.** `GET /counts` (`worklist_tasks.py:46-50`) → `reader_counts` (90-150).
3. **Assign / claim.** `POST /{uid}/assign` (`worklist_tasks.py:81-94`) → `assign_radiologist` (303-311). Sets `study.assigned_radiologist = (radiologist or "").strip() or None` (310). **No atomicity, no ownership check, no tenant check, no role check.** Two radiologists can both succeed and the last write wins (F-13).
4. **Release.** There is no release endpoint. Release is `assign` with `null` or `""`, by any authenticated user (F-13).
5. **Read / unread.** `POST /read` → `mark_read` (314-325) sets `read_at` and `read_by`. `POST /unread` → `mark_unread` (328-336) clears them. The derived task state (281-286) still shows READ for FINALIZED and DICTATED for DRAFT studies, so `unread` does not move them.
6. **STAT.** `POST /stat` → `set_stat` (293-300). Any authenticated user can flag or unflag any study (F-05).
7. **Tenant scope.** Reads are scoped (lines 161-162, 174-175). Writes are not: `select(Study).where(Study.study_instance_uid == study_uid)` at 295, 306, 318, 330.

### (c) Report lifecycle: draft → finalized → "amended" (signing, locking)

1. **Draft.** `POST /api/v1/reports` (`reports.py:62-77`) → `create_report` (`report_service.py:57-92`). Status DRAFT (84). One report per study (72-78; DB unique on `study_id`, models.py:1400). The `radiologist` field is taken from the request body, then the username (88).
2. **Edit.** `PATCH /{id}` (`reports.py:99-114`) → `update_report` (109-117). DRAFT only (111-112). Field-level: any non-None field is set (113-115). `radiologist` is writable from the body (F-12).
3. **Finalize.** `POST /{id}/finalize` (`reports.py:117-130`). Gate: `require_destructive_role` (`deps.py:108-114`). Runs `finalize_report` in a thread (122-124). Sets FINALIZED and `finalized_at` (289-292). Keeps `radiologist` if set, else uses the finalizer (292). Idempotent if already FINALIZED and SENT (294-298). If `HL7_EMR_ENABLED`: `_deliver` builds ORU (`build_oru_message`, 134-216) and sends it over MLLP (`send_oru_to_emr`, 222-257). Sets `oru_status` SENT or FAILED (268-278). Otherwise sets PENDING (304-305). Commits (306).
4. **Resend.** `POST /{id}/resend` (`reports.py:133-142`) → `resend_report` (315-328). Requires FINALIZED and `HL7_EMR_ENABLED`.
5. **"Amend".** There is no amendment model. FINALIZED is enforced only in `update_report` (111-112). The `/save` route (reports.py:173-224) can overwrite FINALIZED content without checks (F-01).
6. **Locking.** No row lock. `finalize_report` and PATCH can interleave (see F-40).
7. **Signing identity.** The signature block prints `report.radiologist` (print page, reports.py:420). That value is client-supplied (F-12). The finalizer's identity is not recorded anywhere (no audit call in `report_service`, F-39).

### (d) Critical result notification and acknowledgement

1. **Flag.** `POST /api/v1/critical-results` (`critical_results.py:65-92`) → `create_critical_result` (`critical_results_service.py:22-98`). Backfills `patient_id` from the study (36-45). Creates PENDING_ACK (57). Audits `CRITICAL_RESULT_FLAGGED` (63). Fires webhook (72-79) and scripts (82-89). Returns. The route then commits (85) and refreshes (88). **Side effects fire before the commit** (F-21).
2. **Notify.** Webhook and script dispatch are fire-and-forget threads (`webhook_service.py:53-65`, `scripting_service.py:308-320`). Delivery is not confirmed to any recipient in code.
3. **Acknowledge.** `POST /{id}/acknowledge` (`critical_results.py:95-116`) → `acknowledge_critical_result` (`critical_results_service.py:101-137`). Idempotent if already ACKNOWLEDGED (116-117). Sets `acknowledged_by`, `acknowledged_at`, notes (119-123). Audits (128-134). **Any authenticated user can acknowledge** (F-21).
4. **STAT overlay.** Any PENDING_ACK critical result marks its study STAT in the reader worklist (`reading_worklist_service.py:72-75`, 82-84).
5. **Escalation / expiry.** Not implemented (model comment only, `models.py:835`).

### (e) Peer review case

1. **Create.** `POST /api/v1/peer-review` (`peer_review.py:65-80`) → `create_case` (`peer_review_service.py:22-59`). `original_reader` is client-supplied (`peer_review.py:28`). Status PENDING (47). Audits `PEER_REVIEW_CREATED` (54-57). Auto-fill is dead (32-38).
2. **Assign.** `POST /{id}/assign` (`peer_review.py:117-131`) → `assign_reviewer` (`peer_review_service.py:62-89`). Rejects same reviewer (72-73) and same as original reader (74-75). Sets `reviewer`, status ASSIGNED, `assigned_at` (77-81). **No status guard, so a COMPLETED case can be reassigned** (F-06).
3. **Score.** `POST /{id}/score` (`peer_review.py:134-148`) → `submit_score` (`peer_review_service.py:92-126`). Score must be 1-4 (Pydantic `ge/le`, `peer_review.py:39`; service 101-102). Rejects COMPLETED (107-108). Sets score, comments, COMPLETED, `completed_at` (111-117). Audit uses `case.reviewer or "unknown"` (120-124), so the scorer's identity is not recorded when unassigned.
4. **Summary.** `GET /summary` (`peer_review.py:95-101`) → `get_score_summary` (150-175): counts COMPLETED cases, average score, and `score >= 3` as significant discrepancies (163-169).

### (f) Dose report extraction from RDSR

1. **Ingest.** C-STORE of an RDSR (SOP 1.2.840.10008.5.1.4.1.1.88.67) enters `index_dose_report` (`dose_service.py:97-163`) from the indexing pipeline (out of scope). Requires `sop_instance_uid` (104-106).
2. **Parse.** `parse_rdsr` (40-94). Reads manufacturer, model, and `Modality` (43-45). Recurses ContentSequence (52-93). Matches DLP, CTDIvol, Dose RP, and irradiation-event concepts by CodeValue or substring (61-86). Overwrites each total on each match (66, 74, 82).
3. **Upsert.** Existing row by SOP UID is updated (111-134). Otherwise inserted (136-152) with an audit entry `DOSE_REPORT_INDEXED` (154-160).
4. **Query.** `GET /study/{uid}` → `get_dose_for_study` (166-175). `GET /patient/{pid}` → `get_patient_cumulative_dose` (178-201) sums DLP, CTDIvol, RP, and event counts across all RDSRs for that patient ID. `GET /analytics` → `get_dose_analytics` (204-219). `GET /report/{sop}` (dose.py:83-94) returns the row, including `file_path`.

### (g) IAN notification

1. **Trigger.** `POST /api/v1/ian/notify` (`ian.py:53-76`) → `notify_study_available` (`ian_service.py:107-221`). Commit at `ian.py:66`.
2. **Load.** Study with all series and all instances, eagerly (113-120). Returns [] for an unknown study (122-124), and the route returns 200 with an empty list.
3. **Build.** `series_structure` (126-141) and the IAN dataset (169), with `InstanceAvailability=ONLINE`.
4. **Resolve nodes.** One named node (`target_ae_title`, 146-154) or all enabled nodes (157-163). Empty list returns [] (165-167).
5. **Send.** For each node: insert PENDING row (182-192), flush (192), then `run_in_executor` the sync send (195-203). Association (`ae.associate`, 78), N-CREATE (90-95), release (102). Set SENT with audit (205-213) or FAILED (214-216).
6. **Log.** `GET /notifications` and `/notifications/{id}` (`ian.py:79-104`).

### (h) FHIR ImagingStudy / Patient exposure

1. **Patient search.** `GET /fhir/Patient` (`fhir.py:124-155`). No auth. Filters: `_id` (conds 134-137), `identifier` (139-140), `name` as `LIKE %name%` (141-142). Returns up to 500 with `total` (145-148). **`name=%` returns the whole table in pages.**
2. **Patient read.** `GET /fhir/Patient/{id}` (`fhir.py:158-167`). Numeric internal ID, no auth.
3. **Study search.** `GET /fhir/ImagingStudy` (`fhir.py:170-231`). Patient by MRN or `Patient/{id}` (183-190), accession (191-192). Batch-loads series (206-212) and instance counts (215-217). No tenant filter (177-181).
4. **Study read.** `GET /fhir/ImagingStudy/{uid}` (`fhir.py:234-248`). No auth.
5. **Data exposed:** name (`_patient_resource` 42-47), MRN identifier (51-52), DOB (54), sex (55), study date and time (81-82), accession identifier (75-80), modality codes (85-86), series descriptions and body site (87-98).

### (i) PIX / PDQ patient identity

1. **Admin AA.** `POST /assigning-authorities` (`pix_pdq.py:109-139`), `DELETE` (185-200): ADMIN only.
2. **Identity feed (HL7, primary path).** `hl7.py:455-460`: A04 and A08 → `handle_adt_feed_patient_registration` (`pix_pdq_service.py:323-381`). Outer session creates or updates the patient (337-358) and **flushes but does not commit** (343-346). Then calls `upsert_patient_identity` (361-367), which opens a **second** session (99) and commits an identity row referencing the uncommitted patient (see F-18). A40 → `handle_adt_feed_patient_merge` (384-430) (see F-07).
3. **Identity REST.** `POST /identities` (`pix_pdq.py:204-239`) → `upsert_patient_identity` in a thread (212-219). **No role check** (F-19). `DELETE` (273-288) → `delete_patient_identity` in a thread, ADMIN only.
4. **PIX query.** `GET /query` (`pix_pdq.py:292-332`) by identifier (`pix_query_by_identifier`, 160-187) or demographics (190-213). Returns all identities for the matched patient across AAs.
5. **PDQ query.** `GET /pdq/query` (`pix_pdq.py:336-389`) by identifier (`pdq_query_by_identifier`, 236-260) or demographics (263-301). Returns name, DOB, sex, phone, and identities (`_build_demographics`, 304-320).
6. **Unique constraints.** One identity per (AA, identifier) (models.py:313). One patient per MRN (models.py:198).

### (j) XDS-I.b document publishing

1. **Provide and register.** `POST /api/v1/xdsi/register` (`xdsi.py:66-100`). Content from base64 (76-77) or text (78-79) or a generated manifest (80-81). **The base64 decode is outside the try block** (76-77 versus try at 83), so malformed base64 becomes an unhandled 500.
2. **Store.** `register_document` (`xdsi_service.py:92-146`): UUIDs (105-106). File key `xds_{patient_id}_{unique_id}.dcm` or `.bin` (111), **with raw `patient_id`**. Written with `storage_service.save_bytes` (112). SHA-256 (114). Row APPROVED (116-131). Audit (135-143).
3. **Find.** `GET /documents` (`xdsi.py:103-115`) → `query_documents` (149-165). Default status APPROVED.
4. **Retrieve.** `GET /documents/{entry_uuid}` (`xdsi.py:118-129`) → `retrieve_document` (176-184). Returns raw bytes with `Content-Type` set to the stored `mime_type` (127).
5. **ebXML.** `GET /documents/{entry_uuid}/ebxml` (`xdsi.py:132-153`) → `generate_ebxml_metadata` (`xdsi_service.py:29-89`), built with ElementTree (escape-safe).

### (k) AI inference request and result handling

1. **Models list.** `GET /api/v1/ai/models` (`ai.py:34-87`). Reports `licensed` per model via `has_feature` (46, 54, 62, 70, 78).
2. **Infer.** `POST /api/v1/ai/infer` (`ai.py:90-115`). Check: `lic.is_active` only (98-99). **No per-model feature check** (F-29). Calls `run_inference` (102-107).
3. **Cache.** `_AI_CACHE` lookup by `series:model:threshold` (58-60). Hit returns immediately.
4. **Load.** `series_slices` and `volume_info` (65-74). Volume assembled in a thread (79).
5. **Compute.** Branch on `model_type` (89-211). Unknown values fall through to organ volumetry (188). Masks and reductions run on the event loop (84-211).
6. **Result.** Built at 213-230. Cached twice under two keys (232-235). Returned without the mask (238).
7. **Mask.** `GET /mask/{uid}` (`ai.py:118-135`) returns the cached uint8 mask for that series alias. No license check, no owner check.
8. **SR.** `POST /api/v1/ai/sr` (`ai.py:138-163`). Check: `lic.is_active` (146-147). Calls `generate_dicom_sr` (150-157). It looks up the study (268-275), builds a Comprehensive SR dataset (277-362), writes it (365-371), creates a new Series (SeriesNumber 9001) and Instance (375-392), and commits (394). `series_uid` and `performed_by` are not used (F-33).
9. **HL7 dispatch.** `dispatch_ai_hl7_report` (410-460) has no callers and is broken (F-34).

---

## 5. Code review findings

Each entry: severity, file:line, quote, problem, scenario, fix, evidence tag.

### 🔴 Blocking

**F-01 🔴 Anonymous draft edits, overwrite of FINALIZED reports, and finalization via `/save`** `[V]`
- `app/api/v1/reports.py:177`: `user: User | None = Depends(get_optional_user),`
- `reports.py:184`: `radiologist_name = body.radiologist or (user.username if user else None) or "Dr. Radiologist"`
- `reports.py:212-215` (update path): `report.clinical_history = body.clinical_history` ... `report.radiologist = radiologist_name`. No status check.
- `reports.py:199-201` and `217-220`: `if body.status.upper() in ("FINALIZED", "SIGNED"): report.status = "FINALIZED"`.
- Problem: the route never checks `user`, so anonymous callers are accepted. The update branch has no DRAFT check (contrast `report_service.update_report`, 111-112). FINALIZE skips `require_destructive_role` (reports.py:118-119). The signer name comes from the body and overrides the authenticated user.
- Scenario: `curl -X POST /api/v1/reports/save -d '{"study_instance_uid":"<uid>","findings":"Normal","impression":"Normal","status":"FINALIZED","radiologist":"Dr. Smith"}'` with no token. Result: a FINALIZED report signed by "Dr. Smith". A second call with new findings overwrites the finalized text. The print page then shows it.
- Fix: `user: User = Depends(require_destructive_role)` (or `get_current_user` for draft-only). Always use `user.username` for `radiologist`. Route FINALIZED through `report_service.finalize_report` so the ORU is sent and the role gate applies. On the update branch, reject when status is FINALIZED (reuse `update_report`). Replace `exclude_none` semantics so omitted fields are not cleared (see F-12a).

**F-02 🔴 Report PHI endpoints without authentication** `[V]`
- `reports.py:160-161`: `async def get_study_report(study_instance_uid: str, db: AsyncSession = Depends(get_db)):`
- `reports.py:228`: `async def print_report(study_instance_uid: str, db: AsyncSession = Depends(get_db)):`
- Problem: no auth dependency. The response includes patient name, patient ID, DOB, sex, age, accession, modality, study date, findings, impression, and radiologist (`report_service.py:482-504`).
- Scenario: any network client that knows or obtains a study UID retrieves the full report and demographics. No check is made on the caller at all (the only dependency is `get_db`).
- Fix: `user: User = Depends(get_current_user)` and tenant scoping (`tenant_service.scope_from_user`) on both routes.

**F-03 🔴 FHIR endpoints are unauthenticated and enabled by default; LIKE wildcards enumerate all patients** `[V]`
- `fhir.py:124-155` (`search_patient`) has no user dependency. Neither do the other four routes (158, 170, 234).
- Verified: `ViewerAuthGateMiddleware` (`main.py:287-300`) gates only `/`, `/viewer`, `/studies-ui`, and `/settings`, and is inert unless `AUTH_GATE_ENABLED`. It never gates `/fhir` or `/api`.
- `fhir.py:142`: `conds.append(Patient.patient_name.like(f"%{name}%"))`
- `config.py:187`: `FHIR_ENABLED: bool = True`. `main.py:554-555` mounts the router when true. `ViewerAuthGateMiddleware` gates only `/`, `/viewer`, `/studies-ui`, and `/settings` (`main.py:295-300`). It is active by default (`config.py:159`, `AUTH_GATE_ENABLED: bool = True`) but never covers `/fhir` or `/api`.
- Problem: anonymous access to Patient and ImagingStudy resources. `name=%` matches everything. `_count` caps each page at 500, but varying the name pattern pages through the table. No tenant scoping (`fhir.py:177-181`).
- Scenario: `GET /fhir/Patient?name=%25&_count=500`, repeated with `name=A%`, `name=B%`, and so on, yields the whole patient table with DOB and sex. `GET /fhir/ImagingStudy?patient=<MRN>` yields study metadata. (Repeated paging by varying the pattern is an inference from `_count` capping each page, `fhir.py:130`, with no offset parameter.)
- Fix: add `Depends(get_current_user)` to every FHIR route (metadata may stay open). Escape `%` and `_` in `name`. Require a minimum name length. Scope by tenant. Default `FHIR_ENABLED` to False, or document it as an intentional public surface.

**F-04 🔴 Unescaped patient, report, and org-settings text in the unauthenticated print page** `[V]`
- `reports.py:251`, `383`, `385`, `389`, `391`, `395`, `397`, `401`, `407`, `411`, `417`, `420`, `422`: all `{...}` values are raw f-string interpolations (examples: `<b>{data['patient_name']}</b>` at 383; `<div class="report-text">{findings}</div>` at 407).
- `reports.py:238-240`, `371`, `374`, `427`: `header_html`, `footer_html`, and `logo_src` are raw. `header_html` comes from `OrganizationSettings.header_html_text`, which an anonymous caller can write (X-01).
- `main.py:227-230`: CSP `script-src 'self' 'unsafe-inline' 'unsafe-eval' 'wasm-unsafe-eval'`. Inline script is allowed, so injected script runs.
- Problem: stored XSS on the archive origin, served on an unauthenticated route.
- Scenario: C-STORE with `PatientName = <img src=x onerror="fetch('/api/v1/auth/...')">`, or an anonymous `/save` with findings `</div><script>...</script>`. A clinician prints the report (`/print/{uid}`) and the script runs with the archive origin. Because the refresh cookie is sent on same-origin requests, the script can reach cookie-authenticated endpoints. Whether any such endpoint exists is `[U]` (see U-9).
- Fix: `html.escape` every interpolation, or use Jinja2 with autoescape. Sanitize `header_html_text` and `footer_html_text` with an allowlist. Validate `logo_path`. Tighten CSP on this route (drop `unsafe-inline` for scripts).

**F-05 🔴 Reading-worklist actions are not tenant-scoped and have no role gate** `[V]`
- `reading_worklist_service.py:293-336`: each of `set_stat`, `assign_radiologist`, `mark_read`, `mark_unread` runs `select(Study).where(Study.study_instance_uid == study_uid)` (lines 295, 306, 318, 330) with no `tenant_id` filter.
- `worklist_tasks.py:69-116`: each route takes only `get_current_user`.
- Contradiction: the module docstring says "Every query is VNA-tenant scoped exactly like the dashboard study list" (`reading_worklist_service.py:16-18`). Reads are scoped (161-162), writes are not.
- Scenario: a VIEWER in tenant A sends `POST /api/v1/worklist-tasks/<tenant-B study uid>/assign {"radiologist":"x"}`. The assignment is written. The same user can `stat` or `mark_read` any study.
- Fix: scope each action by `scope_from_user` (404 on mismatch). Require RADIOLOGIST or ADMIN for `assign` and `stat`. See F-13 for the atomic claim.

**F-06 🔴 Peer review: no role gate, unassigned scoring, and COMPLETED cases can be reopened** `[V]`
- `peer_review.py:65-148`: all six routes use only `get_current_user`.
- `peer_review_service.py:77-81` (assign): no status check. `case.status = "ASSIGNED"` runs even when COMPLETED.
- `peer_review_service.py:101-126` (score): no check that the caller is the assigned reviewer. The route does not pass `reviewer` (`peer_review.py:143-145`). Audit records `performed_by=case.reviewer or "unknown"` (122).
- Scenario: a logged-in user who is the original reader (or a VIEWER) scores their own case. A user reassigns a COMPLETED case to themselves (status goes back to ASSIGNED), rescores it, and `GET /summary` changes. The audit entry says "unknown".
- Fix: require RADIOLOGIST or ADMIN. Reject assignment unless status is PENDING or ASSIGNED. Require `case.reviewer == user.username` on score. Reject if `user.username == case.original_reader`. Derive `original_reader` from the user, not the body (`peer_review.py:28`). Record the actor in the audit row.

**F-07 🔴 PIX A40 merge deletes the source patient's studies and identities instead of moving them** `[H]`
- `pix_pdq_service.py:407-420`: `for identity in source.identities:` sets `identity.patient_id = target.id` (in the else branch, 419-420) or deletes duplicates.
- `pix_pdq_service.py:423-424`: `for study in source.studies: study.patient_id = target.id`
- `pix_pdq_service.py:427`: `session.delete(source)`
- `models.py:219-220`: `Patient.studies ... cascade="all, delete-orphan"`. `models.py:227-228`: `Patient.identities ... cascade="all, delete-orphan"`.
- Problem: assigning `patient_id` directly does not remove the object from `source.studies` or `source.identities`. `session.delete(source)` cascades DELETE to every object still in those collections. The moved studies (and their series and instances, via `Study.series_list` and `Series.instances`, `models.py:366-367, 417-418`) and identities are deleted. The UPDATEs are discarded. The database also cascades deletes of the source's `scheduled_procedure_steps` (`models.py:601`, `ondelete="CASCADE"`). No audit row is written. Stored DICOM files are orphaned.
- Scenario: HL7 A40 merges MRN-OLD into MRN-NEW. Afterwards MRN-NEW has no studies or identities from MRN-OLD. Their series and instance rows are gone. Their worklist steps are gone.
- Reach: `hl7.py:456-458`, only when the HL7 listener is enabled (`config.py:175`, default False).
- Evidence: `[H]`. SQLAlchemy was not installed here, so this was not executed. A test with an in-memory SQLite database would confirm it in minutes.
- Fix: move rows with query-level updates (`update(Study).where(Study.patient_id == source.id).values(patient_id=target.id)`, and the same for identities). Then `session.expire(source, ["studies", "identities"])` or delete with no collection cascade. Write an audit row for the merge. Add a regression test.

**F-08 🔴 Repo integrity: the storage backend package is excluded from git and absent from the checkout** `[V]` (grep and `find_spec`), import failure `[H]`
- `.gitignore:9`: `storage/`. This pattern matches any directory named `storage` at any depth, including `app/services/storage/`.
- `storage_service.py:35`: `from app.services.storage import get_storage_backend`
- `storage_service.py:38`: `from app.services.storage.backend import StorageError`
- `importlib.util.find_spec("app.services.storage")` returns `None`. `git ls-files` lists no `app/services/storage*` path.
- Importers: `ai_service.py:27`, `xdsi_service.py:22`, `dicomweb.py:32`, `indexing_service.py:18`, and others. `main.py:457-505` imports `ai`, `xdsi`, and `dicomweb`.
- Problem: `import app.main` fails with `ModuleNotFoundError`. None of the endpoints in this report can run from this checkout.
- Fix: anchor the ignore rule to the data directory (`/storage/`), then commit the missing package. Verify with `python -c "import app.main"` in a clean clone.

**F-09 🔴 "Clinical AI" outputs are HU-threshold heuristics with fixed confidences and canned normal conclusions** `[V]` for code; clinical validity `[H]`
- ICH: `ai_service.py:91` `blood_voxels = (vol_np >= 55.0) & (vol_np <= 95.0)`; `95` `if total_blood_voxels > 50:`; `100` `confidence = min(0.99, 0.75 + (hematoma_vol_cm3 / 50.0) * 0.2)`; `125`: `summary_text = "No acute intracranial hemorrhage or extra-axial collection identified. Parenchymal density within normal limits."`
- Lung: `ai_service.py:129` `nodule_voxels = (vol_np >= -400.0) & (vol_np <= 100.0)`; `136` `confidence = 0.88`; `146` `"finding_name": "Pulmonary Lesion / Parenchymal Density"`; `159` `"... Fleischner follow-up recommended."`; `161` `"Lungs are clear without focal consolidation, pneumothorax, or dominant pulmonary nodule."`
- Bone and organ: fixed `confidence` 0.96 (181) and 0.92 (206). `radlex_code` and `snomed_code` on every finding (120-121, 156-157, 183-184, 208-209).
- Problem: there is no model. Each finding is a threshold count. Confidence is not derived from data. The "normal" text is returned whenever a count falls below a threshold, so a missed hemorrhage is reported as normal. Thresholds have no brain, lung, or skull masking. The lung pass labels soft tissue and most of a non-chest volume as "Pulmonary parenchymal density" with a Fleischner recommendation.
- Scenario: a 50-voxel hemorrhage is about 0.06 mL at 0.5 × 0.5 × 5 mm voxels (`voxel_vol_cm3 = spacing·spacing·slice/1000`, line 76). It is reported as "No acute intracranial hemorrhage … within normal limits" with `overall_severity = "NORMAL"`. A radiologist who trusts the output may not reread the study.
- Fix: label every output as research-only and not for diagnostic use. Remove the canned normal text. Derive confidence from a validated model or remove it. Require radiologist sign-off before any output reaches a report, the SR, or the EMR.

**X-01 🔴 (cross-scope, outside area F) Anonymous writes to organization settings, templates, and logo upload** `[V]`
- `app/api/v1/settings.py:73-85`: `POST /api/v1/settings/organization` takes `user: User | None = Depends(get_optional_user)` (line 84). The body writes `header_html_text` and `footer_html_text` (97-104), and can upload a logo file (87-94) to `UPLOADS_DIR` with the name `logo_{basename}`. The file is then served under `/static/uploads/` (line 94).
- `settings.py:137-154`, `172-193`, `196-206`: template POST, PATCH, and DELETE take `get_optional_user` and never check it.
- `settings.py:118-134`: `GET /api/v1/templates` has no auth.
- Impact: anonymous users can (1) inject HTML into the unauthenticated print page (F-04), (2) alter the report template body that clinicians insert as default findings, and (3) upload arbitrary file content, including HTML or SVG, to a path served from the app origin. Whether `UPLOADS_DIR` is under `STATIC_DIR` `[U]` (U-10).
- Fix: `Depends(require_admin)` on all of these. Restrict the logo upload to image MIME types with extension allowlist. Owner for `settings.py` should review this.

### 🟡 Important

**F-10 🟡 Report authorship and edit rights: any user can create or edit any DRAFT, and set the signer name** `[V]`
- `reports.py:62-77` (create, `get_current_user`), `reports.py:99-114` (PATCH, `get_current_user`).
- `report_service.py:88`: `radiologist=data.get("radiologist") or performed_by`. `report_service.py:113-115`: `ReportPatch.radiologist` is written.
- `reports.py:184` (`/save`) overrides the signer with the body value.
- Fix: derive `radiologist` from `user.username`. Allow edits only by the assigned radiologist or ADMIN.

**F-10a 🟡 `/save` clears fields that a partial request omits** `[V]`
- `reports.py:212-214`: `report.clinical_history = body.clinical_history`, `report.findings = body.findings`, `report.impression = body.impression` run without `exclude_none`. Omitted fields become NULL.
- Scenario: a client sends `{"findings":"..."}` only, and the saved impression is erased.
- Fix: only assign fields that were provided (`body.model_fields_set`).

**F-11 🟡 HL7 ORU: CR and LF are not escaped, and composite name and MRN fields are escaped instead of composed** `[V]`
- `report_service.py:40` `_ESC_MAP = {"\\": "\\E\\", "|": "\\F\\", "^": "\\S\\", "&": "\\T\\", "~": "\\R\\"}`. CR and LF are absent. Findings and impressions can contain both (templates contain `\n`, `report_service.py:377, 384, 391`).
- `report_service.py:166-169`: `f"{_esc(mrn)}^^^{_esc(issuer)}^MR"` escapes the whole MRN, including any `^` components. The name is `_esc(patient.patient_name)` (169), so DICOM `DOE^JOHN` becomes the single literal `DOE\S\JOHN`.
- Problem: a raw CR is a segment terminator, so text containing CR can inject segments (for example `PID` or `ORC` for another patient). Raw LF breaks OBX parsing on EMRs that treat LF as a terminator. Escaping composite fields makes the receiver see the caret as literal data.
- Scenario: a multi-line template finding is sent with a raw LF, and an OBX-5 parser on the EMR truncates it. After F-01, an anonymous caller can put `\rPID|1||<other MRN>` into findings and inject a segment into the outgoing ORU.
- Fix: escape CR as `\X0D\` or `\.br\` for line breaks and LF likewise, per the HL7 v2 escape rules. Build PID-3 and PID-5 from components, not escaped strings. Verify against the EMR's conformance statement.

**F-12 🟡 Peer-review and report signer identities are client-supplied (covered in F-10, F-06)** `[V]`
- `peer_review.py:28` `original_reader: str`, client-supplied. Fix as F-06.

**F-13 🟡 Claim/assign is last-writer-wins with no ownership and no atomic claim** `[V]`
- `reading_worklist_service.py:303-311`: `study.assigned_radiologist = (radiologist or "").strip() or None` (line 310) with no comparison to the existing value.
- `worklist_tasks.py:81-94`: any user can assign anyone.
- Scenario: radiologists R1 and R2 both `POST /assign`. Both get `200 OK`. The study shows R2, R1 never learns of the conflict, and the audit trail is absent (F-39).
- Fix: `UPDATE studies SET assigned_radiologist=:me WHERE study_instance_uid=:uid AND (assigned_radiologist IS NULL OR assigned_radiologist=:me)`. On zero rows, return 409. Separate `release` (owner or ADMIN only). Ensure the assignee is a real active user, not a free string.

**F-14 🟡 Accession-only match closes a step belonging to another patient** `[V]`
- `worklist_service.py:179-188`: `ScheduledProcedureStep.accession_number == accession` with no `patient_id` filter.
- `mpps_service.py:70-80`: same pattern in `_link_scheduled_step` step 2.
- Scenario: patient B's study arrives with accession "A123". Patient A has an open step with accession "A123" (accessions are reused across sites or after resets). `complete_step` (211-215) marks A's step COMPLETED and links B's study. A's worklist now shows B's study.
- Fix: require `patient_id == patient.id` in the accession match (and in MPPS step 2). Fall back only with a patient. Log collisions.

**F-15 🟡 MPPS N-CREATE retry regresses a completed exam and can flip an unrelated open step** `[V]`
- `mpps_service.py:153-165`: an existing row for the SOP instance is updated in place, with no status guard.
- `mpps_service.py:164`: `pps.status = _str(ds, "PerformedProcedureStepStatus") or "IN PROGRESS"`.
- `mpps_service.py:162-163`: `pps.scheduled_step_id = step.id if step else None`.
- `mpps_service.py:82-99` (fallback step 3): if the original step is now COMPLETED, the search can link a different open step for the same patient and modality.
- `mpps_service.py:173-175`: that step is moved from SCHEDULED to IN PROGRESS.
- Scenario: a modality retries N-CREATE for an exam that already completed. The performed step reverts to IN PROGRESS, is unlinked, and an unrelated scheduled step for the same patient and modality is set IN PROGRESS.
- Fix: if the existing row is COMPLETED or DISCONTINUED, return without changes. Do not relink on re-send. Only move a step on the first N-CREATE.

**F-16 🟡 No state-transition guards on worklist steps or performed steps** `[V]`
- `worklist.py:170-177`: any VALID_STATUS value is accepted from any state. COMPLETED can revert to SCHEDULED (performed_at cleared), CANCELED can become COMPLETED, and TECHNOLOGIST can mark COMPLETED with no study link.
- `mpps_service.py:195-197`: N-SET sets any status string.
- Fix: a transition table (for example, COMPLETED is terminal except ADMIN correction with audit). Reject others with 409.

**F-17 🟡 Critical results: side effects before commit, unrestricted acknowledgement, no study check, no enum** `[V]`
- `critical_results_service.py:71-89`: webhook and script fire inside the service. The route commits later (`critical_results.py:85`). A commit failure leaves a notification for a record that does not exist.
- `critical_results_service.py:101-137` and `critical_results.py:95-116`: any authenticated user can acknowledge. The first acknowledgement wins (116-117). No link to `communicated_to`.
- `critical_results.py:33` and `critical_results_service.py:52`: `finding_level` is free text, upper-cased. The webhook at 76 and 86 receives the original, un-normalized value.
- `critical_results_service.py:22-98`: no check that the study exists. Any UID is accepted.
- Fix: fire side effects after commit (outbox or `BackgroundTasks`). Enforce `finding_level` enum (the model comment names three values). Require the acknowledger to match `communicated_to` or hold a physician role. Verify the study exists.

**F-18 🟡 PIX ADT registration for new patients uses a second session that cannot see the uncommitted parent** `[H]`
- `pix_pdq_service.py:337-346`: `session.add(patient); session.flush()` (not committed).
- `pix_pdq_service.py:361-367`: `upsert_patient_identity(patient.id, ...)`. Line 99 opens `SyncSessionLocal()`, a separate connection and transaction. It inserts a `PatientIdentity` referencing `patient.id` (line 124-133) and commits.
- Problem: the parent row is uncommitted in the outer transaction. On MySQL/InnoDB (the default, `config.py:25`), the foreign-key check in the inner transaction waits on the outer transaction's row lock until `innodb_lock_wait_timeout`, then fails. On SQLite (`session.py:58`, `foreign_keys=ON`), the inner write waits on the outer write lock and fails. Either way A04/A08 registration of a new patient fails. Existing patients do not hit this.
- Scenario: a new admit arrives with an A04 PIX message. The patient row is never committed. Subsequent ADT and order messages for that MRN find no patient.
- Fix: refactor `upsert_patient_identity` to accept the caller's session. Or commit the patient first. Add a test with a new MRN.

**F-19 🟡 PIX identity writes have no role gate and no audit trail** `[V]`
- `pix_pdq.py:204-239` (`create_patient_identity`) uses `get_current_user` only. `pix_pdq.py:43` documents `is_authoritative` as the authority flag, but no code in scope reads it (`[V]` grep). Authoritative and non-authoritative AAs are treated the same.
- `pix_pdq_service.py:139-157` (delete), `384-430` (merge), `88-136` (upsert): no `audit_service` call (`[V]`, grep count is zero for `pix_pdq_service.py`).
- Scenario: any user claims an identifier (first mapping wins because of the unique index, `models.py:313`) for a patient, blocking the legitimate mapping. There is no audit trail.
- Fix: require ADMIN or a feed role. Enforce `is_authoritative`. Audit every identity change and merge.

**F-20 🟡 PIX and PDQ demographic queries: unescaped wildcards and unscoped PHI for any authenticated user** `[V]`
- `pix_pdq_service.py:203`: `stmt.where(Patient.patient_name.ilike(f"%{patient_name}%"))`. Line 277 has the same pattern in PDQ.
- `pix_pdq_service.py:209` and `283`: `limit(50)`, but the caller can page by varying the pattern.
- `pix_pdq.py:336-389` returns name, DOB, sex, phone, and identities (`pix_pdq_service.py:318`).
- Fix: escape `%` and `_`. Require minimum length and at least one exact criterion. Role gate. Tenant scope.

**F-21 🟡 Dose totals overwritten by the last match, not aggregated** `[V]` for code; clinical correctness `[U]`
- `dose_service.py:66`, `74`, `82`: `content.total_dlp_mgy_cm = float(val)`, `content.total_ctdivol_mgy = float(val)`, `content.total_dose_rp_mgy = float(val)`. Each match overwrites. The stored "total" is whichever matching item the depth-first traversal visits last.
- `dose_service.py:85-86`: irradiation events are counted. Per-event CTDIvol and DLP items, if present, would overwrite the study-level value.
- `dose_service.py:61`, `69`, `77`, `85`: code matching uses CodeValue strings `113813`, `113830`, `113725`, `113706` and the literal tokens `"DLP"`, `"CTDIVOL"`, `"DOSE_RP"`, `"EVENT"`. The literal tokens are not DICOM code values. I could not verify the DCM codes offline (U-5).
- Scenario: an RDSR with three irradiation events stores the third event's CTDIvol as the study total. `GET /patient/{pid}` sums these, so cumulative exposure is wrong.
- Fix: decide what the "total" means (study accumulated dose data container). Match on the DCM code sequences from PS3.16. Sum or select explicitly. Test against a multi-event RDSR.

**F-22 🟡 Protocol PATCH with a duplicate name returns 500, and PATCH can blank a name** `[V]` (code); autoflush behavior `[H]`
- `clinical_protocol_service.py:87` sets `name` on the ORM object (the loop at 85-87 covers `name`, `priority`, `enabled`). Lines 89-96 run a SELECT for duplicates. SQLAlchemy's default autoflush flushes the pending UPDATE first. `models.py:1355` makes `name` unique. The resulting `IntegrityError` is not a `ValueError`, so `clinical_protocols.py:142-143` does not catch it and the client gets 500.
- `clinical_protocols.py:45` and `clinical_protocol_service.py:85-88`: `name` has no minimum length. An empty string is written, and the duplicate check is skipped (`if cp.name:` at 88).
- Fix: check duplicates before assigning, or catch `IntegrityError` and return 409. Enforce non-empty name on PATCH.

**F-23 🟡 Raw exception text returned to API clients** `[V]`
- Clients receive raw exception text in 500 responses: `critical_results.py:92`, `reports.py:127`, `xdsi.py:100`, `ai.py:115` and `163`, `ian.py:76`, `pix_pdq.py:223`. This can leak SQL or path detail.
- Fix: log server-side, return a generic message with an error ID.

**F-24 🟡 Dose: `/report/{sop_uid}` returns the server filesystem path** `[V]`
- `dose.py:46`: `"file_path": d.get("file_path")`. `dose.py:83-94` returns it to any authenticated user.
- Fix: omit `file_path` from API responses.

**F-25 🟡 IAN: any user can send outbound DICOM notifications; eager load of all instances; network I/O inside an open transaction** `[V]` (code); blocking `[H]`
- `ian.py:53-76` requires only `get_current_user`. `ian_service.py:143-167` notifies all enabled nodes when no target is given.
- `ian_service.py:113-117`: `joinedload(Study.series_list).joinedload(Series.instances)` loads every instance row for the study into memory, just to read UIDs.
- `ian_service.py:178-203`: each node's association runs while the request's DB transaction is open (commit at `ian.py:66`). Association timeouts (pynetdicom defaults) hold the transaction.
- Fix: role gate (ADMIN or RADIOLOGIST). Query only the UIDs needed. Commit notification rows before network I/O, or use a background task.

**F-26 🟡 XDS: raw `patient_id` in the storage key, and the file containment depends on a missing module** `[V]` (no sanitization in scope); containment `[U]`
- `xdsi_service.py:111`: `filename_key = f"xds_{patient_id}_{unique_id}.dcm" if mime_type == "application/dicom" else f"xds_{patient_id}_{unique_id}.bin"`
- `xdsi_service.py:112`: `storage_service.save_bytes(filename_key, content_bytes)`.
- `storage_service.py:134-135`: `save_bytes` is a pass-through to the backend. The sanitizer `_safe_component` (48-56) and `instance_key` (62-67) are not used here.
- Problem: `patient_id` may contain `/`, `\`, or `..`. Containment depends on the backend that is missing (F-08). Without containment this is an arbitrary-write path for any authenticated user (content and suffix attacker-controlled).
- Fix: validate `patient_id` (for example `^[A-Za-z0-9.^|&-]{1,64}$`) and build the key with `_safe_component`. Confirm containment in the backend.

**F-27 🟡 XDS retrieve returns the caller-chosen Content-Type** `[V]`
- `xdsi.py:34` `mime_type: str = Field("application/dicom", ...)` (no allowlist). `xdsi_service.py:122` stores it. `xdsi.py:127`: `return Response(content=content, media_type=doc.mime_type)`.
- Problem: an authenticated user registers `mime_type="text/html"` and content with a script. A browser that fetches the URL with a bearer token and renders the response (for example in an iframe built from a blob) runs the script in the archive origin. Direct navigation gets 401 (bearer-only auth, `deps.py:46-63`), which limits exposure. CSP allows inline script (`main.py:229`).
- Fix: allowlist MIME types (application/dicom, application/pdf, text/xml). Return `Content-Disposition: attachment` for anything else, or force `application/octet-stream`.

**F-28 🟡 XDS register: unbounded body, base64 decode outside try, and any user can register for any patient** `[V]`
- `xdsi.py:32-33`: `content_base64` and `content_text` have no size limit. The body is held in memory.
- `xdsi.py:76-77`: `base64.b64decode` runs before the try block (line 83), so bad input is an unhandled 500.
- `xdsi_service.py:92-146` does not check that `patient_id` exists in `patients`. Any authenticated user can register documents for any patient.
- Fix: size limit (max_length or body limit). Decode inside the try. Validate `patient_id` and role.

**F-29 🟡 AI: per-model license is not enforced; the helper that does it is unused** `[V]`
- `ai.py:98-99` and `146-147`: `if not lic.is_active:`. The `feature_required` values (`ai.py:44-80`: `ich_triage`, `lung_rads`, `brain_seg`, `bone_age_fracture`/`cobb_angle`, `mpr_3d`) are never checked at inference time.
- `app/core/licensing/deps.py` defines `require_license_feature`, which `__init__.py` exports. `ai.py:13` imports only `LicenseManager` and `get_license_manager`.
- Scenario: a license with only `mpr_3d` can run `brain_hemorrhage`.
- Fix: `Depends(require_license_feature("ich_triage"))` per model, or check `lic.has_feature(feature_required)` for the requested `model_type`.

**F-30 🟡 AI: unknown `model_type` silently runs organ volumetry under the caller's label** `[V]`
- `ai.py:23`: `model_type: str = Field("brain_hemorrhage", ...)`, no enum.
- `ai_service.py:188`: `else:  # organ_volumetry` catches every unknown value. `ai_service.py:217` echoes the caller's `model_type`.
- Scenario: `model_type="lung_rads"` (a typo of the licensed feature name) returns soft-tissue volume labeled `lung_rads` in the response and cache.
- Fix: `Literal["brain_hemorrhage","lung_nodule","bone_segmentation","organ_volumetry"]`, or raise on unknown.

**F-31 🟡 AI: `confidence_threshold` is accepted and unused; it also defeats the cache** `[V]`
- `ai.py:24`: `confidence_threshold: float = Field(0.5, ge=0.1, le=0.99, ...)`. `ai_service.py:48` and `58` are the only uses (the cache key). No model branch reads it.
- Problem: callers get no clinical effect, and each distinct float value (1,000+ possibilities) forces a full recompute (`ai_service.py:79`), so the cache does not limit compute.
- Fix: apply the threshold or remove the parameter. Cache on the inputs that matter.

**F-32 🟡 AI: full-volume numpy work on the event loop, and a cache that can hold GB of masks** `[V]`
- `ai_service.py:79`: volume load is in a thread. Lines 84-211 (mask allocation, comparisons, `np.where`, `np.mean`, `np.std`) run on the event loop.
- `ai_service.py:31-33` and `232-235`: `_MAX_AI_CACHE_SIZE = 32`, but each run inserts two keys (`cache_key` and `series_uid`), so about 16 runs fit. Eviction (233) pops one key, possibly the alias, leaving the other.
- Each cached mask is `slices × rows × cols` bytes (for example 300 × 512 × 512 ≈ 79 MB). Sixteen such entries plus the float volume are gigabytes in process memory.
- Fix: run the whole inference in a worker thread or process. Bound the cache by bytes. Evict both keys together. Consider writing masks to storage instead.

**F-33 🟡 AI SR: inference link and provenance discarded; no template identifier; no audit; any user writes to any study** `[V]`
- `ai_service.py:256` `series_uid: str` and `ai_service.py:259` `performed_by: str = ...` are never used in the body (lines 260-403). The SR does not reference the analyzed series (no `ReferencedSeriesSequence`, no `CurrentRequestedProcedureEvidenceSequence`).
- `ai_service.py:318-362`: no `ContentTemplateSequence`, so the object is not identifiable as TID 1500.
- `ai_service.py:350-351`: `_create_code_item("121206", "DCM", f.get("finding_name", "Volume"))`. Every numeric finding gets the same code value, with a varying meaning.
- `ai_service.py:370-371`: `rel_path = f"{study_uid}/{sr_series_instance_uid}/{sr_sop_instance_uid}.dcm"`. The raw `study_uid` bypasses `storage_service.instance_key` sanitization (`storage_service.py:62-67`). The study lookup (268-275) limits the value to an existing study UID.
- `ai_service.py:281`: `file_meta.MediaStorageSOPClassUID = "1.2.840.10008.5.1.4.1.1.88.33"`, labeled "Comprehensive 3D SR" in the same line. My understanding is that 88.33 is Comprehensive SR and 88.34 is Comprehensive 3D SR, but this was not checked against PS3.4 (U-6).
- No audit call (grep count is zero for `ai_service.py`). Any authenticated user with an active license can create a new SR series in any study (`ai.py:138-163`).
- Fix: use `series_uid` to reference the analyzed series. Use `performed_by` in an audit row and in the SR's Author observation context. Add the TID 1500 template identifier, or stop claiming TID 1500. Use a per-finding concept code. Sanitize the path with `instance_key`. Gate with RADIOLOGIST or ADMIN and tenant scope.

**F-34 🟡 Dead and broken HL7 AI dispatcher (also mutates FINALIZED reports)** `[V]`
- `ai_service.py:410-460` has no callers (`[V]` grep).
- `ai_service.py:421`: `selectinload(Study.reports)`. `Study` has no `reports` relationship (`models.py:317-367` defines only `patient` and `series_list`). This raises `AttributeError` when run.
- `ai_service.py:453`: `await report_service.finalize_report(db, report.id, finalized_by=performed_by)`. The real signature is `finalize_report(report_id: int, performed_by: str = "report") -> dict` (`report_service.py:282`). It is synchronous, takes no session, and has no `finalized_by` parameter. `await` on a dict fails.
- `ai_service.py:456-459`: `finalized.oru_status` on a dict fails.
- `ai_service.py:436-437`: appends to `report.findings` and `impression` even when the report is FINALIZED, bypassing the lock.
- Fix: delete it, or rewrite it to go through `finalize_report` after a DRAFT check, with a radiologist gate.

**F-35 🟡 Reader worklist: `mark_unread` cannot unread a dictated or finalized study** `[V]`
- `reading_worklist_service.py:328-336` clears `read_at` only. `_task_state` (281-286) and the buckets (177-187) still derive READ or DICTATED from the report.
- Fix: document the behavior, or store an explicit reader state.

**F-36 🟡 FHIR output is not valid FHIR R4** `[H]` (FHIR spec, not executed)
- `fhir.py:54`: `"birthDate": p.patient_birth_date or None`. DICOM `YYYYMMDD` is not a FHIR `date` (`YYYY-MM-DD`).
- `fhir.py:81-82`: `"started": f"{study.study_date}T{study.study_time or '000000'}"`. This yields e.g. `20260101T120000.000000`, which is not a FHIR `dateTime`.
- `fhir.py:53-56`, `91-94`: JSON `null` values (`birthDate`, `gender`, `meta.lastUpdated`, series `modality`, `bodySite`). FHIR forbids nulls.
- `fhir.py:56`: `meta.lastUpdated` is a naive datetime with no offset. FHIR instants require an offset.
- `fhir.py:108`: the CapabilityStatement `date` is hard-coded `"2026-01-01"`.
- Fix: convert dates to `YYYY-MM-DD` and times with an offset. Omit null keys. Use the real server date. Validate with the HL7 FHIR validator.

**F-37 🟡 Tenant isolation is applied inconsistently** `[V]`
- Tenant-scoped: reading worklist reads (`reading_worklist_service.py:161-162, 174-175`) and dashboard counts.
- Not tenant-scoped: reports list and get (`report_service.py:99-106`, `reports.py:80-96`), MWL (`worklist.py:101-128`), MPPS (`mpps.py:61-68`), critical results (`critical_results_service.py:151-171`), dose (`dose_service.py:166-219`), XDS (`xdsi_service.py:149-165`), FHIR (all), PIX/PDQ (`pix_pdq_service.py:190-301`), peer review (`peer_review_service.py:129-142`), IAN (`ian_service.py:224-237`).
- Fix: if tenants are a security boundary (`U-8`), apply `scope_from_user` consistently. If not, remove the tenant model claims from the docstrings.

**F-38 🟡 Finalize returns 200 when EMR delivery fails; FAILED is not an error** `[V]`
- `report_service.py:268-279` and `307-312`: a failed send returns `{"ok": True, "delivery": {"status": "FAILED"}}`.
- `reports.py:128-130`: only `ok` False is an error. HTTP 502 is used only for exceptions (125-127).
- Problem: the UI sees success while the EMR has not received the result. The `oru_status` column is the only signal.
- Fix: return 202 or 502 with `delivery.status` when FAILED. Or document that clients must read `delivery`.

**F-39 🟡 No audit trail for report, worklist, reader-task, clinical-protocol, PIX, and AI-SR writes** `[V]` (grep of `add_audit` calls in scope)
- Audit calls exist only in `dose_service.py` (2), `critical_results_service.py` (3), `ian_service.py` (2), `peer_review_service.py` (4), and `xdsi_service.py` (2). None exist in `report_service.py`, `worklist_service.py`, `reading_worklist_service.py`, `clinical_protocol_service.py`, `pix_pdq_service.py`, `ai_service.py`, `mpps_service.py`.
- Impact: finalization, amendment, assignment, STAT, identity merge, and SR creation are not recorded. For medico-legal reports this is a gap.
- Fix: call `audit_service.add_audit` (or a richer audit helper) in each write path, including the performing user.

**F-40 🟡 Report finalize and PATCH can interleave without a row lock** `[H]`
- `report_service.py:109-117` (PATCH, async session) and `282-292` (finalize, sync session) both read a DRAFT, then write. Neither uses `with_for_update`.
- Scenario: PATCH loads a DRAFT. Finalize loads it, sets FINALIZED, and sends ORU with the old findings. PATCH then commits new findings. The EMR has the old text, the database has the new text, and the report is FINALIZED.
- Fix: `SELECT ... FOR UPDATE` (or a version column) in both paths.

### 🟢 Nits

- **F-41 🟢** `reports.py:170` and `148`: mid-module imports, and `reports.py:170` duplicates the import from line 19.
- **F-42 🟢** `reports.py:181`: `get_study_report_metadata` raises `ValueError` (unknown study) with no catch in `save_report`, so the client gets 500. `reports.py:186-224` also has no 409 on a race with another `/save` (unique study_id, models.py:1400).
- **F-43 🟢** `report_service.py:477`: `datetime.now().year` uses local time while the rest of the module uses UTC. Also age ignores the birth-day offset.
- **F-44 🟢** `ai_service.py:302-303`, `315-316`: `datetime.now()` (local time) for `StudyDate`, `StudyTime`, `ContentDate`, `ContentTime`. Other fields are UTC.
- **F-45 🟢** `dose_service.py:45`: `content.modality = str(getattr(ds, "Modality", "CT") or "CT").upper()`. The Modality of an SR object is "SR", so the stored modality is "SR" rather than the irradiating modality (`dose.py:38`). `[H]`.
- **F-46 🟢** `dose_service.py:52-93`: unbounded recursion over nested ContentSequence. A crafted deep RDSR raises `RecursionError` during indexing. `[H]`.
- **F-47 🟢** `xdsi_service.py:56`: `f"urn:oasis:names:tc:ebxml-regrep:ResponseStatusType:{availability_status}"` uses `ResponseStatusType` and the uppercase value "APPROVED". ebRS status URNs use `StatusType:Approved` `[U]` (U-7).
- **F-48 🟢** `peer_review_service.py:34-38`: `getattr(study, "patient_id_str", None)` and `getattr(study, "modalities_in_study", None)` always return None (neither attribute exists on `Study`). Auto-fill is dead code. Replace with `study.patient.patient_id` and `Series.modality`.
- **F-49 🟢** `clinical_protocol_service.py:33` and `41-42`: duplicate check uses the stripped name, but `ClinicalProtocol(**data)` (line 43) stores the unstripped name. `"Foo "` and `"Foo"` can coexist. Also `ProtocolRequest.name` has no minimum length (`clinical_protocols.py:33`), and an all-spaces name raises `ValueError` in the service, which the route maps to 409 (`clinical_protocols.py:84-85`) instead of 422.
- **F-50 🟢** `clinical_protocols.py:81`, `139`, `151`: protocol library writes use `require_destructive_role` (ADMIN, RADIOLOGIST). The name suggests data destruction. Use an ADMIN-only dependency for library curation, or document the choice.
- **F-51 🟢** `clinical_protocol_service.py:110-118`: substring matching has no word boundary. A pattern of "ct" matches "doctor" and "fracture". Use token matching.
- **F-52 🟢** `worklist.py:119-124` and `worklist_service.py:101-102`: user input in LIKE patterns is not escaped (`%`, `_`). Wildcard `*` in MWL excludes NULL station or modality rows, unlike exact match (103-106). Inconsistent results.
- **F-53 🟢** `worklist.py:41`: `start_date` is not validated as DICOM `DA` (8 digits). Ordering is lexicographic.
- **F-54 🟢** `mpps.py:68`: fixed `limit(100)` with no pagination and no status enum.
- **F-55 🟢** `ian_service.py:122-124` and `ian.py:61-73`: unknown study returns 200 with an empty list instead of 404.
- **F-56 🟢** `critical_results_service.py:76` and `86`: `finding_level` passed to webhook and script in the original case while the row stores uppercase (see F-17).
- **F-57 🟢** `pix_pdq.py:221`: `ValueError` is returned as 404 with `str(exc)`, and `pix_pdq.py:222-223` returns 400 for any exception with its text.
- **F-58 🟢** `pix_pdq_service.py:283-300` (PDQ demographics): per-patient AA lookup, identity check, and `_build_demographics` in the loop, about 3 to 4 queries per patient (up to about 200). `pix_pdq_service.py:211-212` (PIX demographics): one query per patient. Batch.
- **F-59 🟢** `ai.py:5` (`import io`), `ai.py:6` (`Optional`), and `ai.py:8` (`Query`) are unused (`[V]` grep). Lint.
- **F-60 🟢** `xdsi.py:98-100`: all registration errors (including storage I/O) return 400. Storage errors should be 500.
- **F-61 🟢** `reports.py:227-236` and `report_service.py:337-354`: a GET (`/print`) writes default `OrganizationSettings` (flush, auto-committed by `get_db`, `session.py:223-231`). Side effect on read.
- **F-62 🟢** `report_service.py:88` and `reports.py:235`: `get_or_create_org_settings` default contact details (`report_service.py:347-350`) are hard-coded example values ("reports@paxs-archive.org", "+1 (800) 555-PAXS"). Printed on the unauthenticated print page.
- **F-63 🟢** `fhir.py:27` `SERVER_BASE = settings.PUBLIC_BASE_URL + "/fhir"`. Default `PUBLIC_BASE_URL` is `"http://0.0.0.0"` (`config.py:281`), so `fullUrl` values are unusable by clients.

---

## 6. Other cross-scope observations (outside area F, noted for the lead)

- **X-02 🟡** `app/core/config.py:136`: `SECRET_KEY: str = "paxs-insecure-dev-key-change-me"`. `deps.py:51` and `76` use it to validate bearer JWTs. Any deployment that does not override it allows forged tokens.
- **X-03 🟡** `app/core/config.py:158`: `AUTH_COOKIE_SECURE: bool = False`. Session cookie is sent over HTTP by default (`core/security.py:31-39`). The cookie is `samesite="lax"` (`security.py:36`), which mitigates CSRF on the cookie-capable routes in this report.
- **X-04 🟢** `app/main.py:227-230`: CSP includes `'unsafe-inline' 'unsafe-eval'` for scripts. This removes the protection CSP would otherwise give against F-04 and F-27.
- **X-05 🟢** `app/core/config.py:254`: `CORS_ORIGINS: str = "*"`. Combined with unauthenticated FHIR (F-03), any origin can read the PHI.

CSRF note (not a finding): cookie-capable mutating routes in this area are reports finalize/resend (`reports.py:117-142`), clinical protocol writes (`clinical_protocols.py:79-155`), and PIX admin writes (`pix_pdq.py:109-200`). Their cookie exposure is mitigated by `SameSite=Lax` (`core/security.py:36`) for cross-site POSTs in modern browsers.

---

## 7. Open questions and unverified items

- **U-1 (blocker for running anything)** Is `app/services/storage/` supposed to be in git? If the storage package is private or generated, the import chain needs it. Confirm before any runtime testing of F-07, F-18, F-26, F-27, and F-28.
- **U-2 `ian_service.py:90-95`** `assoc.send_n_create(ian_ds, sop_class=..., sop_instance=..., msg_id=1)`. pynetdicom's keyword names are not verified here (`requirements.txt:12` pins only `pynetdicom>=2.1`). If the names are wrong, every IAN send raises `TypeError`, which `ian_service.py:99-100` swallows, recording every notification as FAILED. Verify with the pinned version.
- **U-3 (F-07)** Confirm the A40 merge deletes rows with a test: in-memory SQLite, two patients, one study each, call `handle_adt_feed_patient_merge`, count studies afterward.
- **U-4 (F-18)** Confirm on MySQL/InnoDB and on SQLite that A04 for a new MRN fails when `handle_adt_feed_patient_registration` runs.
- **U-5 (F-21)** DICOM concept codes for DLP (113813 or 113838), CTDIvol (113830), Dose (RP) (113725), and Irradiation Event (113706), and the accumulated-dose container structure in CT RDSR. Verify with PS3.16 and a real RDSR sample.
- **U-6 (F-33)** SOP Class 1.2.840.10008.5.1.4.1.1.88.33 (Comprehensive SR) versus 88.34 (Comprehensive 3D SR). Verify with PS3.4.
- **U-7 (F-47)** ebXML RegRep status URNs and the XDS DocumentEntry `objectType` value (`xdsi_service.py:54`, `urn:uuid:7f547165-...`). Verify with ITI TF-3 Table 4.2.3.2 and the ebRS 3.0 spec.
- **U-8 (F-05, F-37)** Are tenants a security boundary in this deployment, or only a data-partitioning convention? This sets the severity of the cross-tenant findings.
- **U-9 (F-04)** Does any cookie-authenticated endpoint exist that a script on the archive origin could use to obtain a bearer token (for example a refresh route)? Outside scope (`api/v1/auth.py`).
- **U-10 (X-01)** Is `UPLOADS_DIR` (settings.py) under `STATIC_DIR` (main.py:559-560)? Confirm the anonymous logo upload is web-served.
- **U-11 (F-09)** Does `volume_service.load_volume_array` produce arrays shaped `(slices, rows, cols)` that match `info["rows"]`, `info["cols"]`, and `len(slice_list)` (`ai_service.py:70-84`)? A mismatch would make `mask[blood_voxels] = 1` raise `IndexError`.
- **U-12 (F-09)** Are the "AI" outputs used clinically today, or only as a prototype? Severity of F-09 depends on it.
- **U-13 (F-17)** Should critical results support ESCALATED and EXPIRED states (documented in `models.py:835`, not implemented)? Is an acknowledger restriction intended?
- **U-14 (F-11)** What HL7 escaping and PID composite format does the target EMR expect? The current builder cannot be validated without it.
- **U-15 (F-14)** Are accession numbers globally unique in this deployment? If yes, F-14 is lower severity.
- **U-16 (F-06)** Is the peer-review workflow intended to allow the original reader to score? Is `reviewer` expected to be a user name or user ID?
- **U-17 (F-36)** Which FHIR consumers depend on the current date formats? Changing them may break existing integrations.
- **U-18 (F-29)** Is the `ai_pipeline` feature meant to unlock all models, as `ai.py:46-78` suggests? If so, F-29 narrows to the missing per-model check for licenses without `ai_pipeline`.
- **U-19** `app/api/v1/settings.py` (org settings, templates) and `app/api/v1/auth.py` are out of scope. Their auth should be reviewed by the owner of that area.
- **U-20** `receiver.py` (MPPS, MWL, IAN handlers), `indexing_service.py` (C-STORE to `find_matching_step` and `index_dose_report`), and `hl7.py` inbound parsing are out of scope. MWL and MPPS DIMSE AE-title allowlisting is unverified.
- **U-21** `webhook_service._deliver` and `scripting_service` run user-supplied scripts with PHI in the payload (`critical_results_service.py:80-89`). Confirm the script store is admin-only.
- **U-22** `handle_adt_feed_patient_registration` assumes the HL7 `issuer` is an AA OID. Check that `parse_adt_message` (out of scope) produces the keys `patient_id`, `issuer`, and `name` used at `pix_pdq_service.py:333-358`.

---

## 8. Method and evidence notes

- All 25 in-scope files were read in full with the Read tool. `deps.py` was read in full.
- Peeks were targeted reads or greps, limited to the names and behaviors cited above.
- No package was installed. Python (3.14.5) was used only to run `importlib.util.find_spec` and check pynetdicom and SQLAlchemy availability, with `-B` to avoid writing bytecode.
- `graphify-out/` does not exist in this checkout, so the CLAUDE.md graphify query step was skipped.
- Items marked `[H]` (F-07, F-18, F-36, F-40, F-45, F-46) need a test run to convert to `[V]`.
- Items marked `[U]` are listed in section 7.
