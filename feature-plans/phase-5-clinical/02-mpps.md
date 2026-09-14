# Phase 5 · Clinical Workflows — 02 · MPPS (Modality Performed Procedure Step SCP)

> 📋 **52-Feature matrix:** see `feature-plans/plan.md` (this feature = #8, ✅).
>
> **Status:** ✅ **Done (2026-08-05)** — full implementation shipped: model +
> migration `e5f6070910a1`, `mpps_service.py`, MPPS N-CREATE/N-SET SCP on the
> shared DICOM port, worklist IN PROGRESS transitions, `/api/v1/mpps` REST,
> dashboard *Performed* section, 9 tests (real DIMSE e2e) — 129 total green
> on SQLite + MySQL 8.0.
> **Priority:** Critical (from `STANDARD_PACS_FEATURES_PLAN.md` §1) · **Effort:** Medium
> **Target SOP Class:** Modality Performed Procedure Step SOP Class (`1.2.840.10008.3.1.2.3.3`)
>
> **Dependencies already shipped:** A1 Modality Worklist (`scheduled_procedure_steps`
> + `worklist_service.py` + MWL C-FIND SCP), HL7 ADT auto-scheduling
> (`app/dicom/hl7.py`), C-STORE auto-completion heuristic (`find_matching_step`
> in `app/services/worklist_service.py`).

---

## 1. What MPPS does

MWL (A1) tells the modality *who* to scan and *what*. **MPPS** is the reverse
channel: the modality tells the PACS **what actually happened** on the scanner.

After selecting a worklist entry, the modality opens an MPPS association and:

1. **N-CREATE** — creates a Performed Procedure Step instance in state
   `IN PROGRESS` the moment the exam starts (patient on the table).
2. **N-SET** — updates that same instance to `COMPLETED` (or `DISCONTINUED`)
   when the exam ends, carrying end date/time and the **Performed Series
   Sequence** — the list of series it created, with their Study/Series UIDs.

Why this matters for PAXS:

- **Real-time status, not inference.** Today the worklist step flips to
  COMPLETED only when C-STORE images arrive and `find_matching_step` guesses
  (accession, then patient+modality). MPPS makes the *modality* the source of
  truth: IN PROGRESS the second the scan starts, COMPLETED when it ends —
  even before a single image is stored.
- **Closed-loop audit.** MPPS ties scheduled order → performed step → stored
  study in one chain, which is what a RIS/EMR integration expects.
- **Discrepancy detection.** An exam that started (MPPS IN PROGRESS) but never
  stored images (no C-STORE) is a missing-study alert — the kind of silent
  failure that currently goes unnoticed.

### 1.1 MPPS SOP Class

| Item | Value |
| :--- | :--- |
| SOP Class UID | `1.2.840.10008.3.1.2.3.3` |
| Service class | **Modality Performed Procedure Step** (a DIMSE "Intervention" service using N-CREATE / N-SET, *not* C-FIND) |
| Request | `N-CREATE-RQ` (start) then `N-SET-RQ` (update) |
| Response | `N-CREATE-RSP` / `N-SET-RSP` — single status word, `0x0000` on success |
| pynetdicom symbols | `pynetdicom.sop_class.ModalityPerformedProcedureStep`, `evt.EVT_N_CREATE`, `evt.EVT_N_SET` |

Key differences from everything in `receiver.py` so far:

- **It is an intervention service, not a query service.** No `0xFF00` pending
  loops — the handler returns a single `(status, dataset)` tuple.
- **Two-phase lifecycle.** N-CREATE makes the instance (idempotent keyed on
  the MPPS SOP Instance UID), N-SET mutates it. `0x0112` (No such SOP
  Instance) is the correct reply when N-SET names an unknown instance.
- **A new presentation context** on the *same* port — register
  `ModalityPerformedProcedureStep` in `start_receiver()` next to the Q/R and
  MWL contexts; pynetdicom routes N-CREATE/N-SET to our handlers only when the
  association negotiated this SOP class.

Registration (one line, alongside the MWL context):

```python
from pynetdicom.sop_class import ModalityPerformedProcedureStep

ae.add_supported_context(ModalityPerformedProcedureStep, ExplicitVRLittleEndian)
```

---

## 2. The N-CREATE / N-SET datasets

### 2.1 N-CREATE-RQ (exam start — status `IN PROGRESS`)

The modality sends a dataset like this (all tags we consume are marked):

```python
ds = Dataset()
ds.SOPClassUID = "1.2.840.10008.3.1.2.3.3"     # inside the attribute list
ds.SOPInstanceUID = mpps_uid                     # unique MPPS instance UID
ds.PatientName = "Hassan^Ali"                    # (0010,0010)
ds.PatientID = "MRN-ADT-1"                       # (0010,0020) — matches patients.patient_id
ds.PatientBirthDate = "19820910"
ds.PatientSex = "M"
ds.AccessionNumber = "ACC1001"                   # (0008,0050)
ds.Modality = "CT"                               # (0008,0060)
ds.PerformedProcedureStepID = "PPS-0001"         # (0040,0253)
ds.PerformedProcedureStepDescription = "CT CHEST W CONTRAST"   # (0040,0254)
ds.PerformedProcedureStepStartDate = "20260805"  # (0040,0244)
ds.PerformedProcedureStepStartTime = "093000"    # (0040,0245)
ds.PerformedStationAETitle = "CT1"               # (0040,0241)
ds.PerformedProcedureStepStatus = "IN PROGRESS"  # (0040,0252)
# The worklist entry this scan fulfils (echoed from MWL):
sched_attrs = Dataset()
sched_attrs.ScheduledProcedureStepSequence = [Dataset()]
sched_attrs.ScheduledProcedureStepSequence[0].ScheduledProcedureStepID = "SPS-ABC123"
sched_attrs.ScheduledProcedureStepSequence[0].AccessionNumber = "ACC1001"
ds.ScheduledStepAttributesSequence = [sched_attrs]           # (0040,0007)
```

The **Affected SOP Instance UID** travels in the DIMSE header of the
N-CREATE-RQ (pynetdicom: `event.request.AffectedSOPInstanceUID`); the dataset
above is `event.request.AttributeList`.

### 2.2 N-SET-RQ (exam end — status `COMPLETED` / `DISCONTINUED`)

```python
update = Dataset()
update.PerformedProcedureStepStatus = "COMPLETED"            # (0040,0252)
update.PerformedProcedureStepEndDate = "20260805"            # (0040,0250)
update.PerformedProcedureStepEndTime = "094500"              # (0040,0251)
# The series that were produced (links the performed step to the stored study):
series_item = Dataset()
series_item.StudyInstanceUID = "1.2.840.113619.2.408.92"     # (0020,000D)
series_item.SeriesInstanceUID = "1.2.3.4.92"                 # (0020,000E)
series_item.Modality = "CT"
series_item.SeriesNumber = 1
series_item.PerformedProcedureStepID = "PPS-0001"
update.PerformedSeriesSequence = [series_item]               # (0040,0275)
```

N-SET carries the same SOP Instance UID as N-CREATE in its DIMSE header
(pynetdicom: `event.request.RequestedSOPInstanceUID`).

### 2.3 Attribute → column map (new `performed_procedure_steps` table)

| DICOM key | Tag | Column | Phase |
| :--- | :--- | :--- | :--- |
| `SOPInstanceUID` (header) | — | `sop_instance_uid` (unique, PK key) | N-CREATE |
| `PerformedProcedureStepStatus` | (0040,0252) | `status` (IN PROGRESS / COMPLETED / DISCONTINUED) | both |
| `PatientID` | (0010,0020) | `patient_id` (FK → patients.id) | N-CREATE |
| `AccessionNumber` | (0008,0050) | `accession_number` | N-CREATE |
| `Modality` | (0008,0060) | `modality` | N-CREATE |
| `PerformedStationAETitle` | (0040,0241) | `performed_station_aet` | N-CREATE |
| `PerformedProcedureStepID` | (0040,0253) | `pps_id` | N-CREATE |
| `PerformedProcedureStepDescription` | (0040,0254) | `pps_description` | N-CREATE |
| `PerformedProcedureStepStartDate` | (0040,0244) | `start_date` | N-CREATE |
| `PerformedProcedureStepStartTime` | (0040,0245) | `start_time` | N-CREATE |
| `PerformedProcedureStepEndDate` | (0040,0250) | `end_date` | N-SET |
| `PerformedProcedureStepEndTime` | (0040,0251) | `end_time` | N-SET |
| `PerformedSeriesSequence` → `StudyInstanceUID` | (0040,0275)/(0020,000D) | `study_instance_uid` | N-SET |
| `PerformedSeriesSequence` → `SeriesInstanceUID` | (0040,0275)/(0020,000E) | `series_instance_uid` (first series) | N-SET |
| `ScheduledStepAttributesSequence` | (0040,0007) | `scheduled_step_id` (FK → scheduled_procedure_steps.id) | N-CREATE |

### 2.4 Handler shapes (mirror the pynetdicom protocol)

Both handlers run inside pynetdicom's own threads, so they use the sync DB
session — exactly like `handle_find` / `handle_worklist_find`:

```python
def handle_n_create(event):
    ds = event.request.AttributeList
    sop_uid = event.request.AffectedSOPInstanceUID
    with SyncSessionLocal() as session:
        mpps_service.create_step(session, sop_uid, ds)   # upsert + link SPS
    return 0x0000, None

def handle_n_set(event):
    sop_uid = event.request.RequestedSOPInstanceUID
    with SyncSessionLocal() as session:
        ok = mpps_service.update_step(session, sop_uid, event.request.AttributeList)
    return (0x0000, None) if ok else (0x0112, None)      # 0x0112 = no such instance
```

Register both in `start_receiver()`:

```python
handlers = [
    (evt.EVT_C_STORE, handle_store),
    (evt.EVT_C_FIND, handle_find),
    (evt.EVT_C_GET, handle_get),
    (evt.EVT_C_MOVE, handle_move),
    (evt.EVT_N_CREATE, handle_n_create),
    (evt.EVT_N_SET, handle_n_set),
]
```

---

## 3. How MPPS closes the loop with the A1 worklist

The whole point of A2 is that MPPS **drives the worklist status machine** that
A1 currently infers. `mpps_service` implements three transitions:

1. **N-CREATE (IN PROGRESS)** — link the performed step to a scheduled step:
   - Primary: `ScheduledStepAttributesSequence` → `ScheduledProcedureStepID`
     (the modality echoes the exact SPS ID it picked from MWL). Match on
     `scheduled_procedure_steps.sps_id`.
   - Fallback: `AccessionNumber` on the SPS (same accession the C-STORE
     matcher uses), then patient + modality via the existing
     `worklist_service.find_matching_step` pattern.
   - Set the linked step's `status = "IN PROGRESS"` (a new state the A1
     `VALID_STATUS` tuple grows to include). No images needed.

