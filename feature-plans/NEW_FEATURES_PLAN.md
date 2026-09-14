# PAXS Archive Server — New Features Implementation Plan

> This document outlines new features for the PAXS Archive Server, organized by priority, effort, and business impact.
> Based on analysis of the codebase (v0.1.0) and the existing roadmap documents (`paxs-docs/ROADMAP.md`).
> Created: 2026-08-05 · Last updated: 2026-08-05 (current-state assessment refreshed against the live codebase).

---

## 📋 52-Feature Status Matrix (canonical — verified 2026-08-06)

> Legend: ✅ Done · 🟡 Partial · ⬜ Planned (not yet) — **41 ✅ · 1 🟡 · 10 ⬜**.
> Details per feature in `plan.md` and `MASTER-ROADMAP.md`.

| # | Feature | Status |
| :-: | :--- | :--- |
| 1 | C-STORE (SCP/SCU) | ✅ |
| 2 | C-FIND (SCP/SCU) | ✅ |
| 3 | C-MOVE (SCP/SCU) | ✅ |
| 4 | C-GET (SCP/SCU) | ✅ |
| 5 | C-ECHO (SCP/SCU) | ✅ |
| 6 | Storage Commitment (N-ACTION/N-EVENT) | ✅ |
| 7 | Modality Worklist (MWL) | ✅ |
| 8 | MPPS | ✅ |
| 9 | IAN | ✅ |
| 10 | DICOM TLS | ✅ |
| 11 | Lossless Compression | ✅ |
| 12 | Lossy Compression | ✅ |
| 13 | VNA (Vendor Neutral Archive) | ✅ |
| 14 | Study Lifecycle Management | ✅ |
| 15 | Auto-routing Rules Engine | ✅ |
| 16 | Prefetching | ✅ |
| 17 | DICOMDIR Creation | ✅ |
| 18 | Migration Tools | ✅ |
| 19 | Transfer Accelerator | ✅ |
| 20 | WADO-URI | ✅ |
| 21 | WADO-RS / QIDO-RS / STOW-RS | ✅ |
| 22 | Hanging Protocols | ✅ |
| 23 | Prior Study Linking | ✅ |
| 24 | GSPS (Presentation State) | ✅ |
| 25 | Key Image Notes (KIN) | ✅ |
| 26 | 2D MPR Backend Prep | ✅ |
| 27 | 3D VRT / MIP Support | ✅ |
| 28 | Dose Monitoring (RDSR) | ✅ |
| 29 | Fusion (PET/CT, MRI/US) | ✅ |
| 30 | Worklist Management (Unread/Stat/Dictated) | ⬜ |
| 31 | Clinical Protocols Library | ⬜ |
| 32 | Peer Review / QA | ✅ |
| 33 | Critical Results Tracking | ✅ |
| 34 | Reporting / Dictation Bridge | 🟡 (reporting + ORU done; dictation ⬜) |
| 35 | Chat & Collaboration | ⬜ |
| 36 | Teaching Files | ⬜ |
| 37 | Study Split/Merge | ✅ |
| 38 | HL7 v2.x ORU/ORM/ADT | ✅ |
| 39 | LDAP / Active Directory | ✅ |
| 40 | XDS-I | ✅ |
| 41 | REST API (General) | ✅ |
| 42 | Webhooks | ✅ |
| 43 | Lua / Python Scripting | ✅ |
| 44 | FHIR ImagingStudy mapping | ✅ |
| 45 | Audit Logging (ATNA) | ✅ (SHA-256; ATNA XML ⬜) |
| 46 | Role-Based Access Control | ✅ |
| 47 | Load Balancing | ⬜ |
| 48 | High Availability / Clustering | ⬜ |
| 49 | Disaster Recovery Replication | ⬜ |
| 50 | Dashboard & Analytics | ✅ |
| 51 | Anonymization Engine | ✅ |
| 52 | Storage Tiering (HSM) | ✅ (via Study Lifecycle cold tier + S3) |

---

## Current State Assessment

### Already Implemented (Phase 1–3 + Phase 4.5/4.6 complete)

