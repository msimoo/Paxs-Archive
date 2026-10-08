# PAXS Viewer — Remediation Round 1

**Date:** 2026-09-26
**Scope:** the five validated high-impact defects from `PAXS_VIEWER_DIAGNOSTIC_AUDIT.md`
**Constraints honoured:** zero regressions · surgical fixes only · no API-schema or viewer-binding changes

---

## Result

| Fix | File(s) | Status | Verification |
|---|---|---|---|
| **V1-1** transport 401 refresh-and-retry for image requests | `app/static/viewer/paxs-study-list-enhance.js` | ✅ Applied | 30 assertions |
| **V1-5** SPA 404 masking of missing assets | `app/main.py` | ✅ Applied | 27 assertions + live probe |
| **V2-2** safe `PUBLIC_BASE_URL` fallback | `app/core/config.py`, `sharing_service.py`, `share.py`, `dashboard.py`, `fhir.py`, `admin_panel_service.py`, `.env.example` | ✅ Applied | 13 behaviour checks |
| **V3-1** in-flight coalescing for volume materialisation | `app/services/volume_service.py` | ✅ Applied | 13 assertions |
| **V3-2** progressive chunked 3D texture upload | `app/static/viewer/paxs-mpr.js` | ✅ Applied | 41 assertions |
| Env & path alignment | `.env.example`, `README.md` | ✅ Applied | — |

**Total: 248 assertions, 0 failures.** All pre-existing suites remain green
(about 35 · toolbar 30 · icon 67 · cascade 5). All patched modules import cleanly and the
app boots with `/viewer`, `/studies-ui`, `/admin` and `/settings` mounted.

---

## V1-1 — Transport-level 401 refresh-and-retry

**File:** `app/static/viewer/paxs-study-list-enhance.js`

**Problem.** `paxsFetch()` implemented correct refresh-and-retry but had only three call
sites (the report modal and `/auth/me`). Cornerstone reaches the network through
`dicomweb-client`, which uses its own `fetch`/XHR, so every WADO-RS frame request bypassed
the refresh path. One transient 401 was terminal for that frame → black viewport, no in-page
recovery.

**Fix.** Install the same single-flight refresh at the transport layer, scoped to the
endpoints the viewer loads images from:

```js
var PAXS_AUTH_RETRY_PATHS = ['/dicom-web/', '/api/v1/volume/', '/api/v1/reports/'];

function paxsInstallFetchGuard() {
  if (!window.fetch || window.__paxsFetchGuard) return;
  window.__paxsFetchGuard = true;
  var origFetch = window.fetch;
  window.fetch = function (input, init) {
    var url = (typeof input === 'string') ? input : (input && input.url) || '';
    var p = origFetch.apply(this, arguments);
    if (!paxsNeedsAuthRetry(url)) return p;
    return p.then(function (res) {
      if (!res || res.status !== 401) return res;
      return paxsRefreshToken().then(function (tok) {
        if (!tok) return res;                     // refresh failed: surface the 401
        var retryInit = { credentials: 'same-origin', headers: {} };
        /* …copy caller init + headers… */
        retryInit.headers['Authorization'] = 'Bearer ' + tok;
        return origFetch.call(window, input, retryInit);   // exactly one replay
      });
    });
  };
}
```

An `XMLHttpRequest` guard (`paxsInstallXhrGuard`) covers the same paths for loaders that use
XHR. Both are installed at boot **before** anything can fetch an image, and both are
idempotent via `__paxsFetchGuard` / `__paxsXhrGuard`.

**Why this is safe.**
* `paxsRefreshToken()` was already single-flight, so a burst of N concurrent 401s triggers
  **one** `POST /auth/refresh` and then N replays — no thundering herd.
* The replay is bounded: `res.status !== 401` short-circuits, and the XHR path sets
  `__paxsRetried` so a permanently-401 server cannot loop.
* Non-matching paths are untouched — the studies list, admin calls and every other endpoint
  behave exactly as before (asserted).

**Verified.** Frame/volume/report endpoints recover from 401 with a bearer replay; five
concurrent 401s produce exactly one refresh; a permanently-401 server surfaces 401 without
looping; unrelated endpoints are not intercepted; refresh failure surfaces the original 401;
re-installation does not double-wrap; the XHR path behaves identically.

