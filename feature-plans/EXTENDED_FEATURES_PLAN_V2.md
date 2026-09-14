# PAXS Archive Server — Extended Features Plan (v2)

> **Created**: 2026-08-05  
> **Baseline**: Existing roadmap Phases 4–9 (production hardening → platform ecosystem)  
> **Scope**: New features NOT covered in the original plan, plus gap analysis and updated priorities

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
| 30 | Worklist Management (Unread/Stat/Dictated) | ✅ |
| 31 | Clinical Protocols Library | ✅ |
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
| 47 | Load Balancing | ✅ (HAProxy + stateless nodes + liveness/readiness probes) |
| 48 | High Availability / Clustering | ⬜ |
| 49 | Disaster Recovery Replication | ✅ (background sweep to S3/named backend + replication_records) |
| 50 | Dashboard & Analytics | ✅ |
| 51 | Anonymization Engine | ✅ |
| 52 | Storage Tiering (HSM) | ✅ (via Study Lifecycle cold tier + S3) |

---

## Executive Summary

The original plan (Phases 4–9) covers the critical path from production hardening through AI, interoperability, and distribution. This document introduces **four new phases (10–13)** that address gaps discovered in the original roadmap and add features driven by market trends and clinical user feedback.

### What's New in This Plan

| Phase | Theme | Why It Matters |
|-------|-------|----------------|
| 10 | Mobile & Tablet Native Experience | 70%+ of radiologists access studies on mobile during on-call |
| 11 | Compliance & Governance | HIPAA/GDPR/IHE compliance is table-stakes for enterprise sales |
| 12 | Analytics & Business Intelligence | Data-driven insights for radiology departments |
| 13 | Developer Experience & API Platform | Accelerate integrations and third-party adoption |

---

## Gap Analysis of Original Plan

These gaps were identified by reviewing the original Phases 4–9 against real-world PACS deployment requirements.

| Gap | Impact | Addressed In |
|-----|--------|-------------|
| No mobile-first viewer | High — on-call radiologists need mobile access | Phase 10 |
| No HIPAA BAA / compliance tooling | Critical — blocks US hospital sales | Phase 11 |
| No GDPR data subject rights (erasure, portability) | Critical — blocks EU deployments | Phase 11 |
| No IHE profile conformance statements | High — required for RFP responses | Phase 11 |
| No radiology department analytics | Medium — CIOs expect dashboards | Phase 12 |
| No dose tracking / DLP reporting | High — regulatory requirement in many countries | Phase 12 |
| No developer portal / SDK | Medium — limits ecosystem growth | Phase 13 |
| No webhook / event system | High — blocks real-time integrations | Phase 13 |
| No multi-tenancy architecture | High — needed for SaaS/cloud offering | Phase 13 |
| No DICOM SR (Structured Reporting) persistence | Medium — needed for AI results storage | Phase 11 |
| No PDF report generation / export | Medium — clinicians want printable reports | Phase 12 |
| No teaching file / case library | Low-Medium — academic centers want this | Phase 12 |

---

## Phase 10: Mobile & Tablet Native Experience (Weeks 25–35)

### 10.1 Responsive DICOM Viewer

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Touch-optimized viewport controls | Medium | Critical | Pinch-zoom, swipe-scroll, two-finger W/L adjustment |
| Responsive layout engine | Medium | Critical | Adaptive grid: 1-up mobile → 4-up tablet → N-up desktop |
| Gesture-based annotation | Medium | High | Draw measurements, arrows, text with touch input |
| Offline study caching | High | High | Service Worker prefetch + IndexedDB for on-call access |

### 10.2 Push Notification System

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Web Push (FCM/VAPID) | Medium | Critical | Notify on new studies, AI alerts, share requests |
| Priority-based routing | Low | High | Critical → push + SMS, routine → email digest |
| Notification preferences UI | Low | Medium | Per-user channel and frequency settings |
| On-call schedule integration | Medium | Medium | Route notifications based on on-call calendar |

