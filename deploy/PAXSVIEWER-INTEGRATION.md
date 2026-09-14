# PaxsViewer Integration (replaces OHIF)

PaxsViewer is the project's **OHIF v3 fork** (D:\Github\Viewers, v3.13.0-beta.89) — a
zero-footprint DICOMweb study browser with the same features as OHIF (worklist, MPR/3D
modes, measurements, hanging protocols). It is served **same-origin** from the archive
itself at `/viewer` — no CORS, no reverse proxy, no token plumbing — because PAXS
implements the full QIDO-RS / WADO-RS / WADO-URI surface it speaks.

## How it works

```
PAXS Archive (FastAPI)
  ├─ /viewer          PaxsViewer dist (built with PUBLIC_URL=/viewer/)
  │     └─ app-config.js  →  dataSources.paxs:
  │            qidoRoot / wadoRoot / wadoUriRoot = /dicom-web   (same origin)
  ├─ /dicom-web/*     QIDO-RS / WADO-RS / STOW-RS (PS3.18)
  └─ /viewer?StudyInstanceUIDs=1.2.3.4   deep link (standard OHIF query form)
```

* PaxsViewer loads the study list from `GET /dicom-web/studies` (QIDO-RS) and renders
  frames via WADO-RS (`/rendered`, `/frames/{n}`, `bulk/{tag}`) — identical to OHIF.
* The DICOMweb endpoints accept the guest/anonymous scope (see `DICOMWEB_PUBLIC`), and
  tenant scoping (VNA) follows the same `get_optional_user` rule as the dashboard.

### 2D MPR backend prep (roadmap #26) — volume endpoints for the WebGL MPR viewer

The MPR extension consumes two same-origin endpoints served by the archive (no CORS):

* `GET /api/v1/volume/{seriesUID}/info` — volume geometry/encoding: rows, columns,
  slice count, pixel + slice spacing, `ImagePositionPatient` / `ImageOrientationPatient`,
  orientation label (AXIAL/SAGITTAL/CORONAL/OBLIQUE), per-slice z-order
  (`instance_order`), rescale slope/intercept (HU LUT), window, dtype/itemsize.
* `GET /api/v1/volume/{seriesUID}/data` — the raw voxel block streamed little-endian
  (`application/octet-stream`), with:
  * `?slices=<start>-<end>` — sub-range streaming for progressive volume load
  * `?rescale=1` — server-side rescale to float32 Hounsfield Units
  * `?format=npy` — numpy `.npy` container instead of raw bytes

Both honor `VOLUME_STREAMING_ENABLED` / `VOLUME_MAX_SLICES` and the standard bearer
scope. Slices are ordered by `ImagePositionPatient` along the dominant axis (z for
axial, x for sagittal, y for coronal); series without positions fall back to
InstanceNumber order. Compressed codestreams (JPEG/JPEG2000) are decoded server-side
— the same `pylibjpeg` plugins the `/frames` path uses. A typical flow: fetch `/info`
once, then pull the volume in `?slices=` ranges and rebuild it in a WebGL texture.

#### The 2D MPR client (injected module)

The Study List toolbar carries an **MPR** button (next to *View*) provided by
`app/static/viewer/paxs-mpr.js` — a self-contained ES5 module injected into the served
viewer by `.tools/inject_viewer_theme.sh` (no rebuild of the fork needed). Clicking it:

1. Lists the study's volumetric series (CT/MR/PT/NM/US preferred) via QIDO-RS.
2. Fetches `/api/v1/volume/{series}/info`, then streams `/data` and decodes the voxel
   block client-side (uint8/int8/uint16/int16/float32 little-endian) applying the
   rescale LUT to float32 Hounsfield Units.
3. Renders three **linked orthogonal planes** (Axial · Z, Sagittal · X, Coronal · Y)
   through WebGL2 `R32F` float textures with window/level in the fragment shader;
   Canvas2D fallback if WebGL2 is unavailable. NEAREST filtering is used deliberately
   (LINEAR on R32F samples black on many drivers), and contexts are created with
   `preserveDrawingBuffer: true` so the buffer survives compositing.
