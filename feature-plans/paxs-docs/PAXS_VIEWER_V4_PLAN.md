# PAXS Viewer v4 — Core Upgrade, New Features & Creative UI/UX Redesign Plan

**Status:** Plan (awaiting approval)
**Scope:** the `/viewer` OHIF fork, `/studies-ui`, and the main dashboard shell
**Goal:** take the viewer from a heavily CSS-patched OHIF fork to a first-class, token-driven, creatively distinctive reading-room workstation — and add the features that make it a real product (measurements→report, comparison, AI triage).

---

## 1. Current State (honest audit)

| Surface | What it is | Health |
|---|---|---|
| `/viewer` | OHIF **v3.13.0-beta.89** (PaxsViewer fork) + cornerstone3D + vtk.js + three.js MPR | Works, but old beta; UI is a patchwork |
| `app-config.js` | DICOMweb datasource (paxs, wadors, lazy load, workers 4–16) | Clean |
| `paxs-viewer-theme.css` | ~500 lines of `!important` overrides; color-coded tool categories (nav=sky, measure=emerald, WL=amber, annot=violet, destructive=rose) | **Fragile** — a "layer cake" of hacks; the creative DNA is here but unstructured |
| `paxs-mpr.js` (1,241 lines) | Floating three.js "PAXS 3D DICOM Workspace": 4-grid, WL presets, cine, camera presets | Works, but **not a first-class OHIF viewport** — it's an overlay that can't share tools/annotations with cornerstone viewports |
| `paxs-study-list-enhance.js` | 8 jobs in one file: thumbnails, modality avatars, logo swap, footer inject, report modal, toolbar colorize, button hiding, study-list logo | Works via MutationObserver + polling — **fragile, untestable** |
| `/studies-ui` | Separate React + Tailwind + shadcn study list bundle | Modern, but disconnected from the viewer |
| `customizations/` | OHIF URL customization catalog (ctPresets, smoothRotate, measurementLabels, worklist, segmentation, veterinary) | Good pattern — underused |
| Backend | Reports engine (`/api/v1/reports/*`), templates, codecs, AI triage, admin | Ready; the viewer barely touches it |

**Structural problems to fix first:**
1. **The theme is a pile of `!important`** — every new feature needs more overrides; category colors are scattered, not data.
2. **MPR is an overlay, not a viewport** — no measurement sharing, no hanging-protocol integration, duplicated state.
3. **The enhancement script is a monolith** doing 8 unrelated jobs with polling.
4. **The OHIF fork is an old beta** — feature-gated upgrades upstream are impossible without isolating our customization layer.
5. **The reporting engine exists but the viewer only has a textarea modal** — no measurements→report flow, no template preview, no print styling parity.

---

## 2. Creative Direction — "The Adaptive Reading Room"

Grounded in the subject (radiology), not a generic dashboard:

**Signature — chromatic tool-state ambience.** The reading room "lights up" in the active tool's category color. Navigating = cool blue ambience, measuring = green, windowing = amber, annotating = violet. A thin glow ring around the active viewport + a soft tint on the workspace edge makes the current tool state readable at a glance, even peripherally, the way a real workstation's physical state is. This is the one memorable element — everything else stays quiet.

**Supporting ideas (kept restrained):**
- **Viewports as lightboxes**: each viewport gets real depth — an inner glass shadow, a 1px luminous edge, and a per-plane ambient glow (axial=cyan, sagittal=teal, coronal=amber) matching the existing MPR color coding. Imaging stays the hero; chrome recedes.
- **Toolbar as a control deck**: the capsule becomes a recessed deck; the active tool is a "lit key" with a category glow; secondary tools hide in a More menu to declutter (progressive disclosure).
- **Study vitals bar**: the bottom bar shows live study "vitals" — series/instance counts, compressed size, load time, modality — in mono type like a patient monitor, not decorative text.
- **Filmstrip as film**: thumbnails keep the modality-tinted treatment, gain an animated "loading study" shimmer and a subtle playback-preview on hover for cine series.
- **Theme engine**: the existing 7 presets (orchid, arctic, verdant, midnight, slate, deep) become real token sets — one source of truth, no `!important` hunts.

**What we deliberately won't do:** no glassmorphism-for-its-own-sake, no animated background noise behind images, no skeuomorphic plastic. Diagnostic clarity first.

---

## 3. Core Architecture Upgrades

### 3.1 Token-driven theme engine (kills the `!important` pile)
- Create `viewer/paxs-theme-tokens.js` — a single JSON of design tokens: `colors` (category set: nav/measure/wl/annot/destructive), `surfaces` (bg/panel/card/border per elevation), `typography`, `radii`, `motion`, plus the per-preset overrides.
- Refactor `paxs-viewer-theme.css` into: (1) a small token→CSS-var base, (2) targeted component rules WITHOUT `!important` where possible (scoped selectors + CSS layers), (3) a generated `paxs-theme-<preset>.css` per preset.
- Register via OHIF `customizationService` (the pattern `customizations/` already uses) so presets are data, and `?theme=` just picks a token set.
- **Verify:** grep the theme file — `!important` count drops by >80%; all 7 presets render; existing screenshot suite (if any) unchanged in behavior.