### 10.3 Quick Actions & Shortcuts

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Quick dictation (Whisper mobile) | Medium | Innovative | Hold-to-dictate on mobile for preliminary reads |
| One-tap share to referring physician | Low | High | Deep-link generation with auto-populated patient context |
| Swipe actions on study list | Low | Medium | Swipe-left to archive, swipe-right to flag/prioritize |
| Haptic feedback for measurements | Low | Medium | Vibration feedback for precise measurement snapping |

---

## Phase 11: Compliance & Governance (Weeks 30–42)

### 11.1 HIPAA Technical Safeguards

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Encryption at rest (AES-256) | Medium | Critical | Database + file storage encryption with key management |
| Access control lists (ACL) | Medium | Critical | Per-study, per-series access control beyond RBAC |
| Automatic session timeout | Low | Critical | Configurable inactivity timeout with forced re-auth |
| Emergency break-glass access | Medium | High | Override access with mandatory audit + justification |
| PHI access audit reports | Medium | Critical | Generate HIPAA-compliant access reports on demand |

### 11.2 GDPR Data Subject Rights

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Right to erasure (Art. 17) | High | Critical | Cascade delete patient data with verification workflow |
| Data portability export (Art. 20) | Medium | Critical | Export all patient data in machine-readable format |
| Consent management | Medium | High | Record and verify consent for data processing |
| Data retention policies | Medium | High | Automated purge after configurable retention period |
| Data Protection Impact Assessment (DPIA) template | Low | Medium | Pre-filled DPIA based on system configuration |

### 11.3 IHE Profile Conformance

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| IHE-SWF (Scheduled Workflow) | High | High | Full SWF actor conformance statements |
| IHE-PIR (Patient Information Reconciliation) | Medium | High | Handle patient merge/unmerge operations |
| IHE-ATNA (Audit Trail and Node Authentication) | Medium | Critical | Syslog audit messages per IHE format |
| IHE-XDS-I.b (Cross-Enterprise Document Sharing for Imaging) | High | Medium | Image document source/consumer actors |
| Conformance statement generator | Medium | Medium | Auto-generate DICOM conformance statements from config |

### 11.4 DICOM Structured Reporting

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| SR template library | Medium | High | TID 1500 (Measurement Report), TID 2000 (Basic Report) |
| SR persistence & query | Medium | High | Store, index, and query SR objects via C-FIND / QIDO |
| SR viewer component | Medium | Medium | Render SR content in web viewer with measurement overlays |
| AI results as SR | Medium | Innovative | Package AI inference results as DICOM SR for interop |

---

## Phase 12: Analytics & Business Intelligence (Weeks 35–48)

### 12.1 Radiology Operations Dashboard

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Study volume analytics | Medium | High | Daily/weekly/monthly volume by modality, body part, referrer |
| Turnaround time tracking | Medium | High | Study arrival → preliminary → final report timestamps |
| Radiologist workload metrics | Medium | High | Studies read per hour, comparison across staff |
| Referring physician patterns | Low | Medium | Top referrers, order patterns, repeat study rates |
| Real-time department overview | Medium | Medium | Live dashboard showing pending, in-progress, completed |

### 12.2 Radiation Dose Tracking

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| RDSR (Radiation Dose SR) parser | Medium | Critical | Extract dose data from DICOM Radiation Dose SR objects |
| Dose index registry | Medium | High | Track DLP, CTDIvol, DAP per study with percentile benchmarks |
| Dose alert system | Low | High | Alert when dose exceeds DRL (Diagnostic Reference Level) |
| Dose reporting dashboard | Medium | Medium | Trends, outliers, per-scanner and per-protocol analytics |
| Regulatory report export | Low | Medium | Generate dose reports for regulatory submissions |

### 12.3 Teaching File & Case Library

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Case submission workflow | Medium | Medium | Select study → auto-anonymize → add teaching annotations |
| MIRC-compatible export | Low | Medium | Export cases in MIRC Teaching File format |
| Case search & tagging | Medium | Medium | Full-text search, RadLex tagging, difficulty levels |
| Quiz / self-assessment mode | Medium | Low | MCQ builder from teaching cases for resident training |