2. **N-SET (COMPLETED)** — set the performed step COMPLETED + end date/time,
   record `study_instance_uid`/`series_instance_uid` from the Performed Series
   Sequence, and flip the linked scheduled step to COMPLETED (linking the
   study UID there too, reusing `worklist_service.complete_step`).

3. **N-SET (DISCONTINUED)** — record the performed step as DISCONTINUED and
   *release* the scheduled step back to SCHEDULED so the technologist can
   re-book it (MWL hides neither SCHEDULED nor IN PROGRESS).

The existing C-STORE heuristic in `indexing_service.py` stays as a **fallback
for modalities that don't speak MPPS** — when an MPPS is already COMPLETED for
that study, the incoming images simply attach to the linked study, and
`find_matching_step` skips already-completed steps (it only matches
SCHEDULED). Both paths coexist without double-completing.

### 3.1 New `performed_procedure_steps` model (sketch)

```python
class PerformedProcedureStep(Base):
    __tablename__ = "performed_procedure_steps"

    id: Mapped[int] = pk()
    sop_instance_uid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    patient_id: Mapped[int | None] = mapped_column(
        ForeignKey("patients.id", ondelete="SET NULL"), nullable=True, index=True
    )
    scheduled_step_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduled_procedure_steps.id", ondelete="SET NULL"), nullable=True, index=True
    )
    status: Mapped[str] = mapped_column(String(16), default="IN PROGRESS",
                                        server_default="IN PROGRESS", nullable=False, index=True)
    pps_id: Mapped[str | None] = mapped_column(String(64))
    pps_description: Mapped[str | None] = mapped_column(String(255))
    modality: Mapped[str | None] = mapped_column(String(16))
    performed_station_aet: Mapped[str | None] = mapped_column(String(64))
    accession_number: Mapped[str | None] = mapped_column(String(64), index=True)
    start_date: Mapped[str | None] = mapped_column(String(8))
    start_time: Mapped[str | None] = mapped_column(String(16))
    end_date: Mapped[str | None] = mapped_column(String(8))
    end_time: Mapped[str | None] = mapped_column(String(16))
    study_instance_uid: Mapped[str | None] = mapped_column(String(128), index=True)
    series_instance_uid: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(),
                                                 onupdate=func.now(), nullable=False)

    patient: Mapped[Patient | None] = relationship()
    scheduled_step: Mapped[ScheduledProcedureStep | None] = relationship()
```

