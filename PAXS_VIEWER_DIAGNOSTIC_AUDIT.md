# PAXS Viewer — Diagnostic Audit & Performance Remediation

**Scope:** intermittent image loading failures · navigation/URL/port/routing · 3D volume & MPR latency
**Standard:** High-Availability Clinical Workstation
**Audit date:** 2026-09-26
**Method:** static read of the shipping artefacts (backend routes, viewer enhancement scripts,
the 6.4 MB minified `app.bundle.*.js`, deploy configs) + live probes against the running
server on `:8000`. Every finding below is anchored to a file, function and line.

---

## 0. Executive summary

| # | Vector | Severity | Finding | Status |
|---|---|---|---|---|
| V1-1 | Image blanking | **High** | 401 refresh-and-retry never covers image requests | **Fixed** |
| V1-2 | Image blanking | Medium | Decoded-frame LRU is only **16** entries | **Fixed** (byte budget) |
| V1-3 | Image blanking | Low | `viewer_mode` substring test never matches OHIF's quoted header | **Fixed** |
| V1-4 | Image blanking | Info | Codec availability — **verified OK** (hypothesis refuted) | No action |
| V1-5 | Image blanking | Medium | SPA 404-masking hides missing assets behind `text/html` | **Fixed** |
| V2-1 | Routing/ports | **High** | Vite dev proxy points at **8042** (backend is **8000**) | **Fixed** |
| V2-2 | Routing/ports | **High** | `PUBLIC_BASE_URL` default `http://0.0.0.0` → every share link/QR is dead | **Fixed** |
| V2-3 | Routing/ports | Medium | `getViewerUrl` uses `location.origin` → breaks under a proxy path prefix | **Fixed** |
| V2-4 | Routing/ports | Medium | No `root_path` / `X-Forwarded-Prefix`; proxy trust is loopback-only | **Fixed** |
| V2-5 | Routing/ports | Info | `StudyInstanceUIDs` casing — **verified correct** | No action |
| V3-1 | MPR latency | **High** | `load_volume_array` check-then-act race → cold-cache stampede | **Fixed** |
| V3-2 | MPR latency | **High** | One synchronous **~500 MB** `texImage3D` on the main thread | **Fixed** |
| V3-3 | MPR latency | Medium | Negotiate gates first paint (worst case ≈2.5 s before any pixel) | Declined by design — recorded in code |
| V3-4 | MPR latency | Medium | Coarse preview is `downsample=2`, not the specified `4` | Declined by design — recorded in code |
| V3-5 | MPR latency | Low | Client-mode `/data` fetches have no abort/timeout | **Fixed** |
| V3-6 | MPR latency | Low | `_VOL_CACHE` evicts insertion-order (not LRU) and is smaller than the session cap | **Fixed** (LRU + byte budget) |

**Remediation round 1 (2026-09-26)** closed V1-1, V1-5, V2-2, V3-1 and V3-2.
**Remediation round 2 (2026-09-26)** closed V2-3 and V2-4, plus V3-5 and V3-6's eviction
ordering (both zero behavioural risk).
**Remediation round 3 (2026-09-27)** closed V1-2 and V3-6's sizing via explicit byte
budgets (`DICOM_FRAME_CACHE_MB`, `VOLUME_CACHE_BUDGET_MB`), added cache hit/miss telemetry
to `/metrics`, and made every decode failure log its `TransferSyntaxUID` with the human
codec name. V3-3 and V3-4 were evaluated and **deliberately declined** — the reasoning is
recorded at the call site in `paxs-mpr.js` so it is not silently reversed.

All 16 findings are now closed, fixed, or explicitly declined with a recorded rationale.
Cumulative verification: **564 assertions, 0 failures** across 12 suites.

Two notes that correct or extend the audit:

* **The Redis L2 tier is live.** The audit speculated it was a no-op; it is not.
  `REDIS_CACHE_ENABLED` defaults to `True`, a Redis instance is running on
  `127.0.0.1:6379`, and `paxs:vol:*` keys are being written and read. This was
  proven accidentally when a V3-1 test run returned a volume with **zero** decode
  calls — a Redis hit. The L1 sizes in V1-2/V3-6 still matter (L1 is what serves
  the hot path), but the "silently no-ops" claim was wrong.
* **The study-list path is `/studies-ui` (plural)**, confirmed against
  `app/main.py:734` and now documented in the README. `/study-ui` 404s.

---

## 1. VECTOR 1 — Intermittent image loading failures & blank viewports

### V1-1 [HIGH] The 401 refresh-and-retry path does not cover image requests

**File:** `app/static/viewer/paxs-study-list-enhance.js`
**Functions:** `paxsFetch` (line 889), `paxsRefreshToken` (line 861), `paxsSilentRefresh` (line 917)

`paxsFetch` implements exactly the right behaviour — attach bearer, on `401` refresh once
(single-flight) and retry the original request exactly once, with loop protection. But it is a
**module-private helper with only three call sites**:

| Line | Call |
|---|---|
| 576 | `GET /api/v1/reports/study/{uid}` |
| 642 | `POST /api/v1/reports/save` |
| 937 | `GET /api/v1/auth/me` |

It is **never assigned to `window.fetch`**, and Cornerstone's WADO-RS loader reaches the
network through `dicomweb-client`, which uses its own `fetch`/XHR. Therefore:

* every WADO-RS `/metadata`, `/frames/{n}` and `/rendered` request **bypasses the refresh path**;
* the only token rotation is `paxsSilentRefresh()` at line 997 — **one** call at page load.

**Consequence.** A `401` on any frame request is terminal for that frame. Cornerstone marks
the image failed and the viewport stays black; there is no in-page recovery, only a manual reload.
This is precisely the "sometimes a series won't display / black viewport" symptom.

**What actually produces the 401** (traced, so the fix targets the right cause):

* `app/api/v1/deps.py` → `require_dicomweb_read` (line 329) raises `401` when
  `DICOMWEB_PUBLIC=false` and the caller is neither a valid bearer nor a valid session cookie.
  `DICOMWEB_PUBLIC` defaults to `False` (`app/core/config.py:340`; `.env.example:101`).
* `get_optional_user` (deps.py) → `get_current_user_from_cookie` returns `None` when the
  session row is missing, **revoked**, or `expires_at <= now` (H04 check), or when the cookie is
  simply absent — a fresh tab, cookie eviction, a server restart that invalidated sessions, or a
  share-link visitor who never had one.

