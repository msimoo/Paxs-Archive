# Phase 5 · Clinical Workflows — 01 · Modality Worklist (MWL SCP)

> 📋 **52-Feature matrix:** see `feature-plans/plan.md` (this feature = #7, ✅).
>
> **Status:** ✅ **Done (2026-08-05)** — full implementation shipped: model +
> migration `d4e5f6070910`, `worklist_service.py`, MWL C-FIND on the shared
> DICOM port, HL7 ADT auto-scheduling, auto-completion on C-STORE/STOW,
> `/api/v1/worklist` REST, dashboard *Schedule* panel, 13 tests (real DIMSE
> e2e) — 91 total green on SQLite + MySQL 8.0.
> **Priority:** Critical (from `STANDARD_PACS_FEATURES_PLAN.md` §1) · **Effort:** High
> **Target SOP Class:** Modality Worklist Information Model – FIND (`1.2.840.10008.5.1.4.31`)
>
> **Dependencies already shipped:** HL7 ADT listener (`app/dicom/hl7.py`) that upserts
> `Patient` rows with `source="his"`; C-FIND SCP (`app/dicom/receiver.py` `handle_find`);
> `dicom_nodes` destination registry + nodes admin UI; Alembic + MySQL/SQLite migrations.

---

## 1. What Modality Worklist does

A modality (CT, MR, CR, US, …) queries the PACS before an exam to pull the
*list of patients who have been scheduled* for imaging at its station. This is
called the **Modality Worklist (MWL)**, and it is the primary mechanism by which
technologists avoid manually re-typing patient demographics into the scanner.

Unlike Q/R C-FIND (query studies that already exist), MWL is a **forward-looking
worklist**: it answers the question *"which scheduled procedures are due at my
station, and who are the patients?"*

### 1.1 MWL SOP Class

| Item | Value |
| :--- | :--- |
| SOP Class UID | `1.2.840.10008.5.1.4.31` |
| Service class | **Modality Worklist Information Model – FIND** (a C-FIND service) |
| Request | `C-FIND-RQ` carrying a worklist query dataset |
| Response | matching **Scheduled Procedure Steps** as `C-FIND-RSP` datasets (`0xFF00` pending per match, `0x0000` final) |
| pynetdicom symbol | `pynetdicom.sop_class.ModalityWorklistInformationModelFind` |

Key differences from the existing Q/R C-FIND in `receiver.py`:

- **No `QueryRetrieveLevel`** — the worklist model is single-level; there is no
  PATIENT/STUDY/SERIES/INSTANCE hierarchy to walk.
- **Scheduling keys match first** — the modality filters on
  `ScheduledStationAETitle` (its own AE title), start date range, and modality.
- **Returned datasets are "scheduled procedure steps"**, not studies — they
  carry the `ScheduledProcedureStepSequence` (0040,0100), requested-procedure
  attributes, and patient demographics.

Registration in `start_receiver()` mirrors the existing Q/R FIND context:

```python
from pynetdicom.sop_class import ModalityWorklistInformationModelFind

ae.add_supported_context(ModalityWorklistInformationModelFind, ExplicitVRLittleEndian)
```

No new TCP port is needed — the modality opens an association on the existing
DICOM port and negotiates this presentation context alongside the others.

---

## 2. Scheduled Procedure Step query

### 2.1 The query a modality sends (C-FIND-RQ identifier)

A CT console at AE `CT1` at 08:00 today asks "what is scheduled for me today?":

```python
from pydicom.dataset import Dataset
from pynetdicom.sop_class import ModalityWorklistInformationModelFind

query = Dataset()
query.ScheduledProcedureStepSequence = [Dataset()]
step = query.ScheduledProcedureStepSequence[0]
step.ScheduledStationAETitle = "CT1"          # (0040,0001) — matches THIS station
step.Modality = "CT"                           # (0008,0060)
step.ScheduledProcedureStepStartDate = "20260805"   # (0040,0002)
# Empty-value keys request *returned* fields:
step.ScheduledProcedureStepStartTime = ""      # (0040,0003)
step.ScheduledProcedureStepID = ""             # (0040,0009)
step.ScheduledProcedureStepStatus = ""         # (0040,0020)
query.RequestedProcedureID = ""                # (0040,1001)
query.RequestedProcedureDescription = ""       # (0032,1060)
query.PatientName = ""                         # (0010,0010)
query.PatientID = ""                           # (0010,0020)
query.AccessionNumber = ""                     # (0008,0050)
```

The empty values follow the same C-FIND convention already implemented in
`handle_find` / `_query_filters`: **an empty value means "return this field for
every match"; a non-empty value is a match filter** (with `*` wildcard
translation to `%` LIKE on the DB side).

### 2.2 Matching key map (worklist keys → `scheduled_procedure_steps` table)

