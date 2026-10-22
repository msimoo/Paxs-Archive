# PACS_Sys — Viewer app and shared frontend packages

> **Area analysis of PACS_Sys** for the [PAXS Archive vs PACS_Sys comparison](../../COMPARISON_PACS_Sys.md). Static, read-only analysis of the `PACS_Sys` repository at HEAD `2ebdbf4` **plus its uncommitted working tree** (2026-10-10). Nothing was built, run or modified. Secret values are never reproduced. Severity labels (🔴/🟡/🟢, RED/YELLOW, CRITICAL/MAJOR) are the analyst's; the comparison document carries the consolidated view.

Repo: `C:\Users\dody1\source\repos\PACS_Sys` (working tree is dirty; citations are to the files as they are on disk).

**Scope read:** `frontends/apps/viewer/**` (src, index.html, package.json, tsconfig, vite.config.ts), `frontends/packages/{api-client,auth,deep-link,dicomweb-sdk,ui}`, workspace `package.json`, `phase-0/viewer` (skim), `conformance/*.md` (skim), plus the BFF study-access route and Traefik routing where they decide what the viewer receives.

**Method and limits:** read-only. No installs, builds, tests, or dev servers. `node_modules` was read only to confirm Cornerstone 1.86 and Vite 5.4 behaviour. Nothing was executed, so every geometry or overlay claim below is code-derived arithmetic. Each one needs a live check before it is treated as a confirmed defect.

**Scope deviations:** `frontends/package.json` does not exist. The npm workspace root is `PACS_Sys/package.json` (workspaces: `frontends/apps/*`, `frontends/packages/*`, `bff-svc`). Viewer and shared-package tests live in `PACS_Sys/tests/` (root `"test": "vitest run tests"`). No DOM or React test exists for the viewer, and `viewportOverlayModel.ts:11-12` says so explicitly.

**Labels:** severity uses `[RED]` (clinical-safety or security defect with a concrete failure path), `[YELLOW]` (real defect, misleading control, or material risk), `[GREEN]` (hygiene or minor). Feature status is REAL, PARTIAL, PLACEHOLDER (state or UI only, or a deferred stub), or ABSENT. Text tags replace emoji by house style.

---

## 1. Architecture

### 1.1 Stack (declared range -> installed in node_modules)

| Component | Declared | Installed | Where |
|---|---|---|---|
| React / react-dom | ^18.3.1 | 18.3.1 | `apps/viewer/package.json:23-24` |
| @cornerstonejs/core | ^1.86.0 | 1.86.1 | `apps/viewer/package.json:13` |
| @cornerstonejs/dicom-image-loader | ^1.86.0 | 1.86.0 | `apps/viewer/package.json:14` |
| @cornerstonejs/tools | ^1.86.0 | 1.86.1 | `apps/viewer/package.json:15`; declared, **not imported** by app code (`cornerstone/initializeCornerstone.ts:49-59` says tools init was reverted) |
| dicom-parser / dicomweb-client | ^1.8.21 / ^0.10.4 | 1.8.21 / 0.10.4 | `apps/viewer/package.json` |
| oidc-client-ts | ^3.0.1 | 3.5.0 | `packages/auth/package.json` |
| motion | ^13.1.0 | 13.1.0 | `packages/ui/package.json` |
| Vite | ^5.4.11 (root) | 5.4.21 | `PACS_Sys/package.json` devDependencies |
| @vitejs/plugin-react | ^4.3.4 | 4.7.0 | root devDependencies |
| TypeScript | ^5.6.3 | 5.9.3 | root devDependencies |
| vitest | ^2.1.8 | 2.1.9 | root devDependencies |

The viewer tsconfig is strict, with `noUnusedLocals` and `noUnusedParameters` (`apps/viewer/tsconfig.json`). The shared packages are consumed as TypeScript source. Each `exports` points at `./src/index.ts`, and Vite aliases each `@pacs/*` straight to source (`apps/viewer/vite.config.ts:27-53`). There is no package build step in the viewer path.

### 1.2 Module map

| File | Lines | Purpose |
|---|---:|---|
| `src/Viewer.tsx` | 2477 | Root screen: reducer wiring, derived view state, preset and VOI application, compare load, export, context menu, JSX |
| `src/App.tsx` | 246 | Deep-link parse, auth bootstrap, BFF calls (access, context, hanging protocol, compare access) |
| `src/main.tsx` | 50 | Mount, root ErrorBoundary, MotionProvider, pre-paint UI scale, pinned dark theme |
| `src/hooks/useViewportGrid.ts` | 1352 | Cornerstone engine session per layout: stack, MPR, MIP/MinIP, VR, compare, volume build, VR retries, teardown |
| `src/hooks/useViewerCommands.ts` | 662 | Command handlers; compare and layout changes via `window.location.assign`; secondary window |
| `src/hooks/useLegacyMenuBar.ts` | 383 | Off-by-default classic menu roving focus |
| `src/commands/viewerCommandRegistry.ts` | 1078 | Single command registry for toolbar, menus, context menus; 27 `deferredCommand` stubs |
| `src/components/MeasurementOverlay.tsx` | 409 | SVG measurement overlay plus click or wheel interaction layer |
| `src/components/ViewportGrid.tsx` | 232 | DOM per layout (`.viewport` elements, overlays, reference panel, compare panels) |
| `src/components/ViewportToolbar.tsx` | 300 | Toolbar (presets, slab and VR sliders, commands) |
| `src/components/ViewportOverlay.tsx` | 59 | Burned-in corner annotation |
| `src/components/rail/*` | 164 + 72 + 79 | Series rail, priors, formatting |
| `src/components/SeriesRail.tsx` | 40 | Rail composition |
| `src/components/ViewerNotices.tsx` | 83 | Warnings and keyboard help |
| `src/components/HangingProtocolNotice.tsx` | 67 | Protocol notice with undo |
| `src/components/KeyImageTray.tsx` | 67 | Session key-image tray |
| `src/components/LegacyMenuBar.tsx` | 108 | Classic menus (off by default) |
| `src/components/CommandButton.tsx` | 65 | Registry command button |
| `src/cornerstone/initializeCornerstone.ts` | 89 | `csInit`, dicom-image-loader config, worker pool, GPU diagnostics |
| `src/cornerstone/volumeLoader.ts` | 179 | Image prefetch to Float32, volume create, deterministic volume IDs, sampling |
| `src/cornerstone/runtimeCapabilities.ts` | 258 | VR preflight: COOP/COEP, SAB, WebGL2, float extensions, 3D texture smoke test |
| `src/cornerstone/gpuDiagnostics.ts` | 51 | WebGL renderer string, integrated or discrete classification |
| `src/dicom/geometry.ts` | 248 | Geometry extraction (classic and enhanced), VOI extraction, `calculateSeriesGeometry` |
| `src/dicom/volumeEligibility.ts` | 19 | CT/MR-only volume gate |
| `src/dicom/loadSeriesIndex.ts` | 260 | Study index: QIDO series search, WADO-RS metadata per series, frame imageIds, IOCM filter |
| `src/dicom/measurements.ts` | 363 | Length, angle, ROI math; anchor mapping |
| `src/dicom/referenceOverlay.ts` | 113 | Reference-line model (schematic percent positions) |
| `src/dicom/seriesSync.ts` | 50 | Same-FoR grouping; `findNearestSliceIndex` |
| `src/dicom/autoVoi.ts` | 126 | Pixel-percentile auto window (2 %-99 %), per slice |
| `src/dicom/seriesDetails.ts` | 148 | Technical details panel |
| `src/dicom/nonImageInspector.ts` | 306 | SR / PR / other object inspector |
| `src/dicom/contextObjectNavigation.ts` | 110 | Navigation to non-image objects |
| `src/state/viewerState.ts` | 276 | `useReducer` state (layout, presets, measurements, compare, VR, oblique) |
| `src/viewerConfig.ts` | 393 | Presets (12 plus default), hint rules, VR limits, performance profile, memory thresholds |
| `src/viewerViewportHelpers.ts` | 116 | MPR IDs, VR preset to VTK preset map, `mapRelativeImageIndex`, bounds check |
| `src/viewportOverlayModel.ts` | 167 | Overlay formatting (W/L, omit-empty rule) |
| `src/obliqueReslice.ts` | 122 | Oblique camera rotation (per pane) |
| `src/obliquePlanning.ts` | 30 | Oblique angle clamp (±60°, 5° steps) |
| `src/temporalNavigation.ts` | 304 | Phase or timepoint grouping inside one series |
| `src/seriesSelection.ts` | 259 | Initial series ranking (description regex, instance count) |
| `src/hangingProtocol.ts` | 177 | Protocol application, preset tie-break, preference |
| `src/viewerContextAdapters.ts` | 112 | Compare candidates, context object cards |
| `src/seriesThumbnails.ts` | 76 | Thumbnail URL, drag MIME |
| `src/viewerTypes.ts` | 323 | Domain types |
| `src/viewerUiScale.ts` | 82 | UI scale preference |
| `src/styles.css` | 2970 | All viewer styles |
| `index.html` | 16 | Shell; no CSP meta |
| `vite.config.ts` | 98 | Aliases, `/dicom-web` dev proxy, COOP/COEP on dev and preview only, manual chunks |

Viewer-only dead or debris files: seven `vite.config.ts.timestamp-*.mjs` files sit in `apps/viewer/` (gitignore does not cover them). `dist/` is gitignored and holds a stale build.

### 1.3 How the viewer gets data

