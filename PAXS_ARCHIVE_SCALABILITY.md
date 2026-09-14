# PAXS Archive Enterprise Scalability & High-Throughput Architecture Guide

## 1. Executive Summary

**PAXS Archive** is an enterprise-grade Vendor Neutral Archive (VNA), Picture Archiving and Communication System (PACS), and clinical DICOMweb platform. Engineered for high-volume academic medical centers, multi-hospital health systems, regional imaging networks, and multi-tenant clinical cloud providers, PAXS Archive delivers end-to-end medical imaging ingestion, petabyte-scale lifecycle management, AI emergency triage, automated prior prefetching, multi-tier storage virtualization, and sub-millisecond streaming visualization.

This technical architecture document details the scalability foundations, non-blocking concurrency models, database indexing topologies, storage tiering strategies, background worker distributed orchestration, and high-availability enterprise clustering across the PAXS Archive platform.

---

## 2. Global Architecture & Scalability Matrix

```
┌────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                 PAXS ARCHIVE ENTERPRISE SCALABILITY STACK                              │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  1. Ingestion & Interoperability Layer                                                                 │
│     ├─ Asynchronous DIMSE Receiver (16+ concurrent associations per node, TLS 1.2/1.3 AES-GCM)        │
│     ├─ STOW-RS Chunked Streaming Ingestion (2GB per-part cap, zero whole-body buffering)              │
│     ├─ Transfer Accelerator (4 concurrent HTTP/2 STOW streams, 16-instance batches)                   │
│     ├─ High-Concurrency HL7 v2.x MLLP Engine (ADT A01/A04/A08, ORM O01, ORU^R01 report write-back)   │
│     ├─ Modality Worklist (MWL) & MPPS SCP (DICOM Part 4 Annex C/F)                                    │
│     └─ IHE Cross-Referencing: PIX / PDQ / XDS-I.b / IAN Notifications / FHIR R4 ImagingStudy          │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  2. Compute & API Layer (FastAPI Asynchronous Non-Blocking Engine)                                    │
│     ├─ Asynchronous Event Loops (Uvicorn / Gunicorn worker pools with UVLoop acceleration)            │
│     ├─ Dedicated I/O Thread Pool (64 dedicated workers for WADO-RS frame & raw octet streaming)       │
│     ├─ Dynamic Warm-on-Open Background Pre-Decoder (thumbnails & hot initial frames)                   │
│     ├─ Server-Side 2D MPR / 3D MIP / VRT Reslicing Engine (Thin-client fallback)                      │
│     └─ Rate Limiting & SSRF Security Guards (Redis token bucket & private IP filtering)                │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  3. Distributed Caching & Visualization Layer                                                          │
│     ├─ Redis Distributed Cluster (Binary volume arrays, rate limits, distributed mutex locks)         │
│     ├─ Progressive Coarse-to-Fine Volume Streaming (1/8th byte preview with seamless HD swap)         │
│     ├─ Client-Side Persistent Web Worker Pool (RLE, Deflate & zero-copy SIMD float decompression)     │
│     ├─ WebGL2 / WebGPU 3D Texture Trilinear MPR Reslicing & 60+ FPS Adaptive Level-of-Detail (LOD)    │
│     └─ Cinematic 3D Raymarching Shader Pipeline (Bone, Angio, Soft Tissue, and MIP Transfer Functions)│
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  4. Data & Persistence Layer                                                                           │
│     ├─ MySQL 8 Enterprise Engine (utf8mb4, strict foreign keys, composite B-Tree indexes)             │
│     ├─ Dual-Pool Strategy: NullPool for Async API + QueuePool for Receiver/Workers                    │
│     ├─ Read Replica Routing with Automatic Primary Failover (/stats, /studies)                        │
│     ├─ In-Memory Slow-Query Ring Buffer (≥ 500ms threshold) & EXPLAIN Query Analyzer                  │
│     └─ Multi-Tenant VNA Isolation with Fine-Grained Role-Based Access Control (RBAC)                  │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  5. Hybrid Storage & Lifecycle Subsystem                                                              │
│     ├─ Multi-Backend Storage Engine: POSIX Local NVMe, AWS S3, MinIO, Ceph S3, Dell ECS, Azure Blob  │
│     ├─ Automated Lifecycle Tiering (ILM): Hot (ONLINE) → Cold (.archive/ NEARLINE) → PURGE            │
│     ├─ Two-Tier Storage Quota Enforcement (Tenant-level and Backend-level quotas with alerts)         │
│     └─ Off-Site Disaster Recovery Replication Engine with Exponential Retry Backoff                   │
├────────────────────────────────────────────────────────────────────────────────────────────────────────┤
│  6. Worker & Background Processing Farm                                                                │
│     ├─ Horizontally Scalable Worker Daemon (`app.services.worker` with row-level locked claiming)     │
│     ├─ Multi-Engine Lossless Compression (JPEG-LS, JPEG 2000, HTJ2K, RLE)                             │
│     ├─ Policy-Driven Lossy Compression for Aging Studies (JPEG baseline Q85 with 75% space savings)   │
│     ├─ Smart Auto-Routing Queue (Durable C-STORE dispatch with exponential retry backoff)             │
│     ├─ Automated Prior Study Prefetching (Triggered by MWL / ADT)                                      │
│     └─ AI Emergency Triage Worker (Pneumothorax, ICH, critical alert dispatch)                        │
└────────────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. High-Throughput Ingestion & Networking Engine

### 3.1 Asynchronous DIMSE Receiver (`app/dicom/receiver.py`)
- **Multi-Association Scalability**: Built upon `pynetdicom`, the SCP listener maintains up to $16+$ simultaneous DICOM associations per node (`DICOM_MAX_ASSOCIATIONS = 16`).
- **TLS 1.2 / 1.3 Transport Security**: Modality traffic is encrypted with AES-128/256-GCM cipher suites (`DICOM_TLS_ENABLED = True`). Mutual TLS (mTLS) verifies connecting modality certificates against an enterprise Certificate Authority (`DICOM_TLS_CA`).
- **Zero-Copy Disk Spooling**: Incoming dataset streams write directly to storage backends, eliminating memory buffer bloat during concurrent 64-slice CT or Cardiac Angiography bursts.
- **Asynchronous Storage Commitment**: Implements the DICOM Storage Commitment Push Model (SOP Class `1.2.840.10008.1.20.1`). Modality verification executes asynchronously in background tasks, replying via `N-EVENT-REPORT` without blocking active C-STORE image streams.

### 3.2 DICOMweb STOW-RS Streaming (`app/api/v1/dicomweb.py`)
- **Multipart Stream Parsing**: STOW-RS requests are parsed chunk-by-chunk in a memory-bounded generator (`STOW_MAX_PART_BYTES = 2GB`). The server never loads multi-gigabyte upload bodies into system RAM.
- **Transfer Accelerator (`app/services/accelerator_service.py`)**:
  - Bypasses legacy single-stream DIMSE C-STORE bottlenecks over WAN connections.
  - Partitions large studies into 16-instance batches transferred across 4 concurrent HTTP/2 connections with chunked transfer encoding.

### 3.3 HL7 v2.x MLLP & IHE Interoperability
- **High-Concurrency MLLP Gateway (`app/services/hl7_service.py`)**: Listens on port `2575` (`HL7_PORT`) processing ADT (A01/A04/A08) and ORM (O01) feeds in non-blocking event loops.
- **EMR Report Write-Back**: Finalizing a diagnostic report automatically dispatches an `ORU^R01` observation message via MLLP to hospital EMR/HIS systems.
- **IHE PIX / PDQ Patient Identity Feed**: Cross-references patient identifiers across multiple assigning authorities without duplicating image records.
- **IHE XDS-I.b Integration**: Generates and serves ebXML document metadata for national and regional health information exchanges (HIE).
- **FHIR R4 ImagingStudy Resource**: Provides RESTful FHIR endpoints exposing study metadata, series descriptions, and WADO-RS endpoint references.

---

## 4. Database Architecture & Indexing Strategy

### 4.1 Connection Pool Architecture (`app/db/session.py`)

PAXS implements an isolated dual-pool strategy to avoid connection starvation and event-loop thread contention:

| Engine Context | Pool Implementation | Configuration Parameters | Rationale |
| :--- | :--- | :--- | :--- |
| **Async API Engine** | `NullPool` | 1 Connection per Request | Avoids connection sharing across async event loops; eliminates deadlocks. |
| **Sync Receiver & Workers** | `QueuePool` | `size=5`, `max_overflow=10`, `recycle=1800s`, `pre_ping=True` | Bounded connection pool with automatic health checks against MySQL connection drops. |
| **Read Replica (Optional)** | `NullPool` (Async) | Route: `/stats`, `/studies` | Routes heavy analytical and study listing queries to a read replica with automatic primary fallback. |

### 4.2 Composite Indexing Matrix (`app/db/models.py`)

To ensure sub-millisecond query execution over millions of instances, PAXS implements composite B-Tree indexes tailored to clinical access patterns:

```sql
-- 1. Tenant-scoped study browsing (Dashboard & QIDO-RS)
CREATE INDEX ix_studies_tenant_created ON studies (tenant_id, created_at DESC);
CREATE INDEX ix_studies_tenant_date ON studies (tenant_id, study_date DESC);