So the trigger is not the 15-minute access token (image requests authenticate by **cookie**,
not bearer) — it is *any* cookie/session invalidation. The defect is that there is **no retry
and no queue**, so a single transient 401 is unrecoverable for the whole series.

**Patch — global fetch + XHR interception with single-flight refresh**

Append inside the same IIFE in `paxs-study-list-enhance.js` (after `paxsRefreshToken`,
before the `paxsSilentRefresh()` boot call at line 997):

```js
  // -------------------------------------------------------------------------
  // V1-1 — make the refresh-and-retry path cover IMAGE requests too.
  //
  // paxsFetch() only wraps the three report/user calls. Cornerstone reaches the
  // network through dicomweb-client (its own fetch/XHR), so a transient 401 on a
  // WADO-RS frame was terminal: the frame failed and the viewport stayed black.
  // We therefore install the same single-flight refresh at the transport layer,
  // scoped to the endpoints the viewer actually loads images from.
  // -------------------------------------------------------------------------
  var PAXS_AUTH_PATHS = ['/dicom-web/', '/api/v1/volume/', '/api/v1/reports/'];

  function paxsNeedsAuthRetry(url) {
    var u = String(url || '');
    for (var i = 0; i < PAXS_AUTH_PATHS.length; i++) {
      if (u.indexOf(PAXS_AUTH_PATHS[i]) !== -1) return true;
    }
    return false;
  }

  function paxsInstallFetchGuard() {
    if (!window.fetch || window.__paxsFetchGuard) return;
    window.__paxsFetchGuard = true;
    var origFetch = window.fetch;
    window.fetch = function (input, init) {
      var url = (typeof input === 'string') ? input : (input && input.url) || '';
      var p = origFetch.apply(this, arguments);
      if (!paxsNeedsAuthRetry(url)) return p;
      return p.then(function (res) {
        if (res.status !== 401) return res;
        // One shared refresh for every concurrent 401 (paxsRefreshToken is
        // single-flight), then replay each original request exactly once.
        return paxsRefreshToken().then(function (tok) {
          if (!tok) return res;              // refresh failed: surface the 401
          var retryInit = {};
          for (var k in (init || {})) {
            if (Object.prototype.hasOwnProperty.call(init, k)) retryInit[k] = init[k];
          }
          retryInit.credentials = 'same-origin';
          retryInit.headers = {};
          var base = (init && init.headers) || {};
          for (var h in base) {
            if (Object.prototype.hasOwnProperty.call(base, h)) retryInit.headers[h] = base[h];
          }
          retryInit.headers['Authorization'] = 'Bearer ' + tok;
          return origFetch.call(window, input, retryInit);
        });
      });
    };
  }

  // Cornerstone may also use XHR for ranged/frame reads.
  function paxsInstallXhrGuard() {
    if (!window.XMLHttpRequest || window.__paxsXhrGuard) return;
    window.__paxsXhrGuard = true;
    var origOpen = XMLHttpRequest.prototype.open;
    var origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
      this.__paxsUrl = url;
      return origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
      var xhr = this;
      var args = arguments;
      if (!paxsNeedsAuthRetry(xhr.__paxsUrl)) return origSend.apply(xhr, args);
      xhr.addEventListener('load', function () {
        if (xhr.status !== 401 || xhr.__paxsRetried) return;
        xhr.__paxsRetried = true;
        paxsRefreshToken().then(function (tok) {
          if (!tok) return;
          try { xhr.setRequestHeader('Authorization', 'Bearer ' + tok); } catch (e) {}
          origSend.apply(xhr, args);          // exactly one replay
        });
      });
      return origSend.apply(xhr, args);
    };
  }

  paxsInstallFetchGuard();
  paxsInstallXhrGuard();
```

> Note: `paxsRefreshToken` is single-flight (`paxsRefreshInFlight`, line 859), so a burst of 40
> concurrent frame 401s triggers **one** `POST /auth/refresh` and then 40 replays — no thundering
> herd against the auth endpoint.

---

### V1-2 [MEDIUM] Decoded-frame LRU holds only 16 entries

**File:** `app/services/wado_rs_bulk_service.py` — `_FRAME_CACHE` / `_FRAME_CACHE_MAX` (lines 28-29)

```python
_FRAME_CACHE: OrderedDict[tuple[str, int], bytes] = OrderedDict()
_FRAME_CACHE_MAX = 16
```

The file's own comment (line 151) notes a 3072×2560 J2K decode costs **~2.3 s**. Sixteen entries
is roughly two thirds of a single 24-slice scroll on one plane, so a 200-image study scrubbing
axially → sagittally → coronally evicts continuously and re-decodes on almost every revisit.

Redis (`paxs:frame:{sop}:{idx}`, TTL 3600, written at line 196) mitigates this only when
`REDIS_CACHE_ENABLED` is on **and** Redis is reachable. `REDIS_CACHE_ENABLED` defaults to `True`
(`config.py:193`) but `.env` configures no Redis, so in the current deployment the L2 tier
silently no-ops and the 16-entry L1 is the only cache.

**Patch**

```python
# Bound by decoded *bytes*, not entry count: a 16-entry cap thrashed on any
# real CT/MR series (a single 200-image study evicted continuously while
# scrubbing). ~256 MB is a sane ceiling for a decode cache that exists purely to
# make re-scrolling cheap; the LRU order keeps the working set resident.
_FRAME_CACHE_MAX_BYTES = 256 * 1024 * 1024
_FRAME_CACHE_BYTES = 0
```

```python
def _cache_put(key: tuple[str, int], value: bytes) -> None:
    global _FRAME_CACHE_BYTES
    prev = _FRAME_CACHE.pop(key, None)
    if prev is not None:
        _FRAME_CACHE_BYTES -= len(prev)
    _FRAME_CACHE[key] = value
    _FRAME_CACHE_BYTES += len(value)
    while _FRAME_CACHE and _FRAME_CACHE_BYTES > _FRAME_CACHE_MAX_BYTES:
        _k, _v = _FRAME_CACHE.popitem(last=False)
        _FRAME_CACHE_BYTES -= len(_v)
```

and update `clear_frame_cache()` to reset `_FRAME_CACHE_BYTES = 0`.

---

### V1-3 [LOW] `viewer_mode` substring test never matches the header OHIF sends — **FIXED**