1. `App.tsx:13-16` reads `VITE_BFF_BASE` (default `http://localhost:3000`), `VITE_AUTH_MODE`, `VITE_OIDC_AUTHORITY` (default `http://localhost:8081/realms/pacs`), and `VITE_DEV_BEARER_TOKEN` (a static dev literal, default at `App.tsx:14`; value not reproduced here).
2. The deep link is parsed client-side (`App.tsx` via `parseViewerLaunchParams`). Nothing touches the archive before the BFF access check.
3. **BFF calls** (`PacsApiClient`, `api-client/src/index.ts:905`). Every request sends `Authorization: Bearer <tokenProvider()>` (`index.ts:1414-1423`):
   - `GET /api/v1/studies/{uid}/access?patient_mrn&issuer_of_patient_id` (`index.ts:932-934`). Returns `dicomweb_base`, `bearer_for_dicomweb`, `expires_at`, `iocm_filter`. Mounted only after this resolves (`App.tsx:160`).
   - `GET /api/v1/studies/{uid}/context` (`App.tsx:179`). Non-blocking; failure only logs (`App.tsx:183-185`). This is the **only** source of patient identity shown on screen.
   - `GET /api/v1/hanging-protocols/match` (`App.tsx:153`). Skipped when the URL already names a layout.
   - Compare: `GET /api/v1/studies/{priorUid}/access` (`App.tsx` compare block). No context call for the prior.
4. **DICOMweb direct from the browser** to `dicomweb_base`. The BFF sets this to `DICOMWEB_PUBLIC_BASE`, default `/dicom-web` (`bff-svc/src/config.ts:128`). Traefik routes `/dicom-web` to `arc:8080` (`infra/traefik/dynamic/routers.yml:16-21`).
   - QIDO series search and WADO-RS series metadata, via `dicomweb-client`, **with** `Authorization: Bearer <bearer_for_dicomweb>` (`dicom/loadSeriesIndex.ts:79-83, 86-96, 131-134`).
   - WADO-RS **frames** through the cornerstone `wadors:` loader, **without** an Authorization header (see SEC-1).
   - Series **thumbnails** as `<img src>`, also without Authorization (`seriesThumbnails.ts:33-45`, `components/rail/RailSeriesList.tsx:152`).
   - Dev only: Vite proxies `/dicom-web` to the archive (`vite.config.ts:56-60`).
5. All series metadata for the study is fetched **eagerly and sequentially** before the first image shows (`loadSeriesIndex.ts:103-131`, a `for ... await` loop).

### 1.4 Token handling

- **Mode resolution:** `resolveBrowserAuthMode` returns `'dev'` only for the exact string, otherwise `'oidc'` (`packages/auth/src/index.ts:29-31`).
- **OIDC:** oidc-client-ts `UserManager`, client id `pacs-viewer` (`App.tsx:~53`), scope `openid profile email roles`, `response_type: 'code'` (PKCE is the library default for code flow), redirect `${origin}/auth/callback` (`auth/src/index.ts:11-23`).
- **Storage:** `userStore: new WebStorageStateStore({ store: window.localStorage })` (`auth/src/index.ts:19`). **Access tokens persist in localStorage.**
- **Acquisition:** `getUser()`, use the token if present and not expired, otherwise `signinRedirect({ state: pathname+search })` and throw the sentinel `'Redirecting to login.'` (`auth/src/index.ts:66-72`). The callback runs once per page load (`auth/src/index.ts:54-64`) and then `history.replaceState` restores the state path (`auth/src/index.ts:49-50`).
- **Refresh:** none. No `automaticSilentRenew` is set (`auth/src/index.ts:11-23`). The viewer never reads `expires_at`, though the BFF returns a fixed now+5 min value (`bff-svc/src/app.ts:338`). An expired token means a full-page redirect mid-session.
- **Dev:** `VITE_AUTH_MODE=dev` returns a static token (`auth/src/index.ts:38-42`).
- **Token passthrough:** the BFF verifies the Keycloak JWT with JWKS and audience (`bff-svc/src/auth.ts`, OIDC branch), stores it as `bearerToken: token` (`auth.ts` OIDC branch), then returns the same JWT to the browser as `bearer_for_dicomweb` (`bff-svc/src/app.ts:337`). The viewer forwards that JWT to the archive (`loadSeriesIndex.ts:82`).
- **URL leakage:** no token is placed in a URL. Patient MRN and issuer **are** placed in URLs (see SEC-3).

### 1.5 Deep-link format

`parseViewerLaunchParams` (`packages/deep-link/src/index.ts:67-95`) and `buildViewerUrl` (`index.ts:97-162`):

`/?studyInstanceUID=<UID>`
`[&patientMrn=][&issuerOfPatientId=][&seriesInstanceUID=][&initialSopInstanceUID=][&initialFrameNumber=N]`
`[&layout=stack|mpr|mip|minip|vr][&viewerProfile=auto|workstation][&compactShell=bool]`
`[&compareStudyInstanceUID=][&compareLayout=side_by_side|reference|overlay][&compareSyncScroll=bool]`
`[&projectionSlabThicknessMm=N][&viewportSeriesInstanceUID=][&keyImageSopInstanceUID=][&keyImageFrameNumber=N]`

Validation: enum whitelists for `layout`, `viewerProfile`, `compareLayout`; booleans from a fixed set; positive integers for frame and thickness values. UIDs are only trimmed and checked for non-empty. There is **no DICOM UID syntax check** (see GREEN-9). The BFF access check runs before any archive read, which is the real gate.

---

## 2. Viewer features

