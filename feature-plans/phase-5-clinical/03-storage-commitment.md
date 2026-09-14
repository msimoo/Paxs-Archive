# Phase 5 · Clinical Workflows — 03 · Storage Commitment (SCP + SCU)

> 📋 **52-Feature matrix:** see `feature-plans/plan.md` (this feature = #6, ✅).
>
> **Status:** ✅ **Implemented — Phase A3** (2026-08-05; 7 DIMSE e2e tests)
> **Priority:** Critical (from `STANDARD_PACS_FEATURES_PLAN.md` §1) · **Effort:** Medium
> **Target SOP Class:** Storage Commitment Push Model SOP Class (`1.2.840.10008.1.20.1`)
>
> **Dependencies already shipped:** A1 MWL + A2 MPPS closed loop; C-STORE /
> C-MOVE with the `dicom_nodes` destination registry (`app/services/node_service.py`);
> pluggable `StorageBackend` (local + S3/MinIO) from Phase B1
> (`app/services/storage/`, `storage_service.py`).

---

## 1. What Storage Commitment does

A modality has a limited local disk. Before it deletes its own copy of a study,
it asks the PACS: **"will you guarantee this data is safely archived?"** — and
the PACS answers **"yes"** (or "no" with a reason) for every instance in the
request.

This is the **Storage Commitment Push Model**: the modality sends the PACS a
list of referenced SOP Instances (by SOP Class UID + SOP Instance UID), the
PACS verifies each one is durably stored, and replies *asynchronously* with a
Storage Commitment **N-EVENT-REPORT** confirming or failing each instance.

### 1.1 The SOP class

| Item | Value |
| :--- | :--- |
| SOP Class UID | `1.2.840.10008.1.20.1` (Push Model) · `1.2.840.10008.1.20.2` (Pull Model) |
| Service class | **Storage Commitment Push Model** (N-ACTION / N-EVENT-REPORT) |
| Request | `N-ACTION-RQ` with `ActionTypeID = 1` (Request Storage Commitment) |
| Response | `N-ACTION-RSP` (receipt ack, `0x0000`) then **asynchronous** `N-EVENT-REPORT-RQ` (`EventTypeID = 1`) with per-instance results |
| pynetdicom symbols | `pynetdicom.sop_class.StorageCommitmentPushModel` (SCP), `evt.EVT_N_ACTION`, `evt.EVT_N_EVENT_REPORT` |

Key points that shape the design:

- **Two-phase protocol.** N-ACTION is the request; the actual commitment
  verdict arrives later as an N-EVENT-REPORT the PACS *initiates* on the same
  association (or a fresh one). The modality must stay associated or be
  reachable to receive it.
- **Per-instance results.** The N-EVENT-REPORT carries a `ReferencedSOPSequence`
  where every instance gets its own entry with a `ReferencedSOPInstanceUID`
  plus either a success or a `FailureReason` (e.g. `0x0110` Processing failure,
  `0x0112` No such object instance, `0x0122` SOP class not supported).
- **We already know the answers.** Because PAXS stores every instance with a
  SHA-256 fingerprint (audit log) and knows its storage key, a commitment
  check is: *does the instance exist in the DB and does its file exist on the
  current backend?* — no new machinery.

### 1.2 Why push model, not pull

The Push Model is what CT/MR/CR/US consoles implement (dcm4che, Orthanc,
fo-dicom, and every major modality). The Pull Model (`1.2.840.10008.1.20.2`)
requires the PACS to *request* the data; it's rare in practice. **Implement the
Push Model SCP** (what modalities expect) and, because we already ship a
C-STORE SCU + `dicom_nodes` registry, **optionally the Pull Model SCU** as a
follow-up (this archive can then commit to *another* archive).

---

## 2. N-ACTION (the commitment request)

### 2.1 The dataset a modality sends

```python
action = Dataset()
action.TransactionUID = generate_uid()          # (0008,1195) — ties RSP to EVENT-REPORT
action.ReferencedSOPSequence = [Dataset(), ...]
action.ReferencedSOPSequence[0].ReferencedSOPClassUID = CTImageStorage
action.ReferencedSOPSequence[0].ReferencedSOPInstanceUID = "1.2.3.5.92.1"
# … one entry per instance the modality wants committed
```

Handler shape (sync session, like every other DIMSE handler):

```python
def handle_n_action(event):
    ds = event.request.AttributeList
    tx_uid = str(getattr(ds, "TransactionUID", ""))
    refs = getattr(ds, "ReferencedSOPSequence", [])
    with SyncSessionLocal() as session:
        results = storage_commit_service.verify_instances(session, refs)
    # Remember the association so the EVENT-REPORT can be sent on it.
    _commit_requests[tx_uid] = (event.assoc, results)
    return 0x0000, None
```

### 2.2 Verification (`storage_commit_service.verify_instances`)

For each `(SOPClassUID, SOPInstanceUID)` reference:

1. Look up the instance in `instances` by `sop_instance_uid`.
2. Check the object still exists on the active storage backend
   (`storage_service.exists(instance.file_path)` — backend-agnostic after B1).
3. Build the per-instance result:
   - found + readable → success entry (`ReferencedSOPInstanceUID` only)
   - not in DB → `FailureReason = 0x0112` (No such object instance)
   - DB row but missing file → `FailureReason = 0x0110` (Processing failure)

The result set is persisted (new `storage_commit_requests` table) so the
answer survives a restart — the event report can be re-sent from a retry queue.

### 2.3 The asynchronous N-EVENT-REPORT

After a short delay (a few seconds — modalities expect it not to be
instantaneous, and real verification may involve the worker), the SCP sends:

```python
report = Dataset()
report.EventTypeID = 1                            # N-EVENT-REPORT Event Type
report.TransactionUID = tx_uid
report.ReferencedSOPSequence = [success_entries + failure_entries]
```

pynetdicom SCU-side: `assoc.send_n_event_report(handler, dataset, StorageCommitmentPushModel, ...)`
from the SCP's own AE. The modality's N-EVENT-REPORT-RSP confirms receipt
(`0x0000`); on timeout/failure the request goes to a retry queue backed by
`dicom_nodes` (re-open a fresh association to the modality's AE).

---

## 3. Integration points already in the codebase

| Existing capability | Where | How Storage Commitment uses it |
| :--- | :--- | :--- |
| Instance rows + storage keys | `instances.file_path` (key after B1) | verification lookups (`sop_instance_uid` index already exists) |
| Backend-agnostic existence check | `storage_service.exists(key)` | works for local + S3/MinIO with no branching |
| SHA-256 tamper evidence | `audit_log.sha256_hash` | commitment success can record the hash as proof-of-archive |
| Peer AE registry | `dicom_nodes` + `get_node_sync` | retry association target if the original association closed |
| MPPS COMPLETED event | A2 `mpps_service` | natural trigger to *initiate* commitment to a third party (Pull-model SCU follow-up) |
| Sync DIMSE thread pattern | `handle_find` / A2 N-CREATE/N-SET | handlers are plain sync functions on the DICOM thread |
| Webhooks | `webhook_service.fire` | `StorageCommitted` / `StorageCommitFailed` events |

### 3.1 Model sketch

```python
class StorageCommitRequest(Base):
    __tablename__ = "storage_commit_requests"

    id: Mapped[int] = pk()
    transaction_uid: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    ae_title: Mapped[str] = mapped_column(String(64))       # requesting modality
    status: Mapped[str] = mapped_column(String(16), default="PENDING")  # PENDING / COMPLETED / FAILED / EXPIRED
    payload_json: Mapped[str] = mapped_column(Text)          # request + per-instance results
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now(), nullable=False)
```

`payload_json` keeps the per-instance result list serializable for the retry
queue and the admin UI — one table, no extra joins.

### 3.2 Verification source of truth

Important subtlety: **commitment must reflect the storage backend, not just
the DB row.** A row without a file (failed move to S3, deleted object, disk
loss) must fail commitment — that's exactly the failure modes the protocol
exists to surface. `verify_instances` always cross-checks
`storage_service.exists()` before reporting success.

---

## 4. Implementation checklist

| # | Task | Files |
| :-: | :--- | :--- |
| 1 | `StorageCommitRequest` model + Alembic migration | `app/db/models.py`, `migrations/versions/…` |
| 2 | `storage_commit_service.py` — `verify_instances`, result building, persistence, retry queue | `app/services/storage_commit_service.py` |
| 3 | `handle_n_action` + registration of `StorageCommitmentPushModel` context and `EVT_N_ACTION`; async N-EVENT-REPORT sender (thread-safe queue) | `app/dicom/receiver.py`, `app/dicom/commit_sender.py` |
| 4 | REST surface: list commit requests + per-instance outcomes (`GET /api/v1/commitments`) | `app/api/v1/commitments.py`, `app/main.py` |
| 5 | Dashboard: storage-commitment panel (requests, statuses, failure reasons) | `app/static/*` |
| 6 | Tests: real DIMSE N-ACTION → N-EVENT-REPORT round trip, missing-instance 0x0112, backend-missing-file failure, retry after closed association, API gating, migration cycle | `tests/test_storage_commit.py` |

## 5. Acceptance criteria

- A test SCU sends N-ACTION for stored instances → receives the N-ACTION-RSP
  (`0x0000`), then an N-EVENT-REPORT whose `ReferencedSOPSequence` marks every
  stored instance successful — verified over real DIMSE.
- References to unknown SOP Instance UIDs come back with `FailureReason 0x0112`;
  a DB row whose file was deleted fails with `0x0110`.
- A request whose association closed before the report is delivered is retried
  from the `dicom_nodes` registry until acknowledged or expired.
- Full suite passes on SQLite + MySQL 8.0; migration downgrade→upgrade cycle OK.

## 5.1 Acceptance criteria — all met

- ✅ Test SCU sends N-ACTION for stored instances → `0x0000` ack → an
  N-EVENT-REPORT whose `ReferencedSOPSequence` marks every stored instance
  successful — verified over real DIMSE.
- ✅ Unknown SOP Instance UIDs come back with `FailureReason 0x0112`; a DB row
  whose file was deleted fails with `0x0110` (both verified over real DIMSE).
- ✅ Request-first flow: N-ACTION before the images → the report fires
  automatically once the last C-STORE lands (no second N-ACTION).
- ✅ A request whose association closed before delivery is retried from the
  `dicom_nodes` registry (fresh association) until acknowledged.
- ✅ Full suite green on SQLite + MySQL 8.0; migration `6070910a1b2c`
  downgrade→upgrade cycle OK; REST gated + listed.

## 6. Follow-ups (not in this slice)

- **Pull-model SCU** — this archive commits *to* another archive (disaster
  recovery), triggered by MPPS COMPLETED + auto-routing rules.
- **Commitment in the anonymizer/export path** — only offer commitment for
  instances that survive anonymization, or fail loudly otherwise.
- **Retention policies** — commitment expiry that forces re-verification for
  data with a defined retention window (ties into `audit_log.sha256_hash`).