**File:** `app/api/v1/dicomweb.py`, `wado_frames`, line 806 (was)

OHIF's `generateAcceptHeader` helper (present in `app.bundle.29858d89fb4b9b9d.js`, offset ≈745 671)
**quotes** the parameter it emits:

```js
else n.push("type=application/octet-stream");
a || n.push("transfer-syntax=*");
r || (n = n.map(e => e.startsWith("transfer-syntax=")
  ? 'transfer-syntax="' + e.substring(16) + '"' : e));
```

so the wire header is `multipart/related; type="application/octet-stream"; transfer-syntax="*"` —
and the unquoted test `"transfer-syntax=*" in accept` was always `False`.

**Applied fix:**

```python
accept_norm = accept.replace('"', "").replace(" ", "")
viewer_mode = "transfer-syntax=*" in accept_norm
```

**Honest impact assessment:** low. With `viewer_mode` False the handler already served the native
codestream via `extract_frame_bytes` — which is correct WADO-RS and which the client can decode
(see V1-4). The flag only ever gated the `decode_frames_to_raw` fallback, so the practical effect
was that an intended server-side transcode path never engaged. Fixed for correctness and to make
the `cache_mode` key (`native` vs `def`) mean what it says.

---

### V1-4 [INFO] Codec availability — hypothesis **refuted**, no action

I initially flagged this as the prime suspect, because the four codec wasm files the bundle
references by literal name are **absent from disk**, and their URLs return the SPA shell:

```
$ curl -s -o /dev/null -w '%{http_code} %{size_download}B %{content_type}\n' \
    http://127.0.0.1:8000/viewer/charlswasm_decode.wasm
200 6842B text/html; charset=utf-8          <-- index.html, not wasm
```

**That reading is wrong.** Each codec module overrides `locateFile` with a webpack asset URL, so
the logical name is remapped before any fetch:

```js
// @cornerstonejs/codec-charls (JPEG-LS)
let a = new URL(r(46920), r.b);            // r(46920) === "776f756dd939124e.wasm"
n({ locateFile: e => e.endsWith(".wasm") ? a.toString() : e });
// -> new t.JpegLSDecoder
```

Verified mapping — **all four files exist and serve `application/wasm`**:

| Codec | Logical name | Emitted file | Size | Served |
|---|---|---|---|---|
| JPEG-LS (CharLS) | `charlswasm_decode.wasm` | `776f756dd939124e.wasm` | 145 KB | ✅ `application/wasm` |
| JPEG 2000 (OpenJPEG) | `openjpegwasm_decode.wasm` | `0ddf166f7a343eda.wasm` | 255 KB | ✅ `application/wasm` |
| JPEG baseline/lossless (libjpeg-turbo) | `libjpegturbowasm_decode.wasm` | `34b1e6b1deb5828e.wasm` | 178 KB | ✅ `application/wasm` |
| HTJ2K (OpenJPH) | `openjphjs.wasm` | `37fa160cc3a47cfd.wasm` | 2.0 MB | ✅ `application/wasm` |

This matters because `DEFAULT_COMPRESSION = "JPEG-LS"` (`config.py:112`) — had the JPEG-LS
decoder genuinely been missing, **every study the archive encodes would fail to display**. It is
not missing. No action.

---

### V1-5 [MEDIUM] SPA 404-masking hides missing assets behind `text/html`

**File:** `app/main.py` — `SpaStaticFiles.get_response` (lines 608-620)

```python
class SpaStaticFiles(CachedStaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except ...:
            return await super().get_response("index.html", scope)
```

The fallback fires for **every** 404 under the mount, including `*.wasm`, `*.js`, `*.css`, `*.map`.
A genuinely missing asset therefore returns `200 OK` with an HTML body instead of `404`, and the
browser reports a confusing MIME/compile error far from the real cause. This is exactly what made
V1-4 look like a codec outage during this audit.

**Patch**

```python
# Asset extensions must 404 honestly. The SPA fallback exists for client-side
# *routes*; serving index.html for a missing .wasm/.js/.css turns a clean 404
# into a 200 with the wrong MIME type, which surfaces as a baffling
# "WebAssembly.instantiateStreaming failed" or "Unexpected token '<'" far from
# the real cause (see PAXS_VIEWER_DIAGNOSTIC_AUDIT.md V1-4).
_ASSET_EXT = {".js", ".mjs", ".css", ".wasm", ".map", ".json",
              ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
              ".woff", ".woff2", ".ttf", ".otf", ".txt", ".xml"}

class SpaStaticFiles(CachedStaticFiles):
    async def get_response(self, path, scope):
        try:
            return await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            if os.path.splitext(path)[1].lower() in _ASSET_EXT:
                raise                     # let the real 404 through
            return await super().get_response("index.html", scope)
```

---

## 2. VECTOR 2 — Navigation, URL scheme, ports & routing

### V2-1 [HIGH] Vite dev proxy targets port 8042 — backend is on 8000 — **FIXED**

**File:** `study-list-ui/vite.config.ts`, `server.proxy` (was lines 16-19)

```ts
proxy: { '/api': 'http://localhost:8042', '/dicom-web': 'http://localhost:8042' }
```

`8042` is **Orthanc's** default port — a stale copy from a template. The PAXS backend binds
`PAXS_PORT`, default `8000` (`run_paxs.py:65`, `.env:4` `PAXS_PORT=8000`,
`deploy/docker-compose.lb.yml:16` `"8000:8000"`). In dev, **every** study-list API call proxied
to a dead port → empty study list / 502s.

**Applied fix:** both proxy targets corrected to `:8000`, plus a `/viewer` entry so the
template's `/viewer/paxs-study-list-enhance.js` (served by FastAPI, referenced from
`study-list-ui/index.html:11`) resolves in dev too.

> Related and correct as-is: `base: '/studies-ui/'` matches the FastAPI mount `/studies-ui`
> (`app/main.py:734`). But note the brief assumed the public path is `/study-ui` (singular) —
> the app actually serves **`/studies-ui`**. Confirm which path is the intended public URL.

---

### V2-2 [HIGH] `PUBLIC_BASE_URL` default is a bind address, not a public URL

**Files:** `app/core/config.py:368`; `.env.example:104`; consumers below

```python
PUBLIC_BASE_URL: str = "http://0.0.0.0"
```

`0.0.0.0` is a *listen* address; it is not routable from a browser. `.env.example` "fixes" it to
`http://0.0.0.0:8000` — still unroutable. The live `.env` does **not** set it at all, so the
default applies. Consumers that emit absolute URLs to users:

