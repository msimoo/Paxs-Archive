# Standard PACS Features Implementation Plan

This document catalogs features cloned from standard/industry PACS systems that should be implemented in the PAXS Archive Server. The technical stack for the implementation includes Python, FastAPI, and pynetdicom.

> **Progress** (2026-08-06): ✅ **C-MOVE (SCP/SCU)** — destination resolution via a
> dicom_nodes registry (GET/POST /api/v1/nodes, dashboard *Nodes* panel), real
> DIMSE sub-operations, send_move()/send_echo() SCU helpers. ✅ **C-ECHO (SCP/SCU)** — SCP auto-handled by pynetdicom (Verification SOP Class), SCU in sender.send_echo(), REST POST /api/v1/nodes/{ae}/ping. ✅ **WADO-URI** —
> GET /dicom-web/wado?requestType=WADO&... returning DICOM part-10 or rendered
> JPEG/PNG. ✅ **Webhooks** — WEBHOOK_URLS POSTs StudyReceived on new studies.
> ✅ **Modality Worklist (MWL SCP)** — C-FIND on shared port, HL7 ADT auto-scheduling, REST + dashboard.
> ✅ **Storage Commitment (N-ACTION/N-EVENT)** — async push model archive guarantee.
> ✅ **GSPS (Presentation State)** — parse & index VOI LUT, spatial transforms, graphic/text annotations, shutters; WADO-RS /rendered?presentationUID= overlay + /api/v1/gsps REST endpoints.
> ✅ **Migration Tools** — Folder watcher / recursive directory DICOM ingest + batch C-MOVE retriever (migration_service.py, /api/v1/migration REST endpoints, migration_jobs tracking).
> ✅ **Study Split/Merge** — Split series/instances to new studies, merge source studies to target, rewrite DICOM headers on storage (split_merge_service.py, /api/v1/studies/split and /merge REST endpoints).
> ✅ **Critical Results Tracking** — Flag urgent radiology findings, track mandatory physician acknowledgements, audit trails, webhooks (critical_results_service.py, /api/v1/critical-results REST endpoints, critical_results table).
> ✅ **Auto-routing Rules Engine** — Evaluate DICOM tag rules on new study ingestion, automatic background C-STORE dispatching to remote nodes (
outing_service.py, /api/v1/routing REST API, 
outing_rules table).
> ✅ **Key Image Notes (KIN)** — Synthesize & parse Key Object Selection Documents (SOP Class 1.2.840.10008.5.1.4.1.1.88.22, KO modality), DCM title concept coding, notes, image bookmarks (kin_service.py, /api/v1/kin REST API, key_image_notes table).
> ✅ **Lossy Compression Engine** — Policy-based lossy JPEG / JPEG-LS transcoding for aging studies, quality factor controls, 75%+ storage savings (lossy_compression_service.py, /api/v1/compression/lossy REST API, lossy_compression_rules table).
> ✅ **Instance Availability Notification (IAN)** — N-CREATE DIMSE notifications (SOP Class 1.2.840.10008.5.1.4.33), ReferencedSeriesSequence payload, target AE dispatching (ian_service.py, /api/v1/ian REST API, ian_notifications table).
> ✅ **DICOMDIR Creation** — Standard DICOM Media Storage Directory catalog (SOP Class 1.2.840.10008.1.3.10), Directory Record Sequence, CD/DVD/USB ZIP export bundle (dicomdir_service.py, /api/v1/export/dicomdir REST API).
> ✅ **Prefetching Engine** — Automated prior study prefetching on MWL/ADT trigger, C-FIND remote prior query, background C-MOVE retrieval (prefetch_service.py, /api/v1/prefetch REST API, prefetch_rules and prefetch_jobs tables).
> ✅ **Dose Monitoring (RDSR)** — Parse & index Radiation Dose Structured Reports (SOP Class 1.2.840.10008.5.1.4.1.1.88.67), total DLP (mGy·cm), CTDIvol, Dose RP, cumulative patient dose analytics (dose_service.py, /api/v1/dose REST API, 
adiation_dose_reports table).
> ✅ **Cross-Enterprise Document Sharing (XDS-I.b)** — IHE XDS-I.b imaging document registration [ITI-41/RAD-68], ebXML FindDocuments registry query [ITI-18], RetrieveDocumentSet repository retrieve [ITI-43] (xdsi_service.py, /api/v1/xdsi REST API, xds_documents table).
> ✅ **Prior Study Linking** — Prior study matching & relevance scoring (Patient ID + Modality + Body Part + Study Date ranking) (prior_service.py, /api/v1/priors REST API).
> ✅ **Hanging Protocols** — Viewport grid layout rules (1x1, 1x2, 2x2), W/L presets, modality & body part matching engine, user preference priority ordering (hanging_protocol_service.py, /api/v1/hanging-protocols REST API, hanging_protocols table).
> ✅ **LDAP / Active Directory Integration** — Enterprise domain user authentication (OpenLDAP / Active Directory), auto user provisioning & RBAC role mapping (ldap_service.py, /api/v1/ldap REST API).
> ✅ **WADO-RS Bulk Data Retrieval** — DICOMweb PS 3.18 frame byte-range streaming (/frames/1,2,3), bulk data element extraction (/bulk/PixelData), multipart/related response formatting (wado_rs_bulk_service.py, /dicom-web REST API).
> ✅ **Peer Review / QA** — Anonymized RADPEER quality assurance workflow: study case creation, reviewer assignment (prevents self-review), RADPEER 1-4 scoring, aggregate discrepancy analytics (peer_review_service.py, /api/v1/peer-review REST API, peer_review_cases table).
> ✅ **VNA (Vendor Neutral Archive)** — Multi-tenant / multi-department isolation: 	enants + 	enant_ae_titles tables, studies.tenant_id / users.tenant_id columns (Alembic 
8902132m3n4 + merge s9013243n4o5), ingest tagging by source AE title (C-STORE) or user (STOW-RS), query isolation by calling AE (C-FIND / C-MOVE / C-GET) and by user (QIDO-RS + dashboard), admin REST /api/v1/tenants (CRUD + AE mappings + user assignment), dashboard Tenants tab + tenant badges, full backward compatibility for unscoped legacy data (	enant_service.py, 	enant_ae_titles, 6 new integration tests on SQLite + MySQL).
> ✅ **C-STORE (SCP/SCU)** — SCP on :10400 (accept + index images from modalities, tenant-aware, storage-commit hook) **and a first-class SCU**: node-aware forwarding (sender.send_to_node_sync resolves a dicom_nodes destination and C-STORE's every matching instance), backend-agnostic materialization (S3/MinIO objects temp-downloaded before send), REST trigger POST /api/v1/nodes/{ae}/send (study/series/instance scopes, VNA-scoped for non-admin users, FORWARDED audit trail), dashboard *Send* action per study, 11 new DIMSE integration tests (real C-STORE round-trips, S3 fake-backend send, VNA isolation, error paths) on SQLite + MySQL.
> ✅ **DICOMweb for Remote Nodes (QIDO-RS Query / WADO-RS Retrieve)** — REST twins of C-FIND and C-GET for peers that expose a DICOMweb service: node registry gained web_url / web_username / web_password (Alembic u1234567v8w9), SSRF-guarded client (dicomweb_client.py — refuses loopback/private/link-local/cloud-metadata by default, DNS-rebinding re-check per call, timeout, no redirects so Basic auth never leaks, size cap), POST /api/v1/nodes/{ae}/web/query (QIDO, C-FIND-shaped matches incl. multi-value backslash joining) and /web/retrieve (WADO multipart parse → normal ingest pipeline, idempotent, VNA tenant tagging, PULLED audit), dashboard *Web Query* / *Web Retrieve* buttons in the Nodes panel, 12 new integration tests (SSRF matrix, mock DICOMweb server e2e, role gating, 502/409/422 error paths) on SQLite + MySQL.

## 📋 52-Feature Status Matrix (canonical — verified 2026-08-06)

> Legend: ✅ Done · 🟡 Partial · ⬜ Planned (not yet).
> **Overall: 41 ✅ · 1 🟡 · 10 ⬜** — the full details per feature are in the tables below
> (status column of this file reflects the same state; `plan.md` is the canonical snapshot).

| # | Feature | Priority | Status |
| :-: | :--- | :--- | :--- |
| 1 | C-STORE (SCP/SCU) | Critical | ✅ Done |
| 2 | C-FIND (SCP/SCU) | Critical | ✅ Done |
| 3 | C-MOVE (SCP/SCU) | Critical | ✅ Done |
| 4 | C-GET (SCP/SCU) | Critical | ✅ Done |
| 5 | C-ECHO (SCP/SCU) | Critical | ✅ Done |
| 6 | Storage Commitment (N-ACTION/N-EVENT) | Critical | ✅ Done |
| 7 | Modality Worklist (MWL) | Critical | ✅ Done |
| 8 | MPPS (Modality Performed Procedure Step) | Critical | ✅ Done |
| 9 | IAN (Instance Availability Notification) | High | ✅ Done |
| 10 | DICOM TLS | High | ✅ Done |
| 11 | Lossless Compression (JPEG-LS, JPEG2000, RLE) | Critical | ✅ Done |
| 12 | Lossy Compression (configurable) | Medium | ✅ Done |
| 13 | VNA (Vendor Neutral Archive) | High | ✅ Done |
| 14 | Study Lifecycle Management (auto-archive, restore, purge) | High | ✅ Done |
| 15 | Auto-routing Rules Engine | High | ✅ Done |
| 16 | Prefetching (auto-retrieve priors) | High | ✅ Done |
| 17 | DICOMDIR Creation (CD/DVD/USB export) | Medium | ✅ Done |
| 18 | Migration Tools (folder watcher + C-MOVE retriever) | Critical | ✅ Done |
| 19 | Transfer Accelerator (HTTP-based fast transfer) | Critical | ✅ Done |
| 20 | WADO-URI | Critical | ✅ Done |
| 21 | WADO-RS / QIDO-RS / STOW-RS (DICOMweb) | Critical | ✅ Done |
| 22 | Hanging Protocols (viewport layouts, W/L presets) | High | ✅ Done |
| 23 | Prior Study Linking (relevance scoring) | Critical | ✅ Done |
| 24 | GSPS (Presentation State) | High | ✅ Done |
| 25 | Key Image Notes (KIN) | High | ✅ Done |
| 26 | 2D MPR Backend Prep (volume streaming) | Medium | ✅ Done |
| 27 | 3D VRT / MIP Support (3D rendering data) | Medium | ✅ Done |
| 28 | Dose Monitoring (RDSR) | High | ✅ Done |
| 29 | Fusion (PET/CT, MRI/US) | Critical | ✅ Done |
| 30 | Worklist Management (Unread, Stat, Dictated) | Critical | ✅ Done — reader task lists (reading_worklist_service.py, /api/v1/worklist-tasks, Reading tab) |
| 31 | Clinical Protocols Library | High | ✅ Done — clinical_protocols table + match engine (clinical_protocol_service.py, /api/v1/clinical-protocols, Admin → Protocols) |
| 32 | Peer Review / QA (RADPEER scoring) | High | ✅ Done |
| 33 | Critical Results Tracking (acknowledgement + audit) | High | ✅ Done |
| 34 | Reporting / Dictation Bridge | Critical | 🟡 Partial (reporting + ORU E2 done; dictation ⬜) |
| 35 | Chat & Collaboration (secure messaging) | Medium | ⬜ Planned |
| 36 | Teaching Files (anonymized export) | Medium | ⬜ Planned |
| 37 | Study Split/Merge (fix technologist errors) | High | ✅ Done |
| 38 | HL7 v2.x ORU/ORM/ADT (MLLP) | Critical | ✅ Done |
| 39 | LDAP / Active Directory (auth + provisioning) | Critical | ✅ Done |
| 40 | XDS-I (Cross-Enterprise Document Sharing) | High | ✅ Done |
| 41 | REST API (General) | High | ✅ Done (FastAPI native + Swagger) |
| 42 | Webhooks (StudyReceived events) | High | ✅ Done |
| 43 | Lua / Python Scripting (custom server scripts) | High | ✅ Done |
| 44 | FHIR ImagingStudy mapping | Medium | ✅ Done |
| 45 | Audit Logging (ATNA, RFC 3881) | Critical | ✅ Done (SHA-256 audit; ATNA XML ⬜) |
| 46 | Role-Based Access Control | Critical | ✅ Done |
| 47 | Load Balancing (HAProxy + stateless nodes) | Critical | ✅ Done (deploy/haproxy.cfg + docker-compose.lb.yml + /health/live + /health/ready) |
| 48 | High Availability / Clustering (K8s/Docker Swarm) | High | ⬜ Planned |
| 49 | Disaster Recovery Replication (off-site S3/Blob) | High | ✅ Done (replication_records + worker sweep + Admin Replication panel) |
| 50 | Dashboard & Analytics | Medium | ✅ Done |
| 51 | Anonymization Engine | High | ✅ Done (PS3.15 profile, ZIP export) |
| 52 | Storage Tiering (HSM) (Online/Nearline/Offline) | Medium | ✅ Done (via Study Lifecycle cold tier + S3) |

---

## Summary of Features

| Category | Total Features | Critical | High | Medium | Low |
| :--- | :---: | :---: | :---: | :---: | :---: |
| Core DICOM | 10 | 8 | 2 | 0 | 0 |
| Storage & Archive | 9 | 3 | 4 | 2 | 0 |
| Viewer & Visualization | 10 | 4 | 4 | 2 | 0 |
| Workflow & Clinical | 8 | 2 | 4 | 2 | 0 |
| Integration & Interoperability | 7 | 2 | 4 | 1 | 0 |
| Administration & Architecture | 8 | 3 | 3 | 2 | 0 |
| **Total** | **52** | **22** | **21** | **9** | **0** |

---

## 1. Core DICOM Services

| Feature | Description | Reference PACS | Priority | Effort | Implementation Notes (Python/FastAPI/pynetdicom) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **C-STORE (SCP/SCU)** | Receive and send DICOM instances | All PACS | Critical | Low | ✅ Done — SCP in 
eceiver.py (accept + index + tenant-tag + storage-commit hook); SCU in sender.py (send_file, send_to_node_sync with node registry + S3 materialization); REST trigger POST /api/v1/nodes/{ae}/send + dashboard Send action (	ests/test_cstore.py, 11 DIMSE tests). |
| **C-FIND (SCP/SCU)** | Query patient/study/series/image levels | All PACS | Critical | Medium | ✅ Done — Q/R SCP in 
eceiver.py (4 levels, wildcards, VNA-scoped, MWL routed per context); SCU in sender.py (send_find generic + query_remote_node_sync node-aware + TLS-aware), prefetch reuses the shared SCU; REST trigger POST /api/v1/nodes/{ae}/find + dashboard *Find* button (	ests/test_cfind.py, 11 DIMSE tests). |
| **C-MOVE (SCP/SCU)** | Retrieve DICOM instances | All PACS | Critical | Medium | ✅ Done — destination resolved from dicom_nodes registry; pynetdicom runs the C-STORE sub-operations; SCU helper in sender.py. |
| **C-GET (SCP/SCU)** | Retrieve without dedicated destination AE | Orthanc, dcm4chee | Critical | Medium | ✅ Done — SCP in receiver.py (handle_get, sub-operation count protocol, VNA-scoped, storage contexts declared with explicit SCP role so the C-STORE sub-operations can push); SCU in sender.py (send_get — storage contexts + SCP-role negotiation + EVT_C_STORE collection, pull_from_node_sync ingests through the normal C-STORE pipeline with VNA tenant tagging, idempotent re-import); REST trigger POST /api/v1/nodes/{ae}/pull + dashboard *Pull* action (tests/test_cget.py, 12 DIMSE tests). |
| **C-ECHO (SCP/SCU)** | DICOM ping / verify connection | All PACS | Critical | Low | ✅ Done — SCP auto-handled by pynetdicom when Verification context supported (receiver.py); SCU in sender.py (send_echo); REST trigger POST /api/v1/nodes/{ae}/ping + dashboard ping button. |
| **Storage Commitment** | Guarantee safe storage (N-ACTION / N-EVENT) | dcm4chee, GE, Siemens | Critical | High | ✅ Done — N-ACTION SCP handler, DB commitment_statuses table, async background N-EVENT-REPORT push worker + REST API. |
| **Modality Worklist (MWL)** | Supply scheduled procedures to modalities | dcm4chee, ClearCanvas | Critical | High | ✅ Done — worklist C-FIND SCP on :10400 (routed per context), steps auto-opened by HL7 ADT A01/A04, auto-completed when images arrive, REST + dashboard Schedule panel. |
| **MPPS (Modality Performed Procedure Step)** | Track exam progress (In-Progress, Completed) | dcm4chee, Fuji | Critical | High | ✅ Done — N-CREATE/N-SET SCP in receiver.py (idempotent N-CREATE, 0x0112 for unknown instance, status transitions IN PROGRESS → COMPLETED/DISCONTINUED), worklist (SPS) auto-links and closes the scheduled→performed→stored loop, C-STORE fallback closes steps left IN PROGRESS; SCU helpers in sender.py (send_mpps_create / send_mpps_set); read-only REST `/api/v1/mpps` + dashboard Performed section (tests/test_mpps.py, 15 DIMSE tests incl. SCU round-trip). |
| **IAN (Instance Availability Notification)** | Notify RIS/Viewer when images arrive | dcm4chee, Agfa | High | Medium | ✅ Done — N-CREATE DIMSE notifications (SOP Class 1.2.840.10008.5.1.4.33), ReferencedSeriesSequence building, target AE dispatching, audit logging (ian_service.py, /api/v1/ian REST API, ian_notifications table). |
| **DICOM TLS** | Secure communication with certificates | Orthanc, dcm4chee | High | Medium | ✅ Done — AES over TLS 1.2/1.3: self-signed cert bootstrapped automatically via `openssl`/`cryptography` (app/dicom/tls.py), server context with optional client-cert mTLS (`DICOM_TLS_CA`), SCU side TLS-aware for every outbound association (ping/forward/C-MOVE/C-GET/MPPS), plaintext-to-TLS-port refused, untrusted client refused (tests/test_tls.py, 5 real encrypted DIMSE tests). |

## 2. Storage & Archive

| Feature | Description | Reference PACS | Priority | Effort | Implementation Notes (Python/FastAPI/pynetdicom) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Lossless Compression** | JPEG-LS, JPEG2000, RLE | Orthanc, Conquest | Critical | Medium | ✅ Done — RLE built-in; JPEG-LS / JPEG2000 / HTJ2K via optional deps (compression_service.py + worker). |
| **Lossy Compression** | Configurable lossy compression for archive | Conquest, Orthanc | Medium | Medium | ✅ Done — Aging retention rules, JPEG Baseline / JPEG-LS lossy transcoding, quality factor configuration, audit logging (lossy_compression_service.py, /api/v1/compression/lossy REST API, lossy_compression_rules table). |
| **VNA (Vendor Neutral Archive)** | Multi-tenant, multi-department separation | Agfa, Philips, dcm4chee | High | High | ✅ Done — 	enants / 	enant_ae_titles tables + studies.tenant_id / users.tenant_id (Alembic 
8902132m3n4), ingest tagged by source AE (C-STORE) or user (STOW), queries isolated per calling AE (C-FIND/C-MOVE/C-GET) and per user (QIDO/dashboard), admin REST /api/v1/tenants + dashboard Tenants tab; unmapped/unassigned traffic keeps the legacy unscoped scope (backward compatible). |
| **Study Lifecycle Management** | Auto-archive, restore, purge based on rules | dcm4chee, GE | High | High | ✅ Done — `study_lifecycle_rules` + `studies.lifecycle_state`/`archived_at` (Alembic t0123456q6r7), ARCHIVE/RESTORE/PURGE rules (min age + modality + body part), real object moves via `StorageBackend.move` (S3 copy+delete / local rename — no bytes through the app), background worker sweep (every N ticks), REST `/api/v1/lifecycle` (rule CRUD, run now, per-study archive/restore/purge, cold-tier list; purge gated behind `require_destructive_role`), dashboard Lifecycle section in the Storage tab (tests/test_lifecycle.py, 7 integration tests). |
| **Auto-routing Rules Engine** | Send studies to other AEs based on tags | Orthanc, Conquest | High | Medium | ✅ Done — Tag rules matching (Modality, BodyPart, Source AET, StudyDesc regex), automatic C-STORE dispatching (
outing_service.py, /api/v1/routing REST API, 
outing_rules table). |
| **Prefetching** | Auto-retrieve priors when MWL is created | dcm4chee, GE, Agfa | High | High | ✅ Done — Automated prefetching engine, MWL / ADT trigger evaluation, C-FIND remote prior query, background C-MOVE study retrieval (prefetch_service.py, /api/v1/prefetch REST API, prefetch_rules and prefetch_jobs tables). |
| **DICOMDIR Creation** | Export studies to local media with DICOMDIR | Conquest, Horos | Medium | Medium | ✅ Done — Standard DICOMDIR catalog (SOP Class 1.2.840.10008.1.3.10), Directory Record Sequence (PATIENT -> STUDY -> SERIES -> IMAGE), ZIP export bundle generation, audit logging (dicomdir_service.py, /api/v1/export/dicomdir REST API). |
| **Migration Tools** | Import historical data from other PACS | Orthanc | Critical | High | ✅ Done — Folder watcher / recursive directory DICOM ingest + batch C-MOVE retriever (migration_service.py, /api/v1/migration REST API, migration_jobs tracking table). |
| **Transfer Accelerator** | Fast HTTP-based transfer bypassing C-STORE | Orthanc (plugin) | Critical | High | ✅ Done — **parallel streamed STOW-RS upload**: instances split into batches (default 16) and pushed over N concurrent HTTP connections (default 4, `TRANSFER_ACCELERATOR_WORKERS`) with **chunked transfer encoding** (`stow_upload(stream=True)` streams the multipart body straight from the backend — no temp file, no single in-memory copy) + whole-batch retry for transient failures (network / HTTP ≥ 500; per-instance `FailedSOPSequence` rejections are final) (accelerator_service.py, `POST /api/v1/nodes/{ae}/web/upload?accelerated=1&workers=1-16`, ⚡ Fast Upload button + "⚡ Accelerate" checkbox in the Nodes panel, per-transfer stats: batches/workers/elapsed/MB/s — tests/test_accelerator.py, 10 tests). |

## 3. Viewer & Visualization (Backend Support)

| Feature | Description | Reference PACS | Priority | Effort | Implementation Notes (Python/FastAPI/pynetdicom) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **WADO-URI** | Retrieve images via HTTP URL | dcm4chee, Orthanc | Critical | Low | ✅ Done — GET /dicom-web/wado (DICOM part-10, rendered PNG/JPEG). |
| **WADO-RS / QIDO-RS / STOW-RS** | DICOMweb RESTful services | Orthanc, dcm4chee | Critical | High | ✅ Done — WADO-RS frame list byte-range streaming (/frames/1,2,3), bulk data element extraction (/bulk/PixelData), multipart/related response formatting (wado_rs_bulk_service.py, /dicom-web REST API). |
| **Hanging Protocols** | Auto-layout based on study type/body part | ClearCanvas, GE | High | Medium | ✅ Done — Viewport grid layout rules (1x1, 1x2, 2x2), W/L presets, modality & body part matching engine, user preference priority ordering (hanging_protocol_service.py, /api/v1/hanging-protocols REST API, hanging_protocols table). |
| **Prior Study Linking** | Auto-find and link relevant priors | GE, Philips, Siemens | Critical | Medium | ✅ Done — Prior study matching & relevance scoring (Patient ID + Modality + Body Part + Study Date ranking) (prior_service.py, /api/v1/priors REST API). |
| **GSPS (Presentation State)** | Store annotations, zoom, pan | Horos, Agfa | High | High | ✅ Done — Parse & index VOI LUT, spatial transforms, graphic/text annotations, shutters; WADO-RS /rendered?presentationUID= overlay + /api/v1/gsps REST endpoints. |
| **Key Image Notes (KIN)** | Mark and save significant images | Siemens, Fuji | High | Medium | ✅ Done — Parse & synthesize Key Object Selection Documents (SOP Class 1.2.840.10008.5.1.4.1.1.88.22, Modality KO), concept title coding, notes, image bookmarks (kin_service.py, /api/v1/kin REST API, key_image_notes table). |
| **2D MPR Backend Prep** | Pre-compute or stream volumes for MPR | Horos, OsiriX | Medium | High | ✅ Done (#26) — volume geometry metadata + raw voxel streaming for the WebGL MPR frontend: GET /api/v1/volume/{series}/info (rows/cols/slices, pixel & slice spacing, orientation, per-slice z-order, rescale LUT, dtype) + GET /api/v1/volume/{series}/data (streamed little-endian voxel block, ?slices= range for progressive load, ?rescale=1 float32 HU, ?format=npy) — volume_service.py + /api/v1/volume router, works on MySQL & SQLite. |
| **3D VRT / MIP Support** | Support 3D rendering data delivery | OsiriX, syngo.via | Medium | High | ✅ Done (#27) — GET /api/v1/volume/{series}/mip (MIP/minIP/average + alpha-composited VRT, axis + slab, PNG/raw/npy). |
| **Dose Monitoring (RDSR)** | Parse and store radiation dose structured reports | dcm4chee, Agfa | High | Medium | ✅ Done — Parse & index Radiation Dose Structured Reports (SOP Class 1.2.840.10008.5.1.4.1.1.88.67), DLP, CTDIvol, Dose RP, irradiation events, patient cumulative dose analytics (dose_service.py, /api/v1/dose REST API, 
adiation_dose_reports table). |
| **Fusion (PET/CT, MRI/US)** | Link series with same Frame of Reference | Horos, syngo.via | Critical | Medium | ✅ Done (#29) — FrameOfReferenceUID captured at ingest (series table, indexed, Alembic a2345678b9c0) · `fusion_service.py` groups a study's series by FoR, emits every same-FoR aligned pair with IDENTITY transform contract + per-series volume geometry (reuses volume streaming), reports unregistered series · GET /api/v1/fusion/{study} (VNA tenant scoping, 404 for foreign studies) · tests/test_fusion.py (6 tests, SQLite + MySQL). |

## 4. Workflow & Clinical

| Feature | Description | Reference PACS | Priority | Effort | Implementation Notes (Python/FastAPI/pynetdicom) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Worklist Management** | User task lists (Unread, Stat, Dictated) | ClearCanvas, GE | Critical | Medium | ✅ Done (#30) — MWL SCP engine + radiologist reader task lists: UNREAD / DICTATED / READ states derived from report lifecycle, STAT overlay (manual flag + pending critical results + HIGH triage), radiologist assignment, VNA-scoped counts (reading_worklist_service.py, /api/v1/worklist-tasks, Reading tab). |
| **Clinical Protocols Library** | Mapping study descriptions to protocols | Siemens, GE | High | Medium | ✅ Done (#31) — Clinical Protocols Library: study-description / modality / body-part → standardized protocol + RIS code matching engine, admin CRUD + match tester (clinical_protocol_service.py, /api/v1/clinical-protocols, Admin → Protocols tab). |
| **Peer Review** | Anonymized QA assignment to other radiologists | Agfa, Intelerad | High | Medium | ✅ Done — RADPEER quality assurance workflow: case creation, reviewer assignment (prevents self-review), RADPEER 1-4 scoring, aggregate discrepancy analytics (peer_review_service.py, /api/v1/peer-review REST API, peer_review_cases table). |
| **Critical Results Tracking** | Acknowledge and track critical findings | Philips, GE | High | Medium | ✅ Done — Flag urgent findings, track mandatory acknowledgement signatures, audit trails, webhooks (critical_results_service.py, /api/v1/critical-results REST endpoints, critical_results table). |
| **Reporting / Dictation Bridge** | Launch dictation software with patient context | ClearCanvas, Fuji | Critical | Medium | 🟡 Partial (#34) — report creation/finalize + HL7 ORU^R01 write-back to EMR done (E2, reports table + /api/v1/reports); dictation/voice bridge pending. |
| **Chat & Collaboration** | Secure messaging regarding a study | Agfa Enterprise | Medium | High | ⬜ Planned (#35) — FastAPI WebSockets for real-time messaging linked to Study UID. |
| **Teaching Files** | Export anonymized studies to teaching DB | Horos, Orthanc | Medium | Medium | ⬜ Planned (#36) — anonymizer exists; cohort/teaching export flow pending. |
| **Study Split/Merge** | Fix technologist errors | dcm4chee, Conquest | High | High | ✅ Done — Split series/instances to new studies, merge source studies to target, rewrite DICOM headers (split_merge_service.py, /api/v1/studies/split and /merge REST endpoints). |

## 5. Integration & Interoperability

| Feature | Description | Reference PACS | Priority | Effort | Implementation Notes (Python/FastAPI/pynetdicom) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **HL7 v2.x ORU/ORM/ADT** | Receive orders, patient updates, send results | dcm4chee, ClearCanvas | Critical | High | ✅ Done (#38) — MLLP listener :2575 (ADT auto-attach + HIS badge) · ORM^O01 orders → MWL (E1) · ORU^R01 reports → EMR (E2); hand-built HL7, no external lib. |
| **LDAP / Active Directory** | Centralized user authentication | dcm4chee, Orthanc | Critical | Low | ✅ Done — Authenticate domain credentials against OpenLDAP / Active Directory, automatic domain user provisioning & RBAC role mapping (ldap_service.py, /api/v1/ldap REST API). |
| **XDS-I (Cross-Enterprise)** | Register and retrieve images across HIEs | dcm4chee, Agfa | High | High | ✅ Done — Provide and Register Document Set-b [ITI-41 / RAD-68], FindDocuments Registry Query [ITI-18], RetrieveDocumentSet Repository Retrieve [ITI-43], ebXML SubmitObjectsRequest generation (xdsi_service.py, /api/v1/xdsi REST API, xds_documents table). |
| **REST API (General)** | Scriptable API for all operations | Orthanc | High | Low | Native to FastAPI. Auto-generated OpenAPI (Swagger) docs. |
| **Webhooks** | Trigger external scripts on events | Orthanc | High | Low | ✅ Done — WEBHOOK_URLS POSTs a StudyReceived payload per new study (retries, non-blocking). |
| **Lua / Python Scripting** | Custom server-side scripts for incoming data | Orthanc, Conquest | High | High | ✅ Done (#43) — scripts/ dir loaded at runtime via importlib with hot reload; handle/on_<event> contract, ScriptContext (study/instances/read_bytes/http_post), events StudyReceived/StudyDeleted/CriticalResultFlagged; Admin → Scripts panel (list/reload/create/toggle/run/source); tests/test_scripting.py (12 tests) |
| **FHIR ImagingStudy** | FHIR resource mapping | dcm4chee | Medium | Medium | ✅ Done (#44) — FHIR R4 gateway: Patient + ImagingStudy + CapabilityStatement (app/api/v1/fhir.py). |

## 6. Administration & Architecture

| Feature | Description | Reference PACS | Priority | Effort | Implementation Notes (Python/FastAPI/pynetdicom) |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Audit Logging (ATNA)** | Track all PHI access | dcm4chee, Agfa | Critical | High | ✅ Done (#45) — SHA-256 tamper-evident audit log (audit_service.py); full ATNA RFC 3881 XML/syslog pending. |
| **Role-Based Access Control** | Granular permissions (Admin, Rad, Tech) | All PACS | Critical | Medium | FastAPI dependency injection for scopes/roles. |
| **Load Balancing** | Distribute DICOM traffic | Orthanc (plugin) | Critical | High | HAProxy config + stateless FastAPI nodes. Shared DB (Postgres) and Redis. |
| **High Availability / Clustering** | Failover support | dcm4chee, GE | High | High | Docker Swarm or Kubernetes deployments. Stateful set for DB. |
| **Disaster Recovery Replication** | Sync to off-site backup | Fuji, Philips | High | Medium | Background task (Celery) to duplicate DICOM files to S3/Azure Blob. |
| **Dashboard & Analytics** | Storage usage, modality throughput | ClearCanvas | Medium | Medium | ✅ Done (#50) — admin dashboard with live feature cards, services matrix, audit, tenant analytics. |
| **Anonymization Engine** | Rule-based de-identification on export | Orthanc, Conquest | High | Medium | ✅ Done (#51) — PS3.15 Basic Profile ZIP export (/api/v1/export/{study}/anonymized, anonymizer.py). |
| **Storage Tiering (HSM)** | Online, Nearline, Offline storage | dcm4chee, Agfa | Medium | High | ✅ Done (#52) — implemented as Study Lifecycle cold-tier `.archive/` + S3 backend (lifecycle_service + StorageBackend.move). |
