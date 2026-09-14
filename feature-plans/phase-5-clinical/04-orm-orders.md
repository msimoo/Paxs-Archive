# Phase 5 · Clinical Workflows — 04 · HL7 ORM (Order Entry → MWL) — Interop write-path

> 📋 **52-Feature matrix:** see `feature-plans/plan.md` (this feature = #38, ✅).
>
> **Status:** ✅ **Done (2026-08-06)** — full implementation shipped in
> `app/dicom/hl7.py` (no new table or migration needed — the A1
> `scheduled_procedure_steps` model was already ORM-ready): ORM^O01 parser
> (MSH/PID/ORC/OBR/ZDS), NW → create worklist step, XO → update, CA/OC/DC/CR →
> cancel, MLLP listener routing, 13 tests in `tests/test_orm.py` (real DIMSE
> MWL e2e + MLLP listener) — 257 total green on SQLite, HL7/worklist/dashboard
> subsets on MySQL 8.0.
> **Priority:** High (from `MASTER-ROADMAP.md` — المرحلة E · Interop Write-Path) · **Effort:** Medium
> **Target protocol:** HL7 v2 **ORM^O01** (General Order Message) over MLLP
> **Milestone:** E1 — **HL7 ORM (أوامر الفحوصات) → MWL**
>
> **Dependencies already shipped:** A1 Modality Worklist
> (`scheduled_procedure_steps` + `worklist_service.py` + MWL C-FIND SCP),
> HL7 ADT listener (`app/dicom/hl7.py` — `_upsert_patient`, HIS provenance,
> race-safe patient creation), MLLP framing + per-connection threads.

---

## 1. What ORM does

The ADT path (already built) answers *"who is in the hospital?"* — it
registers patients and opens a **default** worklist step with no procedure
details. The **ORM** path answers *"what exam was ordered, by whom, when?"* —
it is the RIS's order-entry message: every imaging order becomes a **specific**
SCHEDULED worklist step carrying:

- the **accession number** (filler order number) — the stable link between
  the order, the worklist step, and the stored study;
- the **requested procedure** (code + description, e.g. *CT CHEST
  W CONTRAST*);
- the **modality** (ZDS-1), **scheduled date/time** (OBR-6/7);
- the **ordering provider** (ORC-12 / OBR-16).

This is the *write-path*: the RIS **writes** orders into PAXS, and modalities
**read** them back through MWL C-FIND — no manual booking, no double entry.
Together with ADT (patient registration) it closes the Interop loop that
`MASTER-ROADMAP.md` calls المرحلة E.

### 1.1 Why this is the natural companion to ADT

| | ADT (built) | ORM (this slice) |
| :--- | :--- | :--- |
| Question answered | "Who is admitted?" | "What exam is ordered?" |
| Segments parsed | MSH + PID | MSH + PID + **ORC + OBR + ZDS** |
| Patient row | upsert `source="his"` | same (shared `_ensure_patient`) |
| Worklist step | default (no procedure details) | **specific order** (accession, procedure, modality, provider) |
| Lifecycle | open on A01/A04 | NW create · XO update · CA/OC/DC/CR cancel |

---

## 2. The ORM^O01 message

### 2.1 Anatomy of a radiology ORM order

```
MSH|^~\&|RIS|HOSPITAL|PAXS|ARCHIVE|20260805090000||ORM^O01^ORM_O01|MSG-1|P|2.3
PID|1||MRN123^^^HOSPITAL^MR||Ali^Hassan^^^||19770512|M|||1 St^^City^^||0112345678
ORC|NW|ORD-1001|ACC-1001||||||||Dr^Hany
OBR|1|ORD-1001|ACC-1001|CT CHEST^CT CHEST W CONTRAST|||20260805103000||||||||||Dr^Hany
ZDS|CT|CT CHEST W CONTRAST|CT1
```

Field mapping (DICOM PS3.4 / IHE RAD TF-2 conventions):

| HL7 field | Value | Worklist column | Tag |
| :--- | :--- | :--- | :--- |
| ORC-1 | order control | `status` transition | — |
| OBR-2 / ORC-2 | placer order number | `requested_procedure_id` | (0040,1001) |
| OBR-3 / ORC-3 | filler order number | `accession_number` | (0008,0050) |
| OBR-4.1 | procedure code | `requested_procedure_id` (fallback) | (0040,1001) |
| OBR-4.2 | procedure description | `requested_procedure_description` | (0032,1060) |
| OBR-6 / OBR-7 | requested date-time | `start_date` + `start_time` | (0040,0002/0003) |
| ORC-12 / OBR-16 | ordering provider (XCN) | `scheduled_performing_physician` | (0040,0006) |
| ZDS-1 | modality | `modality` | (0008,0060) |
| config | — | `scheduled_station_aet` (MWL_DEFAULT_STATION_AET) | (0040,0001) |

### 2.2 Order controls (ORC-1)

| Control | Meaning | Action |
| :--- | :--- | :--- |
| `NW` | new order | create a SCHEDULED step (idempotent by accession/placer) |
| `XO` | change order | update the matching open step |
| `CA` / `OC` / `DC` / `CR` | cancel / discontinue | mark the matching open step CANCELED |

Unknown controls are logged and ignored — the listener must never crash on a
message it doesn't understand.

---

## 3. Design: reuse everything already built

### 3.1 No new table, no migration

`ScheduledProcedureStep` (built in A1) already carries every ORM field —
`accession_number`, `modality`, `requested_procedure_id/description`,
`scheduled_performing_physician`, `start_date/time`, `status`. The A1 doc's
"table is already ORM-ready" note is exactly what this milestone banks on.

### 3.2 Shared, race-safe patient handling

The old `_upsert_patient` had inline IntegrityError recovery (adopt-the-winner
when a simultaneous C-STORE wins the INSERT for a brand-new MRN). This is
**extracted** into `_ensure_patient(session, data)` and shared by both paths,
so ORM gets the same race safety with zero duplication:

```python
def _upsert_patient(data):          # ADT — opens a *default* step on A01/A04
    with SyncSessionLocal() as s:
        patient = _ensure_patient(s, data)
        if patient and schedule: _create_default_step(s, patient, data)
        s.commit()

def _upsert_order(data):            # ORM — creates/updates/cancels the *order* step
    with SyncSessionLocal() as s:
        patient = _ensure_patient(s, data)
        if patient is None: return
        if control in _ORDER_CANCEL: _cancel_order_steps(s, patient, data)
        elif control in _ORDER_CREATE: _upsert_order_step(s, patient, data)
        else: log.debug(...); return
        s.commit()
```

### 3.3 Idempotency — the RIS re-sends

RIS/HL7 transports re-deliver (and the integration test literally re-sends the
same order). `_find_open_order_step` matches the **filler order number
(accession)** first, then the **placer order number**, then updates the
existing open step instead of duplicating it. Cancels are equally idempotent
(no matching step → no-op).

### 3.4 The listener routes by message type

`_handle_message` now dispatches on MSH-9:

```python
msg_type = segments[0].split("|")[8] ...
if msg_type.startswith("ORM^O01"):  data = parse_orm_message(text); _upsert_order(data)
else:                                data = parse_adt_message(text);  _upsert_patient(data)  # trigger-gated
```

ADT triggers that aren't in `_HANDLED_TRIGGERS` (e.g. ORU^R01, non-HL7
payloads) still fall through harmlessly.

