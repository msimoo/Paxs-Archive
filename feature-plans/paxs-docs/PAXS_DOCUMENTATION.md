# PAXS Archive — Architecture, Lifecycle, Upgrade & Build Guide

> **Medical Image Archiving System & Diagnostic Workstation (PACS / VNA)**  
> **Author & Architect:** Mohamed O. Elsamani ([elsamanii.rf.gd](https://elsamanii.rf.gd))  
> **Core Stack:** Python 3.12+ (FastAPI + Pynetdicom + SQLAlchemy + Alembic), MySQL 8.0 / SQLite, Redis, Vite / React 19 / TypeScript, OHIF v3.8+ (PaxsViewer).

---

## 📑 Table of Contents
1. [Project Overview & Core Architecture](#1-project-overview--core-architecture)
2. [Complete Directory & File Structure](#2-complete-directory--file-structure)
3. [Component Lifecycle & Data Flow](#3-component-lifecycle--data-flow)
4. [Tools & Technologies Ecosystem](#4-tools--technologies-ecosystem)
5. [How to Run & Develop Locally](#5-how-to-run--develop-locally)
6. [How to Edit, Upgrade & Update Features](#6-how-to-edit-upgrade--update-features)
7. [Database Migrations & Lifecycle (Alembic)](#7-database-migrations--lifecycle-alembic)
8. [Building Standalone Windows Distribution (No Python/Node required)](#8-building-standalone-windows-distribution)
9. [Windows Service Deployment (NSSM) & Health Monitoring](#9-windows-service-deployment-nssm--health-monitoring)
10. [Troubleshooting & Maintenance Checklist](#10-troubleshooting--maintenance-checklist)

---

## 1. Project Overview & Core Architecture

PAXS Archive is a high-performance **Picture Archiving and Communication System (PACS)** and **Vendor Neutral Archive (VNA)** adhering to Domain-Driven Design (DDD) and Clean Architecture principles.

```
                  +---------------------------------------------------------+
                  |                     Clinical Clients                    |
                  |  (Modality CT/MR, OHIF Viewer, Web Dashboard, RIS/HIS)   |
                  +----------------------------+----------------------------+
                                               |
                     +-------------------------+-------------------------+
                     | (DIMSE Port 10400)                                | (HTTP/REST Port 8000)
                     v                                                   v
        +--------------------------+                        +--------------------------+
        |   DICOM DIMSE Receiver   |                        |       FastAPI Engine     |
        |  - C-STORE (SCP/SCU)     |                        |  - DICOMweb (WADO/QIDO)  |
        |  - C-FIND / C-GET / C-MOVE|                       |  - Worklist / MPPS API   |
        |  - MWL / MPPS / Storage-C |                       |  - Reports & Auth / RBAC |
        +------------+-------------+                        +------------+-------------+
                     |                                                   |
                     +-------------------------+-------------------------+
                                               |
                                               v
        +-------------------------------------------------------------------------------+
        |                               Core Domain & Services                          |
        |  - Indexing Service (Metadata extraction & SHA-256 validation)                |
        |  - Storage Service (Local Filesystem & AWS S3 / MinIO Tiering)                |
        |  - Compression Worker (Lossless/Lossy HTJ2K, JPEG-LS, Deflate)                |
        |  - Routing Engine & Replication Worker (Off-site DR)                          |
        +--------------------------------------+----------------------------------------+
                                               |
                     +-------------------------+-------------------------+
                     |                                                   |
                     v                                                   v
        +--------------------------+                        +--------------------------+
        | Database (MySQL/SQLite)  |                        |  Storage (Local / S3)    |
        |  - Patients, Studies     |                        |  - /storage/studies/...  |
        |  - Series, Instances     |                        |  - .archive/ (Cold Tier) |
        |  - Reports, Audit Logs   |                        |  - /backups/ (SQL dumps) |
        +--------------------------+                        +--------------------------+
```

---

## 2. Complete Directory & File Structure

```text
D:\Paxs Archive\
├── app\                                # Core Application Package
│   ├── api\                            # REST & DICOMweb API Endpoints
│   │   ├── v1\                         # API Version 1
│   │   │   ├── auth.py                 # Authentication, JWT & User roles
│   │   │   ├── studies.py              # Study Query/Retrieve, Delete, Share
│   │   │   ├── dicomweb.py             # WADO-RS, QIDO-RS, STOW-RS standard endpoints
│   │   │   ├── nodes.py                # External PACS Nodes (C-FIND, C-GET, C-MOVE SCU)
│   │   │   ├── reports.py              # Radiology Reports & Structured Reporting
│   │   │   ├── worklist.py             # Modality Worklist (MWL) & MPPS API
│   │   │   ├── storage.py              # Storage tiering & backend management
│   │   │   ├── config.py               # Live Server configuration API
│   │   │   └── audit.py                # Tamper-evident SHA-256 audit logs
│   │   └── deps.py                     # Security, DB & Tenant dependencies
│   ├── core\                           # Framework Configurations & Security
│   │   ├── config.py                   # Pydantic BaseSettings (.env aware)
│   │   └── security.py                 # Password hashing & JWT tokens
│   ├── db\                             # Database Access Layer
│   │   ├── models.py                   # SQLAlchemy ORM Models (Patient, Study, Series, Instance)
│   │   └── session.py                  # Async & Sync DB session engines (MySQL/SQLite)
│   ├── dicom\                          # DICOM Protocols & DIMSE Engine
│   │   ├── receiver.py                 # Pynetdicom SCP Server (C-STORE, C-FIND, C-GET, C-MOVE)
│   │   ├── sender.py                   # DICOM SCU Client (C-STORE, C-FIND, C-GET, C-ECHO)
│   │   ├── parser.py                   # Pydicom metadata extractor
│   │   ├── hl7.py                      # HL7 MLLP Listener (ADT, ORM feeds)
│   │   └── tls.py                      # DICOM TLS (AES-128/256 transport security)
│   ├── services\                       # Business Logic & Workers
│   │   ├── indexing_service.py         # Metadata extraction & DB insertion
│   │   ├── storage_service.py          # Dual Local / S3 file manager
│   │   ├── worker.py                   # Background compression & thumbnail queue
│   │   ├── report_service.py           # Diagnostic reports and PDF generation
│   │   ├── lifecycle_service.py        # Hot/Cold storage tiering rules
│   │   └── replication_service.py      # Disaster recovery off-site replication
│   ├── static\                         # Embedded Web Assets & Client UI
│   │   ├── index.html                  # Main Web Dashboard
│   │   ├── app.js                      # Vanilla JS Dashboard application logic
│   │   ├── styles.css                  # Modern Obsidian/Cyan design system
│   │   ├── logo\                       # Application logos and favicons
│   │   └── viewer\                     # Embedded OHIF / PaxsViewer bundle
│   └── main.py                         # FastAPI ASGI factory & lifespan startup
│
├── feature-plans\                      # Architectural specifications & requirements
├── migrations\                         # Alembic Database Migration Scripts
│   ├── env.py                          # Alembic migration engine
│   └── versions\                       # Database revision scripts
├── scripts\                            # Utility & Maintenance Scripts
│   ├── build_exe.bat                   # PyInstaller standalone build script
│   └── install_service.bat             # NSSM Windows Service installer
├── study-list-ui\                      # React 19 + Vite Study List frontend source
├── storage\                            # DICOM file storage root (.dcm files)
├── backups\                            # Database SQL dumps
├── run_paxs.py                         # Python single-command application entry point
├── run_all.bat                         # Master runner (Redis + Backend + Viewer + Health)
├── build_standalone.bat                # Standalone Windows application builder
├── health_monitor.bat                  # Interactive service health manager
├── paxs.spec                           # PyInstaller packaging configuration
├── alembic.ini                         # Alembic database configuration
├── docker-compose.yml                  # Multi-container orchestration (MySQL + PAXS)
└── requirements.txt                    # Python dependencies
```

---

## 3. Component Lifecycle & Data Flow

### A. DICOM Ingestion Lifecycle (C-STORE)
1. **Association**: Modality connects to `:10400` with Presentation Context negotiation.
2. **Transfer**: Slices arrive over DIMSE socket (`receiver.py`).
3. **Storage**: Instance is written to disk under `storage/<StudyUID>/<SeriesUID>/<SOPUID>.dcm` with computed SHA-256 checksum.
4. **Metadata Indexing**: Metadata tags (Patient, Study, Series, Instance) are parsed and committed to MySQL/SQLite.
5. **Background Pipeline**: Async worker triggers thumbnail generation and lossless/lossy compression.

### B. Remote Query & Retrieve (C-FIND / C-GET)
1. User enters **Patient ID** in the Dashboard (`/#nodes`).
2. PAXS sends a `C-FIND` query to the remote PACS to enumerate study instances.
3. PAXS triggers `C-GET` over the association to retrieve all DICOM slices.
4. Retrieved instances pass through the standard ingestion pipeline and become immediately readable in **PaxsViewer** (`/viewer/`).

---

## 4. Tools & Technologies Ecosystem

| Layer | Technology | Purpose |
|---|---|---|
| **Backend API** | FastAPI / Uvicorn | High-throughput asynchronous REST & DICOMweb API |
| **DICOM Engine** | `pynetdicom` & `pydicom` | DIMSE C-STORE, C-FIND, C-GET, C-MOVE, C-ECHO SCP/SCU |
| **Database** | MySQL 8.0 / SQLite / SQLAlchemy | Async ORM with connection pooling & read-replicas |
| **Migrations** | Alembic | Version-controlled database schema evolution |
| **Diagnostic Viewer**| OHIF Viewer v3.8+ (PaxsViewer) | 2D/3D MPR, Volume Rendering, ROI Measurements |
| **Study List UI** | Vite, React 19, Tailwind CSS | Fast, accessible clinical study list |
| **Packaging** | PyInstaller / NSSM | Zero-dependency Windows `.exe` and Background Service |

---

## 5. How to Run & Develop Locally

### Quick Start (All-In-One Batch)
Double-click or run:
```cmd
run_all.bat
```
This automatically starts:
* **PAXS Archive Server** on `http://localhost:8000`
* **DICOM DIMSE Receiver** on port `10400` (AE Title: `PAXS_SCP`)
* **PAXS Viewer** on `http://localhost:8000/viewer/`
* **Interactive Health Monitor**

### Manual Developer Run
```cmd
# 1. Activate virtual environment
venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start Backend Server
python run_paxs.py
```

---

## 6. How to Edit, Upgrade & Update Features

### A. Modifying Backend & DICOM Services
* **REST APIs**: Add or edit routes in [`app/api/v1/`](file:///D:/Paxs%20Archive/app/api/v1/).
* **DICOM Logic**: Modify DIMSE handlers in [`app/dicom/receiver.py`](file:///D:/Paxs%20Archive/app/dicom/receiver.py) or outbound SCU in [`app/dicom/sender.py`](file:///D:/Paxs%20Archive/app/dicom/sender.py).
* **Storage & Archiving**: Adjust storage backends in [`app/services/storage_service.py`](file:///D:/Paxs%20Archive/app/services/storage_service.py).

### B. Modifying Dashboard & Viewer Theme
* **Web Dashboard**: Edit HTML in [`app/static/index.html`](file:///D:/Paxs%20Archive/app/static/index.html), JS in [`app/static/app.js`](file:///D:/Paxs%20Archive/app/static/app.js), CSS in [`app/static/styles.css`](file:///D:/Paxs%20Archive/app/static/styles.css).
* **Viewer Workstation Theme**: Edit [`app/static/viewer/paxs-viewer-theme.css`](file:///D:/Paxs%20Archive/app/static/viewer/paxs-viewer-theme.css).
* **3D MPR & Viewports**: Edit [`app/static/viewer/paxs-mpr.js`](file:///D:/Paxs%20Archive/app/static/viewer/paxs-mpr.js).
* **Browser Cache**: When updating static files, increment the cache-busting query parameter (e.g. `?v=28`) in `index.html`.

---

## 7. Database Migrations & Lifecycle (Alembic)

Whenever you add or modify ORM tables in [`app/db/models.py`](file:///D:/Paxs%20Archive/app/db/models.py):

```cmd
# 1. Generate a new migration script
venv\Scripts\alembic revision --autogenerate -m "add_new_feature_table"

# 2. Review the generated script in migrations/versions/

# 3. Apply migration to current database
venv\Scripts\alembic upgrade head
```

---

## 8. Building Standalone Windows Distribution

To package the entire PAXS Archive application into a standalone Windows package that runs on client PCs without Python or Node.js:

```cmd
build_standalone.bat
```

### What this produces:
* **Output Path**: `dist\PAXS-Server\`
* **Standalone Binary**: `dist\PAXS-Server\PAXS-Server.exe`
* **Standalone Launcher**: `dist\PAXS-Server\start_paxs.bat`
* **Complete Package**: Contains embedded database schemas, web viewer assets, storage directories, and configuration template.

To deploy to a new hospital machine, simply copy the `dist\PAXS-Server` folder to the target PC.

---

## 9. Windows Service Deployment (NSSM) & Health Monitoring

### Install as an Unattended Windows Service:
Run from an Administrator Command Prompt:
```cmd
scripts\install_service.bat
```
* **Service Name**: `PAXSServer`
* **Startup**: Automatically starts on Windows boot (`SERVICE_AUTO_START`).

### Service Health Management:
Run [`health_monitor.bat`](file:///D:/Paxs%20Archive/health_monitor.bat) to:
* Live check status of HTTP (`:8000`), DICOM (`:10400`), Redis (`:6379`), and Viewer.
* Perform live `/health` endpoint probes.
* Restart individual or all services with one keypress.

---

## 10. Troubleshooting & Maintenance Checklist

| Issue | Root Cause | Resolution |
|---|---|---|
| **Port 10400 Conflict** | Another PACS or previous instance is running | Run option `[6] Stop All` in `health_monitor.bat` |
| **Port 8000 Busy** | Zombie Python process holding port | Run `taskkill /F /IM python.exe` or use `health_monitor.bat` |
| **Remote PACS C-GET Rejection** | Remote PACS does not accept PatientID direct pull | PAXS auto-resolves with C-FIND pre-query; ensure AE title is registered in remote PACS |
| **Database Connection Error** | MySQL service stopped or bad credentials | Check `.env` configuration or switch to SQLite: `DATABASE_URL="sqlite+aiosqlite:///./paxs.db"` |
| **Static Cache Not Updating** | Browser caching old `.js`/`.css` | Press `Ctrl + F5` or bump `?v=XX` in `index.html` |
