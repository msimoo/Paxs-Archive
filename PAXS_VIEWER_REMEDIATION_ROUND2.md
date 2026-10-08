# PAXS Viewer — Remediation Round 2

**Date:** 2026-09-26
**Scope:** V2-3 & V2-4 (reverse-proxy readiness) applied; V3-5 & V3-6(LRU) applied as
zero-risk; V1-2, V3-3, V3-4, V3-6(size) evaluated and summarised for your decision.

**Verification: 401 assertions, 0 failures.** All Round 1 suites remain green.

---

## 1. Priority 1 — Reverse proxy & forwarding integrity

### What was actually broken

Investigation first, because the fix depends on it:

| Fact | Evidence |
|---|---|
| **Neither** uvicorn 0.52 nor Starlette 1.3.1 reads `X-Forwarded-Prefix` | `inspect.getsource(uvicorn.middleware.proxy_headers.ProxyHeadersMiddleware)` — reads only `x-forwarded-proto` / `x-forwarded-for`. Starlette 1.3 has no `proxy_headers` module at all. |
| uvicorn does not read `X-Forwarded-Host` either | same source |
| uvicorn's default trust list is `127.0.0.1` | so a proxy container's headers are silently dropped |
| `deploy/haproxy.cfg` sets Proto + `forwardfor` but never Host or Prefix | `frontend http_in` block |
| The viewer bundle derives its asset/route URLs from `window.PUBLIC_URL` and `window.config.routerBasename` | `app.bundle.29858d89fb4b9b9d.js`: `a$ = window.PUBLIC_URL \|\| "/"`, `aK = window.config?.routerBasename \|\| a$` |
| PAXS has **no WebSocket endpoints** | `grep -rn "websocket" app/ --include=*.py` → nothing |
| The dashboard makes ~200 literal `'/api/v1/...'` calls | `grep` over `app/static/app.js` |

That last row is what shaped the design: rewriting every literal was not viable, and
rebuilding the OHIF fork with a different public path is out of scope, so the prefix is
applied at the **transport layer** and at **serve time**.

### Applied — `app/core/proxy_prefix.py` (new)

**`ForwardedPrefixMiddleware`** — pure ASGI, runs *outside* the auth gate (a
non-stripping proxy delivers `/paxs/viewer/...`, which the gate's path check would
otherwise fail to recognise — a fail-open for gating). For a trusted peer it:

1. sets `scope["root_path"]`, which is what `request.base_url` / `url_for` use;
2. strips the prefix from `scope["path"]` when the proxy did not already do so;
3. applies `X-Forwarded-Host` (replacing `Host`) and `X-Forwarded-Proto`;
4. does the same for `websocket` scopes, normalising `wss`→`wss` / `https`→`https`
   across scope types.

**`prefix_markup`** — quote-anchored rewrite of root-absolute app paths. Anchoring on
the opening quote is what makes it safe: `"https://cdn/x/viewer/a"` is untouched (the
character before `/viewer` is `m`), and an already-prefixed `"/paxs/viewer/a"` cannot
be double-prefixed (the `"/viewer` pattern does not occur). Verified idempotent.

**`inject_boot`** — injects a boot shim immediately after `<head>` (before any bundle
runs, all of which are `defer`) that sets `window.PAXS_BASE_PATH` and prefixes
root-absolute same-origin `fetch` / `XMLHttpRequest` URLs at runtime.

### Applied — `app/main.py`

`_spa_shell()` and `_prefixed_text_asset()` now serve `/`, `/viewer`, `/studies-ui`,
`/admin`, `/settings`, `/login` and `/viewer/app-config.js`. **With no prefix they
delegate to `FileResponse` — identical bytes, identical headers**, which is asserted.

`app-config.js` gets an explicit route registered *before* the `/viewer` mount so it
wins route matching; the viewer's `routerBasename` and `wadoRoot`/`qidoRoot`/
`wadoUriRoot` are rewritten there.

### Applied — browser-facing redirects (`app/main.py`, `app/api/v1/auth.py`)