---

## 4. Implementation checklist (shipped)

| # | Task | Files |
| :-: | :--- | :--- |
| 1 | `parse_orm_message` — ORM^O01 → order dict (PID + ORC + OBR + ZDS), XCN provider name, DTM split | `app/dicom/hl7.py` |
| 2 | `_ensure_patient` extraction (shared ADT/ORM race-safe patient upsert) | `app/dicom/hl7.py` |
| 3 | `_upsert_order_step` (NW/XO create-or-update, idempotent by accession/placer) | `app/dicom/hl7.py` |
| 4 | `_cancel_order_steps` (CA/OC/DC/CR → CANCELED) | `app/dicom/hl7.py` |
| 5 | `_handle_message` routing by MSH-9 (ORM vs ADT) | `app/dicom/hl7.py` |
| 6 | Dashboard labels: `HL7 MLLP (HIS ADT + ORM)` in `/health` web matrix + UI copy | `app/api/v1/dashboard.py`, `app/static/index.html`, `app/static/app.js` |
| 7 | Tests: parser, NW create, idempotent re-send, XO update, CA/OC cancel (+scope), real DIMSE MWL e2e, canceled hidden from MWL, XO reflected on MWL, MLLP listener create + cancel | `tests/test_orm.py` |

## 5. Acceptance criteria (all verified)

- An ORM^O01 NW order for a new MRN creates one SCHEDULED worklist step with
  accession, procedure, modality, date/time and provider; the patient row is
  `source="his"`.
- Re-sending the same order does **not** duplicate the step; XO updates it;
  CA/OC/DC/CR marks it CANCELED without touching other patients' steps.
- A modality querying MWL over real DIMSE **sees the order** (accession +
  procedure + modality + HIS demographics), and a canceled order is hidden.
- The MLLP listener path works end-to-end (create + cancel over a real
  socket), and the dashboard /health web matrix shows `HL7 MLLP (HIS ADT + ORM)`.
- Full suite green on SQLite + HL7/worklist/dashboard subsets on MySQL 8.0;
  pyflakes clean.

## 6. Follow-ups (not in this slice)

- **E2 — ORU report write-back to EMR** — the inverse channel: when a report
  is finalized, send ORU^R01 to the EMR (still ⬜ in MASTER-ROADMAP).
- **ORM → webhook** — fire `WorklistCreated` / `WorklistCanceled` through
  `webhook_service.py` so external systems react without polling.
- **Order status hygiene** — sweep stale SCHEDULED steps older than N days
  (a scheduled-but-never-performed order is a no-show alert for the RIS).
- **ZDS-2/3 enrichment** — scheduled protocol code + station from the ZDS
  segment where RISes send them.
