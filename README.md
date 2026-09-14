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
[![License](https://img.shields.io/badge/License-MIT-2EA043?style=for-the-badge)](#-license)

<br>

**[✨ Features](#-features) · [🏛 Architecture](#-architecture) · [🚀 Quick Start](#-quick-start) · [🗺 URL Map](#-your-first-launch--url-map) · [⚙️ Configuration](#️-configuration) · [🧪 Testing](#-testing) · [📂 Structure](#-project-structure) · [🔐 Security](#-security-checklist)**

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

---

## ✨ Features

### 📥 Ingest & Archive
| | |
|:---|:---|
| 🏥 **DICOM SCP** | Full DIMSE receiver on port **10400** (AE: `PAXS_SCP`) — C-STORE, C-FIND, C-GET, C-MOVE, C-ECHO, Modality Worklist, MPPS, Storage Commitment, IAN |
| 🌐 **DICOMweb** | QIDO-RS / WADO-RS / STOW-RS under `/dicom-web` for web viewers & DICOMweb clients |
| 🗜 **Smart compression** | Background worker re-encodes studies to **RLE / JPEG-LS / JPEG 2000 / HTJ2K**, generates thumbnails, applies lossy re-compression policies |
| 🔁 **Routing & replication** | Rules-based auto-forwarding, peer prefetching, priors pull, replication, quotas & lifecycle retention |

### 🖥 Imaging Experience
| | |
|:---|:---|
| 🔬 **PaxsViewer** | Embedded, zero-install diagnostic viewer — MPR / 3D volume rendering, fusion, GSPS overlays, hanging protocols, window / zoom / measure tools |
| 📋 **Worklists** | Reading worklist, DICOM worklist tasks, prior comparisons, peer review, reports & critical-results management |
| 🫀 **Clinical tools** | Clinical protocols, dose tracking, split / merge studies, DICOMDIR import & export, anonymized research export |

### 🏥 Hospital Integration
| | |
|:---|:---|
| 🧬 **HL7 / FHIR gateway** | HL7 v2 ADT listener (patients register via `ADT^A01`) · FHIR R4 API · XDS-I · PIX/PDQ |
| 🔗 **Patient sharing** | Expiring share links & QR codes (`SHARE_EXPIRY_DAYS`) — patients open studies from a phone |
| 🏢 **Multi-tenant** | Tenants, node management, LDAP directory sync, license keys (`paxs.lic`) with a licensing API |

### 🛡 Operations & Safety
| | |
|:---|:---|
| 🔐 **Defense in depth** | Refresh-token rotation with theft detection, brute-force lockout, per-minute rate limits, RBAC roles, DB-backed settings overrides |
| 🧾 **Auditability** | Audit service logs every authentication & admin event; HA health endpoint exposes node identity |
| 📈 **Observability** | Prometheus metrics exporter, `/api/v1/health` probes (`live` / `ready`), Redis cache panel, DB panel |
| ⚖️ **Horizontal scale** | Stateless nodes + HAProxy config + shared storage — see [`deploy/SCALE-OUT-GUIDE.md`](deploy/SCALE-OUT-GUIDE.md) |

---

## 🏛 Architecture

```text
+--------------+   C-STORE / C-FIND / C-GET    +----------------------------------------+
|  Modalities  | ----------------------------> |        PAXS ARCHIVE SERVER             |
|  CT . MR .   |       port 10400  (DIMSE)     |        (one Python process)            |
|  US . CR ... |                               |                                        |
+--------------+                               |  +----------------------------------+  |
+--------------+   QIDO / WADO / STOW          |  | FastAPI  (port 8000)             |  |
|   Viewers /  | <----------------------------> |  |  - REST API  (/api/v1/...)       |  |
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

---

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
git clone https://github.com/msimoo/Paxs-Archive.git paxs-archive
cd paxs-archive

python -m venv venv
.\venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Linux / macOS:**
```bash
git clone https://github.com/msimoo/Paxs-Archive.git paxs-archive && cd paxs-archive
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

---

## 🗺️ Your First Launch — URL Map

Once `run_paxs.py` is running, everything lives behind one host and port:

| 🌐 URL | 📄 What's there |
|:---|:---|
| `http://localhost:8000/` | Landing page → redirects to the Study List |
| `http://localhost:8000/study-list` | 📋 **Study List UI** — search, filter & triage incoming studies |
| `http://localhost:8000/viewer` | 🔬 **PaxsViewer** — diagnostic viewer (MPR, 3D, fusion, GSPS) |
| `http://localhost:8000/admin` | 🛠 **Admin Console** — users, roles, tenants, routing rules, licensing |
| `http://localhost:8000/settings` | ⚙️ **Settings UI** — runtime configuration overrides |
| `http://localhost:8000/docs` | 📖 **OpenAPI / Swagger** — interactive REST API docs |
| `http://localhost:8000/dicom-web` | 🌐 **DICOMweb** root — QIDO-RS / WADO-RS / STOW-RS |
| `http://localhost:8000/api/v1/health` | 💓 **Health probes** — `live` / `ready` for load balancers |
| `0.0.0.0:10400` (DIMSE, not HTTP) | 🏥 **DICOM SCP** — point your modalities / PACS peers here (AE: `PAXS_SCP`) |

> 🔑 First login defaults to the bootstrap admin account created on first start — check the console output or `docs/DEFAULT-CREDENTIALS.md`, and **change it immediately**.

---

## ⚙️ Configuration

All runtime configuration lives in `.env`, layered with DB-backed overrides editable from the **Settings UI** at runtime (no restart required for most values).

| Variable | Default | Purpose |
|:---|:---|:---|
| `SECRET_KEY` | *(none — required)* | Signs JWTs & session cookies. **Must** be a long random value in production |
| `DATABASE_URL` | SQLite | MySQL or SQLite async connection string |
| `STORAGE_BACKEND` | `local` | `local` filesystem or `s3` for MinIO / S3-compatible object storage |
| `DICOM_SCP_PORT` | `10400` | Port for the DIMSE receiver |
| `DICOM_AE_TITLE` | `PAXS_SCP` | Local AE title advertised to modalities |
| `REDIS_URL` | *(unset)* | Optional — enables cache & distributed rate limiting |
| `SHARE_EXPIRY_DAYS` | `7` | Lifetime of patient share links / QR codes |
| `ENABLE_TLS` | `false` | Toggle TLS for HTTP **and** DIMSE listeners |
| `AI_TRIAGE_ENABLED` | `true` | Enables the demo emergency-triage worker (swap in real models here) |

See `.env.example` for the full annotated list, and [`deploy/SCALE-OUT-GUIDE.md`](deploy/SCALE-OUT-GUIDE.md) for multi-node settings (shared storage, HAProxy, Prometheus scrape config).

---

## 🧪 Testing

PAXS ships **77 pytest suites** covering DIMSE conformance, DICOMweb, auth/security, routing, and the worker pipeline.

```bash
# Run the full suite
pytest

# Run with coverage
pytest --cov=app --cov-report=term-missing

# Run just the DICOM conformance suite
pytest tests/dicom -v
```

> 🧵 Tests spin up an isolated SQLite DB and a local storage sandbox by default — no external services required.

---

## 📂 Project Structure

```text
paxs-archive/
├── app/
│   ├── main.py              # Process entrypoint — boots API, DIMSE, worker, HL7
│   ├── api/                 # REST routers (47+)
│   ├── services/            # Domain services (50+)
│   ├── dicom/                # DIMSE SCP, DICOMweb, transfer-syntax handling
│   ├── worker/               # Compression, thumbnails, AI triage, routing
│   ├── models/                # ORM models
│   └── core/                  # Config, security, auth, audit
├── study-list-ui/            # Study List SPA (optional Node.js build)
├── viewer-ui/                 # PaxsViewer SPA
├── admin-ui/                  # Admin Console SPA
├── settings-ui/               # Settings SPA
├── migrations/                 # Alembic schema migrations
├── deploy/                      # Docker Compose, HAProxy, Prometheus, scale-out guide
├── tests/                        # 77 pytest suites
├── docs/                          # Additional docs (credentials, HL7 mapping, etc.)
├── .env.example
├── requirements.txt
└── run_paxs.py                      # 🚀 Start here
```

---

## 🔐 Security Checklist

Before exposing PAXS beyond `localhost`, work through this list:

- [ ] Set a strong, unique `SECRET_KEY` (never commit it)
- [ ] Change the bootstrap admin credentials
- [ ] Enable `ENABLE_TLS=true` for both HTTP and DIMSE, with valid certificates
- [ ] Put PAXS behind a reverse proxy / firewall if it's reachable outside your network
- [ ] Review RBAC roles and remove unused accounts
- [ ] Configure LDAP if you need centralized directory auth
- [ ] Confirm rate limiting and lockout thresholds fit your environment
- [ ] Rotate `SECRET_KEY` and audit logs on a regular schedule
- [ ] Back up the database **and** object storage — PAXS does not back itself up

---

## 🤝 Contributing

Issues and pull requests are welcome. For anything non-trivial, please open an issue first to discuss the change — especially for DIMSE conformance or storage-format work, where compatibility really matters.

---

## 📄 License

Released under the **MIT License** — see [`LICENSE`](LICENSE) for details.

---

<div align="center">

**⭐ If PAXS is useful to you, consider starring the repo — it helps others find it.**

Made for imaging departments that deserve better tooling. 🩻

</div>
| 📈 **Observability** | Prometheus metrics exporter, `/api/v1/health` probes (`live` / `ready`), Redis cache panel, DB panel |
| ⚖️ **Horizontal scale** | Stateless nodes + HAProxy config + shared storage — see [`deploy/SCALE-OUT-GUIDE.md`](deploy/SCALE-OUT-GUIDE.md) |

---

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

---

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

> ⚠️ **Before going live:** set `SECRET_KEY` to a long random string and review the [security checklist](#-security-checklist).

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

### 4️⃣ Create the first (admin) account

Open **<http://localhost:8000/login>** and register — the **first registered user automatically bootstraps as `ADMIN`** (`ALLOW_REGISTER=true` by default; disable it after setup).

### 🗺 Your first launch — URL map

| 🌐 URL | 🧭 What you get |
|:---|:---|
| `http://localhost:8000/login` | 🔑 Sign-in / first-run registration |
| `http://localhost:8000/viewer/` | 🔬 **PaxsViewer** — diagnostic viewer |
| `http://localhost:8000/studies-ui/` | 📋 Study list workbench |
| `http://localhost:8000/admin/` | 🛠 Admin console |
| `http://localhost:8000/settings/` | ⚙️ Settings UI |
| `http://localhost:8000/api/docs` | 📚 Swagger UI (OpenAPI) |
| `http://localhost:8000/api/v1/health` | 💓 Health probe (HA-ready) |
| `dicom://your-host:10400` · AE `PAXS_SCP` | 📡 Modality / router DIMSE target |

### 📤 Send your first study

Point any modality at **AE `PAXS_SCP` · port `10400`**, or push a file with `pynetdicom` (bundled in requirements):

```python
from pydicom import dcmread
from pynetdicom import AE
from pynetdicom.sop_class import CTImageStorage

ae = AE(ae_title=b"MY_MODALITY")
ae.add_requested_context(CTImageStorage)

assoc = ae.associate("127.0.0.1", 10400, ae_title=b"PAXS_SCP")
if assoc.is_established:
    status = assoc.send_c_store(dcmread("ct_slice.dcm"))
    print("✅ stored" if status else "❌ rejected")
    assoc.release()
```

The study appears in the study list, gets compressed & thumbnailized by the worker, and is viewable instantly in PaxsViewer.

---

## 🧰 Alternative Launchers

| Script / file | Purpose |
|:---|:---|
| `run_all.bat` | 🎼 **Master runner** — Redis check → PAXS server → Vite UI dev server → health monitor |
| `run-all-providers.bat` / `.ps1` | 🧩 Multi-provider startup variant |
| `health_monitor.bat` | 💓 Watchdog that monitors the running instance |
| `scripts/install_service.bat` | 🪟 Register PAXS as a **Windows service** |
| `build_standalone.bat` | 📦 PyInstaller build → standalone `PAXS-Server` executable (`paxs.spec`, output in `dist/`) |
| `deploy/docker-compose.lb.yml` | 🐳 Load-balanced Docker deployment (HAProxy + multi-node) — see [`deploy/SCALE-OUT-GUIDE.md`](deploy/SCALE-OUT-GUIDE.md) |
| `deploy/docker-compose.monitoring.yml` | 📈 Prometheus monitoring stack ([`deploy/MONITORING.md`](deploy/MONITORING.md)) |

<details>
<summary>🧑‍💻 <b>Frontend development mode</b> (study-list-ui)</summary>

```powershell
cd study-list-ui
npm install
npm run dev        # Vite dev server  ->  http://localhost:5174/studies-ui/
npm run build      # type-check + production build
npm run lint       # oxlint
```
Stack: **React 19 · TypeScript · Vite · Tailwind CSS 4 · Radix UI · lucide-react**. The production build is served by PAXS itself at `/studies-ui/`.

</details>

---

## ⚙️ Configuration

Everything is driven by environment variables / `.env` — see [`.env.example`](.env.example) for the fully annotated template. Highlights:

| Variable | Default | Purpose |
|:---|:---|:---|
| `DATABASE_URL` | MySQL 8 @ `127.0.0.1` | Database backend (`sqlite+aiosqlite:///./paxs.db` for zero setup) |
| `DICOM_AE_TITLE` / `DICOM_PORT` | `PAXS_SCP` / `10400` | DIMSE receiver identity & port |
| `DICOM_TLS_ENABLED` | `false` | Encrypt the DIMSE channel (TLS 1.2/1.3, self-signed auto-generated) |
| `HTTPS_ENABLED` | `false` | Serve the web UI over HTTPS (`HTTPS_CERT` / `HTTPS_KEY` / `HTTPS_PORT`) |
| `STORAGE_ROOT` | `./storage` | Where studies are archived |
| `DEFAULT_COMPRESSION` | `RLE` | `RLE` \| `JPEG-LS` \| `JPEG2000` \| `HTJ2K` |
| `COMPRESSION_ENABLED` / `THUMBNAIL_ENABLED` | `true` | Background pipeline switches |
| `WORKER_ENABLED` / `WORKER_POLL_SECONDS` | `true` / `5` | Background worker control |
| `SECRET_KEY` | ⚠️ change me | JWT signing secret |
| `TOKEN_EXPIRE_MINUTES` | `480` | Session lifetime |
| `ALLOW_REGISTER` | `true` | Public self-registration (turn off after the first admin exists) |
| `RATE_LIMIT_ENABLED` | `true` | Brute-force / abuse protection |
| `HL7_ENABLED` / `HL7_PORT` | `false` / `2575` | HL7 v2 ADT listener for EMR integration |
| `FHIR_ENABLED` | `true` | FHIR R4 API |
| `TRIAGE_ENABLED` | `true` | AI emergency-flag detectors in the worker |
| `DICOMWEB_PUBLIC` / `CORS_ORIGINS` | `true` / `*` | Open LAN access for viewers (tighten for production) |
| `SHARE_EXPIRY_DAYS` | `30` | Patient share-link lifetime |
| `PAXS_INSTANCE_ID` | `node-1` | Node identity reported in `/health` (scale-out) |

---

## 🔐 Security Checklist

- [ ] `SECRET_KEY` → long random string
- [ ] `ALLOW_REGISTER=false` after creating the admin
- [ ] `HTTPS_ENABLED=true` with real certificates (`certs/`)
- [ ] `DICOM_TLS_ENABLED=true` + `DICOM_TLS_CA` set to your hospital CA (mutual TLS)
- [ ] `CORS_ORIGINS` narrowed from `*` to your viewer origins
- [ ] `RATE_LIMIT_ENABLED=true` (default) and MySQL instead of SQLite
- [ ] Review `TOKEN_EXPIRE_MINUTES` and audit-trail review cadence

---

## 🧪 Testing

The repository ships **77 test suites** covering C-STORE / C-FIND / C-GET, authentication & lockout, compression pipelines, DICOMweb, worklists, AI triage and more:

```powershell
pytest tests/ -x -q          # full suite
pytest tests/test_cstore.py  # a single suite
```

---

## 📂 Project Structure

```text
PAXS-Archive/
├── 🚀 run_paxs.py                  # unified entry point (one process = everything)
├── 📄 .env.example                 # fully annotated configuration template
├── 📦 requirements.txt             # runtime dependencies
├── 🏗 build_standalone.bat         # PyInstaller → standalone EXE
├── 🎼 run_all.bat                  # master launcher (Redis + server + UI + watchdog)
│
├── app/                            # the server
│   ├── main.py                     # FastAPI app, lifespan, gzip middleware, SPA mounts
│   ├── api/v1/                     # 47+ REST routers (auth, dicomweb, worklist, fhir, ...)
│   ├── core/                       # config (pydantic-settings) + licensing engine
│   ├── db/                         # async SQLAlchemy sessions (MySQL / SQLite)
│   ├── dicom/                      # DIMSE receiver, HL7 listener
│   ├── services/                   # 50+ domain services (storage, compression, triage, ...)
│   └── static/                     # embedded SPAs: viewer · admin · studies-ui · settings
│
├── study-list-ui/                  # React 19 + Vite + Tailwind frontend workspace
├── tests/                          # 77 pytest suites
├── migrations/                     # Alembic schema migrations
├── deploy/                         # Dockerfile · compose (LB + monitoring) · HAProxy · Prometheus
├── scripts/                        # ops helpers (license gen, service install, demo data)
├── certs/                          # TLS certificates (self-signed auto-generated on first start)
└── storage/                        # archived studies (created at runtime)
```

---

## 📚 Deep-Dive Docs

| Document | Topic |
|:---|:---|
| [`PAXS_ARCHIVE_SCALABILITY.md`](PAXS_ARCHIVE_SCALABILITY.md) | Full scalability architecture |
| [`deploy/SCALE-OUT-GUIDE.md`](deploy/SCALE-OUT-GUIDE.md) · [`deploy/LOAD-BALANCING.md`](deploy/LOAD-BALANCING.md) | Multi-node HAProxy scale-out |
| [`deploy/MONITORING.md`](deploy/MONITORING.md) | Prometheus observability |
| [`PAXS_AUTH_LICENSE_PLAN.md`](PAXS_AUTH_LICENSE_PLAN.md) | Auth, licensing & RBAC design |
| [`PAXS_VIEWER_FIXES_AND_UPGRADES.md`](PAXS_VIEWER_FIXES_AND_UPGRADES.md) · [`paxs_viewer_explain.md`](paxs_viewer_explain.md) | PaxsViewer internals |
| [`admin_console_explain.md`](admin_console_explain.md) · [`settings_explain.md`](settings_explain.md) | Admin console & settings reference |
| [`scripts_explain.md`](scripts_explain.md) · [`main_views_explain.md`](main_views_explain.md) | Ops scripts & API views |

---

## 🛣 Roadmap Signals

The scalability docs reference an active roadmap — **S3/MinIO object storage**, **Celery/Redis distributed queues**, **read-replicas with lag probing**, and **stateless multi-node load balancing**. Skeletons for all of these already ship behind optional flags.

---

<div align="center">

<br>

<img src="logo/logo.png" alt="PAXS" width="72"/>

**PAXS Archive Server** · *v0.1.0* · built with ❤️ and ☕

<sub>Crafted by <b>M.ELSamani</b> · <a href="https://www.elsamanii.rf.gd">www.elsamani.rf.gd</a></sub>

<br>

![Divider](https://img.shields.io/badge/⚕️_PAXS-One_process%2C_whole_imaging_department-EF2B2D?style=flat-square)

</div>