Found while verifying live: the auth gate built its redirect as
`RedirectResponse(f"/login?next={next_url}")` — **root-absolute, dropping the prefix**.
Behind `/paxs/`, an anonymous visitor to `/paxs/studies-ui` was bounced to `/login`,
outside the app. The same applied to `/logout` and to `role_landing()`'s
`ROLE_LANDING` defaults (`/viewer`, `/`, …).

New `prefix_path(root_path, path)` helper (idempotent, and refuses anything that is not
a plain same-origin path so a protocol-relative URL can never be rewritten into one),
applied at all six sites: both gate redirects, `/logout`, the login handler, and
`role_landing()` via a new `root_path` parameter threaded from the three
`_auth_response` call sites. The open-redirect guard on `?next=` is preserved.

Also fixed two client-side `location.href = "/"` navigations
(`static/admin/admin-bridge.js`, `static/viewer.js`) to carry `PAXS_BASE_PATH`, and the
fallback `viewer.html` handler now goes through `_spa_shell`.

Live-verified:

```
GET /studies-ui                      → 303  /login?next=%2Fstudies-ui
GET /studies-ui  (X-Forwarded-Prefix) → 303  /paxs/login?next=%2Fpaxs%2Fstudies-ui
GET /logout      (X-Forwarded-Prefix) → 303  /paxs/login
GET /logout                           → 303  /login
```

### Applied — config & runner

* `config.py`: `ROOT_PATH`, `FORWARDED_ALLOW_IPS`; `resolve_public_base_url()` now
  appends the prefix (scope `root_path` first, then `ROOT_PATH`) without duplicating it
  when `PUBLIC_BASE_URL` already carries it.
* `run_paxs.py`: `proxy_headers=True`, `forwarded_allow_ips`, `root_path`, with
  startup logging of both so a misconfiguration is visible.

### Applied — frontends

* `study-list-ui/src/lib/api.ts`: new exported `appBasePath()` / `appBaseUrl()`
  (injected value → document path cut at `/studies-ui`). `API_BASE` and `getViewerUrl`
  use them. **`location.origin` was the bug** — it drops any path prefix.
* `study-list-ui/src/components/study-list/StudyListPage.tsx`: the two literal
  `/api/v1/auth/...` fetches now use `appBasePath()`.
* `app/static/app.js`: `viewerUrl()` fallback now carries `PAXS_BASE_PATH`.
* `vite.config.ts`: **unchanged** — `base: '/studies-ui/'` is rewritten at serve time,
  so no rebuild is required.

### 🐞 Bug found in my own middleware

The first version forwarded `X-Forwarded-Proto` verbatim to any scope type, so an
**http** request carrying `X-Forwarded-Proto: wss` would get `scope["scheme"] = "wss"`.
Caught by a test asserting cross-scope normalisation. Fixed:

```python
secure = fwd_proto in ("https", "wss")
if new_scope["type"] == "websocket":
    new_scope["scheme"] = "wss" if secure else "ws"
else:
    new_scope["scheme"] = "https" if secure else "http"
```

### Verified (123 assertions across two suites)

`proxy-prefix-test.py` (69) — prefix normalisation incl. scheme/traversal rejection;
markup rewriting with external URLs untouched and idempotency proven; boot injection;
stripping **and** non-stripping http shapes; `X-Forwarded-Host`/`Proto` end to end;
trusted-peer gating (`*`, glob, CIDR, CIDR-miss, untrusted-ignored); **WebSocket scope**
(root_path, wss, path strip, host replace) and http-vs-ws scheme normalisation;
`ROOT_PATH` static config with and without a header; the real shipped shells rewriting
cleanly.

`proxy-integration-test.py` (54) — against the **real `app.main.app`**: root-mounted
shells byte-unchanged (no shim, no prefix, `no-cache`); sub-path shells prefixed across
`/studies-ui`, `/viewer` (both the study-list shell and the `?StudyInstanceUIDs=`
bundle shell), `/`, `/settings`; `app-config.js` prefix-aware; the injected absolute
base reflecting `X-Forwarded-Host`/`Proto`; non-stripping paths routing; API endpoints
still reaching the app; `ROOT_PATH`-only operation.