| Feature | Status | Evidence |
|---|---|---|
| 2D stack viewing (Cornerstone StackViewport) | REAL | `Viewer.tsx:842-906`; `useViewportGrid.ts:333-369`; prefetch ±2/+4 (`viewerConfig.ts:7-8`) |
| Stack order | PARTIAL | Sorted by **InstanceNumber** (`loadSeriesIndex.ts:173, 212`), not by position. See YELLOW-1 |
| Scroll: wheel, click-drag, keyboard | REAL | `useViewportGrid.ts:328-331, 374`; `viewerCommandRegistry.ts` |
| W/L presets (12 plus default) | REAL | `viewerConfig.ts:90-110`; `resolveDisplayVoiRange` |
| Mouse W/L drag, zoom, pan | ABSENT | Cornerstone tools not initialised (`initializeCornerstone.ts:49-59`). Phase-0 had WindowLevel, Pan, Zoom tools (`phase-0/viewer/src/Viewer.tsx:49-52`); production dropped them |
| Auto VOI when no DICOM VOI (MR, CT) | REAL, per slice | `Viewer.tsx:727-797`; `dicom/autoVoi.ts:71`. Window changes slice to slice. See YELLOW-11 |
| "True size" | PLACEHOLDER (mislabelled) | `Viewer.tsx:1660-1663` says "approximation ... camera reset baseline"; the command is `camera.trueSize` (`registry:305`). It only resets the camera |
| Rotate 90°, flip H/V, invert | REAL as **CSS** display transforms | `Viewer.tsx:1492-1527` applies `transform` and `filter` to `.viewport` only. No pixel effect; not reflected in overlays or in export. See RED-3, RED-4 |
| "Pseudo-color LUT" | PLACEHOLDER (mislabelled) | Real effect is a CSS filter, `sepia(1) hue-rotate(180deg) saturate(2)` (`Viewer.tsx:1507`). Not a LUT. YELLOW-15 |
| "Filter / sharpen" | PLACEHOLDER (mislabelled) | Real effect is `contrast(1.15) brightness(1.05)` (`Viewer.tsx:1510`). No sharpening. YELLOW-15 |
| MPR, orthogonal (axial, coronal, sagittal) | REAL (VTK ORTHOGRAPHIC) | `useViewportGrid.ts:466-480`; `MPR_VIEWPORT_IDS` (`viewerViewportHelpers.ts:18`); gated by `volumeEligibility` (`useViewportGrid.ts:163-167`) |
| Oblique MPR | PARTIAL | Each orthogonal camera rotated independently about its own right axis, ±60° in 5° steps (`obliqueReslice.ts:78-122`, `obliquePlanning.ts:1-3`). Not one coherent oblique plane. YELLOW-14 |
| Reference lines | PARTIAL (schematic) | Drawn in a **separate** panel, positions are percent of the volume extent (`referenceOverlay.ts:75-79`; `ViewportGrid.tsx:118-157`). Not intersections on the images |
| MIP / MinIP | REAL | `csEnums.BlendModes.MAXIMUM/MINIMUM_INTENSITY_BLEND` (`useViewportGrid.ts:447-451`); slab 5-120 mm, default 20 mm (`viewerConfig.ts:19-22`). Auto-window skipped for projections (`Viewer.tsx:707-712`) |
| Slab thickness slider | REAL, but triggers full rebuild | Live path at `Viewer.tsx:1322-1330`, but `projectionSlabThicknessMm` is a dependency of the whole session effect (`useViewportGrid.ts:1351`). See YELLOW-16 |
| VR volume rendering (VTK VOLUME_3D, perspective) | REAL with caveats | `useViewportGrid.ts:469-480, 599-615, 689-690`. Gated by runtime probe, CPU-fallback block, memory estimate, and max image count |
| VR presets | PARTIAL | Four CT VTK presets mapped (`viewerViewportHelpers.ts:38-43`). Applied to **MR** series too (see YELLOW-7) |
| VR opacity | PLACEHOLDER | Stored in state only (`ViewportToolbar.tsx:206-217`). Nothing reads it in the VTK path. Changing it rebuilds the volume (`useViewportGrid.ts:1351`) |
| VR quality (standard/high) | PLACEHOLDER | Stored and shown; no sampling change in the VTK path |
| VR clipping | PLACEHOLDER | Info warning "unavailable in this runtime" (`useViewportGrid.ts:617-623`) |
| VR camera orientation | REAL but fixed | "Anterior" view hard-coded (`useViewportGrid.ts:809-888`), no orientation check. YELLOW-9 |
| Cine (stack only) | REAL | `Viewer.tsx:963-978, 1280-1319`; 4-30 fps (`viewerConfig.ts:24-26`) |
| Temporal navigation (phase or timepoint inside one series) | REAL, stack only | `temporalNavigation.ts:262-270`; keys Shift+arrows. **Not** respected by volume build (RED-1) |
| Hanging protocols | REAL | 6 seeded BFF rules (`bff-svc/src/viewer/studyPresentation.ts`); CT chest opens MPR with `ct-lung` (`:192-202`); CT head auto-cine (`:216`). Notice with revert (`HangingProtocolNotice.tsx`) |
| Prior-study compare (stack only) | REAL | Layouts side-by-side, reference, overlay (`ViewportGrid.tsx:195-232`). Forced to stack (`Viewer.tsx:1266-1277`) |
| Compare sync scroll | PARTIAL | Matches by **relative index** (`viewerViewportHelpers.ts:65-72`; `Viewer.tsx:1443`). Labelled "synchronized" (`registry:598`). YELLOW-12 |
| Series rail and thumbnails | REAL | `RailSeriesList.tsx`; thumbnails are `<img>` with no auth; falls back to a modality glyph |
| Series drag-and-drop onto viewport | REAL | `Viewer.tsx:2341-2363`; private MIME type `seriesThumbnails.ts:59` |
| Key images | PARTIAL, session only | `sessionStorage` key `pacs-viewer:key-images:<studyUID>` (`Viewer.tsx:1150-1168`). Not a DICOM KO. Not written to archive |
| Measurements: length, angle, rectangle ROI | REAL math, overlay misregistered | `dicom/measurements.ts:222-318`. ROI mean on CPU pixel data (`:320-364`). Overlay wrong on non-square viewports (RED-2) and after rotate or flip (RED-3) |
| Measurement persistence | ABSENT (session React state only) | Lost on reload. No SR or KO export. Single-measurement delete absent (`Viewer.tsx:1614-1618`) |
| Burned-in annotation | REAL | `viewportOverlayModel.ts:106-167`; `ViewportOverlay.tsx`. W/L shown only after an explicit preset (`Viewer.tsx:287-293`). No orientation letters, no slice location, no flip state |
| Orientation markers (R/L, A/P, S/I) | ABSENT | No code. OrientationMarkerTool reverted (`initializeCornerstone.ts:49-59`). See RED-4 |
| SR / PR (GSPS) display | ABSENT | Classified only (`dicomweb-sdk/src/index.ts:111-130`). Excluded from render (`loadSeriesIndex.ts:178-179`). Inspector text only (`nonImageInspector.ts`) |
| Fusion (PET/CT) | ABSENT | PT is not in the renderable SOP set (`dicomweb-sdk/src/index.ts:16-21`) |
| Secondary Capture | PARTIAL (non-diagnostic stack) | Rendered with warning; MPR, MIP, VR blocked (`dicomweb-sdk:52-58, 85-90`; `loadSeriesIndex.ts:31-54`; `Viewer.tsx:1186-1194`). Conformance says "NOT rendered" (`conformance/pacs-conformance-statement-v1.md:71`) |
| Snapshot export (PNG) | PARTIAL | `Viewer.tsx:1695-1707`: `canvas.toDataURL` of the first canvas. Excludes DOM overlays, measurements, CSS transforms. YELLOW-15 |
| Review manifest (JSON) | REAL | `Viewer.tsx:1721-1771`: UIDs, key images, measurements, compare state. No pixels |
| Print | REAL, unvalidated | `window.print()` (`Viewer.tsx:1774-1786`) |
| Secondary window | PARTIAL | `window.open` in a new tab; false "blocked" warning always (YELLOW-19) |
| Multi-monitor, multi-viewport beyond compare | ABSENT | No such code |
| Deferred or planned commands | PLACEHOLDER | 27 `deferredCommand(...)` entries in `viewerCommandRegistry.ts` (for example `edit.lockZoomPan`, `view.actualSize`, `filming.sendToPrinter`) |
| Memory guards | PARTIAL | VR only: estimate vs thresholds (`Viewer.tsx:214-228, 355-365`) and image cap (`volumeLoader.ts:161-163`). **None** for MPR or MIP (YELLOW-5) |
| Performance | PARTIAL | Workers capped at min(hwc, 4) (`initializeCornerstone.ts:67-70`); volume load batch 8 (`viewerConfig.ts:9`); Cornerstone default cache 3 GB (`cache.js:12`); eager metadata for all series (`loadSeriesIndex.ts:103-131`) |

---

## 3. Clinical safety

### 3.1 Geometry gate (what runs before MPR, MIP, MinIP, or VR)

Entry point: `classifyVolumeEligibility` (`dicom/volumeEligibility.ts:4-19`) calling `calculateSeriesGeometry` (`dicom/geometry.ts:161-243`). The gate is enforced in the hook before any volume is built (`useViewportGrid.ts:163-167`), and at launch (`Viewer.tsx:1067-1070`).

| Check | Where | Result |
|---|---|---|
| CT or MR only | `volumeEligibility.ts:9-11` | Enforced |
| Secondary Capture present makes series non-diagnostic | `volumeEligibility.ts:5-7`; `loadSeriesIndex.ts:31-54` | Enforced (stack only) |
| At least 3 instances | `geometry.ts:162-164` | Enforced |
| Every instance has geometry (FoR, IOP, IPP, spacing) | `geometry.ts:117-119, 166-169` | Enforced |
| One FoR UID across series | `geometry.ts:204-206` | Enforced |
| IOP equal across instances (tolerance 0.01) | `geometry.ts:207-209` | Enforced |
| Pixel spacing equal (tolerance 0.02) | `geometry.ts:210-212` | Enforced |
| Normal vector non-degenerate | `geometry.ts:121-126` | Enforced |
| Slice spacing consistent (median, tolerance max(0.2 mm, 20 %)) | `geometry.ts:223-227` | Enforced |
| **Duplicate slice positions** | `geometry.ts:217-218` filters out zero spacings | **NOT enforced**. RED-1 |
| **Gantry tilt or in-plane displacement** (positions not on the normal) | Not computed | **NOT enforced**. YELLOW-2 |
| **Row and column orthogonality** | `geometry.ts:121-122` normalises but does not check | **NOT enforced**. GREEN-4 |
| **Rows, Columns, BitsAllocated, rescale consistency across instances** | Not compared | **NOT enforced**. YELLOW-4 |
| **Pixel spacing strictly positive** | `geometry.ts:54-69` (`tuple2`, no check). `measurements.ts:77` does check | **NOT enforced**. GREEN-6 |
| Oblique acquisition vs patient axes | Not checked | **NOT enforced**. Open question |
| ImageType (LOCALIZER, DERIVED) | Used only in name-hint regex (`seriesSelection.ts:17-30`). Not in geometry | Not enforced. Localizer normally fails orientation check |
| Study rejected (IOCM) | `loadSeriesIndex.ts:61-75` | Enforced (blocks load) |
| Hidden series and instances (IOCM) | Hidden series skipped (`:106`). Hidden instances are **fetched** then filtered (`:131-156`) | Client-side filter only. YELLOW-24 |

The volume that is actually rendered is built by Cornerstone's own sort and spacing, not by the viewer's validated numbers. `sortImageIdsAndGetSpacing` sorts by projection on the normal and sets `zSpacing = |d_last - d_first| / (N-1)` (`node_modules/@cornerstonejs/core/dist/esm/utilities/sortImageIdsAndGetSpacing.js`). The viewer's `sliceSpacing` (median, `geometry.ts:223-236`) is used only in the technical-details panel (`seriesDetails.ts:141`) and is never sent to the renderer. So the 20 % tolerance applies to a number the render path ignores (YELLOW-3).

### 3.2 Unsupported and special SOP classes

- **Diagnostic allowlist:** CT, Enhanced CT, MR, Enhanced MR (`dicomweb-sdk/src/index.ts:16-21`). Matches `conformance/phase-1-ct-mr-allowlist.md`.
- **Secondary Capture family:** rendered as non-diagnostic stack, derived MPR, MIP, MinIP, and VR blocked, with a visible notice (`dicomweb-sdk:52-58, 85-90`; `Viewer.tsx:1186-1194, 2365-2372`). Conformance says NOT rendered (YELLOW-27).
- **CR, DX, US, MG, PT, XA:** the modality helper exists (`dicomweb-sdk/src/index.ts:156-158`) but is **never called**. These SOP classes are not in the render set, so their instances are dropped (`loadSeriesIndex.ts:178-179`). A series with no remaining instances is listed as unsupported with an explanation (`:214-222`, `explainNonRenderableSopClasses` at `dicomweb-sdk:132-154`). This matches the "pending validation" line in conformance.
- **SR and PR:** classified, not rendered (`dicomweb-sdk:111-130`). Only the inspector describes them.
- **Silent drop inside a renderable series:** instances with unsupported SOP class are removed without any notice (`loadSeriesIndex.ts:178-179`). GREEN-14.
- **Transfer syntax:** the viewer never reads `(0002,0010)`. Decode is delegated to dicom-image-loader codecs (`initializeCornerstone.ts:72` sets `initializeCodecsOnStartup: false`). An unsupported transfer syntax fails at frame decode. Open question.
- **Photometric interpretation** (MONOCHROME1 inversion, RGB): not read by the viewer. Left to Cornerstone. Open question.

