<tool_call>
  <function=apply_patch>
  <parameter=input>
  *** Begin Patch
  *** Add File: D:\Paxs Archive\feature-plans\NEW_FEATURES_PLAN.md
  +# PAXS Archive Server - New Features Implementation Plan
  +>
  +> This document outlines new features that could be added to the PAXS Archive Server, organized by priority, effort, and business
  impact. Based on analysis of the current codebase (v0.1.0) and existing roadmap documents.
  +>
  +> ---
  +>
  +> ## Current State Assessment
  +>
  +> ### Already Implemented (Phase 1-3 Complete)
  +> - DICOM DIMSE Receiver (C-STORE, C-FIND, C-GET, C-ECHO)
  +> - DICOMweb API (QIDO-RS, WADO-RS, STOW-RS)
  +> - SQLite + SQLAlchemy ORM (Patient -> Study -> Series -> Instance)
  +> - File storage with UID-based hierarchy
  +> - Lossless compression (RLE, JPEG-LS, JPEG 2000, HTJ2K via optional deps)
  +> - Background worker (compression + thumbnails)
  +> - JWT Authentication + RBAC-ready roles
  +> - Patient sharing with QR codes + OTP-ready tokens
  +> - Web Dashboard + OHIF-compatible viewer
  +> - SHA-256 audit logging
  +> - Docker-ready configuration
  +>
  +> ### Partially Implemented / Needs Enhancement
  +> - MySQL/Production database (config exists, not verified)
  +> - Alembic migrations (not initialized)
  +> - DICOM TLS (config option only)
  +> - MinIO/S3 storage backend (interface exists, no implementation)
  +> - Multi-frame DICOM support (parser only)
  +> - Modality Worklist SCP (finder.py exists, incomplete)
  +>
  +> ---
  +>
  +> ## Phase 4: Production Hardening (Weeks 1-4)
  +>
  +> ### 4.1 Database Migrations & MySQL Verification
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | Initialize Alembic | Low | Critical | Create migration environment, baseline current schema |
  +> | Create initial migration | Low | Critical | Auto-generate from SQLAlchemy models |
  +> | MySQL integration tests | Medium | Critical | Verify async MySQL (aiomysql) + sync worker (pymysql) |
  +> | Connection pooling tuning | Low | High | Configure pool_size, max_overflow for production loads |
  +>
  +> ### 4.2 Security Hardening
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | DICOM TLS support | Medium | Critical | Add TLS config to receiver.py, certificate management |
  +> | Rate limiting | Low | High | Add slowapi/FastAPI rate limiter to API endpoints |
  +> | CORS hardening | Low | High | Replace wildcard with explicit origins for production |
  +> | Security headers | Low | Medium | Add CSP, HSTS, X-Frame-Options via middleware |
  +> | Audit log enhancements | Medium | High | Structured logging, log rotation, SIEM integration |
  +>
  +> ### 4.3 Storage Tiering (Smart Tiered Storage)
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | Storage backend abstraction | Medium | Critical | Implement StorageBackend protocol (Local/MinIO/S3) |
  +> | MinIO/S3 implementation | Medium | Critical | S3-compatible storage with presigned URLs |
  +> | Tiering policy engine | High | High | Hot-Warm-Cold migration based on age/access patterns |
  +> | Lifecycle management UI | Medium | Medium | Dashboard for viewing/managing storage tiers |
  +>
  +> ---
  +>
  +> ## Phase 5: Clinical Workflow Features (Weeks 5-10)
  +>
  +> ### 5.1 Modality Worklist (MWL) - High Clinical Value
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | Complete C-FIND SCP for MWL | Medium | Critical | Implement Worklist SOP Class (1.2.840.10008.5.1.4.31) |
  +> | Scheduled Procedure Step query | Medium | Critical | Query by Modality, Date, Station AE Title |
  +> | Patient demographics integration | Medium | High | Pre-populate from HL7 ADT or manual entry |
  +> | MWL dashboard | Low | Medium | Web UI for viewing/managing worklist entries |
  +>
  +> ### 5.2 Zero-CD Patient Sharing Enhancements
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | PWA for offline viewing | High | High | Service Worker + IndexedDB for offline DICOM access |
  +> | SMS/WhatsApp integration | Medium | Medium | Twilio/MessageBird for share link delivery |
  +> | OTP authentication | Medium | High | TOTP or SMS OTP for patient access |
  +> | Access analytics | Low | Medium | Track views, downloads, geographic data |
  +>
  +> ### 5.3 Smart DICOM Router
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | Rules engine | High | High | YAML/JSON rule definition (modality, size, body part to destination) |
  +> | C-STORE SCU sender | Medium | High | Async sender with retry, queue, dead-letter handling |
  +> | Routing dashboard | Medium | Medium | Visual rule builder, routing logs, monitoring |
  +>
  +> ---
  +>
  +> ## Phase 6: AI & Advanced Features (Weeks 11-20)
  +>
  +> ### 6.1 AI Emergency Triage (MONAI Integration)
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | MONAI model serving | High | Innovative | TorchServe/Triton for Stroke, Pneumothorax, ICH detection |
  +> | Inference pipeline | High | Innovative | Celery task: receive - preprocess - infer - prioritize worklist |
  +> | Alert system | Medium | Innovative | Push notifications (Telegram, Slack, PagerDuty) for critical findings |
  +> | Radiologist feedback loop | Medium | Medium | Accept/reject AI findings, retraining data collection |
  +>
  +> ### 6.2 Real-Time Collaborative Viewer
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | WebSocket infrastructure | High | Innovative | FastAPI WebSocket + Redis pub/sub for multi-user sync |
  +> | Shared viewport state | High | Innovative | Sync zoom, pan, W/L, measurements, annotations in real-time |
  +> | Presence & cursors | Medium | Medium | Show connected users, mouse positions, active tools |
  +> | Voice/video integration | High | Medium | WebRTC for audio/video during collaboration |
  +>
  +> ### 6.3 Voice-to-Report (Whisper Integration)
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | Local Whisper deployment | Medium | Innovative | Faster-Whisper or whisper.cpp for offline transcription |
  +> | Medical vocabulary fine-tuning | High | Innovative | Domain adaptation for radiology terminology |
  +> | Structured report generation | Medium | High | Map speech to DICOM SR (Structured Report) templates |
  +> | Viewer microphone integration | Low | Medium | Push-to-talk in OHIF/PWA viewer |
  +>
  +> ---
  +>
  +> ## Phase 7: Interoperability & Standards (Weeks 15-25)
  +>
  +> ### 7.1 HL7/FHIR Gateway
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | HL7 v2.x ADT listener | High | High | MLLP listener for patient admit/discharge/transfer |
  +> | FHIR R4 resources | High | High | Patient, Observation, ImagingStudy, DiagnosticReport |
  +> | Bidirectional sync | High | High | PACS to EMR: orders to MWL, reports to EMR |
  +> | Integration testing | Medium | Medium | Test with OpenEMR, Epic FHIR sandbox, etc. |
  +>
  +> ### 7.2 Automated Anonymization Pipeline
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | DICOM tag anonymization | Medium | High | Basic Profile (PS3.15) + custom rules |
  +> | Pixel data burning | High | High | Burned-in PHI detection/removal (OCR + CV) |
  +> | Research cohort export | Medium | Medium | Cohort builder + anonymized export (ZIP/DICOMDIR) |
  +> | Audit trail | Low | Medium | Immutable log of what was anonymized |
  +>
  +> ---
  +>
  +> ## Phase 8: Distribution & Edge (Weeks 20-30)
  +>
  +> ### 8.1 Windows EXE Distribution
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | PyInstaller build optimization | Medium | Critical | Reduce binary size, handle imagecodecs/pynetdicom |
  +> | NSIS installer | Medium | High | Branded installer with service registration |
  +> | Auto-update mechanism | High | Medium | Check for updates, download, restart service |
  +> | Code signing | Low | High | EV certificate for Windows SmartScreen |
  +>
  +> ### 8.2 Edge PACS (Offline-First)
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | Offline storage & queue | High | Innovative | Local SQLite + background sync queue |
  +> | Bandwidth-aware compression | Medium | Innovative | Adaptive quality based on connection |
  +> | Conflict resolution | High | Innovative | Merge strategies for disconnected edits |
  +> | Remote management | Medium | Medium | SSH tunnel / VPN for edge device management |
  +>
  +> ---
  +>
  +> ## Phase 9: Platform & Ecosystem (Ongoing)
  +>
  +> ### 9.1 Plugin Architecture
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | Plugin API specification | Medium | Medium | Entry points, hooks, configuration schema |
  +> | Example plugins | Low | Medium | Custom compression, AI models, export formats |
  +> | Plugin marketplace | High | Medium | Discovery, installation, versioning |
  +>
  +> ### 9.2 Observability & Operations
  +> | Task | Effort | Priority | Description |
  +> |------|--------|----------|-------------|
  +> | OpenTelemetry integration | Medium | High | Traces, metrics, logs to Jaeger/Prometheus/Grafana |
  +> | Health checks / readiness | Low | High | Kubernetes liveness/readiness probes |
  +> | Backup/restore automation | Medium | High | Scheduled DB + storage backups, point-in-time recovery |
  +> | Disaster recovery runbook | Low | Medium | Documented procedures, RTO/RPO targets |
  +>
  +> ---
  +>
  +> ## Implementation Priority Matrix
  +>
  +> +>                     HIGH IMPACT +>                          | +>     MWL (5.1)           |    AI Triage (6.1) +>     MySQL/
  Alembic (4.1) |    HL7/FHIR (7.1) +>     Storage Tiering (4.3)|   Collaborative (6.2) +>                          | +> LOW EFFORT
  ------------+------------ HIGH EFFORT +>                          | +>     Rate Limit (4.2)    |    Voice Report (6.3) +>
  Security Headers    |    Edge PACS (8.2) +>     CORS Hardening      |    Plugin Arch (9.1) +>                          | +>
  LOW IMPACT +>
  +>
  +> ---
  +>
  +> ## Technical Debt & Foundations (Do First)
  +>
  +> | Item | Effort | Why |
  +> |------|--------|-----|
  +> | Type hints coverage | Low | Enable mypy strict mode, catch bugs early |
  +> | Test coverage >80% | Medium | Current ~60%, critical for refactoring safety |
  +> | API versioning strategy | Low | Prepare for v2 breaking changes |
  +> | OpenAPI spec enhancements | Low | Better client generation, examples |
  +> | Dependency updates | Low | Regular security patches (Dependabot) |
  +> | Documentation overhaul | Medium | User guides, API reference, architecture diagrams |
  +>
  +> ---
  +>
  +> ## Quick Wins (1-2 weeks each)
  +>
  +> 1. Alembic + MySQL verification - Unblocks production deployment
  +> 2. Rate limiting + security headers - Immediate security improvement
  +> 3. MinIO/S3 backend - Enables cloud deployment
  +> 4. MWL SCP completion - High clinical value, mostly implemented
  +> 5. PWA for patient sharing - Differentiator, works offline
  +> 6. OpenTelemetry - Observability foundation for all other features
  +>
  +> ---
  +>
  +> ## Suggested Repository Structure for New Features
  +>
  +> +> feature-plans/ +> ├── NEW_FEATURES_PLAN.md          # This file +> ├── phase-4-production/ +> │   ├── 01-alembic-
  migrations.md +> │   ├── 02-dicom-tls.md +> │   ├── 03-minio-s3-backend.md +> │   └── 04-security-hardening.md +> ├── phase-5-
  clinical/ +> │   ├── 01-modality-worklist.md +> │   ├── 02-zero-cd-sharing.md +> │   └── 03-smart-router.md +> ├── phase-6-ai/ +> │
  ├── 01-ai-triage.md +> │   ├── 02-collaborative-viewer.md +> │   └── 03-voice-to-report.md +> ├── phase-7-interop/ +> │   ├── 01-
  hl7-fhir-gateway.md +> │   └── 02-anonymizer.md +> ├── phase-8-distribution/ +> │   ├── 01-windows-exe.md +> │   └── 02-edge-
  pacs.md +> └── phase-9-platform/ +>     ├── 01-plugin-architecture.md +>     └── 02-observability.md +>
  +>
  +> ---
  +>
  +> ## Recommended Next Steps
  +>
  +> ### Week 1-2: Foundation
  +> 1. Initialize Alembic, create baseline migration
  +> 2. Set up MySQL test container (docker-compose)
  +> 3. Add rate limiting middleware
  +> 4. Implement MinIO/S3 storage backend
  +>
  +> ### Week 3-4: Clinical Core
  +> 1. Complete Modality Worklist SCP
  +> 2. Enhance patient sharing with PWA + OTP
  +> 3. Add OpenTelemetry instrumentation
  +>
  +> ### Week 5-8: AI & Collaboration
  +> 1. Prototype MONAI inference pipeline (Stroke detection)
  +> 2. Design WebSocket architecture for collaborative viewer
  +> 3. Integrate faster-whisper for voice dictation
  +>
  +> ### Week 9-12: Interoperability
  +> 1. Build HL7 ADT listener + FHIR mapper
  +> 2. Implement DICOM anonymization pipeline
  +>
  +> ### Week 13+: Distribution
  +> 1. Optimize PyInstaller build
  +> 2. Create NSIS installer
  +> 3. Edge PACS prototype
  +>
  +> ---
  +>
  +> ## Resource Estimation
  +>
  +> | Phase | Duration | Engineers | Key Dependencies |
  +> |-------|----------|-----------|------------------|
  +> | 4: Production | 4 weeks | 2 | MySQL, MinIO, Certificates |
  +> | 5: Clinical | 6 weeks | 2-3 | HL7 test tools, Twilio |
  +> | 6: AI | 10 weeks | 3-4 | GPU, MONAI expertise |
  +> | 7: Interop | 10 weeks | 2-3 | FHIR server, EMR access |
  +> | 8: Distribution | 10 weeks | 2 | Windows CI, Code signing |
  +> | 9: Platform | Ongoing | 1-2 | Community feedback |
  +>
  +> Total: ~40 weeks for full roadmap with 3-4 engineers
  +>
  +> ---
  +>
  +> ## Skills & Learning Resources Needed
  +>
  +> | Area | Resources |
  +> |------|-----------|
  +> | MONAI / Medical AI | MONAI tutorials, RSNA AI challenges |
  +> | HL7/FHIR | HL7.org, Firely FHIR server, SMART on FHIR |
  +> | DICOM Advanced | PS3.15 (Security), PS3.18 (Web), PS3.17 (Worklist) |
  +> | WebRTC / WebSockets | MDN WebRTC, FastAPI WebSocket docs |
  +> | Windows Packaging | PyInstaller docs, NSIS scripting |
  +> | Observability | OpenTelemetry Python, Grafana Labs tutorials |
  +>
  +> ---
  +>
  +> Document created: 2026-08-05
  +> Based on PAXS Archive Server v0.1.0 codebase analysis
  *** End Patch
  </parameter>
  </function>
  </tool_call>











  
  ### Summary Table

   Category                          Count    Critical    High    Medium    Low
  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━  ━━━━━━━━━━  ━━━━━━  ━━━━━━━━  ━━━━━
   Core DICOM                         10         8         2        0        0
  ────────────────────────────────  ───────  ──────────  ──────  ────────  ─────
   Storage & Archive                   9         3         4        2        0
  ────────────────────────────────  ───────  ──────────  ──────  ────────  ─────
   Viewer & Visualization             10         4         4        2        0
  ────────────────────────────────  ───────  ──────────  ──────  ────────  ─────
   Workflow & Clinical                 8         2         4        2        0
  ────────────────────────────────  ───────  ──────────  ──────  ────────  ─────
   Integration & Interoperability      7         2         4        1        0
  ────────────────────────────────  ───────  ──────────  ──────  ────────  ─────
   Administration & Architecture       8         3         3        2        0
  ────────────────────────────────  ───────  ──────────  ──────  ────────  ─────
   Total                              52         22        21       9        0

  ———