4. Interactions: slice sliders per plane, click-linked crosshairs, drag to adjust
   window/level, wheel to zoom the window, Escape / ✕ / backdrop-click to close.

The module is idempotent and re-runs its launcher on SPA re-renders (short poller +
MutationObserver). Cache-busted with `?v=` so the browser always fetches the latest copy.

#### Thin-client fallback — server-side MPR slice reslicing

Browsers that cannot run the WebGL2 reslicing module fall back to a server-rendered
slice endpoint — the archive does the geometry, the client just displays an image:

* `GET /api/v1/volume/{series}/mpr/slice?plane=axial|sagittal|coronal&index=N`
  — native-plane slices (axial index over the stack, sagittal over columns,
  coronal over rows).
* `GET /api/v1/volume/{series}/mpr/slice?plane=oblique&origin=x,y,z&row=dx,dy,dz&col=dx,dy,dz&size=WxH`
  — an arbitrary patient-space plane (unit direction cosines; the grid is
  centred on `origin` with PixelSpacing-sized samples, trilinear interpolation
  via the patient→voxel transform built from ImageOrientationPatient +
  ImagePositionPatient + spacing).
* `format=png|raw|npy` — 8-bit W/L PNG render (default), raw float32 Hounsfield
  block (little-endian), or numpy container. `window_center` / `window_width`
  override the display window (series window, else robust 2–98th percentile
  autoscale). Response headers carry `X-MPR-Rows/Cols/Plane/Spacing/Window`.

Both guards apply (`VOLUME_STREAMING_ENABLED`, `VOLUME_MPR_MAX_PIXELS` for
oblique output size); non-volumetric series return 404.

### 3D VRT / MIP (roadmap #27) — server-side maximum-intensity projection

`GET /api/v1/volume/{series}/mip` projects the volume along a patient axis over
a slab and returns one 2D image — the classic MIP slab (angiography), minIP,
an X-ray-like average, and a lightweight alpha-composited **VRT** mode, all
computed on the same float32 Hounsfield volume as the MPR reslicer (so
multi-frame / Enhanced instances are expanded identically):

* `axis=z|x|y` — projection axis (z = axial stack, default); the slab runs
  along that axis (stack depth for z, columns for x, rows for y)
* `offset=N` / `thickness=N` — first slab slice and depth in slices (default:
  the whole stack); out-of-range offsets → 416
* `mode=mip|minip|average|vrt` — per-pixel max (default) | min | mean | an
  alpha-composited volume render whose opacity ramp (smoothstep) is derived
  from the display window (Porter-Duff front-to-back "over" compositing,
  color = HU intensity) — a real server-side VRT with no VTK dependency
* `window_center` / `window_width` — display-window override (series window
  or 2–98th percentile autoscale otherwise); used for PNG and the vrt ramp
* `format=png|raw|npy` — 8-bit W/L PNG (default) | raw float32 HU projection
  (little-endian) | numpy .npy container

Response geometry headers: `X-MIP-Rows/Cols/Axis/Mode/Slab/Spacing/Window`.
The output follows the native-plane orientation: z → (rows, cols), x →
(depth, rows), y → (depth, cols). Guards match the volume endpoints: 404 for
non-volumetric series, 400 for bad axis/mode/format/numbers, 416 for an empty
or out-of-range slab, 503 when `VOLUME_STREAMING_ENABLED` is off.

**Multi-frame / Enhanced CT is fully supported** — `series_slices` expands each
`NumberOfFrames > 1` instance into per-frame slices, so `/info` reports one slice
per frame, `/data` streams frames in patient z-order, and `/mpr/slice` reslices
the expanded stack. `/info` reports `multi_frame: false` for the expanded stream
with `source_multi_frame: true` when the source instance was multi-frame.

**Raw frame ↔ resliced plane parity** — `info.frame_map[i]` is the
`{sop_instance_uid, frame}` (1-based WADO-RS file-order frame number) that
serves the same physical slice as the i-th z-ordered volume slice, so thin
clients can toggle between raw frames and resliced planes of the same
multi-frame instance: `/mpr/slice?plane=axial&index=i` ↔
`/dicom-web/studies/{s}/series/{S}/instances/{sop}/frames/{frame}`.
Alternatively, `frames/{N}?zorder=1` interprets `N` directly as a 0-based
z-order index (same geometry as `/mpr/slice?index=N` and `frame_map`
positions); without `zorder` the numbers stay standard file order.

