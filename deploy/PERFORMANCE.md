# PaxsViewer & Archive Performance Guide

How the archive serves the viewer as fast as possible, what is already
implemented, measured results, and the recommended next steps.

## Implemented (this repo, all verified)

### 1. Gzip compression for text responses — `app/main.py` `GzipCompressionMiddleware`

- Every text response (`text/*`, `application/json`, `application/javascript`,
  `image/svg+xml`) ≥ 1 KiB and ≤ 32 MiB is gzip-compressed on the fly when the
  client advertises `Accept-Encoding: gzip`.
- **Binary endpoints are never touched**: `/dicom-web/*` (DICOM objects, frame
  codestreams, rendered PNG/JPEG) and `/api/v1/volume/*`, `/api/v1/fusion/*`,
  `/api/v1/mpr/*` (raw voxel blocks) are skipped by path *and* by content-type,
  so compressed DICOM codecs don't waste CPU for ~0 gain.
- `Vary: Accept-Encoding` is set so caches key correctly.
- Implemented as a plain ASGI send-wrapper (not `BaseHTTPMiddleware`) because
  Starlette's `FileResponse`/`StreamingResponse` have no readable `.body`.

**Measured:** the main viewer bundle is **6,586,880 B raw → 1,760,096 B gzipped
(73% smaller on the wire)**. The ~70 code-split chunks shrink similarly.

### 2. Long-lived caching for hashed static assets — `CachedStaticFiles`

The viewer + admin SPAs are code-split bundles whose chunk names embed content
hashes. Serving them with correct `Cache-Control` means the browser downloads
each chunk exactly once:

| File | Cache-Control |
|------|---------------|
| `*.bundle.<hash>.js/.css`, hash-prefixed `.wasm`/`.woff2`, `assets/index-*.js` | `public, max-age=31536000, immutable` |
| `index.html`, `app-config.js`, `manifest.json`, `sw.js`, `paxs-mpr.js`, `paxs-study-list-enhance.js`, `viewer-version.txt`, `silent-refresh.html` | `no-cache` (revalidate so updates ship) |
| everything else | `public, max-age=86400` |

Applied to `/viewer`, `/admin`, `/static`, `/` (dashboard index → `no-cache`)
and the splash logo. Before this, a reload re-downloaded (or at best
revalidated) the entire bundle because no cache headers were sent.

### 3. Pre-generated thumbnail fast path — `wado_thumbnail`

The background worker already generates a 192 px PNG per instance at ingest
(`.thumbnails/{sop}.png`). The WADO-RS thumbnail endpoint now serves that
object directly (byte-exact, with the existing ETag/immutable headers) whenever
no explicit window is requested and the requested size fits — avoiding a
pydicom read + PIL render per study-list row. Study lists fire dozens of
thumbnail requests per screen.

### 4. Redis render cache for thumbnails — `wado_thumbnail`

When the pre-generated object is missing or disabled (`THUMBNAIL_ENABLED=false`)
or an explicit window/size is requested, the render path (pydicom read + PIL
render, ~2.3 s per J2K instance) is now backed by the same Redis cache as
`/rendered`: key `paxs:thumb:{sop}:{window}:{size}:{fmt}`, 10 min TTL. Measured
on the live archive: first render **2.26 s → cached hit 0.014 s** (byte-identical).

### 5. Warm-on-open study cache priming — `app/services/warm_service.py`

Opening a study in the viewer (QIDO-RS with a specific `StudyInstanceUID`, or
a WADO-RS study-metadata fetch) fires a short-lived background thread that
pre-warms the three read layers for that study's instances:

1. pre-generates any **missing 192 px thumbnails** (`.thumbnails/{sop}.png`) so
   study-list/series thumbnails hit the pre-generated fast path,
2. warms the **Redis thumbnail keys** (`paxs:thumb:{sop}:auto:256:png`, 10 min
   TTL) the WADO-RS thumbnail endpoint computes, so even the rendered fallback
   is instant,
3. pre-decodes the **first frames** into the in-process decoded-frame cache
   (bounded by `WARM_MAX_INSTANCES` / `WARM_MAX_FRAMES`, default 128/32) so
   scrolling the top of a series skips the cold J2K/JPEG-LS decode.