### 3.3 Orientation, laterality, and display transforms

- **No orientation markers.** Nothing renders R/L, A/P, or S/I. The Cornerstone OrientationMarkerTool was reverted (`initializeCornerstone.ts:49-59`). The overlay model has no orientation field (`viewportOverlayModel.ts:28-33`). RED-4.
- **Flip and rotate are CSS only**, applied to `.viewport` (`Viewer.tsx:1499-1518`). The burned-in overlay, the measurement SVG, and the click layer are siblings and are not transformed (`ViewportGrid.tsx:187-191`). After a flip, the image is mirrored and nothing on screen says so. RED-3, RED-4.
- **VR anterior view is hard-coded** (`useViewportGrid.ts:809-888`). It assumes LPS, supine, head-first. Patient position is not checked.
- **MPR pane labels** say "Axial MPR" etc. Oblique tilt of the acquisition is not detected, so an obliquely acquired "axial" is labelled axial.

### 3.4 Spacing and calibration

- Pixel spacing comes from `instance.geometry.pixelSpacing` (`geometry.ts:111-115`, Pixel Measures functional group or top level), with `readNumberPair` as fallback for measurements (`measurements.ts:142`).
- **Silent 1 mm fallback:** `DEFAULT_PIXEL_SPACING_MM = [1, 1]` (`measurements.ts:12`) is used when neither source exists. Length, angle, and area are still printed as "mm" (`measurements.ts:110-124`). The measurement tool is enabled for any stack series, including non-diagnostic ones (`Viewer.tsx:371`). RED-5.
- Row and column order is handled correctly: `PixelSpacing = [row, column]`, so `dy` uses `rowSpacing` and `dx` uses `columnSpacing` (`measurements.ts:229-230, 256-261`).
- Cornerstone StackViewport applies pixel spacing when present (`node_modules/@cornerstonejs/core/dist/esm/RenderingEngine/StackViewport.js:528, 728-736`). The measurement rectangle uses the same physical aspect, so the click mapping is consistent with the drawn image.
- Volume z-spacing is the **average** over the sorted range, not per-slice gaps (`sortImageIdsAndGetSpacing.js`). Non-uniform gaps are silently averaged (YELLOW-3).

### 3.5 Known limitations (as coded)

- No zoom, pan, or mouse W/L (ABSENT).
- Measurements are session-only and lost on reload. No DICOM SR, no KO, no archive write.
- No PET/CT fusion. No PR or GSPS application.
- VR opacity, quality, and clipping are non-functional (PLACEHOLDER).
- Oblique MPR is camera tilt, not reslice. Reference lines are schematic.
- Compare sync is index-proportional, not anatomical.
- Stack order and volume order are different sorts (InstanceNumber vs position).

---

## 4. Security

| # | Area | Finding | Evidence |
|---|---|---|---|
| SEC-1 | Pixel and thumbnail path not bound to BFF authorization | Frames and thumbnails are fetched without the bearer. Cornerstone's `beforeSend` default adds nothing (`node_modules/@cornerstonejs/dicom-image-loader` source map: `imageLoader/internal/options.ts`, `internal/xhrRequest.ts` calls `options.beforeSend` then `setRequestHeader`). The viewer never calls `dicomImageLoader.configure({ beforeSend })` (`initializeCornerstone.ts:63-66`). Traefik's `dicom-web` router has no forwardAuth (`routers.yml:16-21`). Whether arc-light enforces bearer on `/dicom-web` is **not in this repo**. If it does not, anyone who knows study, series, and SOP UIDs can read pixels, which bypasses BFF authorization and IOCM. RED-6 (conditional) |
| SEC-2 | PHI in URLs | `patientMrn` and `issuerOfPatientId` are carried in query strings (`deep-link/src/index.ts:79-80`; `useViewerCommands.ts:252-253, 275`). Compare and layout changes use `window.location.assign` (`useViewerCommands.ts:283, 293, 305, 319`), so the MRN lands in history. The OIDC `state` carries pathname plus search to the IdP (`auth/src/index.ts:71`). YELLOW-20 |
| SEC-3 | Token passthrough | BFF returns the caller's Keycloak JWT as `bearer_for_dicomweb` (`bff-svc/src/app.ts:337`; `auth.ts` OIDC branch sets `bearerToken: token`). The viewer sends it to the archive (`loadSeriesIndex.ts:82`). The archive now holds the user's IdP token. YELLOW-21 |
| SEC-4 | Token storage | Access token in localStorage (`auth/src/index.ts:19`). Readable by any script on the origin. No silent renew (`auth/src/index.ts:11-23`). YELLOW-22 |
| SEC-5 | Dev auth in the build | `.env:11` sets `VITE_AUTH_MODE=dev`. There is no `.env.production`. Vite 5.4.21 loads `.env` in every mode (`node_modules/vite/dist/node/chunks/dep-BK3b2jBa.js:34884-34893`). The built bundle `dist/assets/index-DobtSyw4.js` contains the static dev bearer literal (one occurrence). Mitigated: BFF defaults to OIDC (`bff-svc/src/config.ts:124`) and rejects dev tokens in that mode (`auth.ts`). YELLOW-25 |
| SEC-6 | XSS sinks | None found. Grep for `dangerouslySetInnerHTML`, `innerHTML`, `outerHTML`, `insertAdjacentHTML`, `document.write`, `eval`, `new Function`, `postMessage`, `srcDoc`, `href={`: zero hits in viewer and packages. DICOM strings render as React text. UIDs in API paths use `encodeURIComponent` (`api-client/src/index.ts`; `seriesThumbnails.ts:44`). `buildWadorsFrameImageId` does not encode (`dicomweb-sdk/src/index.ts:13`). GREEN-17 |
| SEC-7 | CSP | None. `index.html` has no CSP meta. Traefik `security-headers` sets no CSP (`routers.yml:124-140`). Viewer code needs no inline script, so a strict `script-src 'self'` is feasible. Recommendation |
| SEC-8 | COOP and COEP | Set only in Vite dev and preview (`vite.config.ts:52-54, 65-67`). No production container config for the viewer is in this repo. VR availability depends on cross-origin isolation (`runtimeCapabilities.ts:198-205`). YELLOW-26 |
| SEC-9 | Error boundary disclosure | `buildDiagnostics` includes `window.location.href` (`packages/ui/src/ErrorBoundary.tsx:23`), so the copied text contains the MRN query. GREEN-12 |
| SEC-10 | Deep-link validation | Enum and boolean whitelists, positive ints. No UID syntax check. The BFF checks patient-context mismatch before issuing access (`bff-svc/src/app.ts` access route, `createStudyContextMismatchError`). Compare study has its own access check (`App.tsx` compare block) |
| SEC-11 | Patient identity | DICOM PatientName and PatientID (0010,0010 / 0010,0020) are **never read** by the viewer (no hits). Identity on screen comes only from BFF context. If the context call fails, the image shows with no identity and only a console warning (`App.tsx:183-185`). YELLOW-23 |
| SEC-12 | Storage hygiene | `Reset preferences` clears only `^pacs[.:-]` keys (`ErrorBoundary.tsx:8, 34-41`). Session key images are stored as UIDs only. Fine |
| SEC-13 | Logging | Production `console.log('[VR-DIAG] ...')` with volume IDs and state (`useViewportGrid.ts:723, 784, 1042, 1147`). YELLOW-18 |

---

## 5. Shared packages

| Package | Exports | Quality, tests | Notes |
|---|---|---|---|
| `@pacs/api-client` (1502 lines, `src/index.ts`) | `PacsApiClient` (about 150 methods), `ApiClientError`, `isAbortError`, `buildMutationRequestOptions`, and about 100 types | Single `request()` (`:1414-1441`) attaches Bearer and handles abort. Typed. Tests: `tests/api-client.test.ts` (13 cases) | Viewer uses only three methods: `getStudyAccess`, `getStudyContext`, `getStudyHangingProtocolMatch`. Rest is for other apps. One large file |
| `@pacs/auth` (74 lines) | `createUserManager`, `createDevTokenProvider`, `resolveBrowserAuthMode`, `createBrowserTokenProvider`, `AuthConfig` | Clear. Tests: `tests/auth-defaults.test.ts` (2 cases, mode default only). **Token provider has no test.** Sentinel string control flow (`'Redirecting to login.'`, `auth/src/index.ts:72`; `App.tsx:115, 203`) | Storage and renew choices are in SEC-4 |
| `@pacs/deep-link` (162 lines) | `ViewerLaunchParams`, `parseViewerLaunchParams`, `buildViewerUrl` | Clean whitelists. Tests: `tests/deep-link.test.ts` (13 cases) | `parsePositiveInt` uses `parseInt`, so `"1e3"` parses as 1 (GREEN-7) |
| `@pacs/dicomweb-sdk` (158 lines) | `buildWadorsFrameImageId`, `PHASE_1_RENDERABLE_SOP_CLASS_UIDS`, `SOP_CLASS_UID_NAMES`, `getImageRenderPolicy`, `isRenderableImageSopClass`, `isNonDiagnosticImageSopClass`, `getSopClassName`, `describeSopClass`, `isStructuredReportSopClass`, `isPresentationStateSopClass`, `getNonRenderableObjectKind`, `explainNonRenderableSopClasses`, `isRenderableImageModality` | Small and readable. Tests: `tests/dicomweb-sdk.test.ts` (4 cases) | `isRenderableImageModality` is dead (GREEN-1). `buildWadorsFrameImageId` does not encode UIDs (GREEN-17). Conformance text disagrees with SC policy (YELLOW-27) |
| `@pacs/ui` (about 20 modules) | `ErrorBoundary` (used), `MotionProvider` (used), `Icon`, `ModalityGlyph` (used), `ConfirmationModal`, `ConnectionIndicator`, `Drawer`, `toastStore`, `pendingWrites` (297 lines), `connectionStore` (234), `i18n` (200), tokens and fonts CSS | `ErrorBoundary` is solid: separate reset, inline styles so it renders without app CSS. Tests: 8 files `tests/ui-*.test.ts` (dirty-guard, announcer, error-boundary, exit-transition, toast-store, csv, i18n, connection). Viewer uses only 4 exports | Unused modules still ship into the viewer barrel. Acceptable |
| Viewer-internal pure modules | Most of the decision logic | `tests/viewer-phase4.test.ts` (62 cases) covers geometry, measurements math, oblique camera, presets, temporal, reducer, VR capability probe, volume ID, sampling. `tests/viewer-overlay-and-previews.test.ts`, `viewer-hanging-protocols.test.ts`, `viewer-command-registry.test.ts`, `viewer-ui-scale.test.ts` | No test covers the SVG overlay registration, rotate or flip interaction, duplicate-position geometry, or gantry tilt. The one measurement mapping test (`viewer-phase4.test.ts:449-477`) uses a 200×200 rect, not the 100×100 rect the overlay uses |