### 12.4 Report Generation & Export

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| PDF report renderer | Medium | High | Study report with key images, measurements, findings |
| Report template engine | Medium | Medium | Customizable templates per institution / modality |
| Batch report export | Low | Medium | Bulk PDF export for record requests / legal holds |
| HL7 ORU report dispatch | Medium | High | Send finalized reports back to EMR via HL7 ORU messages |

---

## Phase 13: Developer Experience & API Platform (Weeks 40–52+)

### 13.1 Webhook & Event System

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Event bus (Redis Streams / NATS) | High | Critical | Publish events: `study.received`, `study.reported`, `ai.alert` |
| Webhook registration API | Medium | High | Register HTTP callbacks with filtering, retry, HMAC signing |
| Event replay & dead-letter | Medium | High | Replay missed events, inspect failed deliveries |
| WebSocket event stream | Medium | Medium | Real-time event subscription for web clients |

### 13.2 Developer Portal & SDK

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Interactive API playground | Medium | High | Swagger UI with auth, sample data, try-it-now |
| Python SDK package | Medium | High | `pip install paxs-sdk` with typed client for all APIs |
| JavaScript/TypeScript SDK | Medium | Medium | `npm install @paxs/sdk` for web integrations |
| Integration cookbook | Low | Medium | Step-by-step guides: EMR, AI pipeline, reporting |
| Postman / Insomnia collection | Low | Medium | Pre-built API collection with environment variables |

### 13.3 Multi-Tenancy Architecture

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| Tenant isolation layer | High | Critical | Schema-per-tenant or row-level security |
| Tenant provisioning API | Medium | High | Create, configure, suspend tenants programmatically |
| Per-tenant storage quotas | Medium | High | Storage limits, usage tracking, overage alerts |
| Tenant-scoped RBAC | Medium | High | Roles and permissions scoped to tenant boundaries |
| Cross-tenant data sharing | High | Medium | Controlled study sharing between tenants (referrals) |

### 13.4 CI/CD & Quality Gates

| Task | Effort | Priority | Description |
|------|--------|----------|-------------|
| GitHub Actions pipeline | Medium | High | Lint → test → build → deploy on PR merge |
| DICOM conformance test suite | High | High | Automated test against DVTk / dcm4che test tools |
| Performance regression tests | Medium | Medium | Benchmark C-STORE throughput, API latency per release |
| Security scanning (SAST/DAST) | Medium | High | Bandit, Safety, Trivy for container images |
| Release automation | Medium | Medium | Semantic versioning, changelog generation, GitHub releases |

---

## Updated Priority Matrix (All Phases)

```
                        HIGH IMPACT
                            |
   MWL (5.1)                |   AI Triage (6.1)
   MySQL/Alembic (4.1)      |   HL7/FHIR (7.1)
   HIPAA Safeguards (11.1)  |   Multi-Tenancy (13.3)
   Storage Tiering (4.3)    |   Collaborative Viewer (6.2)
   Mobile Viewer (10.1)     |   Event System (13.1)
   Dose Tracking (12.2)     |   GDPR Rights (11.2)
                            |
 LOW EFFORT ────────────────+──────────────── HIGH EFFORT
                            |
   Rate Limiting (4.2)      |   Voice Report (6.3)
   Push Notifications (10.2)|   Edge PACS (8.2)
   Security Headers (4.2)   |   Teaching Files (12.3)
   Session Timeout (11.1)   |   Plugin Arch (9.1)
   PDF Reports (12.4)       |   IHE XDS-I.b (11.3)
                            |
                        LOW IMPACT
```

---

## Quick Wins — New Additions (1–2 Weeks Each)