- DICOM DIMSE Receiver (C-STORE, C-FIND, C-GET, C-ECHO) on port 10400
- DICOMweb API (QIDO-RS, WADO-RS, STOW-RS)
- SQLite + SQLAlchemy ORM (Patient → Study → Series → Instance), MySQL 8.0 verified
- UID-based file storage hierarchy
- Lossless compression worker (RLE built-in; JPEG-LS / JPEG 2000 / HTJ2K via optional deps)
- Background worker (compression + thumbnails + AI triage)
- JWT auth + RBAC roles; first registered user becomes ADMIN
- Patient sharing with QR codes + OTP-ready tokens
- Web dashboard + dark reading-room viewer (series switching, window presets, zoom)
- SHA-256 audit logging (tamper detection)
- Docker Compose deployment (MySQL 8.0 + server, auto-migrated on boot)
- **Alembic migrations** — full chain verified on SQLite and MySQL 8.0 (downgrade → upgrade cycles)
- **Rate limiting** — sliding-window per-IP (auth 10/min, global 300/min, 429 + Retry-After)
- **Anonymizer** — PS3.15 PHI-stripped ZIP export (`/api/v1/export/{study}/anonymized`)
- **FHIR R4 gateway** — Patient + ImagingStudy read/search + CapabilityStatement
- **HL7 v2 ADT listener** — MLLP on port 2575, HIS-registered patients auto-attach DICOM studies, dashboard HIS badge
- **AI Emergency Triage** — pluggable detector pipeline in the worker, `triage_flags` table, dashboard banner
- **Windows EXE packaging scripts** — PyInstaller spec + NSSM service scripts (Phase 5 stubs)
- 67 integration tests passing on both SQLite and MySQL 8.0

### Remaining Work (not yet done — from the 52-feature matrix above)

- **26 2D MPR Backend Prep** — ✅ Done: volume geometry + raw voxel streaming endpoints (/api/v1/volume). **29 Fusion** — ✅ Done: FoR grouping + aligned pairs + geometry (/api/v1/fusion/{study}). **27 3D VRT-MIP** — advanced visualization (⬜)
- **30 Worklist Management UI (Unread/Stat/Dictated)** — MWL engine done, task lists pending (⬜)
- **31 Clinical Protocols Library · 35 Chat & Collaboration · 36 Teaching Files** (⬜)
- **34 Dictation bridge** — reporting + ORU^R01 done (E2); voice/dictation pending (🟡)
- **43 Lua/Python Scripting · 47 Load Balancing · 48 HA · 49 DR Replication** (⬜)

> Note: items previously listed here as partial (MWL, DICOM TLS, MinIO/S3, Smart Router)
> are now **✅ Done** — see the matrix above.

---

## Phase 4: Production Hardening (Weeks 1–4)

### 4.1 Database Migrations & MySQL Verification — ✅ DONE

| Task | Effort | Priority | Status |
|------|--------|----------|--------|
| Initialize Alembic, baseline schema | Low | Critical | ✅ Done |
| Initial migration from models | Low | Critical | ✅ Done |
| MySQL integration tests (aiomysql + pymysql) | Medium | Critical | ✅ Done (67 tests, both engines) |
| Connection pooling tuning (pool_size, max_overflow) | Low | High | ⬜ Tune for production load |

### 4.2 Security Hardening — 🟡 PARTIAL

| Task | Effort | Priority | Status |
|------|--------|----------|--------|
| DICOM TLS (receiver cert management) | Medium | Critical | ⬜ Config exists only |
| Rate limiting | Low | High | ✅ Done (sliding window, per-endpoint + middleware) |
| CORS hardening (explicit origins) | Low | High | ⬜ Wildcard for LAN viewers today |
| Security headers (CSP, HSTS, X-Frame-Options) | Low | Medium | ⬜ |
| Audit log enhancements (rotation, SIEM) | Medium | High | ⬜ Structured logging next |

### 4.3 Storage Tiering (Smart Tiered Storage)

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| StorageBackend protocol (Local/MinIO/S3) | Medium | Critical | Abstract read/write behind one interface |
| MinIO/S3 implementation | Medium | Critical | S3-compatible storage + presigned URLs |
| Tiering policy engine (hot/warm/cold) | High | High | Age/access-pattern based migration |
| Lifecycle management UI | Medium | Medium | Dashboard for storage tiers |

---

## Phase 5: Clinical Workflow Features (Weeks 5–10)

### 5.1 Modality Worklist (MWL) — High Clinical Value

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Worklist SOP Class C-FIND SCP | Medium | Critical | Implement 1.2.840.10008.5.1.4.31 |
| Scheduled Procedure Step query | Medium | Critical | By Modality, Date, Station AE Title |
| Patient demographics integration | Medium | High | Pre-populate from HL7 ADT (listener exists) |
| MWL dashboard | Low | Medium | Web UI for worklist entries |

### 5.2 Zero-CD Patient Sharing Enhancements

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| PWA for offline viewing | High | High | Service Worker + IndexedDB for offline DICOM |
| SMS/WhatsApp share delivery | Medium | Medium | Twilio/MessageBird |
| OTP authentication for shares | Medium | High | TOTP or SMS OTP |
| Access analytics | Low | Medium | Views, downloads, geography |

