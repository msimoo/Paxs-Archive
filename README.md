<div align="center">

<img src="logo/logo.png" alt="PAXS Archive Server" width="170"/>

# ⚕️ PAXS **ARCHIVE** SERVER

### *One Python process. A whole imaging department.*

<br>

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](#-at-a-glance)
[![FastAPI](https://img.shields.io/badge/FastAPI-Async%20Core-009688?style=for-the-badge&logo=fastapi&logoColor=white)](#-at-a-glance)
[![DICOM](https://img.shields.io/badge/DICOM-DIMSE%20SCP%20%2B%20DICOMweb-EF2B2D?style=for-the-badge)](#-at-a-glance)
[![MySQL](https://img.shields.io/badge/MySQL%208%20%7C%20SQLite-4479A1?style=for-the-badge&logo=mysql&logoColor=white)](#-at-a-glance)
[![PaxsViewer](https://img.shields.io/badge/PaxsViewer-Embedded%20Diagnostic%20Viewer-F59E0B?style=for-the-badge)](#-imaging-experience)

<br>

**[✨ Features](#-features) · [🏛 Architecture](#-architecture) · [🚀 Quick Start](#-quick-start) · [🗺 URL Map](#️-your-first-launch--url-map) · [⚙️ Configuration](#️-configuration) · [🧪 Testing](#-testing) · [📂 Structure](#-project-structure)**

<br>

![REST Routers](https://img.shields.io/badge/REST_Routers-47%2B-EF2B2D?style=flat-square)
![Domain Services](https://img.shields.io/badge/Domain_Services-50%2B-EF2B2D?style=flat-square)
![Test Suites](https://img.shields.io/badge/Pytest_Suites-77-2EA043?style=flat-square)
![Embedded UIs](https://img.shields.io/badge/Embedded_UIs-4-0078D6?style=flat-square)
![Platforms](https://img.shields.io/badge/Windows_%7C_Linux-supported-24292F?style=flat-square)

</div>

---

> [!IMPORTANT]
> **📌 What is PAXS?** — **PAXS** ( *PACS Archive eXchange Server* ) is a self-hosted, production-grade **DICOM archive & PACS platform**.
> A single `python run_paxs.py` boots an **async REST API**, a **DICOM DIMSE receiver** (`C-STORE` / `C-FIND` / `C-GET` / `C-MOVE` / `C-ECHO`), a **background compression & AI worker**, and serves **four built-in web UIs** — no external app server, no container required (though Docker, load-balancing and Prometheus configs ship in [`deploy/`](deploy/)).

---

## 📋 At a Glance

| ⚙️ Aspect | 📌 Detail |
|:---|:---|
| 🐍 **Runtime** | Python **3.11+** — async FastAPI + Uvicorn, one unified process |
| 🗄 **Databases** | **MySQL 8** (default, production) · **SQLite** (zero-setup dev fallback) |
| 💾 **Object storage** | Local filesystem (default) · S3 / MinIO (optional `STORAGE_BACKEND=s3`) |
| 📡 **Protocols** | DICOM **DIMSE** (C-STORE · C-FIND · C-GET · C-MOVE · C-ECHO · MWL · MPPS · Storage Commitment · IAN) · **DICOMweb** (QIDO-RS · WADO-RS · STOW-RS) · **HL7 v2** ADT · **FHIR R4** · XDS-I · PIX/PDQ |
| 🗜 **Transfer syntaxes** | Uncompressed · **RLE** · **JPEG** · **JPEG-LS** · **JPEG 2000** · **HTJ2K** (high-throughput J2K) |
| 🖥 **Built-in UIs** | **PaxsViewer** (diagnostic viewer) · Study List · Admin Console · Settings |
| 🔐 **Security** | JWT cookie rotation + theft detection · account lockout · rate limiting · RBAC · LDAP · full audit trail · optional TLS for both HTTP **and** DIMSE |
| 🧠 **AI** | Emergency triage detectors (demo pneumothorax heuristic included — plug in real models) |
| ⚖️ **Scale-out** | Stateless nodes behind HAProxy · shared DB/object storage · Prometheus metrics · instance health IDs |
| 📦 **Deployment** | Bare Python · Windows service · PyInstaller standalone EXE · Docker Compose |

## ✨ Features

### 📥 [Ingest & Archive](#-features)
| | |
|:---|:---|
| 🏥 **DICOM SCP** | Full DIMSE receiver on port **10400** (AE: `PAXS_SCP`) — C-STORE, C-FIND, C-GET, C-MOVE, C-ECHO, Modality Worklist, MPPS, Storage Commitment, IAN |
| 🌐 **DICOMweb** | QIDO-RS / WADO-RS / STOW-RS under `/dicom-web` for web viewers & DICOMweb clients |
| 🗜 **Smart compression** | Background worker re-encodes studies to **RLE / JPEG-LS / JPEG 2000 / HTJ2K**, generates thumbnails, applies lossy re-compression policies |
| 🔁 **Routing & replication** | Rules-based auto-forwarding, peer prefetching, priors pull, replication, quotas & lifecycle retention |

### 🖥 [Imaging Experience](#-imaging-experience)
| | |
|:---|:---|
| 🔬 **PaxsViewer** | Embedded, zero-install diagnostic viewer — MPR / 3D volume rendering, fusion, GSPS overlays, hanging protocols, window / zoom / measure tools |
| 📋 **Worklists** | Reading worklist, DICOM worklist tasks, prior comparisons, peer review, reports & critical-results management |
| 🫀 **Clinical tools** | Clinical protocols, dose tracking, split / merge studies, DICOMDIR import & export, anonymized research export |

### 🏥 [Hospital Integration](#-hospital-integration)
| | |
|:---|:---|
| 🧬 **HL7 / FHIR gateway** | HL7 v2 ADT listener (patients register via `ADT^A01`) · FHIR R4 API · XDS-I · PIX/PDQ |
| 🔗 **Patient sharing** | Expiring share links & QR codes (`SHARE_EXPIRY_DAYS`) — patients open studies from a phone |
| 🏢 **Multi-tenant** | Tenants, node management, LDAP directory sync, license keys (`paxs.lic`) with a licensing API |

### 🛡 [Operations & Safety](#-operations--safety)
| | |
|:---|:---|
| 🔐 **Defense in depth** | Refresh-token rotation with theft detection, brute-force lockout, per-minute rate limits, RBAC roles, DB-backed settings overrides |
| 🧾 **Auditability** | Audit service logs every authentication & admin event; HA health endpoint exposes node identity |
| 📈 **Observability** | Prometheus metrics exporter, `/api/v1/health` probes (`live` / `ready`), Redis cache panel, DB panel |
| ⚖️ **Horizontal scale** | Stateless nodes + HAProxy config + shared storage — see [`deploy/SCALE-OUT-GUIDE.md`](deploy/SCALE-OUT-GUIDE.md) |

## 🏛 Architecture

```text
+--------------+   C-STORE / C-FIND / C-GET    +----------------------------------------+
|  Modalities  | ----------------------------> |        PAXS ARCHIVE SERVER             |
|  CT . MR .   |       port 10400  (DIMSE)     |        (one Python process)            |
|  US . CR ... |                               |                                        |
+--------------+                               |  +----------------------------------+  |
+--------------+   QIDO / WADO / STOW         |  | FastAPI  (port 8000)             |  |
|   Viewers /  | <---------------------------> |  |  - REST API  (/api/v1/...)       |  |
|   PaxsViewer |       /dicom-web  (REST)      |  |  - 4 embedded SPA UIs            |  |
+--------------+                               |  |  - JWT auth + rate limiting      |  |
+--------------+   ADT^A01  /  FHIR R4         |  +----------------+-----------------+  |
|   EMR / HIS  | ----------------------------> |                   |                    |
+--------------+                               |  +----------------v-----------------+  |
                                               |  | Background worker                |  |
                                               |  |  - compress (HTJ2K/JP2/JLS/RLE)  |  |
                                               |  |  - thumbnails . AI triage        |  |
                                               |  |  - routing . replication . share |  |
                                               |  +----------------+-----------------+  |
                                               |                   |                    |
                                               |  +----------------v-----------------+  |
                                               |  |  MySQL 8 / SQLite                |  |
                                               |  |  Filesystem / S3 object store    |  |
                                               |  +----------------------------------+  |
                                               +----------------------------------------+
```

**Startup lifecycle** (`app/main.py`): create DB schema → start background worker → start DIMSE receiver → (optional) HL7 listener → serve REST + UIs. Redis is **optional** (cache / rate-limit acceleration); PAXS runs standalone without it.

## 🚀 Quick Start

### ✅ Prerequisites

| Requirement | Notes |
|:---|:---|
| 🐍 **Python 3.11+** | The only *hard* requirement |
| 🍃 **MySQL 8** *(optional)* | Production default — or use zero-setup SQLite |
| 🔴 **Redis** *(optional)* | Auto-detected by `run_all.bat`; standalone mode works without it |
| 🟩 **Node.js 18+** *(optional)* | Only for hacking on the `study-list-ui` frontend |

### 1️⃣ Clone & create a virtual environment

**Windows (PowerShell):**
```powershell
git clone <your-repo-url> paxs-archive
cd paxs-archive

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Linux / macOS:**
```bash
git clone <your-repo-url> paxs-archive && cd paxs-archive
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2️⃣ Configure the environment

```powershell
copy .env.example .env      # Windows
# cp .env.example .env      # Linux / macOS
```

> 💡 **Zero-setup mode (fastest way to try PAXS):** open `.env` and switch the database line to SQLite — no MySQL server needed:
> ```env
> DATABASE_URL=sqlite+aiosqlite:///./paxs.db
> ```

<details>
<summary>🐘 <b>Using production MySQL</b> (click to expand)</summary>

```sql
CREATE DATABASE paxs_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER 'paxs_user'@'localhost' IDENTIFIED BY 'paxs';
GRANT ALL ON paxs_db.* TO 'paxs_user'@'localhost';
```
Then in `.env`:
```env
DATABASE_URL=mysql+aiomysql://paxs_user:paxs@localhost:3306/paxs_db?charset=utf8mb4
```
The DB schema is **created automatically on first start**; Alembic migrations live in `migrations/` (`alembic.ini`) for schema upgrades.

</details>

> [!WARNING]
> ⚠️ **Before going live:** set `SECRET_KEY` to a long random string and review the [🔐 security checklist](#-security-checklist).
>
> 🔒 **Secure-by-default notes:** `DICOMWEB_PUBLIC=false` in `.env.example` — QIDO/WADO/STOW then
> require a bearer token or session cookie (patient share links keep working, scoped to their own
> study). LDAP/AD login is off until `LDAP_ENABLED=true` with `LDAP_SERVER` + `LDAP_BASE_DN` set and
> the optional `ldap3` package installed; every failure mode denies the login. The vendor portal
> never stores a signing key in source — see `deploy/vendor-portal-php/config.local.php.example`.
>
> 🔐 **Isolation & containment:** tenant scoping is the default on every query builder, so a
> tenant-A user gets 404 for tenant-B studies across reads, exports, shares and deletes (admins keep
> archive-wide access). Storage keys must be relative and stay inside `STORAGE_ROOT`; malformed DICOM
> identifiers are rejected at ingest (STOW-RS 400 / C-STORE 0xC000). Pre-B1 archives with absolute or
> CWD-relative paths need `STORAGE_ALLOW_LEGACY_PATHS=true` **temporarily, for migration only**.
> Self-registration is off (`ALLOW_REGISTER=false`) and, when enabled, cannot create destructive roles.

### 3️⃣ Run it

```powershell
python run_paxs.py
```

One command starts **everything** — REST API, DICOM receiver, worker and all four UIs:

```text
[PAXS] Starting on http://0.0.0.0:8000
       DIMSE SCP  :  PAXS_SCP listening on 0.0.0.0:10400
       Worker     :  compression / thumbnails / triage enabled
```

### 4️⃣ Web interfaces (routes)

All four UIs are served **same-origin** from the API process, so their DICOMweb
(`/dicom-web`) and REST (`/api/v1`) calls need no CORS and ride the archive's own
tenant scope.

| Route | What it is |
|---|---|
| `/` | Web dashboard (archive overview, settings, admin) |
| `/studies-ui` | **Modern React + Tailwind + shadcn/ui PACS Study List** — browse, search, filter and launch studies. *(Note the plural `studies-ui`; `/study-ui` is not a valid path.)* |
| `/viewer` | PAXS Viewer. Serves the Study List shell; a deep link with `?StudyInstanceUIDs=<uid>` opens the diagnostic viewer itself. Patient share links use `/viewer?share=<token>`. |
| `/admin` | PAXS Viewer Control Panel (web bridge over `/api/v1/admin-panel`) |
| `/settings` | Organization settings & institutional reporting |
| `/login` | Unified sign-in (already-authenticated visitors are redirected to their role landing page) |

Deep links use the OHIF-standard **PascalCase** parameter:

```text
/viewer/viewer?StudyInstanceUIDs=1.2.840.113619.2.55.3.604688.1
```

`/studies-ui`, `/viewer`, `/admin` and `/settings` are behind the session-cookie
auth gate; `/viewer?share=<token>` stays public for the study the token names.

> **Behind a reverse proxy:** set `PUBLIC_BASE_URL` to the browser-reachable
> origin (e.g. `https://pacs.example.org`) so share links, QR codes and FHIR
> URLs resolve. Leave it empty to derive the origin from
> `X-Forwarded-Host` / `Host`. A bind address (`0.0.0.0`) is rejected at startup.
>
> Serving under a **sub-path** (`https://example.org/paxs/`) additionally needs
> `PAXS_ROOT_PATH=/paxs` (or an `X-Forwarded-Prefix` header) and, for a proxy in
> another container, `PAXS_FORWARDED_ALLOW_IPS` — uvicorn trusts only loopback by
> default, so a proxy's `X-Forwarded-*` headers are otherwise silently ignored.
> Ready-made nginx / Traefik / Caddy / HAProxy blocks and a verification
> checklist: **[`deploy/REVERSE-PROXY.md`](deploy/REVERSE-PROXY.md)**.

<!-- PAXS:CONTINUE -->