Per-frame ordering is resolved through a fallback chain, in priority order:

1. `PerFrameFunctionalGroupsSequence → PlanePositionSequence →
   ImagePositionPatient` (real patient-space positions);
2. `DimensionIndexSequence` Position dimension (`ImagePositionPatient`
   `(0020,0032)` or `InStackPositionNumber` `(0020,9057)`) → per-frame
   `FrameContentSequence → DimensionIndexValues` — correct stack order even
   when frames are scrambled in file order and positions are absent;
3. base `ImagePositionPatient` + `SliceThickness` offset along the plane
   normal (file-order fallback for legacy multi-frame).

The client can therefore degrade gracefully: try `/mpr/slice?plane=axial` for
any browser, and only enable the WebGL module when `WebGL2` + `R32F` support is
confirmed.
* **Deep links**: the dashboard builds every study link through the single
  `viewerUrl()` helper (`app/static/app.js`): `<PUBLIC_BASE_URL>/viewer/viewer?StudyInstanceUIDs=…`.
  The `/viewer/viewer` mode route opens the study directly (the bare `/viewer`
  route lands on the study list instead). `PUBLIC_BASE_URL` comes from
  `/api/v1/health` (`public_base_url`), so links resolve to the same public
  origin whether the dashboard was opened via `localhost`, `127.0.0.1` or a
  reverse-proxy hostname; before the health payload lands, the current origin
  is used as a fallback.
* When the dist is absent (fresh clone) the archive serves its lightweight built-in
  `viewer.html` at `/viewer` instead — the integration degrades, never crashes.

## Building the dist (one-time, from the fork)

```bash
cd /d/Github/Viewers
# PUBLIC_URL must be exported (not a command prefix) so Git Bash doesn't mangle it
export MSYS_NO_PATHCONV=1
export PUBLIC_URL=/viewer/          # bakes /viewer/... asset URLs into index.html
export APP_CONFIG=config/dev.js     # baked config is overwritten by PAXS's own
pnpm --filter @ohif/app run build:viewer
# output → platform/app/dist
```

> Requirements: Node ≥ 24, pnpm ≥ 11 (both present on the build machine).

## Deploying

```bash
# 1. copy the fresh build + PAXS config into the archive
cp -r /d/Github/Viewers/platform/app/dist app/static/viewer
cp deploy/paxsviewer-app-config.js app/static/viewer/app-config.js
echo "3.13.0-beta.89" > app/static/viewer/viewer-version.txt
# 2. re-apply the PAXS customizations (Study List theme link + marker script)
bash .tools/inject_viewer_theme.sh
# 3. restart the server → PaxsViewer mounts at /viewer
```

> The Study List visual theme lives in `app/static/viewer/paxs-study-list-theme.css`
> (outside the dist, so it survives rebuilds). After any fresh `cp -r`, run
> `.tools/inject_viewer_theme.sh` to re-inject the `<link>` + marker script into
> `index.html`.

The archive's `main.py` mounts `app/static/viewer` at `/viewer` with an SPA fallback
(unknown paths → index.html) and logs the version from `viewer-version.txt`. The dashboard
header "Viewer" button and every study-row/reading/worklist link deep-link into it.

## Removing the old OHIF integration

* Delete `app/static/ohif/` (the old v3.12.11 dist) and `deploy/ohif-app-config.js`,
  `deploy/OHIF-INTEGRATION.md` — all `/ohif` mounts and references are gone.
* `tests/test_ohif.py` was replaced by `tests/test_viewer.py` (same DICOMweb replay
  assertions, mount checks now at `/viewer`).

## Tests

| File | Verifies |
|---|---|
| `tests/test_viewer.py` | Replays the viewer's exact DICOMweb request shapes (QIDO accept headers, WADO-RS multipart, WADO-URI), the `/viewer` mount + config + version marker, and the `?StudyInstanceUIDs=` deep link. |