| DICOM key | Tag | Column |
| :--- | :--- | :--- |
| `ScheduledStationAETitle` | (0040,0001) | `scheduled_station_aet` |
| `ScheduledProcedureStepStartDate` | (0040,0002) | `start_date` |
| `ScheduledProcedureStepStartTime` | (0040,0003) | `start_time` |
| `Modality` | (0008,0060) | `modality` |
| `ScheduledProcedureStepID` | (0040,0009) | `sps_id` |
| `ScheduledProcedureStepStatus` | (0040,0020) | `status` (SCHEDULED / IN PROGRESS / COMPLETED / CANCELED) |
| `ScheduledPerformingPhysicianName` | (0040,0006) | `scheduled_performing_physician` |
| `AccessionNumber` | (0008,0050) | `accession_number` |
| `RequestedProcedureID` | (0040,1001) | `requested_procedure_id` |
| `RequestedProcedureDescription` | (0032,1060) | `requested_procedure_description` |
| `RequestedProcedureCodeSequence` | (0032,1064) | `requested_procedure_code` (CSV of CodeMeaning values) |
| `PatientID` | (0010,0020) | via `patients.patient_id` (JOIN) |
| `PatientName`, `PatientBirthDate`, `PatientSex` | (0010,0010/0030/0040) | via `patients.*` (JOIN) |

Default status filter on the SCP: **return only rows where `status != 'CANCELED'`**
(matching the "active worklist" semantics of dcm4chee/Orthanc).

### 2.3 The response (C-FIND-RSP dataset)

For each match the SCP yields `0xFF00` + a dataset shaped like the standard
Modality Worklist IOD:

```python
rsp = pydicom.Dataset()
rsp.ScheduledProcedureStepSequence = [Dataset()]
step = rsp.ScheduledProcedureStepSequence[0]
step.ScheduledStationAETitle = row.scheduled_station_aet
step.Modality = row.modality
step.ScheduledProcedureStepID = row.sps_id
step.ScheduledProcedureStepStartDate = row.start_date
step.ScheduledProcedureStepStartTime = row.start_time
step.ScheduledProcedureStepStatus = row.status
# Patient demographics — read from the patients table (see §3):
rsp.PatientName = patient.patient_name
rsp.PatientID = patient.patient_id
rsp.PatientBirthDate = patient.patient_birth_date
rsp.PatientSex = patient.patient_sex
rsp.AccessionNumber = row.accession_number
rsp.RequestedProcedureID = row.requested_procedure_id
rsp.RequestedProcedureDescription = row.requested_procedure_description
yield 0xFF00, rsp
```

Handler shape is a near-copy of `handle_find`: `SyncSessionLocal` session, one
JOIN to `patients`, `yield 0xFF00` per row, final `yield 0x0000, None`.

---

## 3. Reusing the HL7 ADT patient data

This is the part that makes MWL cheap to build: **the patient side of every
worklist entry already exists** in the `patients` table, created by the HL7 ADT
listener.

### 3.1 Current state (built)

`app/dicom/hl7.py` `_upsert_patient()` already:

- Parses ADT A01/A04/A08/A31/A40 messages (MSH + PID).
- Upserts a `Patient` row keyed on **MRN** (`patient_id`), stamping
  `source="his"` and storing name / DOB / sex / **phone**.
- Is race-safe against a simultaneously-arriving C-STORE for the same MRN
  (IntegrityError → adopt-the-winner).
- C-STORE/STOW images for the same MRN **auto-attach** to that patient
  (`get_or_create_patient` in `app/services/indexing_service.py`).

### 3.2 New: `scheduled_procedure_steps` table

A worklist entry is a thin row that references the ADT-created patient and adds
the scheduling attributes the scanner needs:

```python
class ScheduledProcedureStep(Base):
    __tablename__ = "scheduled_procedure_steps"

    id: Mapped[int] = pk()
    patient_id: Mapped[int] = mapped_column(
        ForeignKey("patients.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sps_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)  # (0040,0009)
    accession_number: Mapped[str | None] = mapped_column(String(64), index=True)
    modality: Mapped[str | None] = mapped_column(String(16), index=True)
    scheduled_station_aet: Mapped[str | None] = mapped_column(String(64), index=True)
    start_date: Mapped[str | None] = mapped_column(String(8), index=True)
    start_time: Mapped[str | None] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(
        String(16), default="SCHEDULED", server_default="SCHEDULED", nullable=False
    )  # SCHEDULED / IN PROGRESS / COMPLETED / CANCELED
    requested_procedure_id: Mapped[str | None] = mapped_column(String(64))
    requested_procedure_description: Mapped[str | None] = mapped_column(String(255))
    scheduled_performing_physician: Mapped[str | None] = mapped_column(String(192))
    # Closed loop: set when the images for this SPS actually arrive.
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    performed_at: Mapped[datetime | None] = mapped_column(DateTime)
```

Plus an Alembic migration (pattern: `c3d4e5f60709_add_dicom_nodes.py`).

### 3.3 How HL7 data flows into the worklist

Three sources populate `scheduled_procedure_steps` — all resolve their patient
via `patient_id` (MRN), so **MWL responses always serve the authoritative
patient demographics**, whether they came from HIS or a modality:

1. **ADT admit/register (A01/A04)** — the *primary* path in this build. When the
   HL7 listener upserts a patient, it also creates a default SCHEDULED step
   (sps_id generated as `SPS-{MRN}-{date}`, station AET from config default,
   modality unset/any). This gives every HIS-registered patient an immediate
   worklist presence with zero manual entry. (Full RIS order entry via HL7 **ORM**
   is listed as a follow-up in §6 — the table schema is already ORM-ready.)

2. **REST scheduling API** — `POST /api/v1/worklist` (ADMIN/TECH gated) lets a
   booking clerk create a step for an existing patient (by MRN or by creating a
   manual patient). Powers the dashboard *Schedule* panel. This is the
   fallback for sites whose HIS doesn't send ADT.

3. **Modality "on the fly"** — if an image arrives (C-STORE) whose MRN matches a
   patient but no step exists, indexing auto-creates a COMPLETED step as a
   historical record (opening worklist items "materialize" only when scheduled).

### 3.4 Closed loop on image arrival (completion)

Extend `get_or_create_study` (in `app/services/indexing_service.py`) with an
**accession/step matcher**, mirroring the existing HIS auto-attach flow:

```
on C-STORE/STOW for study:
    if meta.accession_number matches scheduled_procedure_steps.accession_number:
        OR meta.patient_id + meta.modality matches an open SCHEDULED step:
            mark step COMPLETED
            link study_instance_uid
```

This is the same "arrive late, close the loop" pattern as the HIS badge: the
dashboard's worklist view can show scheduled → in progress → completed, and the
scan shows up pre-linked to its scheduled order.

### 3.5 Why this design reuses rather than duplicates

- **Single source of truth for demographics** — MWL responses JOIN `patients`
  instead of copying name/DOB into the step row. The HIS-data-wins rule from
  `_apply_demographics` automatically applies: a modality that queries MWL gets
  the *authoritative* HIS demographics, and anything the scanner echoes back on
  C-STORE can't clobber them (already enforced in `get_or_create_patient`).
- **No new HL7 parser work** — ADT parsing, MRN keying, race handling and
  provenance stamping are all shipped; MWL adds one small `_upsert_step` beside
  `_upsert_patient` in `hl7.py`.
- **Phone / contact reuse** — `patients.patient_phone` (HL7 PID-13) is available
  to MWL responses and the worklist UI for free.

---

## 4. Implementation checklist

| # | Task | Files |
| :-: | :--- | :--- |
| 1 | `ScheduledProcedureStep` model + Alembic migration (incl. index on `accession_number`, `scheduled_station_aet`) | `app/db/models.py`, `migrations/versions/…` |
| 2 | `worklist_service.py` — query builder (filters → SQL, wildcard LIKE, status filter, date-range), serializer (row + patient → MWL dataset) | `app/services/worklist_service.py` |
| 3 | `handle_worklist_find` in the receiver + register `ModalityWorklistInformationModelFind` context in `start_receiver()` | `app/dicom/receiver.py` |
| 4 | `_upsert_step` on ADT A01/A04 in the HL7 listener (and ORM-ready default station config) | `app/dicom/hl7.py`, `app/core/config.py` |
| 5 | SPS completion on image arrival (accession/patient+modality matcher in indexing) | `app/services/indexing_service.py` |
| 6 | REST CRUD `app/api/v1/worklist.py` (ADMIN/TECH) + dashboard *Schedule* panel (reuse nodes-modal pattern in `app/static/`) | `app/api/v1/`, `app/static/*` |
| 7 | Tests: real DIMSE MWL C-FIND e2e, ADT→worklist appearance, matching keys/wildcards, auto-completion, admin gating, migration cycle | `tests/test_worklist.py` |

## 5. Acceptance criteria

- A modality (or test SCU using `ModalityWorklistInformationModelFind`) can
  query by `ScheduledStationAETitle` + date + modality and receives its open
  steps with full HIS demographics — verified with a real association in tests.
- An ADT A01 for a new MRN makes that patient appear in the worklist **without
  any manual entry**, carrying name/DOB/sex from HIS.
- When the exam is C-STORe'd, the step flips to COMPLETED and links the study.
- Full suite passes on SQLite + MySQL 8.0; migration downgrade→upgrade cycle OK.

## 6. Follow-ups (not in this slice)

- **ORM (order entry) HL7** — the real RIS path for creating scheduled
  procedures with requested-procedure codes; table is already ORM-ready.
- **MPPS (N-CREATE/N-SET)** — the natural companion: modality reports
  IN-PROGRESS/COMPLETED itself, driving `status` transitions instead of the
  indexing heuristics in §3.4.
- **Prefetching** — when a step is created for a patient with priors, trigger
  C-MOVE from the `dicom_nodes` registry to pull prior studies (roadmap item
  "Prefetching" in `STANDARD_PACS_FEATURES_PLAN.md` §2).
- **Webhook** — fire `WorklistCreated` / `StudyCompleted` events through the
  existing `webhook_service.py` so the RIS can react without polling.