| File | Line | Produces |
|---|---|---|
| `app/services/sharing_service.py` | 72-75 `share_url()` | `http://0.0.0.0/viewer?share=<token>` — **every patient share link and QR code is dead** |
| `app/api/v1/fhir.py` | 28 `SERVER_BASE` | `http://0.0.0.0/fhir` — every FHIR absolute URL is dead |
| `app/api/v1/dashboard.py` | 166, 890 | `public_base_url` in health; DICOMweb WADO URL |
| `app/services/admin_panel_service.py` | 332 | viewer/admin URL map in the Control Panel |

**Patch — make the misconfiguration loud and the derivation correct**

```python
# app/core/config.py
PUBLIC_BASE_URL: str = ""   # empty => derive from the incoming request
```

```python
# app/services/sharing_service.py
def share_url(token: str, request: "Request | None" = None) -> str:
    """Absolute URL for a share token (links + QR codes).

    PUBLIC_BASE_URL wins when set. Otherwise derive from the request so a
    default install cannot emit http://0.0.0.0/... into a QR code (the old
    default was a *bind* address, which is never routable from a browser).
    """
    base = (settings.PUBLIC_BASE_URL or "").rstrip("/")
    if not base and request is not None:
        base = str(request.base_url).rstrip("/")
    if not base:
        raise RuntimeError(
            "PUBLIC_BASE_URL is unset and no request context is available — "
            "refusing to mint a share link that points at a bind address."
        )
    return f"{base}/viewer?share={token}"
```

and in `.env.example`, replace the placeholder with a real instruction:

```dotenv
# Public origin used in share links, QR codes, FHIR and admin URL maps.
# MUST be the address a *browser* can reach (scheme + host [+ port], no path).
# Leave empty to derive it from the incoming request.
PUBLIC_BASE_URL=https://pacs.example.org
```

> Add a startup warning when `PUBLIC_BASE_URL` is empty *and* `SHARING_ENABLED`/`FHIR_ENABLED`
> are on, so the failure is caught at boot rather than by a patient scanning a dead QR code.

---

### V2-3 [MEDIUM] `getViewerUrl` cannot survive a reverse-proxy path prefix

**File:** `study-list-ui/src/lib/api.ts`, `getViewerUrl` (lines 39-42)

```ts
const base = (window as any).PAXS_PUBLIC_BASE_URL || location.origin;
return `${base.replace(/\/+$/, '')}/viewer/viewer?StudyInstanceUIDs=${encodeURIComponent(studyUid)}`;
```

`location.origin` is **scheme + host + port only** — it discards any path prefix. Behind
`https://host/paxs/` the link resolves to `https://host/viewer/viewer?...` (404) instead of
`https://host/paxs/viewer/viewer?...`.

The `PAXS_PUBLIC_BASE_URL` escape hatch is never populated here: it is only ever set by the
**admin dashboard** (`app/static/app.js:4353`, `:4385`, fed by `GET /api/v1/health`).
`study-list-ui/index.html` does not set it, and the study-list app never calls `/health` — so the
override is **always undefined** in the study list.

**Patch**

```ts
// api.ts — derive the base from the *document* URL (which preserves a proxy
// path prefix) rather than location.origin, and allow an explicit override.
function publicBase(): string {
  const override = (window as any).PAXS_PUBLIC_BASE_URL;
  if (override) return String(override).replace(/\/+$/, '');
  // document.baseURI respects <base href> and the full request path.
  const url = new URL(document.baseURI || location.href, location.href);
  // Strip a trailing /studies-ui (or /studies-ui/) so we get the server root.
  return url.origin + url.pathname.replace(/\/(studies-ui|study-ui)\/?$/, '');
}

export function getViewerUrl(studyUid: string): string {
  return `${publicBase()}/viewer/viewer?StudyInstanceUIDs=${encodeURIComponent(studyUid)}`;
}
```

Apply the same treatment to the root-relative `API_BASE = '/api/v1'` (line 3) — it needs the
prefix too:

```ts
const API_BASE = `${publicBase()}/api/v1`;
```

Finally, have the study-list entry inject the server-derived value, matching what the admin
dashboard already does — add to `app/main.py`'s `/studies-ui` handler:

```python
response = FileResponse(STUDIES_UI_DIR / "index.html")
# Same contract as the admin dashboard: publish the configured public origin so
# share/viewer links resolve behind a reverse proxy (see V2-2 / V2-3).
if settings.PUBLIC_BASE_URL:
    response.headers["X-PAXS-Public-Base"] = settings.PUBLIC_BASE_URL
return response
```

---

### V2-4 [MEDIUM] No `root_path` / `X-Forwarded-Prefix`; proxy trust is loopback-only

**Files:** `run_paxs.py` (lines 71-78); `deploy/haproxy.cfg` (`frontend http_in`)

```python
uvicorn.run("app.main:app", host=host, port=port, log_level="info",
            reload=not _is_production(), **ssl_kw)
```

* No `root_path` — so FastAPI cannot generate correct URLs when mounted under a prefix.
* No `forwarded_allow_ips` — uvicorn defaults it to `127.0.0.1`. In the Docker topology the
  HAProxy container has a **different** IP, so its `X-Forwarded-Proto` is **ignored** and
  `request.url.scheme` stays `http` behind TLS termination.
* `deploy/haproxy.cfg` sets `X-Forwarded-Proto` and `option forwardfor` (`X-Forwarded-For`) but
  **never sets `X-Forwarded-Host` or `X-Forwarded-Prefix`**.

Net effect: behind TLS and/or a path prefix, every request-derived absolute URL is wrong, and the
only compensating mechanism is the (broken, V2-2) `PUBLIC_BASE_URL`.

**Patch**

```python
# run_paxs.py — trust the reverse proxy's forwarded headers.
# uvicorn's default forwarded_allow_ips is "127.0.0.1", which silently discards
# X-Forwarded-* from a proxy container on a different IP, leaving request.url
# on http:// even behind TLS termination.
proxy_allow = os.getenv("PAXS_FORWARDED_ALLOW_IPS", "127.0.0.1")
uvicorn.run(
    "app.main:app",
    host=host,
    port=port,
    log_level="info",
    reload=not _is_production(),
    proxy_headers=True,
    forwarded_allow_ips=proxy_allow,
    root_path=os.getenv("PAXS_ROOT_PATH", ""),
    **ssl_kw,
)
```