---

## V1-5 — SPA fallback no longer masks missing assets

**File:** `app/main.py` (`SpaStaticFiles`)

**Problem.** The handler fell back to `index.html` for **every** 404 under the mount, so a
missing `.wasm`/`.js`/`.css` returned `200 text/html`. The browser then reported a cryptic
`WebAssembly.instantiateStreaming failed` or `Unexpected token '<'` far from the real cause —
this masking is what made an absent codec file look like a decoding bug during the audit.

**Fix.** Fall back only for navigation-style paths (no recognised asset extension):

```python
_ASSET_EXTENSIONS = { ".js", ".mjs", ".css", ".wasm", ".map", ".json", …,
                      ".png", ".jpg", ".svg", ".ico", ".woff2", …,
                      ".gz", ".br", ".zip", ".bin", ".dat" }

async def get_response(self, path, scope):
    try:
        return await super().get_response(path, scope)
    except StarletteHTTPException as exc:
        if exc.status_code != 404:
            raise
        ext = os.path.splitext(path.rsplit("/", 1)[-1])[1].lower()
        if ext in _ASSET_EXTENSIONS:
            raise                      # honest 404
        return await super().get_response("index.html", scope)
```

**Deliberate design choice.** A *denylist* of asset extensions, not "shell only if there is no
extension at all". The stricter rule would break legitimate dotted navigation routes — OHIF
deep links routinely embed a UID in the path (`/viewer/studies/1.2.840.113619`), whose last
segment looks exactly like a file extension. The denylist fixes the reported problem without
risking those.

**Verified — including live:**

```
before                                     after
/viewer/does-not-exist.wasm  200 text/html  →  404 application/json
/viewer/charlswasm_decode.wasm 200 text/html →  404 application/json
/viewer/does-not-exist.js    200 text/html  →  404 application/json
/viewer/776f756dd939124e.wasm 200 wasm      →  200 application/wasm   (unchanged)
/viewer/some-route           200 shell      →  303 → /login (auth gate), route intact
```

---

## V2-2 — Safe `PUBLIC_BASE_URL`

**Files:** `app/core/config.py` (validator + resolver), `sharing_service.py`, `api/v1/share.py`,
`api/v1/dashboard.py`, `api/v1/fhir.py`, `services/admin_panel_service.py`, `.env.example`

**Problem.** `PUBLIC_BASE_URL` defaulted to `http://0.0.0.0` — a *listen* address, never
routable from a browser — and `.env` never set it. `sharing_service.share_url()` therefore
minted `http://0.0.0.0/viewer?share=<token>`, so **every patient share link and QR code was
dead**. The same value fed FHIR absolute URLs, the admin panel's launch URLs and the health
payload.

**Fix — three parts.**

1. **Validator** normalises the setting at load: strips trailing slashes, and downgrades any
   unroutable value (empty, `0.0.0.0`, `::`) to `""` meaning "not configured".

2. **Resolver** picks the best available origin:

```python
def resolve_public_base_url(request=None, settings_obj=None) -> str:
    """1. explicit PUBLIC_BASE_URL  2. X-Forwarded-Host/Proto
       3. request Host               4. loopback default."""
```

3. **Consumers** use the resolver. `share_url(token, request=None)` gained an optional
   request; `POST /api/v1/share` and `GET /api/v1/share/{token}/qr.png` now pass it.

**Verified behaviour:**

| Input | Resolved origin |
|---|---|
| `PUBLIC_BASE_URL=http://0.0.0.0:8000` (the old default) | `http://localhost:8000` |
| `PUBLIC_BASE_URL=https://pacs.example.org/` | `https://pacs.example.org` (slash stripped) |
| unset, request `Host: 10.0.0.5:8000` | `http://10.0.0.5:8000` |
| unset, `X-Forwarded-Host` + `X-Forwarded-Proto: https` | `https://pacs.hospital.org` |
| unset, request `Host: 0.0.0.0:8000` | `http://localhost:8000` (bind host skipped) |

**Contract preservation.** `settings.PUBLIC_BASE_URL` is still a `str`, so every consumer keeps
working; they simply receive a usable value instead of a bind address. `share_url()`'s new
parameter is optional, so existing callers are unaffected.

---

## V3-1 — In-flight coalescing for volume materialisation