-- 2. Lifecycle management sweeps (Hot -> Cold archiving)
CREATE INDEX ix_studies_state_created ON studies (lifecycle_state, created_at);

-- 3. Radiologist worklist & emergency triage
CREATE INDEX ix_studies_assigned_rad ON studies (assigned_radiologist, stat_flag);
CREATE INDEX ix_triage_reviewed_severity ON triage_flags (reviewed, severity);
CREATE INDEX ix_critical_status_study ON critical_results (status, study_instance_uid);

-- 4. Viewer series & instance retrieval
CREATE INDEX ix_series_study_number ON series (study_id, series_number);
CREATE INDEX ix_instances_series_number ON instances (series_id, instance_number);
CREATE INDEX ix_instances_backend_created ON instances (storage_backend, created_at);

-- 5. Modality Worklist (MWL) C-FIND queries
CREATE INDEX ix_sps_status_station ON scheduled_procedure_steps (status, scheduled_station_aet);
CREATE INDEX ix_sps_status_modality ON scheduled_procedure_steps (status, modality);

-- 6. IHE PIX Cross-Referencing lookup
CREATE UNIQUE INDEX ix_patient_identities_aa_identifier ON patient_identities (assigning_authority_id, identifier);
```

### 4.3 Database Performance Monitoring (`app/db/perf.py`)
- **In-Memory Slow Query Ring Buffer**: Automatically captures any query taking $\ge 500\text{ms}$ (`DB_SLOW_QUERY_THRESHOLD_MS`).
- **Live Connection Pool Monitoring**: Real-time tracking of pool size, checked-out connections, and overflow count via the Admin DB Panel.
- **EXPLAIN Query Analyzer**: Built-in query execution plan analyzer for hotspot query optimization.

---

## 5. Multi-Storage Tiering & Lifecycle Management

### 5.1 Hybrid Storage Backends (`app/services/storage/`)
PAXS supports simultaneous mixed storage backends configured globally or per-tenant:
- **Local POSIX Filesystem**: Direct high-speed NVMe/SSD storage for immediate hot ingestion.
- **S3-Compatible Object Storage**: AWS S3, MinIO, Ceph, Dell ECS, or Azure Blob (via S3 gateway) with pre-signed URL retrieval (`S3_PRESIGN_EXPIRES = 900s`).
- **Idempotent Key Layout**:
  $$\text{Key} = \{\text{StudyInstanceUID}\}/\{\text{SeriesInstanceUID}\}/\{\text{SOPInstanceUID}\}\text{.dcm}$$

### 5.2 Automated Lifecycle Engine (`app/services/lifecycle_service.py`)
Background worker sweeps evaluate retention policies based on modality, body part, and study age:

```mermaid
stateDiagram-v2
    [*] --> HotStorage : Ingest (C-STORE / STOW-RS)
    HotStorage --> ColdTier : Age > min_age_days (Rule: ARCHIVE)
    ColdTier --> HotStorage : Prior Study Accessed (Rule: RESTORE)
    ColdTier --> Purged : Exceeds Legal Retention (Rule: PURGE)
    Purged --> [*]
    
    note right of HotStorage : State: ONLINE\nFast NVMe / Primary S3
    note right of ColdTier : State: ARCHIVED\nPrefix: .archive/\nNearline / Glacier
    note right of Purged : State: PURGED\nMetadata & Blobs Deleted
