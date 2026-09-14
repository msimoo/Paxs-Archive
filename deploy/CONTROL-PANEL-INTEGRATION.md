# PaxsViewer Control Panel — web integration

The **PaxsViewer Control Panel** (from `D:\Github\control-panel-electron`, the
Electron app by Mohamed O. Elsamani) is a service monitor for the
Viewers/Orthanc stack. This document explains how it is served from the PAXS
Archive server as a web dashboard at **`/admin`** — the same approach used for
PaxsViewer at `/viewer`.

## What it monitors

| Service | Default port | Notes |
|---|---|---|
| Orthanc DICOM Server | 8042 | `../orthanc/Orthanc.exe` |
| Viewer Python proxy | 3000 | `serve_orthanc.py` |
| Admin Node API | 4000 | `platform/server` |
| Admin frontend (Vite) | 5173 | `npm run dev` |
| MySQL database | — | Windows service / systemd unit |

Health is checked with **real** probes: a TCP connect to each port and
`sc query` (Windows) / `systemctl` or `pgrep` (Linux) for MySQL.

## How the integration works

1. **Static bundle** — the built renderer (`out/renderer` after
   `npm run build` in `control-panel-electron`) lives in
   `app/static/admin/` with a `admin-version.txt` marker. `main.py` mounts it
   with the same SPA static mount as the viewer:

   ```python
   app.mount("/admin", SpaStaticFiles(directory=ADMIN_DIR, html=True), name="admin")
   ```

2. **Web bridge** — the renderer is an Electron app that talks exclusively
   through `window.electronApi` (IPC). `app/static/admin/admin-bridge.js` is
   injected into `index.html` before the bundle and installs a compatible
   `window.electronApi` backed by PAXS REST endpoints:

   | electronApi method | PAXS endpoint |
   |---|---|
   | `getInitialSnapshot` | `GET /api/v1/admin-panel/status` |
   | `getLogs` | `GET /api/v1/admin-panel/logs` |
   | `startAll` / `stopAll` | `POST /api/v1/admin-panel/actions/start-all` · `stop-all` |
   | `restartService` | `POST /api/v1/admin-panel/actions/restart` |
   | `launchViewer` / `launchAdmin` | `POST …/actions/launch-viewer` · `launch-admin` |
   | `openViewerInBrowser` / `openAdminInBrowser` | `POST …/actions/open-viewer` · `open-admin` |
   | `getConfig` / `saveConfig` | `GET` / `POST /api/v1/admin-panel/config` |
   | `onHealthUpdate` / `onServiceStatusChanged` / `onLogEntry` | polling — the bridge polls `/status` + `/logs` every `monitorIntervalMs` and re-dispatches the events |

3. **Auth** — the panel rides the archive's own login: the bridge reads
   `localStorage["paxs_token"]` and sends `Authorization: Bearer …`; a 401
   redirects to `/`. Status/logs/config-get require any authenticated user;
   **actions and config-save require ADMIN**.

4. **Config persistence** — ports/paths/interval are stored in
   `admin_panel.json` beside the archive data (override the location with
   `ADMIN_PANEL_CONFIG_FILE`). The Settings tab edits them; only services PAXS
   itself started this session can be stopped (a foreign service on the port
   is reported as *running — not managed by this session*).

## Backend modules

- `app/api/v1/admin_panel.py` — the REST router.
- `app/services/admin_panel_service.py` — config, health probes, activity
  log, best-effort process control (mirrors the Electron `ServiceManager`).
- `app/static/admin/admin-bridge.js` — the in-browser `electronApi` shim.

## Rebuilding the bundle

Requires the fork checked out at `D:\Github\control-panel-electron`
(node_modules installed — it ships with the repo):

```bat
cd D:\Github\control-panel-electron
npm run build            REM -> out/renderer
```

Then sync into PAXS (strips maps, writes the version marker, injects the
bridge script):

```bat
build-win\build.bat      REM step 0 syncs /admin from the fork automatically
```

or manually:

```bat
robocopy D:\Github\control-panel-electron\out\renderer app\static\admin /E
```

and re-inject the bridge if you replaced `index.html` (a Python one-liner
inserts `<script src="./admin-bridge.js"></script>` before the module script).

## Verification

```bat
venv\Scripts\python -m pytest tests\test_admin_panel.py -q
```

Covers the mount (200 + bridge injection), the SPA fallback, the auth gate,
the snapshot shape, config round-trip and clamping, and the action endpoints.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `/admin` 404s | The bundle is missing from `app/static/admin/` — run the sync step above. |
| Panel shows all "Unknown" | You're not logged in (or the token expired) — the bridge redirects to `/`; log in and reopen `/admin`. |
| "running but not managed by this session" | The service was started outside PAXS (e.g. by the Electron Control Panel); stop it there or via task manager. |
| Start buttons do nothing | The configured path (`orthancPath`, `apiDir`, …) doesn't exist on this machine — fix it in the panel's Settings tab. |