**File:** `app/services/volume_service.py`

**Problem.** `load_volume_array` locked the L1/L2 lookup and the cache insert, but the
expensive decode loop between them ran unlocked with no in-flight registry. N concurrent
`/mpr/slice` or `/tiles` requests for one uncached series each re-read and re-decoded the
whole stack (~500 MB of voxels for a 512×512×500 CT). The tile-level coalescer only dedups
*identical* tile keys, so nine different tiles on a cold cache meant nine full decodes.

**Fix.** A claim/wait loop over `_IN_FLIGHT_VOLUMES: dict[str, threading.Event]`:

```python
owns_event = False
waits = 0
while True:
    with _IN_FLIGHT_VOLUMES_LOCK:
        current = _IN_FLIGHT_VOLUMES.get(cache_key)
        if current is None:
            event = threading.Event()
            _IN_FLIGHT_VOLUMES[cache_key] = event
            owns_event = True
            break
        event = current
    if waits >= _IN_FLIGHT_MAX_WAITS:
        owns_event = False          # owner keeps failing: decode it ourselves
        break
    waits += 1
    event.wait(timeout=_IN_FLIGHT_WAIT_SECONDS)
    cached = _cached_any()
    if cached is not None:
        return cached
```

`threading.Event`, not `asyncio.Event` — callers run this via `asyncio.to_thread`, i.e. on a
worker thread, where an `asyncio.Event` is neither thread-safe nor waitable. The `finally`
releases waiters on both success and failure, and only the thread that *registered* the event
may release it.

### 🐞 Bug found in the first version of this patch

The first implementation re-claimed ownership **unconditionally** on wake:

```python
if not is_owner:
    event.wait(...)
    if _cached_any() is None:
        # every waiter did this -> all became owners
        _IN_FLIGHT_VOLUMES[cache_key] = threading.Event()
        is_owner = True
```

So when an owner failed, all three waiters woke, all re-claimed, and all three re-decoded —
**37 `_frame_array` calls instead of 13**. The test caught it (`only ONE re-materialisation
after the failure (calls == 13) -> got 37`). The claim/wait loop above is the fix: after
waking, a waiter re-checks the cache and only *one* claims the next round; the rest wait again.

**Verified.** 8 concurrent cold-cache loads → exactly 12 decodes (one materialisation); warm
cache adds zero; a failing owner surfaces the error to its own caller while exactly one
re-materialisation serves the rest and the registry drains; distinct series are not serialised
against each other; empty input is still a fast no-op.

---

## V3-2 — Progressive chunked 3D texture upload

**File:** `app/static/viewer/paxs-mpr.js`

**Problem.** `initGl2()` uploaded the entire volume in one synchronous call:

```js
gl.texImage3D(gl.TEXTURE_3D, 0, gl.R32F, cols, rows, slices, 0, gl.RED, gl.FLOAT, state.vol);
```

`state.vol` is a **Float32Array**, so a 512×512×500 CT is 131 M voxels → **~500 MB** pushed to
the GPU on the main thread in a single task. That is the reported freeze on large studies. The
guard permitted up to 256 M voxels (~1 GB).

**Fix.** Two thresholds and two paths:

```js
var PAXS_TEX_DIRECT_MAX_BYTES = 64 * 1024 * 1024;   // <= 64 MB: one upload (unchanged)
var PAXS_TEX_CHUNK_BYTES = 8 * 1024 * 1024;         // ~8 MB per animation frame
```

* **≤ 64 MB** → the original single `texImage3D`. Byte-identical behaviour, so small and
  medium studies see zero change.
* **> 64 MB** → allocate immutable storage once (`gl.texStorage3D`), then stream the volume in
  slabs with `gl.texSubImage3D`, yielding via `requestAnimationFrame` between chunks. The
  first chunk runs synchronously so the viewport shows data immediately; the render loop keeps
  painting and the image sharpens as slabs land.

Two guards stop a superseded upload: a generation counter bumped by `releaseGl2()`
(`myGen !== GL_UPLOAD_GEN`), and `gl.isContextLost()`. Progress is tracked in
`state.texUpload = { uploaded, total, done, chunked, failed? }`.

### 🐞 Bug avoided in this patch