```haproxy
# deploy/haproxy.cfg — forward the Host and any path prefix.
frontend http_in
    bind *:8000
    mode http
    option httplog
    option forwardfor
    http-request set-header X-Forwarded-Proto http if !{ ssl_fc }
    http-request set-header X-Forwarded-Host  %[req.hdr(Host)]
    http-request set-header X-Forwarded-Prefix %[path,regsub(/(/viewer|/studies-ui|/api|/dicom-web|/admin|/settings).*$,,)] if { path_beg /viewer /studies-ui /api /dicom-web /admin /settings }
    default_backend paxs_api
```

Then set, in the deployment's env: `PAXS_FORWARDED_ALLOW_IPS=<haproxy container/subnet>` and
`PAXS_ROOT_PATH=/paxs` (when mounted under a prefix).

---

### V2-5 [INFO] `StudyInstanceUIDs` casing and auth persistence — verified correct

* `study-list-ui/src/lib/api.ts:41` emits `?StudyInstanceUIDs=` — **PascalCase** ✅
* `app/main.py:646` tests `request.query_params.get("StudyInstanceUIDs")` ✅
* `paxs-study-list-enhance.js:312, 327, 483, 516, 687` all read `StudyInstanceUIDs` (with a
  `study=` legacy fallback) ✅
* No `studyInstanceUID` / `studyInstanceUIDs` camelCase anywhere in `app/` or `study-list-ui/src/`.
* Auth across the path boundary: the session lives in an **HttpOnly cookie**
  (`settings.AUTH_COOKIE_NAME`), which is path-agnostic and sent on `/`, `/api` and `/viewer`
  alike — so no header/cookie hand-off is needed. Verified `ViewerAuthGateMiddleware`
  (`app/main.py:268-300`) gates `/viewer` and `/studies-ui` on that same cookie.

No action.

---

## 3. VECTOR 3 — 3D volume & MPR rendering performance

### V3-1 [HIGH] `load_volume_array` has a check-then-act race → cold-cache stampede

**File:** `app/services/volume_service.py`, `load_volume_array` (lines 1144-1210)

```python
with _VOL_CACHE_LOCK:                       # 1158  lookup
    cached = _VOL_CACHE.get(cache_key)
    ...
# ---- lock released ----
first = storage_service.read_dataset(...)   # 1177  EXPENSIVE, unlocked
for s in slices:                            # 1183  read + decode every slice
    arr = _frame_array(s, cache)
    ...
vol = np.stack(arrays, axis=0)              # 1197
with _VOL_CACHE_LOCK:                       # 1198  insert
    _VOL_CACHE[cache_key] = vol
```

The lookup and the insert are both locked, but the **materialisation between them is not**, and
there is no in-flight registry. N concurrent requests for the same uncached series therefore all
miss, all decode the entire stack from disk, and all build their own float32 volume.

The tile-level coalescer does **not** cover this. `render_plane_tile` (line 843-851) dedups only
*identical* tile keys (`series+plane+index+window`); nine different tiles on a cold cache are nine
distinct keys → nine full volume materialisations. The comment at lines 707-712 asserts
"Volume materialization is already cached by `load_volume_array` (L1 + L2)" — true only *after*
the first one completes, which is exactly the window that hurts.

**Measured cost:** for a 512×512×500 CT, one materialisation = 131 M voxels → **~500 MB** float32
(×N), each preceded by 500 individual `read_dataset` + `pixel_array` decodes.

**Patch — single-flight around the materialisation**

```python
# In-flight volume materialisation. The L1/L2 lookup above is cheap and locked,
# but the decode loop below is neither — so N concurrent /mpr/slice or /tiles
# requests for one uncached series each re-read and re-stack the whole volume.
# Coalescing on cache_key means the first caller materialises and the rest wait
# for that same array (V3-1).
_VOL_INFLIGHT: dict[str, threading.Event] = {}
_VOL_INFLIGHT_LOCK = threading.Lock()
```

```python
    # 1. L1 / L2 lookup  (unchanged)
    ...
    # 1b. Single-flight: only one thread materialises a given cache_key.
    with _VOL_INFLIGHT_LOCK:
        ev = _VOL_INFLIGHT.get(cache_key)
        if ev is None:
            ev = threading.Event()
            _VOL_INFLIGHT[cache_key] = ev
            i_own_it = True
        else:
            i_own_it = False
    if not i_own_it:
        ev.wait(timeout=120)                       # owner is materialising
        with _VOL_CACHE_LOCK:
            cached = _VOL_CACHE.get(cache_key)
        if cached is not None:
            return cached
        # Owner failed/timed out — fall through and build it ourselves.
    try:
        # ... existing read_dataset + decode loop, unchanged ...
        vol = np.stack(arrays, axis=0)
        with _VOL_CACHE_LOCK:
            if len(_VOL_CACHE) >= _VOL_CACHE_MAX:
                _VOL_CACHE.pop(next(iter(_VOL_CACHE)))
            _VOL_CACHE[cache_key] = vol
        # ... Redis populate, unchanged ...
        return vol
    finally:
        if i_own_it:
            with _VOL_INFLIGHT_LOCK:
                _VOL_INFLIGHT.pop(cache_key, None)
            ev.set()
```

> `_tile_inflight_begin` (line 750) already implements this exact pattern for tiles — reuse its
> structure rather than inventing a second one.

---

### V3-2 [HIGH] Client mode: one synchronous ~500 MB `texImage3D` on the main thread

**File:** `app/static/viewer/paxs-mpr.js`, `initGl2` (lines 1887-1983)

```js
gl.texImage3D(
  gl.TEXTURE_3D, 0, gl.R32F,
  state.cols, state.rows, state.slices,
  0, gl.RED, gl.FLOAT,
  state.vol                       // Float32Array
);
```

`state.vol` is a **Float32Array** (declared line 106; produced by the decode worker at
lines 1578/1586/1594) — 4 bytes per voxel regardless of source dtype. For 512×512×500:

* 131,072,000 voxels
* × 4 bytes = **524,288,000 B ≈ 500 MB** host RAM
* and a **500 MB `R32F` VRAM texture**

uploaded in a **single synchronous call on the main thread**. There is no chunking, no
`texSubImage3D` streaming, no PBO, no `requestAnimationFrame` yielding. The UI freezes for the
duration of the upload (and the driver-side conversion), which is exactly the reported
"freezes on large CT studies (>500 slices)".