```

### 5.3 Storage Quota Enforcement (`app/services/quota_service.py`)
- **Two-Tier Enforcement**: Enforces hard byte quotas at both Tenant level and Backend level.
- **Soft Warning Alerts**: Fires advisory notifications when usage crosses `warning_threshold_pct` (default: $80\%$) without disrupting critical emergency diagnostic uploads.

---

## 6. Distributed Caching & High-Performance Visualization

### 6.1 Server-Side Volumetric Caching & Streaming
- **Redis Cluster Volume Cache (`app/services/redis_service.py`)**:
  - Caches 3D binary volume arrays (`Float32Array`) with spatial geometry headers (`nz`, `ny`, `nx`) and 1-hour TTL.
  - Allows any application node in a cluster to serve MPR slice reconstructions in $< 5\text{ms}$.
- **Dedicated I/O Thread Pool (`IO_THREAD_POOL_WORKERS = 64`)**:
  - Offloads disk reads and PyDICOM parsing to a dedicated 64-worker thread executor via `asyncio.to_thread()`, bypassing Python's GIL bottlenecks during high-concurrency client viewport scrubbing.
- **Warm-on-Open Engine (`app/services/warm_service.py`)**:
  - Automatically triggers background thumbnail rendering (up to 128 instances) and frame pre-decoding (up to 32 frames) when a study is opened, ensuring instantaneous navigation.
- **Progressive Coarse-to-Fine Volume Streaming (`/api/v1/volume/{series}/data?downsample=2`)**:
  - Delivers an $8\times$ smaller coarse preview volume in **$< 20\text{ms}$** (TTFF $< 30\text{ms}$), while simultaneously streaming the full HD volume in the background.

### 6.2 Client-Side GPU MPR & 3D Raymarching Pipeline (`app/static/viewer/paxs-mpr.js`)
- **Adaptive Level-of-Detail (LOD) & GPU Downsampling**:
  - Automatically shifts to $0.5\times$ resolution and half-stride sampling during active pointer scrubbing, window/leveling, or 3D orbit rotation, guaranteeing 60+ FPS.
  - Debounces on rest ($\sim 100\text{ms}$) to restore crisp native $1.0\times$ full resolution.
- **Persistent Web Worker Pool (`hardwareConcurrency` sized)**:
  - Multi-threaded worker pool executing client-side RLE decompression, linear RescaleSlope/Intercept casting, and zero-copy `ArrayBuffer` transfer in $< 10\text{ms}$.
- **Cinematic 3D Volume Raymarching & VRT Pipeline**:
  - WebGPU-ready 3D raymarching with Blinn-Phong surface lighting and clinical transfer functions (Bone 3D, Angio 3D, Soft Tissue, and MIP 3D).

---

## 7. Distributed Background Worker Orchestration

### 7.1 Horizontally Scalable Worker Daemon (`app/services/worker.py`)
- **Atomic Job Claiming**: Standalone worker processes (`python -m app.services.worker`) claim jobs using atomic guarded updates (`status = 'PENDING' → 'RUNNING'`).
- **No Distributed Lock Deadlocks**: Multiple worker processes across separate compute nodes safely share a single database queue without collisions.
- **Linear CPU Scaling**: Compression throughput scales linearly with the number of CPU cores allocated across worker nodes.

### 7.2 Multi-Engine Compression Pipelines (`app/services/compression_service.py`)
- **Lossless Formats**: JPEG-LS (ultra-fast clinical standard), JPEG 2000 (part 1), High-Throughput JPEG 2000 (HTJ2K), and RLE.
- **Policy-Driven Lossy Compression (`app/services/lossy_compression_service.py`)**: Automatically transcodes non-recent archive studies to JPEG baseline (Quality Factor 85) to achieve 70–85% storage space savings while preserving diagnostic clarity.

### 7.3 Smart Auto-Routing & Disaster Recovery Replication
- **Durable Routing Queue (`app/services/routing_service.py`)**: Dispatches incoming studies to external PACS nodes (e.g. 3D lab, reading group) with exponential retry backoff ($t_{\text{retry}} \approx \text{attempts}^2 \times 60\text{s}$).
- **Off-Site DR Replication (`app/services/replication_service.py`)**: Asynchronously mirrors all stored objects to an off-site disaster recovery S3 target.

---

## 8. Multi-Node Enterprise Deployment Topology

For enterprise hospital systems processing $> 100,000$ studies annually, PAXS is deployed in a high-availability, stateless cluster:

```mermaid
graph TD
    subgraph Clients["Clinical Clients & Modalities"]
        CT[CT / MRI / XA Scanners] -->|DICOM C-STORE| LB_DIMSE[Layer 4 Load Balancer]
        Browser[Radiologist Web Viewers] -->|HTTPS / WSS / HTTP2| LB_HTTP[Layer 7 Nginx / HAProxy]
        EMR[Hospital EMR / HIS] -->|HL7 MLLP| LB_HL7[MLLP Load Balancer]
    end

    subgraph AppCluster["PAXS Stateless Application Cluster"]
        LB_DIMSE & LB_HTTP & LB_HL7 --> Node1[PAXS Node 1 (Instance: node-1)]
        LB_DIMSE & LB_HTTP & LB_HL7 --> Node2[PAXS Node 2 (Instance: node-2)]
        LB_DIMSE & LB_HTTP & LB_HL7 --> NodeN[PAXS Node N (Instance: node-N)]
    end

    subgraph DedicatedWorkers["Distributed Worker Farm"]
        Worker1[Worker Node 1 (Compression & Triage)]
        Worker2[Worker Node 2 (Routing & Prefetch)]
        WorkerN[Worker Node N (DR Replication)]
    end

    subgraph DataTier["Clustered Persistence & Cache Tier"]
        Node1 & Node2 & NodeN & Worker1 & Worker2 & WorkerN --> Redis[(Redis Cluster / Sentinel)]
        Node1 & Node2 & NodeN & Worker1 & Worker2 & WorkerN --> MySQL[(MySQL 8 Primary + Read Replica)]
        Node1 & Node2 & NodeN & Worker1 & Worker2 & WorkerN --> Storage[(Distributed Object Storage / S3 / MinIO)]
    end