### 5.3 Smart DICOM Router

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Rules engine (YAML/JSON) | High | High | Modality / body part / size → destination |
| C-STORE SCU sender with queue | Medium | High | Retry, dead-letter handling |
| Routing dashboard | Medium | Medium | Visual rule builder + logs |

---

## Phase 6: AI & Advanced Features (Weeks 11–20)

### 6.1 AI Emergency Triage (MONAI) — 🟡 PIPELINE DONE, MODELS NEXT

| Task | Effort | Priority | Status |
|------|--------|----------|--------|
| Pluggable detector pipeline | Medium | Innovative | ✅ Done (heuristic pneumothorax detector) |
| Real MONAI model serving | High | Innovative | ⬜ TorchServe/Triton (Stroke, ICH) |
| Inference pipeline in worker | High | Innovative | ✅ Done (claim-then-run, exactly-once) |
| Alert system (Telegram/Slack/PagerDuty) | Medium | Innovative | ⬜ |
| Radiologist feedback loop | Medium | Medium | ⬜ Accept/reject + retraining data |

### 6.2 Real-Time Collaborative Viewer

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| WebSocket + Redis pub/sub | High | Innovative | Multi-user viewport sync |
| Shared viewport state | High | Innovative | Zoom, pan, W/L, measurements, annotations |
| Presence & cursors | Medium | Medium | Connected users, active tools |
| Voice/video (WebRTC) | High | Medium | |

### 6.3 Voice-to-Report (Whisper Integration)

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Local Whisper (faster-whisper / whisper.cpp) | Medium | Innovative | Offline transcription |
| Radiology vocabulary fine-tuning | High | Innovative | Domain adaptation |
| Structured report → DICOM SR | Medium | High | Map speech to SR templates |
| Viewer push-to-talk | Low | Medium | |

---

## Phase 7: Interoperability & Standards (Weeks 15–25)

### 7.1 HL7/FHIR Gateway — 🟡 READ PATH DONE

| Task | Effort | Priority | Status |
|------|--------|----------|--------|
| HL7 v2.x ADT listener (MLLP) | High | High | ✅ Done (ADT^A01/A04/A08/A31, patient upsert) |
| FHIR R4 read resources | High | High | ✅ Done (Patient, ImagingStudy, CapabilityStatement) |
| Write/sync path (orders → MWL, reports → EMR) | High | High | ⬜ Bidirectional |
| Integration testing (OpenEMR, Epic sandbox) | Medium | Medium | ⬜ |

### 7.2 Automated Anonymization Pipeline — 🟡 EXPORT DONE

| Task | Effort | Priority | Status |
|------|--------|----------|--------|
| DICOM tag anonymization (PS3.15) | Medium | High | ✅ Done (ZIP export endpoint) |
| Pixel data burning (OCR/CV PHI removal) | High | High | ⬜ |
| Research cohort export (DICOMDIR) | Medium | Medium | ⬜ Cohort builder |
| Anonymization audit trail | Low | Medium | ✅ Done (audit-logged) |

---

## Phase 8: Distribution & Edge (Weeks 20–30)

### 8.1 Windows EXE Distribution — 🟡 SCRIPTS DONE

| Task | Effort | Priority | Status |
|------|--------|----------|--------|
| PyInstaller build (spec + batch) | Medium | Critical | ✅ Done (scripts/build_exe.bat) |
| NSIS branded installer | Medium | High | ⬜ |
| Auto-update mechanism | High | Medium | ⬜ |
| Code signing (EV cert) | Low | High | ⬜ |

### 8.2 Edge PACS (Offline-First)

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Offline storage & sync queue | High | Innovative | Local SQLite + background sync |
| Bandwidth-aware compression | Medium | Innovative | Adaptive quality |
| Conflict resolution | High | Innovative | Merge strategies |
| Remote management | Medium | Medium | SSH tunnel / VPN |

---

## Phase 9: Platform & Ecosystem (Ongoing)

### 9.1 Plugin Architecture

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Plugin API spec (entry points, hooks) | Medium | Medium | |
| Example plugins | Low | Medium | Compression, AI models, exports |
| Plugin marketplace | High | Medium | Discovery, install, versioning |

### 9.2 Observability & Operations

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| OpenTelemetry (traces/metrics/logs) | Medium | High | Jaeger/Prometheus/Grafana |
| Health / readiness probes | Low | High | K8s liveness/readiness |
| Backup/restore automation | Medium | High | Scheduled DB + storage backups |
| Disaster recovery runbook | Low | Medium | RTO/RPO targets |

---

## Implementation Priority Matrix