Plus an Alembic migration (pattern: `d4e5f6070910_add_scheduled_procedure_steps.py`).

### 3.2 Why this design is cheap

- **No new DICOM parsing.** The tags in §2.3 are the same families the
  receiver already understands; pydicom exposes them as plain attributes.
- **Reuses A1's service layer.** SPS linking reuses the accession-first,
  patient+modality-fallback matcher; completion reuses `complete_step`.
- **Same port, same thread model.** No new sockets, no async plumbing — N-CREATE
  and N-SET handlers are sync, like every other DIMSE handler.

---

## 4. Implementation checklist

| # | Task | Files |
| :-: | :--- | :--- |
| 1 | `PerformedProcedureStep` model + Alembic migration | `app/db/models.py`, `migrations/versions/…` |
| 2 | `mpps_service.py` — parse dataset → row, upsert keyed on `sop_instance_uid`, SPS link (sps_id → accession → patient+modality), status transitions, serializer for the API | `app/services/mpps_service.py` |
| 3 | `handle_n_create` / `handle_n_set` in the receiver + register `ModalityPerformedProcedureStep` context + `EVT_N_CREATE`/`EVT_N_SET` handlers | `app/dicom/receiver.py` |
| 4 | Grow worklist status machine: `IN PROGRESS` accepted everywhere (worklist API `VALID_STATUS`, MWL response already carries the status) | `app/api/v1/worklist.py`, `app/services/worklist_service.py` |
| 5 | REST surface `GET /api/v1/mpps` (list/filter by status) so the dashboard can show live exam state | `app/api/v1/mpps.py`, `app/main.py` |
| 6 | Dashboard: performed-steps section in the Schedule modal (reuse node-list markup) | `app/static/index.html`, `app/static/app.js`, `app/static/styles.css` |
| 7 | Tests: real DIMSE N-CREATE→N-SET e2e (IN PROGRESS→COMPLETED), SPS link + IN PROGRESS transition, DISCONTINUED release, unknown-instance 0x0112, idempotent re-N-SET, API gating, migration cycle | `tests/test_mpps.py` |