---

## 2. Priority 2 — Applied because they carry zero behavioural risk

### V3-5 — Abort deadline on volume fetches ✅ applied

`paxs-mpr.js`: the negotiate and RTT probes already used an `AbortController`; the
`/data` fetches did not. A stalled server left the socket open forever, and because a
hung socket never rejects, the loader's `.catch` never fired — `state.busy` stayed
`true` and the workspace wedged with no error shown.

```js
var VOLUME_FETCH_TIMEOUT_MS = 120000;   // generous, but finite

function fetchWithDeadline(url, ms) {
  var ctrl = (typeof AbortController === 'function') ? new AbortController() : null;
  if (!ctrl) return fetch(url);
  var timer = setTimeout(function () { ctrl.abort(); }, ms || VOLUME_FETCH_TIMEOUT_MS);
  return fetch(url, { signal: ctrl.signal }).then(
    function (res) { clearTimeout(timer); return res; },
    function (err) { clearTimeout(timer); throw err; }
  );
}
```

Applied to all four volume payload fetches (progressive preview, full-resolution,
split-mode coarse, fusion overlay). **Why zero risk:** a healthy server is unaffected —
the timer is cleared on both settle paths, and the only behavioural change is that a
*hang* becomes an error the user can see.

Verified (21 assertions): stalled fetch aborts and rejects with `AbortError`; healthy
fetch passes through with no abort; network errors still propagate; graceful fallback
where `AbortController` is absent; custom vs default deadline; and source-level checks
that no bare `fetch('/api/v1/volume/...')` remains.

### V3-6 (eviction ordering only) ✅ applied

`_VOL_CACHE` was a plain `dict` evicted with `pop(next(iter(...)))` — oldest
**inserted**, so a series in constant use could be dropped ahead of one touched once
long ago. Now an `OrderedDict` with `move_to_end` on hit and `popitem(last=False)`.
`_TILE_CACHE` already worked this way.

**Why zero risk:** purely which entry is discarded; no size, budget or API change.

Verified (11 new assertions): with the oldest entry promoted, a fresh insert evicts the
true LRU head rather than the oldest-inserted key; a cache *hit* promotes recency so the
working set survives; the cap is never exceeded.

---

## 3. Evaluated, **not** applied — summary of proposed patches

These change memory budgets or first-paint behaviour, so they are yours to call. Each
is ready to apply on your word.

### V1-2 — frame decode cache sizing

> **Naming note:** the brief lists V1-2 as "Error reporting and observability", but in
> `PAXS_VIEWER_DIAGNOSTIC_AUDIT.md` **V1-2 is the decoded-frame LRU**. I've assumed the
> audit's definition. If you did want observability work, see §4 below — say the word
> and I'll do that instead.

**Current:** `wado_rs_bulk_service.py` — `_FRAME_CACHE_MAX = 16` *entries*, LRU.

**Why it matters:** the file's own comment puts a 3072×2560 J2K decode at ~2.3 s. Sixteen
entries is under a full scroll of one plane on a 24-slice series, so a 200-image study
scrubbed across three planes evicts continuously and re-decodes on almost every revisit.
(The Redis L2 tier is live and helps, but L1 is what serves the hot path.)

**Proposed patch — bound by bytes, not entries:**