Best-effort, bounded, deduped (in-process set + Redis lock), and it never
delays the HTTP response that triggered it. Toggle: `WARM_ON_OPEN_ENABLED`
(admin Settings > Storage). Live on the archive, opening the DX J2K study
warmed **2 thumbnails + 2 Redis keys + 2 frames in one pass**.

### 6. Already in place before this pass

- **Immutable ETag caching on every WADO-RS pixel response** (`_cached_response`
  in `app/api/v1/dicomweb.py`): frame codestreams, rendered images, thumbnails
  and bulk data return content-hash ETags + `private, max-age=31536000,
  immutable` and short-circuit to `304` — scrolling back through a CT stack and
  re-opening a study hit the browser cache, not the disk.
- **Redis cache for `/rendered`** (10 min TTL) keyed by sop/window/viewport.
- **64-thread I/O executor** (`IO_THREAD_POOL_WORKERS`): frame decode is
  offloaded with `asyncio.to_thread` so dozens of concurrent per-slice requests
  run in parallel instead of queueing behind the event loop.
- **MySQL 8 default with full indexing** for archive-scale queries (see the
  database migration set).
- **Warm-on-open priming** (`app/services/warm_service.py`) — thumbnails,
  Redis thumbnail keys and the first frames are pre-warmed in the background
  the moment a study is opened in the viewer (QIDO deep-link / study
  metadata), so scrolling and re-opens hit warm caches.
- **Range requests** (`accept-ranges: bytes`) are served by Starlette for
  static files.

## Recommended next steps (not yet implemented)

### HTTP/2 (biggest remaining win for first load)
The ~70 chunk requests currently ride HTTP/1.1 (one connection, 6-per-host
parallelism, head-of-line blocking). HTTP/2 multiplexes them all on one
connection. `h2` isn't installed in this venv; enable it with:

```bash
venv/Scripts/python -m pip install "uvicorn[standard]"
# then run with --http2, or edit run_paxs.py:
#   uvicorn.run(app, host="127.0.0.1", port=8000, http2=True)
```

Alternatively terminate TLS at HAProxy/Caddy in front (roadmap #47) — they
speak HTTP/2 natively and add `br` (brotli) compression with zero code changes.

### Client-side (edit the source bundle in `D:\Github\Viewers`, rebuild)
- `<link rel="preload" as="script">` for `app.bundle.*.js` and the 2-3 biggest
  chunks (they're all requested by the entry chunk anyway — preload shaves a
  round-trip).
- Enable the existing service worker (`init-service-worker.js`/`sw.js`) for
  full offline caching of chunk files (it's present but not registered by
  default in some configs).
- Cornerstone stack prefetch is on by default; tune
  `stackPrefetch`/cache sizes for huge stacks. The browser-side decoded-image
  cache (LRU in cornerstone) already prevents re-decoding scrolled slices.

### Server capacity
- `run_paxs.py` runs a **single worker** by design (the DICOM receiver, worker
  and rate-limiter are in-process, so multiple uvicorn workers would fight over
  port 10400). For more throughput run one PAXS node per core behind HAProxy
  (roadmap #47 — stateless nodes share DB + object storage; `/health` reports
  `PAXS_INSTANCE_ID`).

## How to verify

```bash
# gzip + immutable cache on a hashed chunk
curl -s -o /dev/null -w "size=%{size_download} enc=%header{content-encoding} cc=%header{cache-control}\n" \
  -H "Accept-Encoding: gzip" http://localhost:8000/viewer/app.bundle.<hash>.js

# entry point revalidates
curl -s -o /dev/null -w "cc=%header{cache-control}\n" http://localhost:8000/viewer/

# binary DICOM endpoints are never gzipped
curl -s -o /dev/null -w "enc=%header{content-encoding}\n" \
  -H "Accept-Encoding: gzip" "http://localhost:8000/dicom-web/studies?limit=1"
```

Tests: `tests/test_perf_serving.py` (15 tests — gzip round-trip, wire bytes,
cache headers, thumbnail fast path + Redis cache layers, no-gzip on binary).