**`phase-0/viewer`** (365 lines, skim): a spike with the same Cornerstone deps. It uses WindowLevel, Pan, Zoom, and StackScroll tools, plus direct `wadors:` loading with no auth. Production removed the tools and added the BFF. The tools removal was a response to a layout-switch regression (`initializeCornerstone.ts:49-59`).

**`conformance/`** (skim): `pacs-conformance-statement-v1.md:64-67` lists CT, MR, Enhanced CT, Enhanced MR as "viewer renders". Lines 68-73 list CR, DX, US, SC, and PDF as archive-accepted and not rendered, with SC stated NOT rendered (line 71). The code renders SC as non-diagnostic (YELLOW-27). Transfer syntaxes listed at `:96-101`. The viewer enforces none of them itself.

---

## 6. Code review findings

Severity: RED, YELLOW, GREEN. Findings are ordered by impact. "Scenario" is the failure path. "Fix" is the minimum change.

### RED

**RED-1. Duplicate slice positions pass the geometry gate and produce a z-compressed, interleaved volume.**
- `dicom/geometry.ts:215-221`:
  ```
  const spacings = sliceLocations.slice(1).map(...);
  const positiveSpacings = spacings.filter((spacing) => spacing > EPSILON).sort(...);
  if (positiveSpacings.length === 0) { return ... 'Series has duplicate slice positions.' }
  ```
- Issue: zero spacings are dropped before the consistency test, so duplicates are invisible unless **every** spacing is zero.
- Why it matters: the viewer already models phase and timepoint groups **inside one series** (`temporalNavigation.ts:262-270`, tests at `viewer-phase4.test.ts:923-1028`). Such series (dynamic MR, multi-phase CT, multi-b-value DWI, multi-echo in one SeriesInstanceUID) are normal inputs. `createVolumeForSeries` uses **all** instances (`volumeLoader.ts:155, 170-178`).
- Failure scenario: 4 phases × 60 slices at spacing s. The gate passes (`positiveSpacings` are all s). Cornerstone sets `zSpacing = 59s/239 ≈ 0.25 s` (`sortImageIdsAndGetSpacing.js`). The volume has 240 slices with phases interleaved as adjacent slices. The MIP slab (for example 20 mm) then contains about four times as many phase images, so the MIP is a max over phases. MPR and VR show the same interleaving.
- Fix: count duplicate positions (including zeros) and mark the series `unsafe_geometry` with a clear reason. Better, split the series by temporal group (or refuse derived views for multi-group series). Add a test with duplicated locations.

**RED-2. Measurement graphics are drawn in a 100×100 box stretched to the viewport.**
- `components/MeasurementOverlay.tsx:117-120`:
  ```
  const measurementRect = useMemo(
    () => resolveMeasurementViewportRect(activeInstance, 100, 100),
  ```
