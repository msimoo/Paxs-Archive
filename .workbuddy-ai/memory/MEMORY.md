# PAXS Archive — durable project conventions

Curated, long-lived notes. Daily detail lives in `YYYY-MM-DD.md`; this file is for
things that stay true across sessions.

## Environment

- **Backend binds `:8000`** (`PAXS_PORT`; `.env`, `run_paxs.py`, `deploy/docker-compose.lb.yml`).
  `:8042` is Orthanc's default — seeing it means a stale template copy.
- **Python venv:** `./venv/Scripts/python.exe` (Windows). Prefer it over the managed runtime
  for anything that imports `app.*`, since the project deps (pydantic, pydicom, numpy,
  sqlalchemy) are installed there.
- **Node for tests:** `C:/Users/Mohamed Omer/.workbuddy-ai/binaries/node/versions/22.22.2-3/node.exe`
  with `NODE_PATH=C:/Users/Mohamed Omer/.workbuddy-ai/binaries/node/workspace/node_modules` (jsdom).
- **Redis is live** on `127.0.0.1:6379` and `REDIS_CACHE_ENABLED` defaults to `True`. It backs
  the L2 tiers (`paxs:frame:*`, `paxs:vol:*`, tile PNGs). Tests that assert decode counts MUST
  disable it, or a previous run's cache makes results non-deterministic.
- `app/api/v1/metrics.py` is the Prometheus module (there is no `app/core/metrics.py`).

## Routes / mounts (see README "Web interfaces")

`/` dashboard · `/studies-ui` (**plural**; `/study-ui` is invalid) · `/viewer` (study-list
shell, or the viewer bundle with `?StudyInstanceUIDs=`) · `/admin` · `/settings` · `/login`.
Deep links use **PascalCase** `?StudyInstanceUIDs=`; patient shares use `/viewer?share=`.

## Conventions to preserve

- **Logging is PHI-free.** Series/SOP UIDs, modality and pixel-layout attributes are fine;
  patient demographics and filesystem paths are not. `app/core/dicom_diagnostics.py` exists
  for this — use `decode_context(ds)` in decode failure paths.
- **Viewer mode 503s are never hard blocks.** `_server_mode_active` / `_client_mode_active`
  return `False` for `hybrid` (the default), and `?force=1` bypasses. Don't "tighten" them.
- **`hybrid` is the shipped `VIEWER_PROCESSING_MODE`**; negotiate is a *gate* in the viewer,
  not a hint raced against a deadline (V3-3 declined — reasoning recorded in `paxs-mpr.js`).
- **Cache memory is bounded by bytes**, not entry counts: `VOLUME_CACHE_BUDGET_MB`,
  `DICOM_FRAME_CACHE_MB`. Both are independent of `HYBRID_SERVER_VOL_BUDGET_MB` on purpose
  (policy input vs hard cap). `0` means *disabled* and must not be masked by a default.
- **SPA shells are served through `_spa_shell()`** in `main.py` so reverse-proxy prefix
  rewriting and the runtime boot shim apply. With no prefix configured it degrades to
  `FileResponse` — byte-identical. Add new SPA entry points through it.
- **`/metrics` is not public.** Access is by source address via `METRICS_ALLOW_IPS`
  (default loopback + RFC1918). Prometheus scrapes without credentials.
- **`resolve_public_base_url(request)`** is the single source for browser-facing absolute
  URLs (share links, QR, FHIR, admin). Never emit `PUBLIC_BASE_URL` raw — it may be empty.

## Test suites

All in `.workbuddy-ai/tmp/`. Run each as a **separate Bash call** (a `for` loop with `$()`
and `>/dev/null` misbehaves in this sandbox). Every suite prints `  PASS: N   FAIL: N` and
exits non-zero on failure.

| Suite | Covers |
|---|---|
| `about-test.js` / `toolbar-test.js` / `icon-test.js` / `cascade-test.js` | viewer UI enhancements |
| `auth-guard-test.js` | V1-1 global 401 refresh-and-retry |
| `tex-upload-test.js` | V3-2 chunked 3D texture upload |
| `fetch-deadline-test.js` | V3-5 volume fetch deadlines |
| `spa-404-test.py` | V1-5 SPA 404 masking |
| `volume-coalesce-test.py` | V3-1 in-flight coalescing, V3-6 LRU + byte budget |
| `proxy-prefix-test.py` / `proxy-integration-test.py` | V2-3/V2-4 reverse proxy |
| `memory-observability-test.py` | memory budgets, cache metrics, decode diagnostics, metrics auth |

JS suites are extracted-source harnesses: they pull the real function text out of the
shipping file with brace matching and drive it against mocks, so the test cannot drift from
what ships. Prefer that pattern over re-implementing logic in the test.

## Gotchas that have bitten

- `py_compile` does **not** catch missing imports or call-site signature drift. Follow a
  compile with an import/boot smoke test, and grep every caller when changing a signature
  (this is how `/api/v1/config` broke silently).
- `SpaStaticFiles` masks 404s with `index.html` — fixed for asset extensions, but remember
  it when a missing file looks like a 200.
- In FastAPI test routes an **unannotated** `request` param becomes a *query* param → 422.
- `.env` sets no `PUBLIC_BASE_URL` / `VIEWER_PROCESSING_MODE` / `REDIS_CACHE_ENABLED`, so the
  code defaults apply (`hybrid`, Redis on, `PUBLIC_BASE_URL` empty → derived per request).