| # | Feature | Phase | Impact | Notes |
|---|---------|-------|--------|-------|
| 1 | Automatic session timeout | 11.1 | Critical | HIPAA requirement, ~2 days of work |
| 2 | Web Push notifications (FCM) | 10.2 | High | Immediate UX improvement for on-call |
| 3 | RDSR dose data extraction | 12.2 | High | Parse existing DICOM objects, no new infra |
| 4 | Webhook registration API | 13.1 | High | Unlocks third-party integrations |
| 5 | PDF report export | 12.4 | Medium | WeasyPrint / ReportLab, high user demand |
| 6 | Python SDK (typed client) | 13.2 | Medium | Auto-generate from OpenAPI spec |
| 7 | Pinch-zoom touch controls | 10.1 | Medium | CSS + JS only, no backend changes |
| 8 | DICOM SR persistence | 11.4 | Medium | Extends existing SOP Class support |

---

## Updated Resource Estimation (Full Roadmap)

| Phase | Duration | Engineers | Key Dependencies |
|-------|----------|-----------|------------------|
| 4: Production Hardening | 4 weeks | 2 | MySQL, MinIO, Certificates |
| 5: Clinical Workflows | 6 weeks | 2–3 | HL7 test tools, Twilio |
| 6: AI & Advanced | 10 weeks | 3–4 | GPU, MONAI expertise |
| 7: Interoperability | 10 weeks | 2–3 | FHIR server, EMR access |
| 8: Distribution | 10 weeks | 2 | Windows CI, Code signing |
| 9: Platform Ecosystem | Ongoing | 1–2 | Community feedback |
| **10: Mobile Experience** | **10 weeks** | **2** | **FCM, Service Worker expertise** |
| **11: Compliance** | **12 weeks** | **2–3** | **Legal review, IHE testing tools** |
| **12: Analytics & BI** | **12 weeks** | **2–3** | **Charting library, RDSR samples** |
| **13: Developer Platform** | **12 weeks** | **2–3** | **Redis/NATS, OpenAPI generator** |

> **Total**: ~86 weeks for full extended roadmap with 3–4 engineers  
> **Recommended team ramp**: Start with Phases 4+11 in parallel (production + compliance), then layer in features

---

## Recommended Execution Timeline

### Quarter 1 (Weeks 1–12)
1. **Phase 4**: Production hardening (Alembic, MySQL, TLS, S3)
2. **Phase 11.1**: HIPAA technical safeguards (encryption, ACLs, session timeout)
3. **Quick Wins**: Session timeout, push notifications, dose extraction

### Quarter 2 (Weeks 13–24)
1. **Phase 5**: Clinical workflows (MWL, patient sharing, smart router)
2. **Phase 10**: Mobile viewer + push notifications
3. **Phase 12.2**: Dose tracking foundation

### Quarter 3 (Weeks 25–36)
1. **Phase 6**: AI triage + collaborative viewer prototype
2. **Phase 11.2–11.4**: GDPR, IHE profiles, structured reporting
3. **Phase 12.1**: Operations dashboard

### Quarter 4 (Weeks 37–52)
1. **Phase 7**: HL7/FHIR gateway
2. **Phase 13**: Event system, developer portal, multi-tenancy
3. **Phase 8**: Windows distribution + Edge PACS

### Ongoing
- **Phase 9**: Plugin architecture + observability
- **Phase 12.3–12.4**: Teaching files, report generation
- **Phase 13.4**: CI/CD pipeline maturation

---

## Success Metrics

| Metric | Target | Measurement |
|--------|--------|-------------|
| HIPAA compliance score | 100% | NIST CSF self-assessment |
| API response time (p95) | < 200ms | OpenTelemetry metrics |
| Mobile viewer load time | < 3s on 4G | Lighthouse performance audit |
| C-STORE throughput | > 50 studies/min | Benchmark test suite |
| Test coverage | > 85% | pytest-cov |
| Uptime SLA | 99.9% | Health check monitoring |
| Webhook delivery success | > 99.5% | Dead-letter queue metrics |
| Developer onboarding time | < 30 min | Measure from docs to first API call |

---

*Document created: 2026-08-05*  
*Extends PAXS Archive Server v0.1.0 roadmap (Phases 4–9)*  
*Next review: 2026-09-01*
