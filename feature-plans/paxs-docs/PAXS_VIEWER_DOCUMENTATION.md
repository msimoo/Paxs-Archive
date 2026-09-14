# PAXS Viewer — Architecture, Integration, Customization & Build Guide

> **Clinical Diagnostic DICOM Web Workstation (2D/3D MPR & Study List)**  
> **Integrated with:** PAXS Archive Server via DICOMweb (WADO-RS, QIDO-RS, STOW-RS)  
> **Core Stack:** OHIF Viewer v3.8+ (React 18/19), Cornerstone3D, Three.js 3D Engine, WebGL2, Vanilla CSS/JS Extensions.

---

## 📑 Table of Contents
1. [Overview & Core Architecture](#1-overview--core-architecture)
2. [Viewer Directory & Asset Structure](#2-viewer-directory--asset-structure)
3. [DICOMweb Communication Lifecycle (WADO/QIDO/STOW)](#3-dicomweb-communication-lifecycle)
4. [Key Features & Viewport Capabilities](#4-key-features--viewport-capabilities)
5. [3D & 2D Multi-Planar Reconstruction (MPR) Engine](#5-3d--2d-multi-planar-reconstruction-mpr-engine)
6. [How to Customize & Theme the Viewer](#6-how-to-customize--theme-the-viewer)
7. [Diagnostic Reporting & Template System](#7-diagnostic-reporting--template-system)
8. [Building & Re-Compiling Frontend Assets](#8-building--re-compiling-frontend-assets)
9. [Troubleshooting & Performance Optimization](#9-troubleshooting--performance-optimization)

---

## 1. Overview & Core Architecture

PAXS Viewer is a customized, high-performance web-based medical image viewer designed for radiologists, clinicians, and hospital departments. It functions as both a standalone web app and an embedded subsystem served directly through PAXS Archive at `/viewer/`.

```
           +-------------------------------------------------------------+
           |                      PAXS Viewer Web UI                     |
           |   (Study List / 2D Series Viewport / 3D MPR / Annotations)  |
           +------------------------------+------------------------------+
                                          |
                        HTTP / DICOMweb   | (JSON & Binary Stream)
                                          v
           +-------------------------------------------------------------+
           |                   PAXS Archive DICOMweb API                 |
           |  - QIDO-RS: /dicom-web/studies (Study queries & series meta)|
           |  - WADO-RS: /dicom-web/studies/{uid}/series/{uid}/instances |
           |  - WADO-URI: /dicom-web/thumbnail (Fast lossy preview PNGs) |
           |  - Reports: /api/v1/reports (Diagnostic Report Editor)      |
           +------------------------------+------------------------------+
                                          |
                                          v
           +-------------------------------------------------------------+
           |           Local Fast Storage (.dcm) & DB (MySQL/SQLite)     |
           +-------------------------------------------------------------+
```

---

## 2. Viewer Directory & Asset Structure

All production runtime files for the viewer reside in [`app/static/viewer/`](file:///D:/Paxs%20Archive/app/static/viewer/):

```text
app/static/viewer/
├── index.html                   # HTML Entry Point (loads bundle, CSS themes, scripts)
├── app-config.js                # OHIF DICOMweb data source configuration
├── app.bundle.*.js              # Compiled React/Cornerstone3D core engine
├── app.bundle.css               # Base OHIF CSS tokens
│
├── paxs-viewer-theme.css        # Master Workstation Theme (Obsidian/Slate, Tool categories)
├── paxs-study-list-theme.css    # Study List matching design tokens (Cyan/Teal accents)
├── paxs-study-list-enhance.js   # Dynamic Study List thumbnails, initial avatars & routing
├── paxs-mpr.js                  # 3D/2D Multi-Planar Reconstruction (MPR) engine
│
├── assets/                      # Icons, logos, and manifest icons
│   ├── logo.png                 # PAXS header logo
│   └── favicon.ico              # Workspace favicon
└── *.wasm                       # WebAssembly codecs (JPEG-LS, HTJ2K, Deflate decompression)
```

---

## 3. DICOMweb Communication Lifecycle

```
[Browser / Viewer]                               [PAXS Backend Server]
        |                                                 |
        |---- 1. GET /dicom-web/studies?limit=50 -------->| (QIDO-RS Search)
        |<--- JSON array with Patient & Study metadata ---|
        |                                                 |
        |---- 2. GET /thumbnail?studyUID=... ------------>| (Instant Thumbnail)
        |<--- 200 OK (80x80 preview PNG) -----------------|
        |                                                 |
        |---- 3. GET /dicom-web/studies/{uid}/metadata -->| (Series & Slices)
        |<--- DICOM JSON Metadata & SOP UIDs -------------|
        |                                                 |
        |---- 4. GET /dicom-web/.../instances/{sop}/frames| (WADO-RS Pixel Data)
        |<--- Raw binary pixel buffer (16-bit CT/MR) -----|
        |                                                 |
  [Render Canvas]
```

---

## 4. Key Features & Viewport Capabilities

1. **Tool Categorization & Visual Dock**:
   * **Navigation (Sky Blue `#38bdf8`)**: Zoom, Pan, Stack Scroll, Rotate, Flip, Cine Loop.
   * **Measurement (Emerald Green `#34d399`)**: Length, Angle, Cobb Angle, Ellipse ROI, Bidirectional.
   * **Contrast / Window Level (Amber `#fbbf24`)**: CT Presets (Soft Tissue, Bone, Lung, Brain).
   * **Annotation (Violet `#a78bfa`)**: Arrow annotations, Text markers, Calibration line.
2. **Multi-Series Comparison**:
   * Drag-and-drop series thumbnails from left drawer into viewport grids (1x1, 1x2, 2x2, 3x3).
3. **Cine Player**:
   * Continuous loop playback with adjustable frame rate (5 FPS, 15 FPS, 30 FPS).
4. **Export & Sharing**:
   * DICOM snapshot capture, DICOM SR structured reports, and quick patient share link with QR code.

---

## 5. 3D & 2D Multi-Planar Reconstruction (MPR) Engine

The custom MPR system in [`paxs-mpr.js`](file:///D:/Paxs%20Archive/app/static/viewer/paxs-mpr.js) enables true volumetric clinical viewing:

* **3D Volumetric Scene (Three.js)**:
  * Interactive 3D bounding box with live real-time cutplanes (Axial, Coronal, Sagittal).
  * 3D spatial orientation cube (A/P, L/R, S/I) and orbit camera controls.
* **4-Corner ACR Radiology Overlays**:
  * Top-Left: Patient Name, Patient ID, Modality.
  * Top-Right: Institution Name, Study Date/Time.
  * Bottom-Left: Slice Location (mm), Slice Thickness, Active Cutplane index.
  * Bottom-Right: Window Width / Window Center (W/L) and live **Hounsfield Unit (HU)** under cursor.
* **WebGL2 Hardware Acceleration**:
  * Interpolates 3D voxel matrices on the GPU for instant reslicing without network lag.

---

## 6. How to Customize & Theme the Viewer

### A. Changing Theme Colors and Tool Styling
Edit [`app/static/viewer/paxs-viewer-theme.css`](file:///D:/Paxs%20Archive/app/static/viewer/paxs-viewer-theme.css):
* CSS variables in `:root`:
  * `--paxs-navy`: Background obsidian color (`#020617`).
  * `--paxs-blue` / `--paxs-sky`: Primary action colors (`#38bdf8`).
  * `--paxs-emerald`: Measurement tools (`#34d399`).
  * `--paxs-amber`: Window/Level tools (`#fbbf24`).

### B. Modifying the Study List Experience
Edit [`app/static/viewer/paxs-study-list-enhance.js`](file:///D:/Paxs%20Archive/app/static/viewer/paxs-study-list-enhance.js):
* Configures thumbnail resolution and fallback avatars with patient initials.
* Sets header brand click redirection to `/viewer`.

### C. Updating the Logo & Favicon
* Place your logo image at [`app/static/logo/logo.png`](file:///D:/Paxs%20Archive/app/static/logo/logo.png).
* Favicon is served from [`app/static/logo/logo.ico`](file:///D:/Paxs%20Archive/app/static/logo/logo.ico).
* The header automatically sets logo height to `32px` with smooth contrast drop-shadow.

---

## 7. Diagnostic Reporting & Template System

PAXS Viewer includes a built-in **Diagnostic Report Editor** accessible directly from the viewer footer (`📝 Diagnostic Report`):

* **Auto Metadata Fill**: Loads Patient Name, ID, Modality, Accession Number, and Study Description automatically.
* **Clinical Templates**: Auto-suggests templates based on the modality (e.g. *Chest CT w/ Contrast*, *Brain MRI*, *Standard X-Ray*).
* **Workflow Statuses**:
  * **Draft (`DRAFT`)**: Saves progress without signing.
  * **Finalized (`FINALIZED`)**: Signs and locks report into the database with audit tracking.
* **Print Engine**: Generates a standardized clinical PDF report ready for physical printing.

---

## 8. Building & Re-Compiling Frontend Assets

### Option 1: Live Static Updating (No Node.js build required)
Since customizations (styling, MPR, thumbnails, reporting) are cleanly modularized in `paxs-viewer-theme.css`, `paxs-study-list-enhance.js`, and `paxs-mpr.js`, you can **directly edit these files**.

After making changes:
1. Increment the version string (e.g., `?v=36`) in [`app/static/viewer/index.html`](file:///D:/Paxs%20Archive/app/static/viewer/index.html).
2. Hard-refresh the browser (**`Ctrl + F5`**).

### Option 2: Building the React Study List UI (`study-list-ui`)
If modifying the standalone React study list UI under `study-list-ui/`:
```cmd
cd study-list-ui
npm install
npm run build
```
The compiled output in `study-list-ui/dist` can be copied to `app/static/studies-ui/`.

---

## 9. Troubleshooting & Performance Optimization

| Issue | Root Cause | Solution |
|---|---|---|
| **Large CT/MR Slow Loading** | High uncompressed voxel count | Enable HTJ2K / JPEG-LS compression in Settings -> Storage -> Lossless Compression |
| **MPR 3D Volume Black Viewport** | WebGL2 disabled in browser | Enable Hardware Acceleration in browser settings (`chrome://settings/system`) |
| **Viewer Session Lost / 401** | JWT auth token expired | Sign in via Main Dashboard (`http://localhost:8000`), token is shared via cookie/localStorage |
| **Logo or Styles Cached** | Browser cached old bundle | Increment `?v=XX` in `app/static/viewer/index.html` and press `Ctrl + F5` |
