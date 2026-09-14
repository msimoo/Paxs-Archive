# 🏥 PAXS Archive Server - Implementation Checkpoint

> ✅ **محدّث (2026-08-06)** — الأرقام أدناه تعكس الواقع الحالي بعد اكتمال Core DICOM 1–10 و معلم **E1 (HL7 ORM → MWL)** و **E2 (تقارير → EMR عبر ORU)** و **بند 14 (Study Lifecycle Management)** و **DICOMweb للعقد البعيدة** و **auto-archive للدراسات المسحوبة عبر WADO-RS** و **بند 19 (Transfer Accelerator — نقل HTTP متوازي بتشفير chunked)**.
> **317 اختباراً ناجحاً على SQLite** و MySQL subsets خضراء (Interop 43/43 + Accelerator 10/10)، lint نظيف.
> **`MASTER-ROADMAP.md`** هو مصدر الحقيقة للمعالم والتفاصيل.

**Project Version:** v0.1.0
**Last Updated:** 2026-08-06
**Overall Progress:** ~60% ▓▓▓▓▓▓░░░░ — كل Core DICOM (10/10) مكتمل، والمراحل A/B مغلقة

---

## 📋 52-Feature Status Matrix (canonical — verified 2026-08-06)

> Legend: ✅ Done · 🟡 Partial · ⬜ Planned (not yet) — **41 ✅ · 1 🟡 · 10 ⬜**.
> Details per feature below and in `plan.md`.

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

## 📊 Progress Summary Dashboard