```
                  HIGH IMPACT
                        │
     MWL (5.1)          │          AI Triage models (6.1)
     Storage Tiering    │          HL7/FHIR write-path (7.1)
     (4.3)              │          Collaborative viewer (6.2)
                        │
  LOW EFFORT ───────────┼─────────── HIGH EFFORT
                        │
     Security headers   │          Voice-to-report (6.3)
     CORS hardening     │          Edge PACS (8.2)
     DICOM TLS (4.2)    │          Plugin architecture (9.1)
                        │
                  LOW IMPACT
```

---

## Technical Debt & Foundations (Do First)

| Item | Effort | Why |
|------|--------|-----|
| Type-hint coverage | Low | Enable mypy strict mode |
| Test coverage > 80% | Medium | Current ~67 tests; safety net for refactors |
| API versioning strategy | Low | Prepare for v2 breaking changes |
| OpenAPI spec polish | Low | Better client generation, examples |
| Dependency updates | Low | Regular security patches |
| Documentation overhaul | Medium | User guides, API reference, diagrams |

---

## Quick Wins (1–2 weeks each)

1. ~~Alembic + MySQL verification~~ — ✅ done
2. ~~Rate limiting + security headers~~ — ✅ rate limiting done; add headers
3. **MinIO/S3 backend** — enables cloud deployment
4. **MWL SCP completion** — high clinical value, partially implemented
5. **PWA for patient sharing** — differentiator, works offline
6. **OpenTelemetry** — observability foundation for everything else

---

## Suggested Repository Structure for New Features

```
feature-plans/
├── NEW_FEATURES_PLAN.md          # This file
├── phase-4-production/
│   ├── 01-alembic-migrations.md
│   ├── 02-dicom-tls.md
│   ├── 03-minio-s3-backend.md
│   └── 04-security-hardening.md
├── phase-5-clinical/
│   ├── 01-modality-worklist.md
│   ├── 02-zero-cd-sharing.md
│   └── 03-smart-router.md
├── phase-6-ai/
│   ├── 01-ai-triage.md
│   ├── 02-collaborative-viewer.md
│   └── 03-voice-to-report.md
├── phase-7-interop/
│   ├── 01-hl7-fhir-gateway.md
│   └── 02-anonymizer.md
├── phase-8-distribution/
│   ├── 01-windows-exe.md
│   └── 02-edge-pacs.md
└── phase-9-platform/
    ├── 01-plugin-architecture.md
    └── 02-observability.md
```

---

## Recommended Next Steps

### Week 1–2: Foundation
1. ~~Alembic + MySQL~~ — ✅ done
2. ~~Rate limiting~~ — ✅ done
3. Implement MinIO/S3 storage backend (largest remaining Phase-4 gap)
4. DICOM TLS + security headers

### Week 3–4: Clinical Core
1. Complete Modality Worklist SCP (MWL)
2. Enhance patient sharing with PWA + OTP
3. Add OpenTelemetry instrumentation

### Week 5–8: AI & Collaboration
1. Swap a real MONAI model into the triage `DETECTORS` pipeline
2. Design WebSocket architecture for the collaborative viewer
3. Integrate faster-whisper for voice dictation

### Week 9–12: Interoperability
1. HL7/FHIR **write path**: orders → MWL, reports → EMR
2. Cohort builder + DICOMDIR anonymized export

### Week 13+: Distribution
1. Optimize PyInstaller build (binary size, codecs)
2. NSIS installer + auto-update
3. Edge PACS prototype

---

## Resource Estimation

| Phase | Duration | Engineers | Key Dependencies |
|-------|----------|-----------|------------------|
| 4: Production | 4 weeks | 2 | MinIO, certificates |
| 5: Clinical | 6 weeks | 2–3 | HL7 test tools, Twilio |
| 6: AI | 10 weeks | 3–4 | GPU, MONAI expertise |
| 7: Interop | 10 weeks | 2–3 | FHIR server, EMR access |
| 8: Distribution | 10 weeks | 2 | Windows CI, code signing |
| 9: Platform | Ongoing | 1–2 | Community feedback |

Total: ~40 weeks for the full roadmap with 3–4 engineers.

---

## Skills & Learning Resources Needed

| Area | Resources |
|------|-----------|
| MONAI / Medical AI | MONAI tutorials, RSNA AI challenges |
| HL7 / FHIR | HL7.org, Firely FHIR server, SMART on FHIR |
| DICOM Advanced | PS3.15 (Security), PS3.18 (Web), PS3.17 (Worklist) |
| WebRTC / WebSockets | MDN WebRTC, FastAPI WebSocket docs |
| Windows Packaging | PyInstaller docs, NSIS scripting |
| Observability | OpenTelemetry Python, Grafana Labs tutorials |

---

Document created: 2026-08-05 · Based on PAXS Archive Server v0.1.0 codebase analysis