start work on 1. Core DICOM Services on 10: DICOM TLS  
  ### 1. Core DICOM Services (10)

   #      Feature                                     Priority    Status
  ━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━  ━━━━━━━━━
   1      C-STORE (SCP/SCU)                           Critical    ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   2      C-FIND (SCP/SCU)                            Critical    ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   3      C-MOVE (SCP/SCU)                            Critical    ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   4      C-GET (SCP/SCU)                             Critical    ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   5      C-ECHO (SCP/SCU)                            Critical    ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   6      Storage Commitment (N-ACTION/N-EVENT)       Critical    ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   7      Modality Worklist (MWL)                     Critical    ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   8      MPPS (Modality Performed Procedure Step)    Critical    ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   9      IAN (Instance Availability Notification)    High        ✅ Done
  ─────  ──────────────────────────────────────────  ──────────  ─────────
   10     DICOM TLS                                   High        ✅ Done

  ———
   2. Storage & Archive 19: Transfer Accelerator (HTTP-based fast transfer)
  ### 2. Storage & Archive (9)

   #      Feature                                                      Priority    Status
  ━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━  ━━━━━━━━━
   11     Lossless Compression (JPEG-LS, JPEG2000, RLE)                Critical    ✅ Done
  ─────  ───────────────────────────────────────────────────────────  ──────────  ─────────
   12     Lossy Compression (configurable)                             Medium      ✅ Done
  ─────  ───────────────────────────────────────────────────────────  ──────────  ─────────
   13     VNA (Vendor Neutral Archive)                                 High        ✅ Done
  ─────  ───────────────────────────────────────────────────────────  ──────────  ─────────
   14     Study Lifecycle Management (auto-archive, restore, purge)    High        ✅ Done
  ─────  ───────────────────────────────────────────────────────────  ──────────  ─────────
   15     Auto-routing Rules Engine                                    High        ✅ Done
  ─────  ───────────────────────────────────────────────────────────  ──────────  ─────────
   16     Prefetching (auto-retrieve priors)                           High        ✅ Done
  ─────  ───────────────────────────────────────────────────────────  ──────────  ─────────
   17     DICOMDIR Creation (CD/DVD/USB export)                        Medium      ✅ Done
  ─────  ───────────────────────────────────────────────────────────  ──────────  ─────────
   18     Migration Tools (folder watcher + C-MOVE retriever)          Critical    ✅ Done
  ─────  ───────────────────────────────────────────────────────────  ──────────  ─────────
   19     Transfer Accelerator (HTTP-based fast transfer)              Critical    ✅ Done

  ———

  ### 3. Viewer & Visualization 26     2D MPR Backend Prep (volume streaming)

   #      Feature                                               Priority    Status
  ━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━  ━━━━━━━━━
   20     WADO-URI                                              Critical    ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   21     WADO-RS / QIDO-RS / STOW-RS (DICOMweb)                Critical    ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   22     Hanging Protocols (viewport layouts, W/L presets)     High        ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   23     Prior Study Linking (relevance scoring)               Critical    ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   24     GSPS (Presentation State) (annotations, zoom, pan)    High        ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   25     Key Image Notes (KIN)                                 High        ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   26     2D MPR Backend Prep (volume streaming)                Medium      ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   27     3D VRT / MIP Support (3D rendering data)              Medium      ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   28     Dose Monitoring (RDSR)                                High        ✅ Done
  ─────  ────────────────────────────────────────────────────  ──────────  ─────────
   29     Fusion (PET/CT, MRI/US) (Frame of Reference)          Critical    ✅ Done

  ———

  ### 4. Workflow & Clinical (8) 30: Worklist Management (Unread, Stat, Dictated)

   #      Feature                                                Priority    Status
  ━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━  ━━━━━━━━━
   30     Worklist Management (Unread, Stat, Dictated)           Critical    ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   31     Clinical Protocols Library                             High        ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   32     Peer Review / QA (RADPEER scoring)                     High        ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   33     Critical Results Tracking (acknowledgement + audit)    High        ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   34     Reporting / Dictation Bridge                           Critical    ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   35     Chat & Collaboration (secure messaging)                Medium      Planned
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   36     Teaching Files (anonymized export)                     Medium      Planned
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   37     Study Split/Merge (fix technologist errors)            High        ✅ Done

  ———

  ### 5. Integration & Interoperability (7)

   #      Feature                                           Priority    Status
  ━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━
   38     HL7 v2.x ORU/ORM/ADT (MLLP)                       Critical    ✅ Done
  ─────  ────────────────────────────────────────────────  ──────────  ──────────────────────────
   39     LDAP / Active Directory (auth + provisioning)     Critical    ✅ Done
  ─────  ────────────────────────────────────────────────  ──────────  ──────────────────────────
   40     XDS-I (Cross-Enterprise Document Sharing)         High        ✅ Done
  ─────  ────────────────────────────────────────────────  ──────────  ──────────────────────────
   41     REST API (General)                                High        ✅ Done (FastAPI native)
  ─────  ────────────────────────────────────────────────  ──────────  ──────────────────────────
   42     Webhooks (StudyReceived events)                   High        ✅ Done
  ─────  ────────────────────────────────────────────────  ──────────  ──────────────────────────
   43     Lua / Python Scripting (custom server scripts)    High        ✅ Done
  ─────  ────────────────────────────────────────────────  ──────────  ──────────────────────────
   44     FHIR ImagingStudy mapping                         Medium      ✅ Done

  ———

  ### 6. Administration & Architecture (8)  47     Load Balancing (HAProxy + stateless nodes) 

   #      Feature                                                Priority    Status
  ━━━━━  ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━  ━━━━━━━━━━  ━━━━━━━━━
   45     Audit Logging (ATNA) (RFC 3881)                        Critical    ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   46     Role-Based Access Control                              Critical    ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   47     Load Balancing (HAProxy + stateless nodes)             Critical    ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   48     High Availability / Clustering (K8s/Docker Swarm)      High        Planned
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   49     Disaster Recovery Replication (off-site S3/Blob)       High        ✅ Done
  ─────  ─────────────────────────────────────────────────────  ──────────  ─────────
   52     Storage Tiering (HSM) (Online/Nearline/Offline)        Medium      Planned

  ———

  - ✅ Done: 17 features (C-MOVE, Storage Commitment, MWL, IAN, Lossy Compression, Auto-routing, Prefetching, DICOMDIR,
    Migration Tools, WADO-URI, WADO-RS, Hanging Protocols, Prior Linking, GSPS, KIN, Dose Monitoring, Peer Review,
    Critical Results, LDAP, XDS-I, REST API, Webhooks, Study Split/Merge = 23 actually)


    and add postgresql database configuration and full support if user want to use it let  the user chose one mysql or postgresql

    Start work on 4. Workflow & Clinical on number 34. Reporting / Dictation Bridge

    I want to integrate the PaxsViewer project located on dir "D:\Github" into this paxs archive project as viewer for the dicom images and remove ohif integration and set this new one, It's uses same features of ohif

    I want to build all the project into standalone windows application EXE and with a tools to check it's healthy and working status and also use the terms.md and logo images file for application icon in the build and create new folder for build the project