| Category | Progress | % | Status |
| :--- | :--- | :--- | :--- |
| **Core DICOM Services** | ▓▓▓▓▓▓▓▓▓▓ | 100% | ✅ Done (1–10) |
| **Storage & Archive** | ▓▓▓▓▓▓▓▓░░ | 80% | ✅ Done (8/9 — #19 Transfer Accelerator now complete) |
| **Web Viewer & Visualization** | ▓▓▓▓▓▓░░░░ | 60% | 🔨 In Progress |
| **Clinical Workflow** | ▓▓▓▓▓░░░░░ | 55% | 🔨 In Progress |
| **Integration & Interoperability** | ▓▓▓▓▓▓▓▓░░ | 75% | 🔨 In Progress (E1 ORM + E2 ORU done) |
| **Security & Compliance** | ▓▓▓▓▓▓░░░░ | 62% | 🔨 In Progress |
| **Administration & Operations** | ▓▓▓▓░░░░░░ | 45% | 🔨 In Progress |
| **Mobile & Accessibility** | ▓▓░░░░░░░░ | 20% | 🔨 In Progress |
| **AI & Advanced Analytics** | ▓░░░░░░░░░ | 10% | 🔨 In Progress (Triage done) |

---

## 📋 Detailed Checkpoint Tables

### 1. Core DICOM Services — ✅ 100% (all 10 items + DICOMweb)

| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| C-STORE SCP | ✅ Done | 100% | Phase 1 | Receiver accepts + indexes + tenant-tags + storage-commit hook |
| C-STORE SCU | ✅ Done | 100% | Phase 1 | `send_file`/`send_to_node_sync` + node registry + S3 materialization |
| C-FIND SCP | ✅ Done | 100% | Phase 1 | 4 Q/R levels, wildcards, VNA-scoped, MWL routed per context |
| C-FIND SCU | ✅ Done | 100% | Phase 1 | `send_find` generic + `query_remote_node_sync` node-aware |
| C-MOVE SCP | ✅ Done | 100% | Phase 1 | Destination resolved from `dicom_nodes` registry |
| C-MOVE SCU | ✅ Done | 100% | Phase 1 | `send_move` + migration/prefetch reuse |
| C-GET SCP | ✅ Done | 100% | Phase 1 | `handle_get` sub-op count protocol, VNA-scoped |
| C-GET SCU | ✅ Done | 100% | Phase 1 | `send_get` + `pull_from_node_sync` (streaming ingest) |
| C-ECHO SCP | ✅ Done | 100% | Phase 1 | Verification context auto-handled |
| C-ECHO SCU | ✅ Done | 100% | Phase 1 | `send_echo` + REST `/ping` |
| Modality Worklist (MWL SCP) | ✅ Done | 100% | Phase 2 | HL7 ADT auto-open, C-STORE auto-close |
| MPPS SCP/SCU | ✅ Done | 100% | Phase 2 | N-CREATE/N-SET + worklist linking + SCU helpers |
| Storage Commitment SCP/SCU | ✅ Done | 100% | Phase 2 | N-ACTION → verify → N-EVENT-REPORT |
| IAN (N-CREATE) | ✅ Done | 100% | Phase 2 | SOP 1.2.840.10008.5.1.4.33 + dispatch |
| DICOM TLS | ✅ Done | 100% | Phase 1 | AES over TLS 1.2/1.3, mTLS via `DICOM_TLS_CA` |
| DICOMweb QIDO-RS | ✅ Done | 100% | Phase 1 | - |
| DICOMweb WADO-RS | ✅ Done | 100% | Phase 1 | - |
| DICOMweb STOW-RS | ✅ Done | 100% | Phase 1 | - |
| DICOMweb UPS-RS | ⏳ Backlog | 0% | Phase 4 | TBD |

### 2. Storage & Archive
| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| SQLite storage | ✅ Done | 100% | Phase 1 | For edge/embedded deployments |
| MySQL 8.0 support | ✅ Done | 100% | Phase 2 | Full suite green on MySQL (Alembic head) |
| File-based storage | ✅ Done | 100% | Phase 1 | DICOM part 10 format, UID hierarchy |
| MinIO / S3 object storage | ✅ Done | 100% | Phase 2 | Unified `StorageBackend`, presigned URLs |
| Transfer Accelerator (#19) | ✅ Done | 100% | Phase 2 | Parallel streamed STOW-RS HTTP upload: batches (default 16) over N concurrent connections (default 4) with chunked transfer encoding, whole-batch retry on transient failures, per-transfer stats (accelerator_service.py, `web/upload?accelerated=1`, ⚡ Fast Upload in the Nodes panel — tests/test_accelerator.py, 10 tests) |
| Lossless compression | ✅ Done | 100% | Phase 1 | JPEG-LS, JPEG2000 |
| Lossy compression (rules) | ✅ Done | 100% | Phase 2 | Auto-transcode aged studies, quality factor |
| Anonymization / De-identification | ✅ Done | 100% | Phase 2 | Profile-based tag scrubbing |
| Tiered Storage / ILM Rules (Study Lifecycle) | ✅ Done | 100% | Phase 2 | `study_lifecycle_rules` (ARCHIVE/RESTORE/PURGE بعمر/نمط/جزء) + أعمدة `studies.lifecycle_state`/`archived_at` + نقل كائنات عبر `StorageBackend.move` + مسح worker تلقائي + REST `/api/v1/lifecycle` + قسم Lifecycle في لوحة Storage + **auto-archive للدراسات المسحوبة عبر WADO-RS** — خانة "Tier to cold storage" في Web Retrieve (خيار `auto_archive`) + إعداد `LIFECYCLE_AUTO_ARCHIVE_WADO_PULL` (tests/test_lifecycle.py + tests/test_wado_auto_archive.py — 3 اختبارات جديدة) |
| Storage quota management | ⏳ Backlog | 0% | Phase 3 | - |

### 3. Web Viewer & Visualization
| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| Web dashboard | ✅ Done | 100% | Phase 1 | Feature cards + admin console + services matrix |
| Viewer (zero-footprint) | ✅ Done | 100% | Phase 1 | Series switching, window presets, zoom |
| Thumbnail generation | ✅ Done | 100% | Phase 1 | Async worker extraction |
| Hanging Protocols | ✅ Done | 100% | Phase 2 | - |
| Key Image Notes (KIN) | ✅ Done | 100% | Phase 2 | - |
| Presentation States (GSPS) | ✅ Done | 100% | Phase 2 | - |
| Dose Monitoring | ✅ Done | 100% | Phase 2 | - |
| Multi-Planar Reconstruction (MPR) | 🟡 Backend prep | 40% | Phase 4 | Volume geometry + raw voxel streaming endpoints done (#26, /api/v1/volume/*); client-side WebGL reslicing pending |
| 3D Volume Rendering | ⏳ Backlog | 0% | Phase 4 | High resource requirement |
| Segmentation Display (SEG) | ⏳ Backlog | 0% | Phase 4 | - |

### 4. Clinical Workflow
| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| Background worker | ✅ Done | 100% | Phase 1 | Job queue implemented |
| Auto-routing / Forwarding Rules (Smart Router) | ✅ Done | 100% | Phase 2 | Rule engine (modality/body part/source AE/description → node) + **durable send queue**: `routing_queue` (Alembic v1234567w8x9) + `enqueue_dispatch` idempotent + worker sweep with exponential-backoff retry (`retry_backoff × attempts²` حتى `max_attempts` ثم FAILED) + manual re-queue + stuck-item recovery + REST `/api/v1/routing/queue` (list/retry/run) + Send Queue panel + dashboard queue counters (tests/test_routing_queue.py — 8 اختبارات) |
| Prefetching (priors) | ✅ Done | 100% | Phase 2 | - |
| Prior linking | ✅ Done | 100% | Phase 2 | - |
| Critical Results Notification | ✅ Done | 100% | Phase 2 | Ack loop with referring physician |
| Peer Review Workflow | ✅ Done | 100% | Phase 2 | - |
| Emergency AI Triage | ✅ Done | 100% | Phase 2 | Pluggable detectors, demo heuristic |
| Study Assignment / Worklists | 📋 Planned | 0% | Phase 3 | UI for assignment |
| Report Generation (PDF) | 📋 Planned | 0% | Phase 3 | - |
| Emergency/Trauma Fast-track | ⏳ Backlog | 0% | Phase 4 | Priority queue needed |

### 5. Integration & Interoperability
| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| Patient sharing (QR) | ✅ Done | 100% | Phase 1 | Secure short-lived links |
| HL7 v2 (ADT + ORM + ORU) | ✅ Done | 100% | Phase 2 | MLLP listener · ADT A01/A04/A08/A31 auto-scheduling · **ORM^O01 أوامر الفحوصات → خطوات MWL** (NW/XO/CA/OC/DC/CR) · **ORU^R01 تقارير → EMR**: جدول `reports` (Alembic w1234567x8y9) + `report_service` (بناء ORU يدوي MSH/PID/PV1/ORC/OBR/OBX مع LOINC 50005-5/50122-5 وهروب HL7) + إرسال MLLP مع ACK + حالة تسليم (SENT/FAILED/PENDING) + إعادة إرسال + finalize مزدوج idempotent + REST `/api/v1/reports` (CRUD/finalize/resend ببوابة دور) (tests/test_oru.py — 9 اختبارات) |
| FHIR R4 | ✅ Done | 100% | Phase 2 | - |
| IHE XDS-I.b | ✅ Done | 100% | Phase 3 | - |
| Webhooks for events | ✅ Done | 100% | Phase 2 | - |
| RESTful Management API | ✅ Done | 100% | Phase 2 | OpenAPI-documented |
| DICOMweb for remote nodes (QIDO-RS Query / WADO-RS Retrieve / STOW-RS Upload) | ✅ Done | 100% | Phase 2 | SSRF-guarded `dicomweb_client` (loopback/private/link-local blocked by default + per-call re-check) · `POST /api/v1/nodes/{ae}/web/query` + `/web/retrieve` + `/web/upload` (destructive-role gate, `PULLED`/`FORWARDED` audit, VNA tagging) · **STOW-RS upload**: multipart/related to `{web_url}/studies`, FailedSOPSequence partial-failure parsing, temp-file body (no memory doubling), 409-as-success · **lifecycle tiering**: `auto_archive` per pull + global `LIFECYCLE_AUTO_ARCHIVE_WADO_PULL` → study lands in cold `.archive/` tier · node `web_url`/`web_username`/`web_password` (Alembic u1234567v8w9) · Web Query / Web Retrieve / Web Upload buttons in the Nodes panel (tests/test_web_nodes.py — 12 tests + tests/test_wado_auto_archive.py — 3 tests + tests/test_stow_upload.py — 9 tests) |
| IHE PIX/PDQ (Patient ID) | ⏳ Backlog | 0% | Phase 5 | - |

### 6. Security & Compliance
| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| JWT auth | ✅ Done | 100% | Phase 1 | Short expiration |
| RBAC roles | ✅ Done | 100% | Phase 1 | ADMIN / RADIOLOGIST / PHYSICIAN |
| SHA-256 audit log | ✅ Done | 100% | Phase 1 | Tamper-evident fingerprints |
| DICOM TLS / SSL | ✅ Done | 100% | Phase 1 | Self-signed bootstrap + mTLS |
| Rate limiting | ✅ Done | 100% | Phase 2 | Global per-minute limiter |
| LDAP / Active Directory | ✅ Done | 100% | Phase 2 | - |
| OIDC (OAuth 2.0) Support | 📋 Planned | 0% | Phase 3 | - |
| Data encryption at rest | 📋 Planned | 0% | Phase 2 | AES-256 |
| HIPAA/GDPR Compliance Reports | ⏳ Backlog | 0% | Phase 3 | Exportable audit trails |

### 7. Administration & Operations
| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| Docker / Compose config | ✅ Done | 100% | Phase 1 | MySQL 8.0 + receiver + worker |
| DICOM Node (AET) Management | ✅ Done | 100% | Phase 2 | Full CRUD + Ping/Find/Pull/Send |
| Admin Dashboard Analytics | ✅ Done | 100% | Phase 2 | Live feature cards, services matrix, audit, tenants |
| VNA Tenant management | ✅ Done | 100% | Phase 2 | Tenant isolation + AE mapping |
| Migration tools | ✅ Done | 100% | Phase 2 | Folder watcher + C-MOVE batch |
| System metrics (Prometheus/Grafana) | 📋 Planned | 0% | Phase 2 | Expose `/metrics` endpoint |
| Log aggregation (ELK/Loki) | 📋 Planned | 0% | Phase 3 | - |
| High Availability (HA) Clustering | ⏳ Backlog | 0% | Phase 5 | State replication needed |

### 8. Mobile & Accessibility
| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| Responsive Web UI | ✅ Done | 100% | Phase 2 | Mobile breakpoints + themes |
| Progressive Web App (PWA) | 📋 Planned | 0% | Phase 3 | Service workers |
| Push Notifications (iOS/Android) | ⏳ Backlog | 0% | Phase 4 | Requires app store presence |
| Mobile-optimized Viewer | 📋 Planned | 0% | Phase 3 | Touch gestures |

### 9. AI & Advanced Analytics
| Feature Name | Status | Completion % | Phase | Notes |
| :--- | :---: | :---: | :---: | :--- |
| Emergency Triage detectors | ✅ Done | 100% | Phase 2 | Pluggable pipeline, demo pneumothorax heuristic |
| AI Inference Pipeline / Gateway | ⏳ Backlog | 0% | Phase 5 | Send series to external AI |
| MONAI / Clara Integration | ⏳ Backlog | 0% | Phase 5 | DICOM orchestration |
| Result SR / PDF rendering | ⏳ Backlog | 0% | Phase 5 | Display AI results in viewer |
| NLP for Report Searching | ⏳ Backlog | 0% | Phase 5 | Semantic search |

---

## 🎯 Milestone Checkpoints

| Milestone | Description | Target | Status |
| :--- | :--- | :--- | :--- |
| **M1: Core PACS Parity** | Match standard open-source functionality (like basic Orthanc). Basic DICOM SCU/SCP, Web Viewer, Local Storage. | Q3 2026 | ✅ 100% |
| **M2: Enterprise Ready** | Ready for medium-scale clinics (like dcm4chee). S3 storage, Auto-routing, TLS, DICOM Node Management, MySQL. | Q4 2026 | 🔨 75% |
| **M3: Clinical Workflow** | Match systems like ClearCanvas. HL7, Worklists, Reporting, SSO, Advanced Routing. | Q1 2027 | 🔨 50% |
| **M4: Advanced Visualization** | Match Horos/OsiriX functionality in the web. MPR, 3D, KIN, Segmentation, Mobile Viewer. | Q3 2027 | 📋 15% |
| **M5: Full Platform** | Exceed open-source platforms. HA Clustering, IHE Profiles, AI pipelines. | 2028 | ❌ 0% |

---

## ⚠️ Risk Register

| Risk | Impact | Likelihood | Mitigation Strategy |
| :--- | :---: | :---: | :--- |
| **OHIF WebGL Performance** on large datasets (5000+ slices) | High | Medium | Implement aggressive volume streaming (WADO-RS streaming) and memory management. |
| **Database Scalability** with millions of instances | High | Low | MySQL 8.0 supported; add partitioning for large archives. |
| **HL7 Parsing Complexity** (vendor-specific variations) | Medium | High | Established parser + custom mapping scripts. |
| **Compliance/Security Breaches** (HIPAA) | Critical | Low | Regular automated pen-testing, enforce TLS 1.3, strict RBAC, default-deny network configs. |

---

## 🔗 Dependencies Tracker

| Component / Library | Usage | Version | Status |
| :--- | :--- | :--- | :--- |
| `pynetdicom` | Core DICOM networking stack (DIMSE, TLS) | Latest | ✅ Integrated |
| `OHIF Viewer` / custom viewer | Zero-footprint web visualization | v3.x | ✅ Integrated |
| `SQLite` / `MySQL 8.0` | Relational Metadata DB | v3.x / 8.0 | ✅ Integrated |
| `MinIO` SDK | S3-compatible Object Storage | Latest | ✅ Integrated |
| `python-hl7` | HL7 v2 parsing and generation | Latest | ✅ Integrated |
| `Docker` / `Docker Compose` | Containerization & Deployment | v24+ | ✅ Integrated |
| `Redis` | Job Queues / Caching (optional) | v7+ | 📋 Pending |