### 3.2 MPR → first-class OHIF viewport
- Refactor `paxs-mpr.js` into a proper OHIF extension module: register a `volume` viewport type so MPR planes appear in the same layout grid as cornerstone viewports, share the same hanging protocol, and expose measurement data.
- Keep the three.js spatial volume as one viewport of that type (not a floating overlay).
- Bridge: cornerstone annotations on an MPR plane map back to the volume (DICOM coordinate space), so a length drawn in the 3D view is a real measurement.
- **Verify:** open a CT study → 4-grid layout renders MPR planes + 3D in the same grid; drawing a measurement in MPR shows in the measurement panel.

### 3.3 Enhancement script → focused modules
Split `paxs-study-list-enhance.js` (8 jobs) into:
- `paxs-study-thumbs.js` (thumbnails/modality avatars)
- `paxs-branding.js` (logo, footer, terms/report buttons)
- `paxs-toolbar.js` (category coloring, deck, More menu)
- `paxs-report-modal.js` (report editor → uses backend)
- `paxs-common.js` (shared helpers, the mutation-observer utility)
Loaded once from `viewer/index.html`; each self-contained and idempotent.
- **Verify:** study list + viewer still enhance after split; each module guards `window.__paxs*Loaded`.

### 3.4 OHIF fork pinning (decision, not build)
Two tracks:
- **A (recommended now):** keep the pinned 3.13 fork, isolate all PAXS changes behind the customization layer so a future fork bump is a config drop-in.
- **B (optional, risky):** evaluate tracking upstream OHIF (v4) — larger effort, breaks MPR/theme work; only if feature gaps (e.g., new cornerstone features) justify it.
Record the decision in `PAXSVIEWER-INTEGRATION.md`.

---

## 4. New Features (prioritized)

### P0 — Core product features
1. **Measurement panel → report flow.** A right rail listing all measurements (length, angle, ROI, ellipse) per study with values/units; **"Send to report"** auto-fills them into the findings text (the backend `/api/v1/reports/*` + templates already exist). One-click opens the report editor with measurements pre-filled.
2. **Comparison mode.** Prior/current study side-by-side in a 2×1 hanging protocol; loads prior studies via QIDO (`ModalitiesInStudy` + date range), with linked scrolling + synced WL.
3. **Tool-state chromatic ambience** (the signature from §2) — implemented as a token-driven "active category" class on the workspace root.

### P1 — Product differentiation
4. **AI triage badges** on the study list (the backend already runs AI emergency triage) — e.g., "⚠ suspected fracture" with confidence, filtered views.
5. **Cine upgrade** — frame-rate control, keyboard scrubbing (`←/→` + `J/K/L`), loop modes, and the filmstrip hover-preview.
6. **Hanging protocols** — user-saved layouts (per modality), e.g. "CT trauma 2×2", applied automatically.
7. **Command palette (Ctrl+K)** — search tools, presets, studies; keyboard-first.
8. **Report-first workflow** — from the study list: "Prepare report" opens the editor directly (study already selected), template picker with live preview, print button to the existing `/api/v1/reports/print/{uid}`.

### P2 — Stretch
9. **DICOM SR export** — save measurements/annotations as a Structured Report stored to the archive.
10. **Light theme** for the viewer (token engine makes this cheap).
11. **PWA offline queue** — mark studies for offline; `sw.js` already exists, extend for queued WADO-RS.
12. **Teach-mode pointer** — a shared cursor overlay for training/screenshare (needs a tiny backend channel or localStorage-only demo).
13. **Annotation exports** (JSON for re-import; CSV of measurements).

---

## 5. UI/UX Redesign Phases (each independently verifiable)

### Phase A — Token system foundation
- [ ] Build `paxs-theme-tokens.js` (all colors currently hardcoded in `paxs-viewer-theme.css` mapped to tokens).
- [ ] Convert base styles to CSS variables + CSS layers; remove `!important` where safe.
- [ ] Verify: theme file `!important` count drops >80%; all pages render; dark theme unchanged.

### Phase B — Signature: chromatic ambience + lightbox viewports
- [ ] Active-category class on workspace root → ambient glow ring + edge tint (nav/measure/wl/annot/destructive).
- [ ] Viewport depth: inner glass shadow + luminous edge + per-plane ambient glow (axial/sagittal/coronal colors).
- [ ] Verify: switching tools visibly re-tints the room; reduced-motion mode disables the ambient animation; screenshot pass.

### Phase C — Control deck toolbar + study vitals
- [ ] Recessed capsule deck, lit active key, More menu (progressive disclosure).
- [ ] Vitals bar: series/instances, compressed size, load time, modality in mono type.
- [ ] Verify: toolbar decluttered, keyboard-only usable, vitals update live.