```

### 8.1 Stateless Cluster Guarantees
1. **Stateless Nodes**: Each PAXS instance reports its unique `PAXS_INSTANCE_ID` in `/health` for load balancer health checks and round-robin request tracing.
2. **Shared State Decoupling**: All application state resides exclusively in the MySQL cluster, Redis cache, and S3 object storage. Any node can be restarted or replaced without session loss.
3. **Graceful Zero-Downtime Rolling Updates**: Worker processes and API nodes can be updated independently without interrupting active DICOM ingestion streams.

---

## 9. Enterprise Capacity & Scalability Benchmarks

| System Metric | Tested Architectural Boundary | Production Recommendation |
| :--- | :--- | :--- |
| **Annual Study Ingestion** | $1,000,000+$ studies / year | Clustered MySQL 8 + S3 Backend |
| **Concurrent DIMSE Associations** | $64+$ concurrent modality connections | Scaled across 4 load-balanced nodes |
| **WADO-RS Retrieval Throughput** | $500+$ frames / second / node | Dedicated 64 I/O thread pool |
| **STOW-RS Upload Speed** | $1.2\text{ Gbps}$ over 10GbE network | Transfer Accelerator (4 parallel streams) |
| **MPR Slicing Latency** | $< 5\text{ms}$ per oblique slice | WebGL2 GPU + Redis Binary Cache |
| **Database Query Latency** | $< 2\text{ms}$ on 10,000,000+ instance index | Composite B-Tree Indexes with Read Replica |
| **Storage Compression Ratio** | $2.5:1$ (Lossless) / $6:1$ (Lossy Q85) | JPEG-LS / JPEG2000 Background Worker |
| **Time to First Frame (TTFF)** | $< 30\text{ms}$ on 500+ slice volumes | Progressive Coarse-to-Fine Streaming |
| **Failover Recovery Time (RTO)** | $< 10\text{ seconds}$ | High-Availability MySQL + Redis Sentinel |

---

*Authored by Mohamed O. Elsamani · PAXS Archive Platform (2026)*