The chunk loop originally guarded on `state.gl2.volTex !== tex`. But `state.gl2` is assigned
*after* `uploadVolumeTexture()` returns, so on the synchronous first chunk the guard would have
compared against the **previous** (released) `state.gl2` and returned immediately — silently
uploading nothing for every large volume. The generation counter avoids the ordering problem
entirely because `releaseGl2()` (not `initGl2`) owns the increment.

**Verified.** Small volumes use exactly one `texImage3D` and schedule no frames; large volumes
use `texStorage3D` + one `texSubImage3D` per slice across multiple frames, contiguous
`z = 0..N-1`, each slab the right voxel count; the first frame does not upload everything;
a generation bump and a context loss both abort the loop promptly; an allocation failure
returns `false` so `initGl2` falls back to CPU rendering; a mid-upload driver error stops the
loop and records the failure.

---

## Environment & path alignment

* **`.env.example`** — replaced the unroutable `PUBLIC_BASE_URL=http://0.0.0.0:8000` with an
  empty value plus documentation of the derivation order and the bind-address rejection; added
  a **Redis cache** section explaining that `REDIS_CACHE_ENABLED=true` backs the shared L2
  tiers (decoded frames, HU volumes, rendered tiles) and that turning it off leaves only the
  small L1 caches.
* **`README.md`** — new "Web interfaces (routes)" section documenting all six routes,
  including **`/studies-ui` (plural — `/study-ui` is not valid)**, the PascalCase
  `?StudyInstanceUIDs=` deep-link contract, and the reverse-proxy guidance.

---

## Correction to the audit

The audit speculated that the Redis L2 tier was a silent no-op. **That was wrong.** Redis is
running on `127.0.0.1:6379`, `REDIS_CACHE_ENABLED` defaults to `True`, and `paxs:vol:*` keys
are actively written and read. This surfaced accidentally: a V3-1 test run returned a
correctly-shaped volume with **zero** decode calls, which could only be a Redis hit. The test
now disables Redis so it measures the L1 path deterministically, and the synthetic volumes it
created were flushed from Redis.

The L1 cache sizes (V1-2's 16-entry frame LRU, V3-6's `_VOL_CACHE_MAX = 8`) still matter —
L1 is what serves the hot path — but the "silently no-ops" framing was incorrect.

---

## Still open (from the audit, not in this round)

| # | Finding | Why it was left |
|---|---|---|
| V1-2 | Decoded-frame LRU is 16 entries | Memory-budget decision — the proposed fix changes the cache's memory ceiling |
| V2-3 | `getViewerUrl` breaks under a proxy path prefix | Needs a product decision on the deployment shape |
| V2-4 | No `root_path` / `X-Forwarded-Prefix`; proxy trust is loopback-only | Deployment-specific; should land with V2-3 |
| V3-3 | Negotiate gates first paint | Changes first-paint ordering — worth measuring before changing |
| V3-4 | Coarse preview is `downsample=2`, not `4` | Interacts with V3-3's timing work |
| V3-5 | Client `/data` fetches have no abort/timeout | Small, but pairs naturally with V3-3 |
| V3-6 | `_VOL_CACHE` insertion-order eviction; size 8 < 20 sessions | Touches the memory budget; recommend a byte-budget design |

---

## Reproducing the verification

```bash
# JavaScript (jsdom) — 208 assertions
cd .workbuddy-ai/tmp
NODE_PATH=… node about-test.js         # 35
NODE_PATH=… node toolbar-test.js       # 30
NODE_PATH=… node icon-test.js          # 67
NODE_PATH=… node cascade-test.js       #  5
NODE_PATH=… node auth-guard-test.js    # 30  (V1-1)
NODE_PATH=… node tex-upload-test.js    # 41  (V3-2)

# Python — 40 assertions
cd "D:/Paxs Archive"
./venv/Scripts/python.exe .workbuddy-ai/tmp/spa-404-test.py          # 27  (V1-5)
./venv/Scripts/python.exe .workbuddy-ai/tmp/volume-coalesce-test.py  # 13  (V3-1)

# Live probes
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' \
  http://127.0.0.1:8000/viewer/charlswasm_decode.wasm    # expect 404
curl -s -o /dev/null -w '%{http_code} %{content_type}\n' \
  http://127.0.0.1:8000/viewer/776f756dd939124e.wasm     # expect 200 application/wasm
```