The guard at line 1891 permits up to **256 M voxels** (~1 GB) before refusing:

```js
if (maxDim > 1024 || state.slices * state.rows * state.cols > 256 * 1024 * 1024) return;
```

Two aggravating factors:

1. **Needless float32.** The worker takes a float32 path even when the rescale is the identity
   (lines 1579-1580: `if (slope === 1 && intercept === 0) { for (i…) vol[i] = i16[i]; }`) — it
   still allocates a Float32Array, doubling memory versus the source int16.
2. **No VRAM accounting.** `EXT_color_buffer_float`/`OES_texture_float_linear` are checked for
   *filtering*, but nothing checks the texture against actual available VRAM before the upload.

**Patch A — chunked upload across frames (works with the existing float32 volume)**

```js
// V3-2: a single texImage3D of a 500 MB R32F texture blocks the main thread for
// seconds on a >500-slice CT. Allocate once, then stream the volume in
// row-block slabs via texSubImage3D, yielding to the event loop between slabs so
// the UI (and the progress bar) keeps painting.
var PAXS_TEX_SLAB_ROWS = 32;

function uploadVolumeTexture(gl, tex, cols, rows, slices, vol, onDone) {
  // Allocate storage with a null upload, then fill it slab by slab.
  gl.bindTexture(gl.TEXTURE_3D, tex);
  gl.texImage3D(gl.TEXTURE_3D, 0, gl.R32F, cols, rows, slices, 0,
                gl.RED, gl.FLOAT, null);

  var sliceStride = rows * cols;                 // voxels per slice
  var r = 0;
  (function nextSlab() {
    if (r >= rows) { onDone(gl.getError() === 0); return; }
    var end = Math.min(r + PAXS_TEX_SLAB_ROWS, rows);
    for (var z = 0; z < slices; z++) {
      var srcOff = z * sliceStride + r * cols;
      gl.texSubImage3D(
        gl.TEXTURE_3D, 0,           // target, level
        0, r, z,                    // xoffset, yoffset, zoffset
        cols, end - r, 1,           // width, height, depth
        gl.RED, gl.FLOAT,
        vol.subarray(srcOff, srcOff + (end - r) * cols)
      );
    }
    r = end;
    requestAnimationFrame(nextSlab);            // yield between slabs
  })();
}
```

Replace the direct `gl.texImage3D(...)` at 1925-1936 with `uploadVolumeTexture(gl, tex, ...)`, and
move the `gl.getError()` check plus `attachContextLossHandler(renderCv)` into the `onDone`
callback.

**Patch B — skip float32 when the rescale is the identity**

In the decode worker (lines 1576-1600), when `slope === 1 && intercept === 0` and the source is
`int16`, allocate the texture as `gl.R16I`/`gl.SHORT` and upload the `Int16Array` view directly —
halving both RAM and VRAM. This is the single largest memory win for typical CT.

**Patch C — honour real VRAM limits**

```js
// Query the driver instead of assuming 1 GB is available.
var max3D = gl.getParameter(gl.MAX_3D_TEXTURE_SIZE);
var maxRenderbuffer = gl.getParameter(gl.MAX_RENDERBUFFER_SIZE);
var bytes = cols * rows * slices * 4;
// Conservative ceiling; fall back to the server pipeline above it.
if (bytes > 768 * 1024 * 1024) {
  console.warn('PAXS MPR: volume needs ' + (bytes >> 20) + ' MB VRAM — using server pipeline');
  return;
}
```

---

### V3-3 [MEDIUM] Negotiate gates first paint

**File:** `app/static/viewer/paxs-mpr.js` — `loadVolume` (line 1234), `negotiateProcessingMode` (line 384)

```js
negotiateProcessingMode(seriesUid).then(function (result) {
  if (state.seriesUid !== seriesUid) return;
  applyNegotiatedMode(result);
  ...                                   // <-- ALL volume loading happens in here
```

Every volume fetch — including the coarse preview — is inside the `.then`, so **nothing renders
until negotiate resolves**. Negotiate in turn depends on the RTT probe
(`HEAD /info`, `RTT_TIMEOUT_MS = 3000`, line 360; `RTT_UNREACHABLE_MS = 5000`), and the call
itself is bounded by `NEGOTIATE_TIMEOUT_MS = 2500` (line 277).

Worst case on a slow/unreachable server: **up to ~2.5 s before the first volume request is even
issued**, then the `/data` fetch on top. For a hybrid deployment this is latency added to the
critical path purely for a routing decision.

Mitigating (and worth preserving): on failure the local probe stands (line 437), so it degrades
rather than breaks — the viewer still works, just late.

**Patch — race negotiate against a short deadline and start the local pipeline optimistically**

```js
// V3-3: negotiate is a routing hint, not a prerequisite for pixels. Race it
// against a short deadline and start the already-probed local pipeline in
// parallel; if negotiate arrives late it only re-routes the *next* navigation.
var NEGOTIATE_FIRST_PAINT_MS = 600;

function negotiateWithDeadline(seriesUid) {
  return Promise.race([
    negotiateProcessingMode(seriesUid),
    new Promise(function (resolve) {
      setTimeout(function () { resolve(null); }, NEGOTIATE_FIRST_PAINT_MS);
    }),
  ]).then(function (result) {
    if (result === null) {
      // Late: apply whenever the real answer lands, without blocking.
      negotiateProcessingMode(seriesUid).then(function (late) {
        if (state.seriesUid === seriesUid) applyNegotiatedMode(late);
      });
    }
    return result;
  });
}
```

and call `negotiateWithDeadline` in place of `negotiateProcessingMode` at line 1234.

---

### V3-4 [MEDIUM] Coarse preview is `downsample=2`, not the specified `4`

**File:** `app/static/viewer/paxs-mpr.js` — lines 1390-1397; `SPLIT_DOWNSAMPLE` at 2401

```js
var isLargeVolume = info.slices >= 16;
if (isLargeVolume) {
  fetch('/api/v1/volume/' + encodeURIComponent(seriesUid) + '/data?downsample=2')
```

The spec asks for `downsample=4` delivering instant interactivity (<300 ms) before high-res slices
arrive. In practice:

* `downsample=2` → `2³ = 8×` smaller payload.
* `downsample=4` → `4³ = 64×` smaller payload — the tier that actually arrives in tens of ms.
* `SPLIT_DOWNSAMPLE = 4` (line 2401) is used **only** in split mode (`loadSplitCoarseVolume`, 2413).

So in normal client/hybrid mode the preview is the weaker tier, and because the fetch sits inside
the negotiate `.then` (V3-3) it cannot mask negotiate latency either.

**Patch**

```js
// V3-4: use the 64x-coarser tier for the first paint (the spec's <300ms target);
// the 2x tier is then fetched as an intermediate refinement before full res.
var PAXS_PREVIEW_DOWNSAMPLE = 4;
fetch('/api/v1/volume/' + encodeURIComponent(seriesUid) +
      '/data?downsample=' + PAXS_PREVIEW_DOWNSAMPLE)
```

Then chain the `downsample=2` pass before the full-resolution fetch so the viewport sharpens in
two steps rather than one.

---

### V3-5 [LOW] Client-mode `/data` fetches have no abort or timeout

**File:** `app/static/viewer/paxs-mpr.js` — lines 1397 and 1445

```js
fetch('/api/v1/volume/' + encodeURIComponent(seriesUid) + '/data').then(function (r) { ... })
```

Unlike the negotiate and RTT probes — both of which correctly build an `AbortController` with a
deadline (lines 373-375, 396-398) — the two volume fetches have neither. A stalled server leaves
the socket open indefinitely, and because a hung socket never rejects, the `.catch` at line 1495
never fires and `state.busy` stays `true` forever (the workspace is wedged with no error shown).

**Patch**

```js
function fetchWithDeadline(url, ms) {
  var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
  var timer = setTimeout(function () { if (ctrl) ctrl.abort(); }, ms);
  return fetch(url, ctrl ? { signal: ctrl.signal } : undefined)
    .finally(function () { clearTimeout(timer); });
}

// VOLUME_FETCH_TIMEOUT_MS: generous — a 500 MB volume over a slow link is real —
// but finite, so a hung server surfaces as an error instead of a wedged viewer.
var VOLUME_FETCH_TIMEOUT_MS = 120000;
return fetchWithDeadline(url, VOLUME_FETCH_TIMEOUT_MS).then(function (r) { ... });
```

---

### V3-6 [LOW] `_VOL_CACHE` evicts insertion-order and is smaller than the session cap

**File:** `app/services/volume_service.py` — lines 677, 1172-1173, 1199-1201

```python
_VOL_CACHE: dict[str, np.ndarray] = {}      # 676
_VOL_CACHE_MAX = 8                          # 677
...
if len(_VOL_CACHE) >= _VOL_CACHE_MAX:
    _VOL_CACHE.pop(next(iter(_VOL_CACHE)))  # 1173 / 1201 — oldest INSERTED
```

Two problems:

1. **Not LRU.** `next(iter(dict))` yields the oldest *inserted* key, so a volume in constant use
   is evicted ahead of one touched once long ago. `_TILE_CACHE` (line 688) is already an
   `OrderedDict` with `move_to_end` on hit — the volume cache should match.
2. **Undersized.** `HYBRID_MAX_SERVER_MPR_SESSIONS = 20` (`config.py:282`) permits 20 concurrent
   server sessions, but only **8** volumes fit in L1. Under any real concurrent load the cache is
   guaranteed to thrash, re-materialising ~500 MB per miss.

**Patch**

```python
_VOL_CACHE: "OrderedDict[str, np.ndarray]" = OrderedDict()   # LRU, like _TILE_CACHE
_VOL_CACHE_MAX = 8
```

```python
# hit path
with _VOL_CACHE_LOCK:
    cached = _VOL_CACHE.get(cache_key)
    if cached is not None:
        _VOL_CACHE.move_to_end(cache_key)     # <-- keep the working set resident
        return cached
```

and size it against the session cap (or make it a byte budget):

```python
# Never smaller than the number of sessions the hybrid engine will admit, or the
# cache is guaranteed to thrash under the load it was built for (V3-6).
_VOL_CACHE_MAX = max(8, settings.HYBRID_MAX_SERVER_MPR_SESSIONS)
```

> `_VOL_CACHE_MAX = 20` × ~500 MB worst case is 10 GB. That is why a **byte budget** is the right
> long-term shape here — mirror `HYBRID_SERVER_VOL_BUDGET_MB` (`config.py:275`, default 4096) and
> evict by bytes rather than entry count.

---

## 4. Verification

### 4.1 Automated

```bash
# --- V1-3: the viewer_mode normalisation -------------------------------------
python - <<'PY'
import re, pathlib
src = pathlib.Path('app/api/v1/dicomweb.py').read_text(encoding='utf-8')
assert 'accept_norm = accept.replace(\'"\', "").replace(" ", "")' in src
assert 'viewer_mode = "transfer-syntax=*" in accept_norm' in src
print('V1-3 OK')
PY

# --- V2-1: no stale 8042 anywhere in the dev config ---------------------------
! grep -rn "8042" study-list-ui/vite.config.ts && echo "FAIL: 8042 still present" || echo "V2-1 OK"

# --- V2-2: PUBLIC_BASE_URL must not default to a bind address ----------------
! grep -n 'PUBLIC_BASE_URL: str = "http://0.0.0.0"' app/core/config.py \
  && echo "V2-2 OK" || echo "FAIL: unroutable default still present"

# --- V1-4: every codec wasm must serve application/wasm, not text/html -------
for f in 776f756dd939124e.wasm 0ddf166f7a343eda.wasm 34b1e6b1deb5828e.wasm 37fa160cc3a47cfd.wasm; do
  printf '%-30s ' "$f"
  curl -s -o /dev/null -w '%{http_code} %{content_type}\n' "http://127.0.0.1:8000/viewer/$f"
done
# EXPECT: 200 application/wasm on all four.
# A 200 with text/html means SpaStaticFiles is masking a 404 -> apply the V1-5 patch.

# --- V2-5: PascalCase parameter must be used everywhere -----------------------
! grep -rniE "studyInstanceUIDs?" app/ study-list-ui/src/ \
  && echo "V2-5 OK" || echo "FAIL: camelCase studyInstanceUID(s) found"

# --- backend syntax gate ------------------------------------------------------
python -m compileall -q app/ && echo "backend compiles"
```

**Regression guard for V3-1** (proves one materialisation, not N) — add as a pytest:

```python
@pytest.mark.asyncio
async def test_concurrent_mpr_slices_materialise_volume_once(monkeypatch):
    """N concurrent cold-cache reslices must decode the stack exactly once."""
    calls = {"n": 0}
    real = volume_service._frame_array
    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)
    monkeypatch.setattr(volume_service, "_frame_array", counting)
    volume_service._VOL_CACHE.clear()

    await asyncio.gather(*[
        asyncio.to_thread(volume_service.reslice_plane, slices, info, "axial", i)
        for i in range(1, 9)
    ])
    # Without the single-flight patch this is len(slices) * 8.
    assert calls["n"] == len(slices), f"volume decoded {calls['n']} times"
```

**Regression guard for V3-2** (proves the upload no longer blocks) — in the browser console:

```js
// Long-task observer: with the chunked uploader in place there must be no single
// task longer than ~100ms during a >500-slice volume load.
new PerformanceObserver((l) => l.getEntries().forEach(e =>
  console.log('longtask', Math.round(e.duration) + 'ms'))).observe({ entryTypes: ['longtask'] });
window.__paxsMprState = null;   // then open MPR on a >500-slice CT
```

### 4.2 Manual network inspection

**A. Zero broken images (Vector 1).** DevTools → Network → filter `frames`.
* Every `/frames/{n}` must be `200` with either `application/octet-stream` or
  `multipart/related`, and the `transfer-syntax=` parameter must name the *source* syntax
  (`1.2.840.10008.1.2.4.80` for JPEG-LS, `…4.90` for J2K, `…4.201` for HTJ2K).
* Scroll a 200-image series end to end. Expect **no** `401`, **no** `404`, and no
  `application/octet-stream` entry whose body starts with `<!doctype html`.
* Force the 401 path to prove the V1-1 fix: DevTools → Application → Cookies → delete
  `AUTH_COOKIE_NAME`, then scroll. Expect **one** `POST /api/v1/auth/refresh`, then the frame
  requests replay with `Authorization: Bearer …` and succeed — instead of a black viewport.

**B. Routing (Vector 2).** From `/studies-ui`, click a study:
* The request URL must be `…/viewer/viewer?StudyInstanceUIDs=<uid>` — PascalCase, **no** `:5173`,
  `:5174` or `:8042` in it, and it must preserve any `/paxs` prefix.
* `curl -sI 'http://127.0.0.1:8000/viewer/viewer?StudyInstanceUIDs=1.2.840.1'` → `200`.
* `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/study-ui` → confirm whether the
  singular path is meant to exist (the app serves `/studies-ui`).
* Behind the proxy, assert the headers land:
  `curl -sI https://host/viewer/ | grep -i 'x-forwarded'` and confirm `request.url.scheme`
  appears as `https` in the app log.
* Share link: create a share in the UI and assert the returned `url` host is a real browser-reachable
  origin — **not** `0.0.0.0`.

**C. MPR throughput (Vector 3).** DevTools → Performance, record while scrolling MPR on a
>500-slice CT:
* **Before** the V3-2 patch: one dominant ~1–4 s task at `initGl2`.
  **After:** upload work spread across `requestAnimationFrame` slabs, no task > ~100 ms.
* DevTools → Network → filter `mpr/slice`: during a continuous scrub the request count should
  collapse to ~1 per settled position (the 200 ms `SERVER_WL_DEBOUNCE_MS`, line 2211, and the
  400 ms `SPLIT_FINE_DEBOUNCE_MS`, line 2402) — not one per mouse-move event.
* DevTools → Memory, cold-load a series and scrub: `_VOL_CACHE` behaviour shows up as the server
  RSS. Expect the server to materialise the volume **once** (V3-1) and to serve subsequent planes
  from L1.
* Confirm the coarse tier actually used: filter `volume/.*data` and check the query string is
  `?downsample=4` for the first paint.

---

## 5. Suggested order of work

1. **V2-2** — one-line config/default change that un-breaks every share link and QR code.
2. **V1-5** — makes the next asset problem diagnosable instead of mysterious (this audit lost time to it).
3. **V1-1** — turns an unrecoverable blank viewport into a transparent retry.
4. **V3-1** — highest server-side throughput win; reuses an existing pattern in the same file.
5. **V3-2** — highest client-side win for large studies; the reported UI freeze.
6. **V3-6 / V1-2** — cheap cache corrections that compound with the above.
7. **V2-3 / V2-4** — required before any reverse-proxy or sub-path deployment.
8. **V3-3 / V3-4 / V3-5** — latency polish once the correctness issues above are closed.

---

## 6. Things I checked and found **correct** (so you don't re-audit them)

* **Codec availability** — all four codec wasm files ship and serve `application/wasm`; the
  logical→hashed remap happens via `locateFile` (V1-4).
* **`StudyInstanceUIDs` casing** — PascalCase end to end; no camelCase variants exist (V2-5).
* **Port binding in the runner** — `run_paxs.py` reads `PAXS_HOST`/`PAXS_PORT` from the
  environment; no hardcoded `localhost:8000` anywhere in `app/static/viewer/*.js` or the
  study-list source.
* **Tile coalescing** — `render_plane_tile` has L1 LRU + Redis L2 + correct single-flight
  (`_tile_inflight_begin`, line 845); the gap is only the *volume* materialisation beneath it.
* **Worker decode** — the blob-worker pool (line 1559) transfers buffers zero-copy
  (`postMessage(..., [vol.buffer])`) and has per-request timeouts plus an `onerror` that settles
  every pending request (H15). Decoding is genuinely off the main thread.
* **Mode guards do not misfire by default** — `VIEWER_PROCESSING_MODE` defaults to `hybrid`, and
  both `_server_mode_active` (line 281) and `_client_mode_active` (line 374) deliberately return
  `False` for `hybrid`. The `503` guards on `/data` and `/mpr/slice` only fire for an *explicit*
  `server`/`client` resolution, and `?force=1` always bypasses them.
* **Client 503 avoidance** — `serverFallbackAllowed()` (line 645) checks the resolved mode *before*
  issuing server-slice requests, so a client-mode session does not generate doomed calls. (Minor
  residual gap: no client code reads the `X-PAXS-Mode-Hint` header, so if a 503 does occur the
  viewer relies solely on its own pre-check.)
* **WebGL context loss** — handled (`attachContextLossHandler`, line 1982) with a graceful
  degradation to server-rendered Canvas 2D rather than a silent black screen.
