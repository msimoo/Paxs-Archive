# PAXS Viewer — Remediation Round 3

**Date:** 2026-09-27
**Scope:** memory governance settings (V1-2 / V3-6), first-paint pipeline decision
(V3-3 / V3-4), and observability (§4).

**Verification: 546 assertions, 0 failures** (229 JS + 317 Python). All Round 1 and
Round 2 suites remain green.

---

## 1. Memory governance (V1-2 & V3-6)

### Settings added — `app/core/config.py`

| Setting | Default | Governs |
|---|---|---|
| `DICOM_FRAME_CACHE_MB` | `256` | The decoded-frame LRU in the WADO-RS bulk service (`_FRAME_CACHE`) |
| `VOLUME_CACHE_BUDGET_MB` | `1024` | The L1 materialised-volume cache (`_VOL_CACHE`) |

Both are deliberately **independent of `HYBRID_SERVER_VOL_BUDGET_MB`**. That setting is
a *policy* input to viewer mode negotiation ("how much volume work does this server
intend to do"); these are *hard memory caps* enforced by the caches themselves. Keeping
them separate means a negotiation change can never silently raise the process's
worst-case RSS — which was the actual defect: the old caps (8 volumes / 16 frames) were
hardcoded, so neither budget governed anything.

Registered as editable settings under the **`viewer`** group in
`app/services/settings_service.py`, with `min`/`max` bounds (volume 64–65536,
frame 16–16384). `_coerce()` gained optional per-key range enforcement so the bounds are
actually applied on write, and `_field()` in the config API now surfaces `min`/`max` so
the settings UI can constrain input rather than letting an operator submit a value the
server will reject.

### Wiring — `volume_service.py` / `wado_rs_bulk_service.py`

Both caches moved from **entry counts to byte budgets**:

```python
def volume_cache_budget_bytes() -> int:
    """0 means caching disabled — returned as 0, NOT masked with a default:
    an `or` fallback would silently turn a deliberate disable into the full
    default budget."""
    raw = getattr(settings, "VOLUME_CACHE_BUDGET_MB", 1024)
    try:
        mb = int(raw)
    except (TypeError, ValueError):
        mb = 1024
    return max(0, mb) * 1024 * 1024
```

Insertion does byte accounting and LRU eviction:

```python
def _vol_cache_put_locked(cache_key, vol):
    budget = volume_cache_budget_bytes()
    if budget <= 0:
        cache_stats.set_gauges("volume", bytes=0, entries=0)
        return
    _VOL_CACHE.pop(cache_key, None)
    _VOL_CACHE[cache_key] = vol
    cache_stats.incr("volume", "inserts")
    total = sum(int(v.nbytes) for v in _VOL_CACHE.values())
    while len(_VOL_CACHE) > 1 and total > budget:   # never evict what we just added
        _k, evicted = _VOL_CACHE.popitem(last=False)
        total -= int(evicted.nbytes)
        cache_stats.incr("volume", "evictions")
    cache_stats.set_gauges("volume", bytes=total, entries=len(_VOL_CACHE))
```

Three design points worth keeping:

* **The total is recomputed from the dict, not tracked as a running delta.** `_VOL_CACHE
  .clear()` is called directly by tests and cache resets; a stale counter would then evict
  everything or nothing. One `sum()` per insert, then incremental eviction, is O(n) with
  n in the single digits.
* **The just-inserted entry is never evicted** (`len > 1`). Without that guard a single
  volume larger than the whole budget would be dropped immediately, so the cache would
  hold nothing and every request would re-decode — a silently useless cache. Asserted.
* **`0` disables caching** rather than meaning "1 byte". `_vol_cache_put_locked` returns
  early and reports an empty gauge.

The frame cache also **gained a lock**. It previously had none, yet the warm service
decodes on a background thread (`warm_service.py:169`) while request handlers decode on
the event loop — and byte accounting is a read-modify-write, so the race went from
"possible corruption of an OrderedDict" to "definitely wrong accounting". The lock costs
nothing against decodes measured in seconds.

### Coherence validation — `validate_memory_budgets()`

Returns warnings (not failures) for `VOLUME_CACHE_BUDGET_MB > HYBRID_SERVER_VOL_BUDGET_MB`
and for negative budgets; logged at startup in `main.py`.

> **A finding from my own test suite.** The first version also warned when the byte budget
> could not hold one reference volume (~500 MB) per admitted server session. That fired on
> the **shipped defaults** — 1024 MB against `HYBRID_MAX_SERVER_MPR_SESSIONS=20`. On
> reflection the heuristic was wrong to warn: the byte budget is the authoritative memory
> constraint, and the session cap is an admission limit the cache need not fully serve
> (20 × 500 MB would be 10 GB, which was never realistic, and the old 8-entry cap implied
> up to 4 GB). A warning that fires for the default configuration trains people to ignore
> it, so the volume-size heuristic was **removed** and the real signal is now *measured*:
> `paxs_cache_utilisation_ratio` and the miss rate. Documented in the function docstring so
> it is not re-added.

---

## 2. First-paint pipeline (V3-3 & V3-4) — deliberately unchanged

Per instruction, negotiate gating stays intact and no optimistic multi-pipeline
re-routing was introduced. The progressive preview also stays at `downsample=2` rather
than the coarser 4.

Rather than leave this as a silent absence, both decisions are now recorded **at the call
site** in `paxs-mpr.js` so a future reader cannot "optimise" them back without knowing the
reasoning:

```js
// DELIBERATE (V3-3 / V3-4, reviewed 2026-09-27): negotiate is a GATE here,
// not a hint raced against a deadline, and the progressive preview stays at
// downsample=2 rather than the coarser 4. Both were evaluated as latency
// improvements and deliberately declined: starting the local pipeline before
// negotiate resolves can re-route a live session from client to server mode
// one navigation later, and a softer first frame is a worse diagnostic
// trade than the sub-second latency it saves. Do not "optimise" either
// without measuring a real hybrid deployment first — see
// PAXS_VIEWER_REMEDIATION_ROUND2.md §3.
```

---

## 3. Observability (§4)

### 3a. Cache hit/miss telemetry

New `app/core/cache_stats.py` — deliberately dependency-free (no `settings`, no numpy) so
it can be imported by both caches, by `/metrics`, and by tests with no cycles. One lock
over plain ints; a cache operation is microseconds against decodes measured in seconds.

Counters are cumulative since process start (Prometheus `counter` semantics — `rate()`
over the scrape window is unaffected by a restart). Gauges are **set**, not incremented,
so they stay correct even when a cache is cleared by another module.

Emitted at `/metrics`:

```
paxs_cache_operations_total{cache="volume",result="hit"}    12
paxs_cache_operations_total{cache="volume",result="miss"}    3
paxs_cache_operations_total{cache="volume",result="insert"}  4
paxs_cache_operations_total{cache="volume",result="evict"}   1
paxs_cache_bytes{cache="volume"}            536870912
paxs_cache_entries{cache="volume"}                  1
paxs_cache_budget_bytes{cache="volume"}    1073741824
paxs_cache_utilisation_ratio{cache="volume"} 0.500000
```

plus the same series for `cache="frame"`. Both caches always report, even with zero
traffic — a series that vanishes between scrapes would break `rate()` and alerting.

Budgets are read at **scrape time**, so a runtime settings override is reflected without a
restart. A zero budget yields ratio `0.000000` rather than dividing by zero.

This is what closes the loop on §1: `utilisation_ratio` pinned at 1.0 with a high miss rate
means raise the budget; a low miss rate means it is already generous.

### 3b. TransferSyntaxUID in decode failures

New `app/core/dicom_diagnostics.py`. A decode failure is almost always a *codec* problem —
no handler for the syntax, or a pixel layout the handler cannot express — and the previous
logs said only "pixel_array failed", which cost real time during the audit (V1-4).

`decode_context(ds, frame=…, series_uid=…)` emits one PHI-free line:

```
transfer_syntax=1.2.840.10008.1.2.4.80 (JPEG-LS Lossless)
  | modality=CT bits=16/16 samples=1 photometric=MONOCHROME2 512x512 frames=1
  | series=1.2.3 sop=1.2.3.4 frame=3
```

The **human codec name** is the point: an operator reads "JPEG-LS Lossless", not a UID
they have to look up. pydicom's `UID.name` is authoritative and used when available, with a
built-in fallback table for the syntaxes this archive produces.

Wired into every decode failure path:

| File | Site |
|---|---|
| `wado_rs_bulk_service.py` | `decode_frames_to_raw` pixel_array failure (the primary site) |
| `wado_rs_bulk_service.py` | encapsulated-frame extraction fallback |
| `wado_rs_bulk_service.py` | pixel-array frame extraction failure |
| `volume_service.py` | `iter_volume_bytes` slice decode failure |
| `triage_service.py` | the previously **silent** skip on an undecodable instance |

The triage skip is now logged at DEBUG rather than dropped silently — no noise at the
default level, but the systematic codec gap becomes visible when someone looks. The
existing `log.exception` (with traceback) was preserved rather than downgraded to
`log.error`.

Only DICOM identifiers and pixel-layout attributes are emitted — no patient demographics,
no filesystem paths. Asserted by the test suite.

---

## 4. Regression found and fixed in Round 2's work

`/api/v1/config` was **broken** and would have returned HTTP 500. Round 2 made
`dashboard.health()` require `request: Request` (to build a prefix-aware
`public_base_url`), but `config.py` called `await health()` with no arguments:

```
REGRESSION CONFIRMED -> TypeError health() missing 1 required positional argument: 'request'
```

Fixed by extracting the payload into `health_payload(request: Request | None = None)` with
the endpoint as a thin wrapper, and pointing `config.py` at the payload. Both paths now
work, and the shape has a single definition. Verified: `/api/v1/config` returns 401
(auth required) rather than 500, and `health_payload()` is callable with no arguments.

This is a good argument for the observability work in §3: a 500 on a settings page is
exactly the kind of failure that metrics and log context catch early.

---

## 4b. Prometheus monitoring was completely broken

Found while documenting the new metrics in `deploy/MONITORING.md`, whose text claimed
`/metrics` was *"unauthenticated by design (Prometheus standard)"*. It is not:

```python
@router.get("/metrics")
async def metrics(request: Request, _user=Depends(get_current_user_any)) -> Response:
```

`get_current_user_any` raises 401 when neither a bearer token nor a session cookie is
present. Meanwhile `deploy/prometheus.yml` scrapes `http://paxs-node-N:8000/metrics`
with **no credentials**. So:

* every scrape returned **401** — the whole `paxs-nodes` job was down;
* `up{job="paxs-nodes"} == 0` meant the `PaxsNodeDown` alert fired for **every node**;
* the worker-backlog and replica-lag alerts were evaluating against no data at all;
* the documentation actively asserted the opposite of the code.

**Fix.** Access is now granted by source address via `METRICS_ALLOW_IPS`
(default `127.0.0.1,::1` plus the RFC1918 ranges — exactly what the shipped
Docker/HAProxy topology needs, since Prometheus scrapes over a private network).
Peers in the list scrape unauthenticated; every other caller must authenticate exactly
as before. `*` is the documented opt-in for a genuinely trusted network.

This reuses the existing `TrustedHosts` matcher (renamed from `_TrustedHosts` in
`proxy_prefix.py`, since it now serves two related trust decisions). It is deliberately
**not** a blanket "make it public" fix: the payload discloses version, topology, queue
depths and cache pressure.

`deploy/MONITORING.md` and `.env.example` are corrected, including a note recording what
was wrong so the claim does not get re-introduced.

Verified: loopback and RFC1918 peers scrape 200 with no credentials; `203.0.113.9` and
`8.8.8.8` get 401; `METRICS_ALLOW_IPS="*"` opens it; an empty allowlist refuses even
loopback; and the real app renders the full body end to end.

---

## 5. Verification

| Suite | Assertions | Result |
|---|---|---|
| `memory-observability-test.py` (**new**) | 136 | ✅ |
| `proxy-prefix-test.py` | 81 | ✅ |
| `proxy-integration-test.py` | 62 | ✅ |
| `volume-coalesce-test.py` (extended for byte budgets) | 29 | ✅ |
| `spa-404-test.py` | 27 | ✅ |
| `about-test.js` | 35 | ✅ |
| `toolbar-test.js` | 30 | ✅ |
| `icon-test.js` | 67 | ✅ |
| `cascade-test.js` | 5 | ✅ |
| `auth-guard-test.js` | 30 | ✅ |
| `tex-upload-test.js` | 41 | ✅ |
| `fetch-deadline-test.js` | 21 | ✅ |
| **Total** | **564** | **0 failures** |

The new suite covers: settings defaults; every `validate_memory_budgets()` branch
including the deliberately-absent heuristic; settings_service registration, group, type and
min/max enforcement (valid, out-of-range, non-numeric); the config API surfacing bounds;
frame-cache byte budget, LRU order, oversized-entry retention, all four counters, both
gauges, and clear-resets-gauges; the volume budget reader including `0`/negative/garbage;
`0` genuinely storing nothing; Prometheus exposition shape, HELP/TYPE declarations, and the
zero-budget divide-by-zero guard; decode-context formatting, codec labelling, bare-dataset
safety, and the absence of patient identifiers; decode-failure logging proving both the
graceful fallback and the both-paths-fail case carry the transfer syntax; the `/metrics`
source-address allowlist (trusted allowed, public refused, wildcard opt-in, empty
refuses-all); and the real app rendering the full metrics body end to end.

```bash
cd "D:/Paxs Archive"
./venv/Scripts/python.exe .workbuddy-ai/tmp/memory-observability-test.py
./venv/Scripts/python.exe .workbuddy-ai/tmp/volume-coalesce-test.py
./venv/Scripts/python.exe .workbuddy-ai/tmp/proxy-prefix-test.py
./venv/Scripts/python.exe .workbuddy-ai/tmp/proxy-integration-test.py
./venv/Scripts/python.exe .workbuddy-ai/tmp/spa-404-test.py
```

Live check of the new metrics:

```bash
curl -s -u <admin> http://127.0.0.1:8000/metrics | grep paxs_cache
```

---

## 6. Files touched

| File | Change |
|---|---|
| `app/core/cache_stats.py` | **new** — dependency-free cache telemetry registry |
| `app/core/dicom_diagnostics.py` | **new** — PHI-free decode context + transfer-syntax labelling |
| `app/core/config.py` | the two budget settings; `validate_memory_budgets()` |
| `app/services/settings_service.py` | both keys registered under `viewer` with min/max; range enforcement in `_coerce` |
| `app/services/volume_service.py` | `_VOL_CACHE` → byte budget + LRU + stats; budget reader; decode-failure context |
| `app/services/wado_rs_bulk_service.py` | `_FRAME_CACHE` → byte budget + lock + stats; decode-failure context |
| `app/services/triage_service.py` | the silent decode skip is now logged at DEBUG with context |
| `app/api/v1/metrics.py` | cache counters/gauges + HELP/TYPE; budgets read at scrape time; `require_metrics_access` source-address allowlist |
| `app/api/v1/config.py` | surfaces `min`/`max`; uses `health_payload` |
| `app/api/v1/dashboard.py` | `health_payload(request=None)` + thin `health` endpoint |
| `app/core/proxy_prefix.py` | `_TrustedHosts` → public `TrustedHosts` (now shared with metrics access) |
| `app/main.py` | logs memory-budget warnings at startup |
| `app/static/viewer/paxs-mpr.js` | V3-3/V3-4 decision recorded at the call site |
| `.env.example` | both budgets documented, plus `METRICS_ALLOW_IPS` |
| `deploy/MONITORING.md` | cache metric panels, thrash alerts, corrected `/metrics` auth claim |
| `PAXS_VIEWER_DIAGNOSTIC_AUDIT.md` | statuses updated |

---

## 7. Still open

| # | Finding | Status |
|---|---|---|
| V3-3 | Negotiate gates first paint | **Deliberately not applied** — decision recorded in code |
| V3-4 | Preview is `downsample=2` | **Deliberately not applied** — same |
| — | Observability item 2 from Round 2 §4 (`X-PAXS-Mode-Hint` client handling) | Not requested this round; the server already sends it on every 503 and no client reads it |