```python
# Bound by decoded bytes: an entry cap thrashes on any real series (16 frames of
# a 512x512 CT is 8 MB). A byte budget scales with image size instead.
_FRAME_CACHE_MAX_BYTES = 256 * 1024 * 1024
_FRAME_CACHE_BYTES = 0

def _cache_put(key, value):
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

**Risk:** memory ceiling only — the process may hold up to ~256 MB of decoded frames
instead of ~8 MB. **Recommendation:** make the budget a setting
(`FRAME_CACHE_BUDGET_MB`) defaulting to 256, so it is tunable per deployment.

### V3-3 — negotiate gates first paint

**Current:** `paxs-mpr.js:1234` — every volume fetch, including the coarse preview, is
inside `negotiateProcessingMode(...).then(...)`, so nothing renders until negotiate
resolves. `NEGOTIATE_TIMEOUT_MS = 2500`, and negotiate itself waits on the RTT probe
(`RTT_TIMEOUT_MS = 3000`). Worst case ≈2.5 s before the first volume request is issued.

**Proposed patch — race negotiate against a short deadline:**

```js
var NEGOTIATE_FIRST_PAINT_MS = 600;

function negotiateWithDeadline(seriesUid) {
  return Promise.race([
    negotiateProcessingMode(seriesUid),
    new Promise(function (r) { setTimeout(function () { r(null); }, NEGOTIATE_FIRST_PAINT_MS); }),
  ]).then(function (result) {
    if (result === null) {
      // Late: apply when it lands, without blocking the first paint.
      negotiateProcessingMode(seriesUid).then(function (late) {
        if (state.seriesUid === seriesUid) applyNegotiatedMode(late);
      });
    }
    return result;
  });
}
```

**Risk:** the first volume load may start on the locally-probed pipeline and be re-routed
one navigation later. On a hybrid deployment that is a visible mode change mid-session.
**Recommendation:** measure before/after on a real hybrid setup first; this is the one
finding where the "fix" could feel worse than the latency it removes.

### V3-4 — coarse preview tier

**Current:** the progressive preview uses `downsample=2` (8× smaller). `SPLIT_DOWNSAMPLE
= 4` (64× smaller) is used only in split mode. The audit's stated target — instant
interactivity before high-res arrives — is the ×4 tier.

**Proposed patch:** use `downsample=4` for the first paint, then chain the ×2 pass, then
full resolution. **Risk:** three fetches instead of two, and a visibly softer first frame.
Pairs naturally with V3-3; do them together or not at all.

### V3-6 — `_VOL_CACHE` sizing

**Current:** `_VOL_CACHE_MAX = 8` entries, while `HYBRID_MAX_SERVER_MPR_SESSIONS = 20`
admits 20 concurrent server sessions — so the cache is smaller than the load it was built
for, and thrashing is guaranteed under concurrency. (Eviction ordering is already fixed;
this is the size.)

**Proposed patch — byte budget tied to the existing hybrid budget**, so the cache can
never exceed what `server_overloaded()` already assumes:

```python
_VOL_CACHE_BUDGET_BYTES = settings.HYBRID_SERVER_VOL_BUDGET_MB * 1024 * 1024
# evict by bytes rather than entry count
while _VOL_CACHE and _vol_cache_bytes > _VOL_CACHE_BUDGET_BYTES:
    _k, _v = _VOL_CACHE.popitem(last=False)
    _vol_cache_bytes -= _v.nbytes
