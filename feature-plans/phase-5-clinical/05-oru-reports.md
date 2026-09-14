# 05 — HL7 ORU (Report Write-back to EMR) — Phase E2

> 📋 **52-Feature matrix:** see `feature-plans/plan.md` (this feature = #34, 🟡 Partial — reporting + ORU done, dictation pending).
>
> **Status: ✅ Done (2026-08-06)** — implements roadmap item **E2: تقارير → EMR (ORU)**,
> closing the Interop **write-path** (E1 ORM inbound ✅ → E2 ORU outbound ✅).
>
> Evidence: `tests/test_oru.py` — **9 tests** green on SQLite + MySQL 8.0
> (Interop subset 43/43), full SQLite suite **305/305**, lint clean,
> migration `w1234567x8y9` upgrade→downgrade→upgrade cycle verified.

## Problem

A radiologist drafts a report for a study (findings + impression) inside PAXS
and finalizes it. For the EMR to act on the result, the report must be pushed
out to the HIS/EMR as an **HL7 v2 ORU^R01** (Observation Result, Unsolicited)
message over MLLP — the standard radiology-result write-back channel.

Nothing in the codebase produced reports yet. E2 adds:

1. a **`reports` table** (draft → finalized lifecycle, delivery state),
2. an **ORU^R01 message builder** (hand-built, no HL7 library — mirrors
   `app/dicom/hl7.py`),
3. a **blocking MLLP sender** with ACK handling,
4. a **REST surface** (`/api/v1/reports`) + role gate,
5. a **dashboard counter** for reports.

## Design

### Model — `RadiologyReport` (`app/db/models.py`)

| column | type | notes |
| :--- | :--- | :--- |
| `id` | int PK | |
| `study_id` | FK `studies.id` (unique) | one report per study |
| `study_instance_uid` | str | denormalized for REST/list |
| `patient_id` | str | denormalized from study.patient |
| `status` | str | `DRAFT` / `FINALIZED` |
| `clinical_history` / `findings` / `impression` | text | editable while DRAFT |
| `radiologist` | str | defaults to performing user |
| `oru_status` | str | `PENDING` / `SENT` / `FAILED` |
| `oru_error` | text | delivery failure detail |
| `oru_sent_at` / `created_at` / `finalized_at` | datetime | |

Migration: `migrations/versions/w1234567x8y9_add_radiology_reports.py`
(chained after `v1234567w8x9` — 26th migration, linear head).

### Config (`app/core/config.py`)

```
HL7_EMR_ENABLED      = False   # master switch for ORU delivery
HL7_EMR_HOST         = "127.0.0.1"
HL7_EMR_PORT         = 2580    # EMR MLLP listener
HL7_EMR_APPLICATION  = "EMR"
HL7_EMR_FACILITY     = "EMR"
HL7_SENDING_APPLICATION = "PAXS"
HL7_SENDING_FACILITY    = "PAXS_ARCHIVE"
```

### Service — `app/services/report_service.py`

- **Async CRUD** used by the REST layer: `create_report` (rejects duplicate
  study / unknown study), `get_report`, `list_reports` (filters `status`,
  `study_uid`), `update_report` (DRAFT only). The async study lookup uses
  `selectinload(Study.patient)` — lazy loading is illegal on aiosqlite.
- **`build_oru_message(report, study, patient)`** — pure builder, ORU^R01:
  - `MSH|^~\&|PAXS|PAXS_ARCHIVE|EMR|EMR|{now}||ORU^R01^ORU_R01|PAXS-{id}|P|2.3`
  - `PID` — MRN with assigning authority, name, DOB, sex
  - `PV1|1|O`
  - `ORC|RE|{placer}|{filler}`
  - `OBR` — 26-field: placer order #, filler order # (accession), procedure
    code^description (modality from first series), observation datetime from
    study date/time, ordering provider, service section `RAD`, result status `F`
  - `OBX` ×2 — findings (**LOINC 50005-5**) + impression (**LOINC 50122-5**),
    `TX`, result status `F`
  - User text is HL7-escaped (`\` `|` `^` `&` `~`) so it can't corrupt the
    message; line breaks in findings survive for the EMR renderer.
- **`send_oru_to_emr(message)`** — blocking MLLP socket
  (`<0x0B> … <0x1C><0x0D>`), 5 s timeouts, reads the ACK. `MSA|AE/AR` →
  failure; empty/missing ACK → delivered (best-effort receiver).
- **`finalize_report(id)` / `resend_report(id)`** — sync core via
  `SyncSessionLocal` (MLLP blocks, so the async route wraps it in
  `asyncio.to_thread`). Finalize locks the report; when `HL7_EMR_ENABLED`
  delivers the ORU and records `oru_status`/`oru_error`/`oru_sent_at`,
  otherwise leaves `PENDING`. **Idempotent**: a second finalize on an already
  `SENT` report returns without re-delivering (no duplicate results in the EMR).

### API — `app/api/v1/reports.py`

| method | path | role | notes |
| :--- | :--- | :--- | :--- |
| POST | `/api/v1/reports` | auth | create DRAFT (409 duplicate / 404 no study) |
| GET | `/api/v1/reports` | auth | list, `?status=` / `?study_uid=` (422 on bad status) |
| GET | `/api/v1/reports/{id}` | auth | get |
| PATCH | `/api/v1/reports/{id}` | auth | edit DRAFT (409 if finalized, 404 missing) |
| POST | `/api/v1/reports/{id}/finalize` | **ADMIN/RADIOLOGIST** | 403 for PHYSICIAN |
| POST | `/api/v1/reports/{id}/resend` | **ADMIN/RADIOLOGIST** | 409 unless finalized |

Registered in `app/main.py`. Dashboard `/stats` `features["reports"]` counts
reports by status + delivery states.

## Test matrix (`tests/test_oru.py` — 9 tests)

| test | what it proves |
| :--- | :--- |
| `test_build_oru_message_structure` | segment layout, LOINC codes, escaping (`^`→`\S\`, `|`→`\F\`), accession as filler |
| `test_report_crud_via_api` | create→edit→list→get, duplicate 409, unknown study 404, edit missing 404 |
| `test_finalize_requires_destructive_role` | PHYSICIAN finalize → 403 |
| `test_finalize_without_emr_leaves_pending` | EMR disabled → `PENDING`; finalized report locked (PATCH 409) |
| `test_finalize_sends_oru_over_mllp` | **real MLLP round-trip**: mock EMR receives framed ORU^R01, ACK recorded, `SENT` |
| `test_finalize_emr_down_fails_then_resend` | dead EMR → `FAILED` + error; resend while down stays FAILED; EMR up → resend `SENT` |
| `test_double_finalize_is_idempotent` | second finalize on a SENT report → no duplicate frame at the EMR |
| `test_resend_draft_rejected` | resend on DRAFT → 409 |
| `test_list_reports_filter` | `?status=` and `?study_uid=` filters, 422 on bogus status |

## Files

- `app/db/models.py` — `RadiologyReport`
- `migrations/versions/w1234567x8y9_add_radiology_reports.py`
- `app/core/config.py` — `HL7_EMR_*` / `HL7_SENDING_*`
- `app/services/report_service.py` — new
- `app/api/v1/reports.py` — new
- `app/main.py` — router registration
- `app/api/v1/dashboard.py` — reports counters
- `tests/test_oru.py` — new