### Phase D — Study list & branding
- [ ] Thumbnails with shimmer loading + cine hover-preview; modality avatars; AI badges (P1.4).
- [ ] Split enhancement script into the 5 modules (§3.3); logo/footer/report wiring preserved.
- [ ] Verify: study list enhancements work after split; no duplicate footers/logos.

### Phase E — MPR viewport + measurement/report integration
- [ ] MPR planes + 3D as OHIF viewports in one grid (§3.2); measurement bridge.
- [ ] Measurement panel with "Send to report" → report editor pre-filled (P0.1).
- [ ] Report-first entry from study list (P1.8); template picker with preview.
- [ ] Verify: full round-trip — open study → measure → send to report → save draft → print.

### Phase F — Comparison, cine, protocols, palette
- [ ] Comparison mode (P0.2), cine upgrade (P1.5), hanging protocols (P1.6), Ctrl+K palette (P1.7).
- [ ] Verify: each feature via the QA checklist; no regressions in earlier phases.

### Phase G — Theme presets engine + light theme + polish
- [ ] 7 presets as token sets; `?theme=` picker; light theme (P2.10).
- [ ] Micro-interactions: filmstrip shimmer, button "lit key" press states, vitals count-up.
- [ ] A11y: focus rings, contrast pass on all categories, `prefers-reduced-motion`.
- [ ] Verify: preset matrix renders (7×2 themes); Lighthouse-ish pass; console clean.

---

## 6. Affected Files (planned)

| File | Change | Blocks |
|---|---|---|
| `viewer/paxs-theme-tokens.js` | **create** — design tokens + presets | B–G |
| `viewer/paxs-viewer-theme.css` | **rewrite** — token-driven, CSS layers, minimal `!important` | B–G |
| `viewer/paxs-theme-<preset>.css` | **create** — 7 preset token sets | G |
| `viewer/paxs-mpr.js` → `viewer/mpr/` | **refactor** — OHIF volume viewport extension | E |
| `viewer/paxs-study-list-enhance.js` | **split** into 5 modules | D |
| `viewer/index.html` | **modify** — load modules + theme engine | D–G |
| `viewer/app-config.js` | **modify** — register customization profiles, MPR extension | E |
| `viewer/customizations/` | **extend** — measurement panel, AI badges, hanging protocols, palette | D–F |
| `/studies-ui` | **modify** — link "Prepare report", AI badges, thumbnails parity | D |
| `app/static/index.html` / `app.js` | **modify** — "Prepare report" entry, viewer deep-link parity | D |
| `PAXSVIEWER-INTEGRATION.md` | **modify** — fork decision + customization-layer docs | 3.4 |
| Backend (`app/api/v1/reports.py`, `dashboard.py`) | **modify (small)** — measurements→report payload, AI badge endpoint if missing | P0/P1 |

---

## 7. Execution Plan (suggested order)

1. **Phase A** (tokens) — foundation, no behavior change.
2. **Phase B** (signature ambience + lightbox viewports) — the visual identity.
3. **Phase D** (study list split + badges) — unblocks the monolith and gives visible wins.
4. **Phase E** (MPR viewport + measurements→report) — the highest-value product feature.
5. **Phase C** (deck + vitals), then **F** (comparison/cine/protocols/palette), then **G** (presets/light/polish).
6. Backend work rides alongside E (report payload) and D (AI badge).

Each phase ends with a screenshot + QA checklist pass and is independently shippable.

## 8. Verification & QA checklist

```bash
# dev server (existing run method)
# manual matrix per phase:
#   dark + light · 7 presets · reduced-motion · keyboard-only
#   open CT + MR study · window presets · measure → panel → report → print
#   comparison mode · cine scrub · Ctrl+K palette · AI badge on list
# console: zero errors; network: no 4xx/5xx on viewer assets
```

## 9. Rollback Plan

- Every phase is additive/isolated: the enhancement modules and theme tokens load from `index.html` — reverting is removing the script/link tags.
- The OHIF fork and `/studies-ui` bundles are untouched artifacts; all PAXS work lives in separate files → a `git checkout` of `viewer/paxs-*.js|css` + `index.html` restores the pre-v4 state.
- The MPR refactor keeps the old `paxs-mpr.js` path until the new viewport extension is proven (dual-path, then delete).

## 10. Risks

| Risk | Mitigation |
|---|---|
| CSS-layer refactor breaks layout | Phase A is behavior-neutral; screenshot pass before/after |
| MPR refactor destabilizes the working 3D view | Dual-path: old overlay stays until new viewport passes |
| Old OHIF beta blocks a feature | Decision in §3.4; customization-layer isolation keeps options open |
| `!important`-removal regressions | Scoped selectors + CSS layers; visual diff per phase |
| Backend coupling (report payload) | Additive fields; existing endpoints stay backward-compatible |
| Scope creep in the creative pass | Signature (ambience) is the only "wow"; everything else is restraint |

---

*Next step: awaiting your go. Suggested start: **Phase A (token system)** — it's the foundation every other phase sits on.*