```

**Risk:** `HYBRID_SERVER_VOL_BUDGET_MB` defaults to **4096** — a 4 GB L1 ceiling. That is
coherent (the overload check uses the same number) but it is a large change in worst-case
RSS. **Recommendation:** introduce a separate `VOLUME_CACHE_BUDGET_MB` (suggest 1024) and
either raise `HYBRID_SERVER_VOL_BUDGET_MB` to match or keep the cache strictly below it,
so the two cannot disagree.

---

## 4. On "error reporting and observability"

Not requested by the audit, but since the brief mentioned it, the cheapest high-value
additions would be:

1. **Cache effectiveness counters** — hit/miss for `_FRAME_CACHE`, `_VOL_CACHE`,
   `_TILE_CACHE`, exported on the existing `/metrics` (Prometheus) endpoint. This turns
   V1-2/V3-6 from guesswork into measurement, and would tell you immediately whether the
   budgets above are even needed.
2. **Decode-failure attribution** — `decode_frames_to_raw` already logs on failure; add
   the transfer syntax and SOP UID so a codec gap is diagnosable from the log alone
   (the audit lost time to exactly this, see V1-4).
3. **`X-PAXS-Mode-Hint` client handling** — the server sends it on every `503`; no client
   code reads it. Wiring it up would let the viewer re-route instead of relying solely on
   its own pre-check.

Say which of these you want and I'll implement it.

---

## 5. Verification

| Suite | Assertions | Result |
|---|---|---|
| `proxy-prefix-test.py` (new) | 81 | ✅ |
| `proxy-integration-test.py` (new) | 62 | ✅ |
| `fetch-deadline-test.js` (new) | 21 | ✅ |
| `volume-coalesce-test.py` (extended: +11 for V3-6) | 22 | ✅ |
| `spa-404-test.py` | 27 | ✅ |
| `about-test.js` | 35 | ✅ |
| `toolbar-test.js` | 30 | ✅ |
| `icon-test.js` | 67 | ✅ |
| `cascade-test.js` | 5 | ✅ |
| `auth-guard-test.js` | 30 | ✅ |
| `tex-upload-test.js` | 41 | ✅ |
| **Total** | **421** | **0 failures** |

`proxy-prefix-test.py` also covers `prefix_path` and `role_landing` directly, including
the open-redirect guard and idempotency. `proxy-integration-test.py` re-enables the auth
gate for its final section so the redirect behaviour is tested as a browser would hit it.

Also: `tsc --noEmit -p tsconfig.app.json` clean on the study-list-ui; `py_compile` /
`compileall` clean; `node --check` clean on both viewer scripts.

```bash
# JavaScript (jsdom)
cd .workbuddy-ai/tmp
NODE_PATH=… node about-test.js toolbar-test.js icon-test.js cascade-test.js \
                auth-guard-test.js tex-upload-test.js fetch-deadline-test.js

# Python
cd "D:/Paxs Archive"
./venv/Scripts/python.exe .workbuddy-ai/tmp/proxy-prefix-test.py
./venv/Scripts/python.exe .workbuddy-ai/tmp/proxy-integration-test.py
./venv/Scripts/python.exe .workbuddy-ai/tmp/spa-404-test.py
./venv/Scripts/python.exe .workbuddy-ai/tmp/volume-coalesce-test.py

# TypeScript
cd study-list-ui && ./node_modules/.bin/tsc --noEmit -p tsconfig.app.json
```

---

## 6. Files touched in this round

| File | Change |
|---|---|
| `app/core/proxy_prefix.py` | **new** — middleware, markup rewriting, boot shim |
| `app/core/config.py` | `ROOT_PATH`, `FORWARDED_ALLOW_IPS`; prefix-aware `resolve_public_base_url` |
| `app/main.py` | middleware registration; `_spa_shell` / `_prefixed_text_asset`; `/viewer/app-config.js` route; prefix-aware auth-gate redirects, `/logout`, login handler, fallback viewer |
| `app/api/v1/auth.py` | `role_landing(..., root_path)`; `_auth_response` threads the prefix |
| `run_paxs.py` | `proxy_headers`, `forwarded_allow_ips`, `root_path` + startup logging |
| `app/services/volume_service.py` | `_VOL_CACHE` → LRU `OrderedDict` (V3-6) |
| `app/static/viewer/paxs-mpr.js` | `fetchWithDeadline` on all volume fetches (V3-5) |
| `app/static/app.js` | `viewerUrl()` fallback carries the prefix |
| `app/static/admin/admin-bridge.js`, `app/static/viewer.js` | `location.href = "/"` carries `PAXS_BASE_PATH` |
| `study-list-ui/src/lib/api.ts` | `appBasePath` / `appBaseUrl`; `API_BASE`, `getViewerUrl` |
| `study-list-ui/src/components/study-list/StudyListPage.tsx` | auth fetches use `appBasePath()` |
| `deploy/REVERSE-PROXY.md` | **new** — nginx / Traefik / Caddy / HAProxy + Docker, and verification |
| `.env.example` | reverse-proxy section |
| `README.md` | routes section points at the reverse-proxy guide |