## 5. Acceptance criteria

- A modality (or test SCU using `ModalityPerformedProcedureStep`) sends
  N-CREATE for an existing scheduled step → a `performed_procedure_steps` row
  appears **and** the worklist step flips to IN PROGRESS, all before any
  images are stored (verified over a real association).
- The matching N-SET with status COMPLETED + a Performed Series Sequence flips
  both the performed step and the scheduled step to COMPLETED and links the
  study UID.
- N-SET for an unknown SOP Instance UID gets `0x0112`; a duplicate N-CREATE
  for the same instance UID updates instead of erroring (idempotent).
- Full suite passes on SQLite + MySQL 8.0; migration downgrade→upgrade cycle OK.

## 6. Follow-ups (not in this slice)

- **MPPS→RIS webhook** — fire `ExamStarted` / `ExamCompleted` through
  `webhook_service.py` so the EMR sees real-time exam state (planned as part
  of the HL7 ORM work).
- **Missing-study watchdog** — a worker sweep that flags IN PROGRESS performed
  steps older than N minutes with no stored instances.
- **Storage Commitment (A3)** — MPPS COMPLETED is the natural trigger to issue
  the N-ACTION storage-commitment request for the just-completed study.
- **RT workflow** — MPPS for radiotherapy (RT Performed Procedure Step) shares
  this service skeleton; only the IOD differs.