- `components/MeasurementOverlay.tsx:307`: `<svg className="measurement-overlay-svg" viewBox="0 0 100 100" preserveAspectRatio="none">`. `styles.css` `.measurement-overlay` and `.measurement-overlay-svg` are `inset: 0` and `100%`, with no aspect ratio (`styles.css:2172-2183`).
- Issue: the image rectangle is letterboxed inside a square 100-unit box, and then the SVG is stretched non-uniformly to the real viewport. Clicks are mapped with the real size (`:153-154, 190-191`), so the **numbers** can be right while the **graphics** sit in the wrong place.
- Failure scenario (arithmetic, unverified live): viewport 800×400, image 512×512 with 0.5 mm spacing. The real image rectangle is x = 200..600. Left edge (anchor x = 0) draws at x_px = 0. Right edge (anchor x = 511) draws at x_px = 800. A segment across the middle half of the image (true screen x = 300 to 500 px) is drawn from x = 200 to 600 px, twice as wide and offset. The rule holds whenever viewport aspect differs from 1:1 in the 100-unit frame, which is the normal case for a wide viewport. The existing test (`viewer-phase4.test.ts:449-477`) uses a 200×200 rect and does not catch it.
- Fix: draw the SVG in the same pixel space as the viewport (`viewBox` equal to the element's `clientWidth/clientHeight`, or compute the rect from the live element size) and drop `preserveAspectRatio="none"`.

**RED-3. Rotate and flip are not applied to the click mapping, the measurement graphics, or the ROI statistics.**
- `Viewer.tsx:1499-1518` applies `transform` and `filter` to `.viewport` only.
- `MeasurementOverlay.tsx:148-158, 180-194` maps clicks from the interaction layer, which is a sibling of `.viewport` (`ViewportGrid.tsx:188-190`), so **not transformed**.
- Consequences:
  1. Committed measurements are drawn in the untransformed frame, so they no longer sit on the anatomy after a rotation or flip.
  2. A new point at screen position P is stored as pixel P (unrotated). The image pixel actually under P in the rotated view is R⁻¹(P). Length is still right for isotropic pixels, because rotations are isometries. It is wrong for anisotropic pixels after a 90° rotation, because the screen x-axis then runs along the row direction, but `dx` uses column spacing (`measurements.ts:229-230`).
  3. ROI mean (`MeasurementOverlay.tsx:274-288`, `measurements.ts:320-364`) averages the pixel rectangle at the unrotated coordinates, not the rectangle drawn on screen. This is the closest analogue in this codebase to a "wrong pixel rectangle" defect.
- Why it is reachable: rotate and flip are enabled whenever `viewportReady` (`viewerCommandRegistry.ts:~440-470`), and measurements are enabled in stack mode (`Viewer.tsx:371`).
- Fix: either (a) apply the same transform to the overlay and the interaction layer and invert it in the mapping, or (b) bake the transform into the image (rotate and flip at the DICOM or canvas level) and then map. Until fixed, disable measurement while a rotation or flip is active.

**RED-4. Orientation is not shown, and a flip or rotate is not labelled.**
- No orientation markers (grep: none). `initializeCornerstone.ts:49-59` reverted the OrientationMarkerTool. `viewportOverlayModel.ts:28-33` has no orientation or flip field. `ViewportOverlay.tsx` draws only the four corners.
- Failure scenario: a radiologist flips horizontally to compare a side, and the screen shows a mirrored image with no L or R marker and no "flipped" indicator. A wrong-side reading is possible. The same applies to a 90° rotation.
- Fix: draw patient-orientation letters from the IOP (projected onto LPS axes) on the viewport edges, and add "Flipped H" or "Rotated 90°" to the bottom-right overlay. Do not allow flip or rotate without this.

**RED-5. Uncalibrated images are measured in "mm" against an assumed 1 mm/pixel.**
- `dicom/measurements.ts:12` `const DEFAULT_PIXEL_SPACING_MM: [number, number] = [1, 1];` and `:142` `const pixelSpacing = instance.geometry?.pixelSpacing ?? readNumberPair(...) ?? DEFAULT_PIXEL_SPACING_MM;`.
- `Viewer.tsx:371` enables measurement on any stack view. `formatMeasurementValue` always appends "mm" (`measurements.ts:110-124`).
- Failure scenario: a Secondary Capture, ultrasound, or screenshot without Pixel Spacing is measured. The output reads "42.0 mm" when the true size is unknown.
- Fix: return `null` spacing when no calibration exists, show "uncalibrated (pixels)" instead of mm, and disable area and length for non-diagnostic series.

**RED-6. Pixel and thumbnail requests are not bound to the BFF access decision (conditional).**
- Evidence in SEC-1: `loadSeriesIndex.ts:79-83, 131-134` (bearer on metadata only); no `beforeSend` (`initializeCornerstone.ts:63-66`); `seriesThumbnails.ts:44`, `RailSeriesList.tsx:152`; no forwardAuth on `/dicom-web` (`routers.yml:16-21`); BFF access check runs only on the metadata path.
- Failure scenario: if arc-light accepts unauthenticated `GET .../frames/n` and `.../thumbnail`, any client with UIDs can read pixels. The BFF's per-study authorization and IOCM hiding are then advisory.
- Fix: confirm arc-light's auth configuration. If it is open, put an auth gate in front of `/dicom-web` (forwardAuth on Traefik or an arc-side token check), and set `dicomImageLoader.configure({ beforeSend })` to add the bearer. Note that `<img>` cannot send a bearer, so thumbnails need a cookie or a signed URL.

### YELLOW

**YELLOW-1. Stack order (InstanceNumber) and volume order (position) are different. Stack index is reused as volume index.**
- `loadSeriesIndex.ts:173, 212` sorts by InstanceNumber. Cornerstone sorts volumes by projection on the normal (`sortImageIdsAndGetSpacing.js`). `geometry.ts:237` computes `sortedSopInstanceUIDs` but **nothing reads it** (grep).
- `useViewportGrid.ts:440-441` takes the stack index as the request; `:1177-1189` scrolls the volume by `startDerivedImageIndex - sourceSliceIndex`, treating the two indices as the same space; `:1202-1205` writes the volume index back into `currentImageIndex`.
- Failure scenario: a series numbered head to foot, or one with a reversed normal. The volume index is then mirrored. Switching from stack image 10 to MPR shows the slice at position 10 from the other end, and the overlay still reads "Image 10". Auto-VOI for the displayed slice also uses the wrong instance (`Viewer.tsx:1388`).
- Fix: sort the stack by position along the normal (use the already computed `sortedSopInstanceUIDs`) or map indices through SOP UID.

**YELLOW-2. Gantry tilt and in-plane displacement are not detected.** `geometry.ts:215-227` only checks spacing along the normal. Cornerstone's z model also assumes stacking along the normal. A tilted series is sheared in MPR, MIP, and VR without any notice. Fix: check that consecutive IPP differences are parallel to the normal within a tolerance, and mark tilted series `unsafe_geometry`.

**YELLOW-3. The viewer's validated spacing is not the spacing the renderer uses.** `geometry.ts:223-236` validates a median with a 20 % tolerance. The renderer uses `(d_last - d_first)/(N-1)` (`sortImageIdsAndGetSpacing.js`). Non-uniform gaps are averaged silently. Fix: derive the render spacing from the same numbers that were validated, or fail validation when the per-slice gaps exceed the tolerance.

**YELLOW-4. Consistency across instances is incomplete.** Rows, Columns, BitsAllocated, PixelRepresentation, and rescale are not compared (`geometry.ts:191-213`). Cornerstone reads these from the middle or first instance (`generateVolumePropsFromImageIds.js`). Mixed sizes in one series produce a wrong volume. Fix: add those comparisons to the gate.

**YELLOW-5. No memory or image-count guard for MPR, MIP, or MinIP.** The only limit is `maxImageCount` for VR (`useViewportGrid.ts:510-513`, `viewerConfig.ts:10, 16, 77-86`). For derived views the only backstop is Cornerstone's 3 GB default cache, which throws `CACHE_SIZE_EXCEEDED` (`generateVolumePropsFromImageIds.js`, `cache.js:12`). Fix: apply the same estimate and cap to all volume layouts.

**YELLOW-6. The VR memory estimate is about 2× low for 16-bit data.**
- `Viewer.tsx:225`: `const bytesPerVoxel = Math.max(1, (bitsAllocated / 8) * samplesPerPixel);` gives 2 bytes for 16-bit.
- Cornerstone allocates Float32 for 16-bit unless `use16BitDataType` (`generateVolumePropsFromImageIds.js`: `if (!use16BitDataType || ...) { ... new Float32Array(length) }`). `use16BitDataType` is false by default (`init.js:15-16`), and the norm16 path is iOS-only (`init.js:77`). The image cache also holds Float32 frames (`volumeLoader.ts:13-16, 25`).
- Failure scenario: discrete GPU, 2000 images of 512×512 16-bit. The estimate is about 1,000 MiB (2 bytes per voxel), under the 1,536 MB block (`viewerConfig.ts:16-18, 77-86`), so VR is allowed. The Float32 volume is about 2.1 GB, and the cached frames are about 2.1 GB more. Cornerstone's default cache is 3 GB.
- Fix: use 4 bytes per voxel for the volume and add the frame cache, or set `use16BitDataType` explicitly and measure.

**YELLOW-7. VR presets are CT transfer functions applied to MR.** `VR_PRESET_TO_VTK_PRESET` maps to `CT-AAA`, `CT-Bone`, `CT-Lung`, `CT-Soft-Tissue` (`viewerViewportHelpers.ts:38-43`). They are applied with no modality check (`useViewportGrid.ts:599-603, 689-690`). MR is volume-eligible (`volumeEligibility.ts:9`), and `canUseDerivedViews` allows it (`Viewer.tsx:351`). MR intensities are not HU, so the opacity mapping is arbitrary. Fix: gate VR presets to CT, or add MR presets.

**YELLOW-8. VR reduced-sampling retry is disclosed only at info level.**
- `useViewportGrid.ts:1234-1256`: on a VTK "Not implemented" error, retry with 256, then 128, then 64 slices. `volumeLoader.ts:34-62` picks indices across the range. Cornerstone computes spacing from the first and last positions, so the extent is kept but the slices are sparse and unevenly spaced.
- The only message is `addWarning({ level: 'info', ... 'reduced slice sampling (N/M)' })` (`:1210-1215`).
- Failure scenario: thin vessels or fractures disappear in the 3D image, and the radiologist reads it as complete.
- Fix: warning level, and state the consequence ("3D image uses N of M slices; thin structures may be missing").

**YELLOW-9. VR camera placement is repeated and overrides user interaction.** `finalizeVrRender` runs synchronously and at rAF#1, rAF#2, 100 ms, 400 ms, and 1000 ms, and again on every position shift for 3 s (`useViewportGrid.ts:828-906, 1065-1162`). Each pass calls `setPosition` and `setFocalPoint` (`:877-884`). A user drag within those seconds is undone. The "anterior" view is also fixed (`:809-888`). Fix: run placement once after the first successful bounds check and never again unless the volume changes.

**YELLOW-10. Manual fallback transfer function silently replaces the preset.** `useViewportGrid.ts:953-981` installs a broad grayscale ramp when the preset's opacity is empty or all zero. The user is not told, and the image looks like a valid VR of the selected preset. Fix: fail the VR layout with a reason, or label the view "fallback transfer function".

**YELLOW-11. Auto window changes from slice to slice.** For MR or CT without DICOM VOI, the window is a 2 %-99 % percentile of **that slice's** pixels (`dicom/autoVoi.ts:71-126`, `Viewer.tsx:727-797`, cache keyed by instance at `:741`). Scrolling changes contrast on every slice. Fix: compute one window per series (or per study) from a sample of slices, and keep it while scrolling.

**YELLOW-12. Compare sync is index-proportional and is labelled as synchronized.** `mapRelativeImageIndex` maps by fraction of the stack (`viewerViewportHelpers.ts:65-72`; `Viewer.tsx:1126, 1443`; `useViewportGrid.ts:414`). Prior studies usually have a different FoR and different coverage, so the matched level can be wrong. `findNearestSliceIndex` matches by position but only for the same FoR, and it is used only for same-study series switches (`useViewerCommands.ts:112`). The label reads "Compare scrolling is synchronized" (`registry:598`). Fix: match by anatomy (for example, report level or landmark), or label it "proportional sync, not anatomical".

**YELLOW-13. Reference lines are not on the images.** They are a schematic panel with percent positions (`referenceOverlay.ts:75-79`, `ViewportGrid.tsx:118-157`). A radiologist may read them as intersection lines. Fix: draw intersection lines from geometry on each MPR pane, or rename the panel.

**YELLOW-14. Oblique is not a coherent oblique reslice.** `buildObliqueMprCamera` rotates each orthogonal camera about its own right axis (`obliqueReslice.ts:78-122`; `Viewer.tsx:630-651`). The three panes do not share one oblique plane, so they do not intersect consistently. The label says "Oblique MPR" (`ViewportGrid.tsx:86-91`). Fix: compute a true oblique reslice (sample the volume along a plane) or label it "camera tilt".

**YELLOW-15. Several controls are mislabelled and the snapshot is incomplete.**
- "True size" only resets the camera (`Viewer.tsx:1660-1663`; `registry:305`).
- "Pseudo-color LUT" is a CSS sepia and hue-rotate (`Viewer.tsx:1506-1508`; `registry:485`).
- "Filter / sharpen" is contrast and brightness (`Viewer.tsx:1509-1510`; `registry:465`).
- "Add display to film" downloads a PNG (`Viewer.tsx:1688-1719`; `registry:501`). The PNG excludes overlays, measurements, rotation, flip, invert, and filters, because those are CSS and not in the canvas (`Viewer.tsx:1695-1707`). The status message says "film/print workflow handoff".
- Fix: rename the controls to what they do, and either bake the transforms and overlays into the export or label the PNG "image only, no annotations".

**YELLOW-16. Slider changes rebuild the whole derived volume.** `useViewportGrid.ts:1351` lists `projectionSlabThicknessMm`, `viewerState.vr.opacityPercent`, `viewerState.vr.quality`, `viewerState.vr.clippingEnabled`, and `viewerState.compare.syncScroll` as dependencies of the session effect. Each change tears down the engine, removes cached volumes (`:1340-1346`), and rebuilds. The live path at `Viewer.tsx:1322-1330` already sets slab thickness on existing viewports, so the rebuild is redundant. Fix: remove the no-op and live-path dependencies from the session effect.

**YELLOW-17. Measurements are lost on reload and have no archive representation.** Session React state only (`state/viewerState.ts:103-124`; no persistence). The overlay export is JSON metadata only (`Viewer.tsx:1721-1771`). Fix: decide the requirement. If measurements must survive, store them as DICOM SR or as archive-side annotations.

**YELLOW-18. Debug logging and DOM walks ship to production.** `useViewportGrid.ts:692-807` walks twelve ancestors with `getBoundingClientRect` and `getComputedStyle` (forces layout) on every VR build. `:723, 784, 1042, 1147` log `console.log('[VR-DIAG] ...')` with volume IDs and state. Fix: remove or gate behind a dev flag.

**YELLOW-19. The secondary-window warning is always shown.** `useViewerCommands.ts:336-341`: `const opened = window.open(url, '_blank', 'noopener,noreferrer'); if (!opened) { addWarning(... blocked ...) }`. Per the HTML and window.open behaviour (platform knowledge, not repo code), `noopener` makes `window.open` return `null` even when the tab opens. So the "blocked" branch runs on every successful open. Fix: drop `noopener` (and set `opener = null` after open) so the return value is meaningful, or remove the warning on success.

**YELLOW-20. PHI in URLs and in the OIDC state.** `useViewerCommands.ts:252-253` puts `patientMrn` and `issuerOfPatientId` into `nextLaunch`; `:275` builds the URL; `:283-319` assigns it. The IdP receives `state` = pathname plus search (`auth/src/index.ts:71`). MRN appears in browser history, proxy and access logs, and the IdP's logs. Fix: keep patient context in the BFF session or a short-lived token. At minimum, do not put it in `state`.

**YELLOW-21. The archive receives the user's IdP JWT.** `bff-svc/src/app.ts:337` returns `bearer_for_dicomweb: user.bearerToken`. `auth.ts` sets `bearerToken` to the verified token. The viewer forwards it to the archive (`loadSeriesIndex.ts:82`). Fix: issue a scoped, short-lived DICOMweb token (or use a BFF-side proxy), and do not return the user's own token to the browser.

**YELLOW-22. Access token in localStorage with no silent renew.** `auth/src/index.ts:19` `userStore: new WebStorageStateStore({ store: window.localStorage })`; no `automaticSilentRenew` (`:11-23`). Any XSS reads the token. Expiry forces a redirect mid-study. Fix: keep the session in memory or use `sessionStorage`, enable silent renew, and check `expires_at`.

**YELLOW-23. Patient identity on screen depends on a non-blocking call.** `App.tsx:179-185` loads context without blocking; failure only logs. The overlay omits patient lines (`viewportOverlayModel.ts:114-120`), and the toolbar shows "Unknown patient" (`Viewer.tsx:2013`). The DICOM PatientName and PatientID are never compared (no `0010` tags in the viewer). Fix: block the viewer on context success, or show a visible "identity unavailable, do not use" banner. Compare DICOM PatientID with the launch MRN.

**YELLOW-24. IOCM hiding is not access control.** `loadSeriesIndex.ts:131-156` downloads metadata for hidden instances (only hidden series are skipped, `:106`), then filters. The data is in the browser. Fix: have the BFF or archive omit hidden objects from the metadata response.

**YELLOW-25. Dev auth can be compiled into a production build.** `.env:11` sets `VITE_AUTH_MODE=dev`, `.env` is loaded in all modes (`vite/dist/node/chunks/dep-BK3b2jBa.js:34884-34893`), there is no `.env.production`, and the built bundle contains the dev bearer literal (SEC-5). Mitigation: BFF defaults to OIDC (`bff-svc/src/config.ts:124`). Fix: add `.env.production` with `VITE_AUTH_MODE=oidc`, and fail the build when dev mode is set outside `vite` dev.

**YELLOW-26. No CSP and no production cross-origin isolation in the repo.** `index.html` has no CSP. COOP and COEP appear only in `vite.config.ts:52-54, 65-67`. `runtimeCapabilities.ts:198-205` makes VR unavailable without isolation, so production VR availability depends on a container config that is not in this repo. Fix: add both headers to the viewer-spa container and a strict CSP (SEC-7).

**YELLOW-27. Conformance and code disagree on Secondary Capture.** `pacs-conformance-statement-v1.md:71` says "NOT rendered by viewer". `dicomweb-sdk/src/index.ts:88` and `loadSeriesIndex.ts:179` render it as a non-diagnostic stack. Fix: update the conformance statement, or stop rendering SC.

**YELLOW-28. Stack and series selection rely on free-text heuristics.** Preset hints (`viewerConfig.ts:108-130`) and initial series (`seriesSelection.ts:17-30`) match SeriesDescription and ProtocolName with regexes. `/\btemporal\b/` maps to bone window (`viewerConfig.ts`, `ct-bone` patterns). A brain series with "temporal" in the description and no "brain" token gets W2000 L500. The overlay shows the window, so it is visible but not obvious. Fix: prefer coded attributes, and show the reason in the notice.

### GREEN

- **GREEN-1.** `dicomweb-sdk/src/index.ts:156-158` `isRenderableImageModality` is exported and unused. Remove or use.
- **GREEN-2.** `@cornerstonejs/tools` is a declared dependency with `cornerstone-tools-submodules.d.ts` shims, and nothing imports it. Remove, or restore tools deliberately (with a persistent tool group).
- **GREEN-3.** Stale comments: `Viewer.tsx:230-243` has orphan comment blocks for code that no longer exists. `Viewer.tsx:1332-1336` and `useViewportGrid.ts:31` say 1,199 lines; the hook is 1,352.
- **GREEN-4.** `geometry.ts:121-126` normalises IOP vectors without checking orthogonality or unit length. Malformed data is silently repaired.
- **GREEN-5.** `viewerState.ts:96` sets the initial VR `disabledReason` to "Volume rendering is not implemented yet." It is used via `||` at `Viewer.tsx:2387` until the first effect runs.
- **GREEN-6.** `geometry.ts:54-69` (`tuple2`) does not check positive pixel spacing. Zero spacing passes validation, while `measurements.ts:77` does check. Fix: check `> 0` in the geometry extractor.
- **GREEN-7.** `deep-link/src/index.ts:20-24` `parsePositiveInt` uses `Number.parseInt`, so `"1e3"` becomes 1.
- **GREEN-8.** ROI mean includes the boundary pixel (`measurements.ts:332-335` floor min, ceil max), biasing the mean slightly.
- **GREEN-9.** Launch UIDs are only trimmed (`deep-link/src/index.ts:69-75`). The BFF gate makes this safe in practice, but a DICOM UID syntax check is cheap.
- **GREEN-10.** `Viewer.tsx:1167, 1175, 1183` call `sessionStorage` and `localStorage` directly, with no try and catch. Quota or private-mode errors would reach the root error boundary. Other preference helpers do guard (`hangingProtocol.ts:45-59`).
- **GREEN-11.** `Viewer.tsx:1067-1070` applies the MPR, MIP, and MinIP launch fallback to stack, but VR launch has no such fallback. `useViewportGrid.ts:156-161` returns with a status message and leaves the layout on VR. Fix: fall back to stack for VR as well.
- **GREEN-12.** `ErrorBoundary.tsx:23` copies `window.location.href` into the diagnostics, including the MRN query.
- **GREEN-13.** `autoVoi.ts:81` `sampleStep = floor(length / 16384)`. For 512×512 the step is 16, so only columns that are multiples of 16 are sampled. Small bias, deterministic.
- **GREEN-14.** Unsupported instances are dropped silently inside a renderable series (`loadSeriesIndex.ts:178-179`). Add a count to the series notice.
- **GREEN-15.** `gpuDiagnostics.ts:34-46` creates a WebGL context and never releases it (`runtimeCapabilities.ts:157-160` does release its own).
- **GREEN-16.** `loadSeriesIndex.ts:103-131` fetches series metadata sequentially with `await` inside the loop. Use bounded parallelism (for example 4) to cut time to first image.
- **GREEN-17.** `dicomweb-sdk/src/index.ts:13` does not encode UIDs in the frame URL. Use `encodeURIComponent`, as the API client does.
- **GREEN-18.** `App.tsx` and `auth/src/index.ts:72` use the sentinel string `'Redirecting to login.'` for control flow. Use a typed error.
- **GREEN-19.** `seriesDetails.ts:141` shows `spacingBetweenSlices` falling back to the validated `sliceSpacing`, which the renderer does not use (see YELLOW-3).
- **GREEN-20.** `geometry.ts:237` `sortedSopInstanceUIDs` is computed and unused (see YELLOW-1).
- **GREEN-21.** Seven `vite.config.ts.timestamp-*.mjs` files are in `apps/viewer/`. Delete them.
- **GREEN-22.** Auto-cine starts without user action for the CT head rule (`Viewer.tsx:1252-1264`; `studyPresentation.ts:216`). It is pausable and disclosed, but worth a sign-off.
- **GREEN-23.** The CT chest rule opens studies in MPR by default (`studyPresentation.ts:192-202`). Disclosed with a notice and an undo. Confirm it is intended for every CT chest.
- **GREEN-24.** Compare and layout changes reload the whole page (`useViewerCommands.ts:283-319`). Works, but costs a full re-authorize and re-index.

---

## 7. Strengths

1. **Geometry gate exists and is wired in.** MPR, MIP, MinIP, and VR are blocked by the same `volumeEligibility` check at launch (`Viewer.tsx:1067-1070`) and at runtime (`useViewportGrid.ts:163-167`). Non-diagnostic series never reach a volume (`volumeEligibility.ts:5-7`).
2. **Explicit, visible limits.** Secondary Capture carries a banner and a reason (`Viewer.tsx:2365-2372`). Non-renderable objects get explanations (`dicomweb-sdk/src/index.ts:132-154`).
3. **VR preflight is thorough.** Cross-origin isolation, WebGL2, float colour buffer, float linear, 3D texture limits, and a real 3D R32F upload and sample smoke test (`runtimeCapabilities.ts:106-258`). The reason text is specific.
4. **Memory and resource guards for VR.** Estimate vs warn and block thresholds, a CPU-fallback block (`Viewer.tsx:375`), and a per-profile image cap (`viewerConfig.ts:52-86`).
5. **Careful teardown.** Volume cache entries are removed before engine destroy (`useViewportGrid.ts:1324-1346`), with the reason documented against stale WebGL handles. Session IDs guard stale async work (`viewportSessionRef`, `:527, 1230`).
6. **Deterministic volume IDs and a narrow retry.** Retry only on cache-collision messages (`volumeLoader.ts:106-121, 123-148`).
7. **Patient-context and access controls on the server path.** The BFF checks patient-context mismatch before issuing access (`bff-svc/src/app.ts` access route, `createStudyContextMismatchError`). Study access is gated before any archive read (`App.tsx:160`). The compare study is separately authorized (`App.tsx` compare block).
8. **Burned-in overlay follows clinical formatting rules.** W/L not range (`viewportOverlayModel.ts:69-74`); omit missing fields, never dashes (`:14-21`); DOB keeps the year (`:83-96`, with the reasoning written down).
9. **No unsafe DOM sinks anywhere** (SEC-6). DICOM text renders as React text.
10. **Reversible automation.** Hanging protocols are disclosed with a notice and can be reverted in one action (`hangingProtocol.ts`; `HangingProtocolNotice.tsx`).
11. **Honest labelling of session-only state.** Key images are labelled "Session key images" (`KeyImageTray.tsx:27-29`). The review manifest says what it contains.
12. **Error isolation.** Root error boundary, plus an inline boundary around the viewport so the toolbar survives a Cornerstone crash (`Viewer.tsx:2373-2376`, `main.tsx`).
13. **Good unit coverage of pure logic.** `viewer-phase4.test.ts` has 62 cases, and the viewer keeps decisions in framework-free modules that can be tested without jsdom.
14. **Bounded resources.** Worker pool capped at min(hardwareConcurrency, 4) (`initializeCornerstone.ts:67-70`). Prefetch window is small (`viewerConfig.ts:7-9`).
15. **Deep-link input is whitelisted** for enums and booleans (`deep-link/src/index.ts`).
16. **Dark pinned theme for diagnostic contrast** (`main.tsx` comment; `index.html` theme-color).

---

## 8. Open questions

1. **Does arc-light enforce bearer auth on `/dicom-web`** (frames, thumbnails, QIDO, WADO metadata)? This decides RED-6 and whether SEC-1 is a breach or a design choice. The answer is in the arc-light configuration, which is not in this repo.
2. **What are the production COOP and COEP headers** on the viewer-spa container? VR is unavailable without them (YELLOW-26).
3. **Which build environment produces the deployed viewer?** Is `.env` present in CI, and is there any `.env.production`? (YELLOW-25)
4. **Are multi-phase, multi-b-value, or multi-echo objects stored in one SeriesInstanceUID** in this archive's real data? That decides how urgent RED-1 is.
5. **Are there reversed-direction or InstanceNumber-disordered series** in the sample data? (YELLOW-1)
6. **Are acquisitions gantry-tilted or obliquely planned** for the head CT and spine MR protocols? (YELLOW-2)
7. **Which transfer syntaxes actually decode in the worker?** The viewer never checks `(0002,0010)`. Codecs are loaded on demand (`initializeCornerstone.ts:72`).
8. **Is MONOCHROME1 inverted correctly?** Cornerstone should handle it, but the viewer does not read PhotometricInterpretation, and I did not verify.
9. **Is the "measurement overlay misregistration" (RED-2) reproduced live?** My conclusion is arithmetic from code. A live check on a wide viewport is required before the fix is prioritised.
10. **Is "True size" intended as a camera reset?** If clinicians expect a physical-size view, it is missing.
11. **Should VR presets apply to MR at all?** Confirm with a radiologist (YELLOW-7).
12. **Is anatomical compare sync required?** Prior studies usually have a different FoR (YELLOW-12).
13. **Is session-only measurement acceptable for the product?** (YELLOW-17)
14. **Should Secondary Capture render at all?** The conformance statement says no. The code says yes (YELLOW-27).
15. **Does the BFF accept the viewer's OIDC token audience** (`pacs-viewer` client) given `KEYCLOAK_AUDIENCE` defaults to `pacs-bff` (`bff-svc/src/config.ts:126`)? Confirm the Keycloak audience mapper.
16. **Is the 5-minute `expires_at`** from the BFF meant to drive refresh? The viewer ignores it (YELLOW-22).
17. **What is the intent of the `[VR-DIAG]` instrumentation** (YELLOW-18)? Confirm it is temporary.
18. **Cornerstone's default 3 GB cache is in use.** Is that the intended ceiling for on-prem workstations, given the VR estimates (YELLOW-6)?

---

## 9. Comparison hooks for the other PACS

Specific checks to run on the other viewer, so the comparison is like-for-like:

- **Pixel-rectangle mapping.** This repo's only pixel-rectangle logic is the click-to-pixel mapping (`measurements.ts:164-197`) and CPU-side ROI mean (`:320-364`). There is **no GPU read-back** code here (grep for `readPixels`, `getImageData`, `drawImage`, `texImage` gives only capability probes and one `toDataURL`). If the other viewer copies a GPU-read-back sub-rectangle, the defect class is different and should be compared on its own terms. Here the analogous defect is RED-2 and RED-3: the overlay and the click mapping live in a different transform space from the image.
- **Rotate, flip, and transform parity.** Ask whether the other viewer keeps overlays, measurements, and the click layer in the same transform as the image. In this repo, no.
- **Orientation markers.** Ask whether the other viewer shows R/L, A/P, S/I. Here, no (RED-4).
- **Duplicate and multi-group position handling.** Ask whether the other viewer's geometry gate counts duplicate positions (RED-1 here).
- **Stack vs volume index.** Ask which order the other viewer's stack uses, and whether MPR index maps back through SOP UID (YELLOW-1 here).
- **VR memory estimate.** Ask whether the other viewer estimates bytes per voxel from the allocated type (YELLOW-6 here).
- **Uncalibrated measurement.** Ask whether the other viewer prints mm when Pixel Spacing is absent (RED-5 here).
- **Thumbnail and frame authorization.** Ask whether the other viewer's pixel path carries the same authorization as its metadata path (RED-6 here).
- **Token handling.** Ask where the access token lives, whether it is returned to the browser as a second bearer, and whether MRN appears in URLs (SEC-2, SEC-3, SEC-4 here).

---

## Appendix: key file references

- Viewer root and JSX: `apps/viewer/src/Viewer.tsx` (2477 lines)
- Engine session, VR, teardown: `apps/viewer/src/hooks/useViewportGrid.ts` (1352 lines)
- Geometry gate: `apps/viewer/src/dicom/geometry.ts:161-243`; `dicom/volumeEligibility.ts:4-19`
- Study index and frame URLs: `apps/viewer/src/dicom/loadSeriesIndex.ts`
- Measurements and overlay: `apps/viewer/src/dicom/measurements.ts`; `components/MeasurementOverlay.tsx`
- Volume build: `apps/viewer/src/cornerstone/volumeLoader.ts`
- VR preflight: `apps/viewer/src/cornerstone/runtimeCapabilities.ts`
- Auth: `frontends/packages/auth/src/index.ts`; bootstrap in `apps/viewer/src/App.tsx`
- BFF token and access route: `bff-svc/src/auth.ts`, `bff-svc/src/app.ts:300-340`
- Routing and headers: `infra/traefik/dynamic/routers.yml`
- Build env: `PACS_Sys/.env` (variable names only; values not reproduced), `apps/viewer/vite.config.ts`
- Conformance: `conformance/pacs-conformance-statement-v1.md:64-101`; `conformance/phase-1-ct-mr-allowlist.md`
- Shared packages: `frontends/packages/{api-client,auth,deep-link,dicomweb-sdk,ui}/src/`
- Tests: `PACS_Sys/tests/viewer-phase4.test.ts`, `viewer-overlay-and-previews.test.ts`, `viewer-hanging-protocols.test.ts`, `viewer-command-registry.test.ts`, `viewer-ui-scale.test.ts`, `dicomweb-sdk.test.ts`, `auth-defaults.test.ts`, `deep-link.test.ts`, `api-client.test.ts`, `ui-*.test.ts`
- Cornerstone (read-only): `PACS_Sys/node_modules/@cornerstonejs/core/dist/esm/utilities/{sortImageIdsAndGetSpacing,generateVolumePropsFromImageIds}.js`, `.../core/dist/esm/init.js`, `.../core/dist/esm/cache/cache.js`, `PACS_Sys/node_modules/@cornerstonejs/dicom-image-loader/dist/cornerstoneDICOMImageLoader.bundle.min.js.map` (sources `imageLoader/internal/options.ts`, `internal/xhrRequest.ts`, `wadors/getPixelData.ts`)
