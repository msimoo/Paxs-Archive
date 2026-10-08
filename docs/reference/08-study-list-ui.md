# React + TypeScript study list (`study-list-ui/`)

> **Area deep-dive** — part of the [PAXS Archive documentation](../README.md). Produced by static analysis of commit `e90d32c` (2026-10-08); line numbers refer to that commit. Findings here are consolidated, de-duplicated and prioritized in [CODE_REVIEW.md](../CODE_REVIEW.md) — where the two differ, CODE_REVIEW.md takes precedence. Labels such as `[read]`, `[verified]`, `[V]`, `[H]` or `[inferred]` describe how each claim was checked (read in code vs. inferred).

> ⚠️ **Baseline: this deep-dive describes commit `e90d32c`.** What changed in `5a32aa4` (fixes, regressions, new code and endpoints) is in [5a32aa4/05-clinical-ldap-frontend.md](5a32aa4/05-clinical-ldap-frontend.md). The current status of every finding is in [CODE_REVIEW.md](../CODE_REVIEW.md).

## 0. Method, limits, legend

- Read every in-scope file with the Read tool (list in section 1). `package-lock.json` was not reviewed beyond reading the pinned versions.
- Not run: `npm install`, `tsc -b`, `vite build`, `oxlint`, and the app itself. `node_modules` is absent, and installing would write into the repo. Build, lint, and runtime behavior are therefore unverified.
- Library behavior was checked against the locked versions' published dist, downloaded separately for inspection: `@radix-ui/react-select@2.3.7` (`radix-select-index.mjs`) and `@radix-ui/react-dialog@1.1.23` (`radix-dialog-index.mjs`).
- Backend cross-checks: `app/api/v1/dashboard.py`, `share.py`, `nodes.py`, `auth.py`, `app/db/models.py`, `app/main.py`, `run_paxs.py`, `app/services/admin_panel_service.py`, `app/static/viewer/paxs-study-list-enhance.js`, `app/static/studies-ui/index.html`, `build_standalone.bat`, and the deployed bundle `app/static/studies-ui/assets/index-Bxwcl_Y6.js` (the one the deployed `index.html` references).
- Severity legend: **[BLOCKING]** = wrong or unsafe clinical/data behavior, or a crash; **[IMPORTANT]** = functional, accessibility, security, or build defect with a real user impact; **[NIT]** = cleanup or hardening.

### Executive summary

The list path is wired to real endpoints: `GET /api/v1/studies` (search, filters, sort, pagination, KPI counts) and `/api/v1/auth/me`. Several user-facing actions are simulated in the client and ship to users: **delete, forward-to-node, share link/QR, the report content and demographics in the detail drawer, and an entire mock viewer**. Each of these shows success or clinical-looking content with no backend call. The real endpoints for delete, forward, and share already exist in the backend, and the UI does not call them. A single null `patient_name` would blank the whole app.

---

## 1. Files covered

| Path (relative to study-list-ui/) | Lines | Purpose |
|---|---|---|
| package.json | 45 | Scripts (`dev`, `build`=`tsc -b && vite build`, `lint`=`oxlint`, `preview`); deps |
| vite.config.ts | 25 | base `/studies-ui/`, React + Tailwind plugins, `@` alias, dev port 5174, proxy `/api` and `/dicom-web` to 8042, `outDir: dist`, `emptyOutDir: false` |
| tsconfig.json | 7 | Solution file referencing app and node configs |
| tsconfig.app.json | 33 | App compile options (target es2023, bundler resolution, `verbatimModuleSyntax`, path alias; no `strict`) |
| tsconfig.node.json | 23 | Config for `vite.config.ts` only (`module: nodenext`, `types: node`) |
| .oxlintrc.json | 8 | Oxlint: plugins react/typescript/oxc; 2 rules |
| index.html | 14 | Shell; favicon `/static/logo/logo.ico`; loads `/src/main.tsx` |
| README.md | 32 | Unmodified Vite template boilerplate (not app documentation) |
| .gitignore | 24 | Ignores node_modules, dist, logs, editor files |
| src/main.tsx | 13 | createRoot, StrictMode, renders App, imports index.css |
| src/App.tsx | 18 | Top-level view switch (list vs viewer); TooltipProvider |
| src/index.css | 113 | Google Fonts import, Tailwind v4 `@theme` tokens, scrollbar, two keyframes |
| src/types/study.ts | 60 | Study, StudyResponse, StudyCounts, StudyFilters, ModalityCode |
| src/lib/api.ts | 42 | fetchStudies, deleteStudy (unused), getViewerUrl |
| src/lib/constants.ts | 28 | Modality colors and labels, STATUS_FILTERS and SPECIAL_FILTERS (unused), getModColor |
| src/lib/utils.ts | 6 | `cn()` (clsx + tailwind-merge) |
| src/hooks/use-studies.ts | 91 | Filter, fetch, debounce, abort, counts state for the list |
| src/components/study-list/StudyListPage.tsx | 286 | Page shell: header, KPI, filters, table, pagination, drawer and modals, toast, logout |
| src/components/study-list/StudyTable.tsx | 334 | Table and grid views, sort header, row actions, skeleton, empty state |
| src/components/study-list/SearchToolbar.tsx | 57 | Search input, modality select, from/to date inputs |
| src/components/study-list/StatusChips.tsx | 52 | All/Online/Archived and Critical/Reported/EMR/HIS filter chips |
| src/components/study-list/KpiHeader.tsx | 82 | Five KPI cards (two are static text) |
| src/components/study-list/Pagination.tsx | 51 | Previous/next and "Page X of Y" |
| src/components/study-list/StudyDetailDrawer.tsx | 181 | Study detail dialog (contains fabricated report and demographics) |
| src/components/study-list/ShareModal.tsx | 90 | Share dialog (fake link and fake QR) |
| src/components/study-list/ForwardModal.tsx | 98 | Forward dialog (simulated transfer) |
| src/components/study-list/DeleteModal.tsx | 61 | Delete dialog (simulated deletion) |
| src/components/common/PaxsFooter.tsx | 168 | Footer, custom Terms/Privacy modal |
| src/components/viewer/PacsViewerDashboard.tsx | 662 | Mock DICOM viewer with hard-coded patient and AI results; unreachable |
| src/components/ui/avatar.tsx | 48 | shadcn Avatar (no importers) |
| src/components/ui/badge.tsx | 38 | shadcn Badge (used) |
| src/components/ui/button.tsx | 55 | shadcn Button (used) |
| src/components/ui/card.tsx | 79 | shadcn Card (used by KpiHeader) |
| src/components/ui/dialog.tsx | 120 | shadcn Dialog on Radix (used) |
| src/components/ui/dropdown-menu.tsx | 198 | shadcn DropdownMenu (no importers) |
| src/components/ui/input.tsx | 24 | shadcn Input (used) |
| src/components/ui/scroll-area.tsx | 46 | shadcn ScrollArea (used by drawer) |
| src/components/ui/select.tsx | 158 | shadcn Select (used) |
| src/components/ui/separator.tsx | 29 | shadcn Separator (no importers) |
| src/components/ui/skeleton.tsx | 15 | Skeleton (used by table) |
| src/components/ui/tooltip.tsx | 30 | Tooltip (used) |
| src/assets/hero.png | binary, 12.8 KB | Unreferenced |
| src/assets/vite.svg | 1 (minified) | Unreferenced Vite template asset |
| package-lock.json | (106.5 KB) | Not reviewed; pins read for section 2 |

Outside the explicit list but consumed by the build: `public/favicon.svg` and `public/icons.svg` are copied into `dist` (the deployed folder contains both). `icons.svg` is not referenced anywhere.

---

## 2. Tech stack and build

### 2.1 Locked versions (from package-lock.json)

Runtime:
- react 19.2.8, react-dom 19.2.8
- tailwindcss 4.3.3, @tailwindcss/vite 4.3.3
- lucide-react 1.31.0, class-variance-authority 0.7.1, clsx 2.1.1, tailwind-merge 3.6.0
- date-fns 4.4.0 (**not imported anywhere**)
- Radix: react-select 2.3.7, react-dialog 1.1.23, react-tooltip 1.2.16, react-scroll-area 1.2.18, react-slot 1.3.3 (used). react-avatar 1.2.6, react-dropdown-menu 2.1.24, react-separator 1.1.15 (only imported by unused ui files). react-checkbox 1.3.11, react-label 2.1.15, react-popover 1.1.23, react-toggle 1.1.18, react-toggle-group 1.1.19 (**not imported anywhere**).

Dev:
- vite 8.2.1, @vitejs/plugin-react 6.0.5, typescript 6.0.3, oxlint 1.78.0
- @types/react 19.2.18, @types/react-dom 19.2.4, @types/node 24.13.3

### 2.2 Scripts and output

- `npm run build` = `tsc -b && vite build` (package.json:8). Output: `dist/` (vite.config.ts:22). `emptyOutDir: false` (vite.config.ts:23), so old hashed assets are kept.
- Deployment: `build_standalone.bat:44-46` runs `xcopy /E /I /Y "study-list-ui\dist\*" "app\static\studies-ui\"`. The backend mounts that directory at `/studies-ui` (`app/main.py:595`, `:667-675`, `SpaStaticFiles`). `app/main.py:298` redirects unauthenticated `/studies-ui` requests to `/login` (cookie session; `app/api/v1/auth.py:8`).
- Base path: `base: '/studies-ui/'` (vite.config.ts:7). Matches the mount.
- Dev: port 5174 (vite.config.ts:15, `run_all.bat:65`, README).
- Dev proxy: `'/api' -> http://localhost:8042` and `'/dicom-web' -> http://localhost:8042` (vite.config.ts:16-19). See finding I7.
- Assets in `app/static/studies-ui/assets/`: 22 entries from successive builds.

### 2.3 TypeScript and lint

- tsconfig.app.json: target es2023, `moduleResolution: bundler`, `verbatimModuleSyntax: true`, `erasableSyntaxOnly: true`, `noUnusedLocals: false`, `noUnusedParameters: false` (lines 27-28). **No `strict` key** (so strict mode is off: strictNullChecks and noImplicitAny default to false). Covered by finding I12.
- tsconfig.node.json has `noUnusedLocals: true` but also no `strict`.
- Oxlint (.oxlintrc.json:4-7): only `react/rules-of-hooks: error` and `react/only-export-components: warn`. Type-aware rules are not enabled (the README suggests enabling them).
- No test files, no test script.

---

## 3. Component tree

```
main.tsx: createRoot(#root) + StrictMode
 └ App (App.tsx:6)               state: view 'list' | 'viewer' (:7); wraps TooltipProvider
    ├ StudyListPage (StudyListPage.tsx:27)   [the only reachable page]
    │   ├ header buttons (Report, Nodes, Schedule, Upload) + user chip + Logout
    │   ├ KpiHeader({counts})
    │   ├ error banner
    │   ├ StatusChips({filters, counts, setStatusFilter, toggleSpecialFilter})
    │   ├ view toggle (table | grid)            local state viewMode
    │   ├ SearchToolbar({filters, setSearch, setModality, setDateRange})
    │   ├ StudyTable({studies, loading, filters, setSort, viewMode, onStudyClick, onActionClick})
    │   │     └ SortHeader (declared inside StudyTable, StudyTable.tsx:40)
    │   ├ Pagination({page, limit, total, setPage})
    │   ├ PaxsFooter()                           state: termsOpen, activeTab
    │   ├ StudyDetailDrawer({study, isOpen, onClose, onAction, onToast})  Radix Dialog
    │   ├ ShareModal({study, isOpen, onClose, onToast})                  state: copied, expiry
    │   ├ ForwardModal({study, isOpen, onClose, onToast})                state: node, syntax, loading
    │   └ DeleteModal({study, isOpen, onClose, onConfirm})               state: loading
    └ PacsViewerDashboard({onBack})   UNREACHABLE (see I8); ~50 useState
```

Hook: `useStudies(initialFilters?)` (use-studies.ts:22) is called only from StudyListPage (:28-42). It returns `studies, total, counts, loading, error, filters, setSearch, setModality, setSort, setPage, setStatusFilter, toggleSpecialFilter, setDateRange, refresh` (`refresh` is unused).

Per-component detail:

| Component | Props | Local state | Responsibility and notes |
|---|---|---|---|
| App | none | view (:7) | Switches to viewer, but `setView('viewer')` is never reached (`onOpenViewer` is never called) |
| StudyListPage | onOpenViewer (declared :24, never used) | viewMode, selectedStudy, drawerOpen, shareOpen, forwardOpen, deleteOpen, toastMessage, currentUser (:44-51) | Owns useStudies; fetches `/api/v1/auth/me` (:53-64); logout (:66-77); toast (:91-94); dispatches row actions (:79-89) |
| KpiHeader | counts | none | Card 1 "DIMSE :10400 Listening" and card 5 "Multi-Tenant Mode Active" are static text (I4). "Refresh" does not exist |
| StatusChips | filters, counts, setStatusFilter, toggleSpecialFilter | none | Chip values are passed through `as any` (StatusChips.tsx:29) |
| SearchToolbar | filters, setSearch, setModality, setDateRange | none | Controlled search input; modality select (values from MOD_LABELS); two native date inputs |
| StudyTable | studies, loading, filters, setSort, viewMode, onStudyClick, onActionClick | none (inner SortHeader) | Table (`div` grid) and card grid; skeleton while loading; empty state |
| Pagination | page, limit, total, setPage | none | Computes totalPages from total/limit |
| PaxsFooter | none | termsOpen, activeTab | Footer; a custom modal for Terms and Privacy (not Radix) |
| StudyDetailDrawer | study, isOpen, onClose, onAction, onToast | none | Detail dialog built from the list row only (no detail fetch); contains fabricated content (B4) |
| ShareModal | study, isOpen, onClose, onToast | copied, expiry | Builds a fake URL and fake QR (B3) |
| ForwardModal | study, isOpen, onClose, onToast | node, syntax, loading | Simulated 1.5 s transfer (B2) |
| DeleteModal | study, isOpen, onClose, onConfirm | loading | Simulated 1 s deletion (B1) |
| PacsViewerDashboard | onBack | about 50 useState values | Mock viewer. Unreachable (I8) |
| ui/* (shadcn) | per primitive | none | Dialog, Select, Tooltip, ScrollArea, Button, Badge, Card, Input, Skeleton used. Avatar, DropdownMenu, Separator unused |

---

## 4. Data layer

### 4.1 lib/api.ts (42 lines)

| Function (line) | Method and URL | Query params | Response type | Auth | Error handling |
|---|---|---|---|---|---|
| fetchStudies (:5-29) | GET `/api/v1/studies?...` (API_BASE :3 = `/api/v1`) | page, limit, sort, order (always); state; critical/reported/oru/his = `1` when true; search; modality; tenant (never set by UI); from/to with dashes removed (YYYYMMDD) | `StudyResponse` (cast from `res.json()`, :28, no runtime validation) | `Authorization: Bearer <localStorage paxs_token>` if present (:22-24); cookies by default | `!res.ok` throws `Failed to load studies (<status>)` (:27). Abort signal passed through |
| deleteStudy (:31-37) | DELETE `/api/v1/studies/{uid}` | none | void (204) | Bearer if present | `!res.ok` throws. **Never called** (B1) |
| getViewerUrl (:39-42) | none (URL builder) | `StudyInstanceUIDs` (encoded) | string | none | Base is `window.PAXS_PUBLIC_BASE_URL` or `location.origin` (:40); trailing slashes stripped |

Calls made outside api.ts:
- GET `/api/v1/auth/me` (StudyListPage.tsx:58): Bearer if present, `credentials: 'same-origin'`. Errors swallowed (`.catch(() => {})`, :63).
- POST `/api/v1/auth/logout` (StudyListPage.tsx:68): errors logged with console.error, then the page always redirects to `/login` (:75).

Endpoints present in the backend but not used by the UI: `POST /api/v1/share` (share.py:27), `GET /api/v1/nodes` (nodes.py:88), `POST /api/v1/nodes/{ae}/send` (nodes.py:184), `GET /api/v1/studies/{uid}` detail (dashboard.py:697), `DELETE /api/v1/studies/{uid}` (dashboard.py:834; the client function exists but is never called).

Image URLs: `study.thumbnail` is a relative path `/api/v1/studies/{uid}/series/{series}/thumbnail.png` (dashboard.py item builder). It is rendered by `<img>` (StudyTable.tsx:96, :219), which sends cookies but no Authorization header.

Backend query semantics checked against the UI:
- `sort`: UI sends `date` (default), `accession`, and `patient_name`. The backend accepts `patient`, `accession`, `series`, `instances`, `modality`, `created`, and falls back to date for anything else (dashboard.py: `if sort == "patient"` near line 535; `else` near line 566). **`patient_name` is not accepted** (I2).
- `limit` is clamped to 1..100 by the backend (dashboard.py:423). UI always sends 20.
- Date range: backend supports from-only, to-only, and both (dashboard.py ~471-479). The UI's partial ranges work.
- Counts keys (`total, online, archived, critical, reported, oru_sent, his`) match `StudyCounts` (types/study.ts:33-41).

### 4.2 Token and auth handling

- Session is a server cookie (auth.py:8 says the cookie gates `/viewer` and `/studies-ui`). Login (`app/static/login.html:241`, `app/static/app.js:52, 603`) also stores a Bearer token in `localStorage['paxs_token']`.
- The SPA attaches the Bearer token on `fetchStudies`, `deleteStudy`, `auth/me`, and `auth/logout`. It is removed only on logout (StudyListPage.tsx:73) and in the legacy app.
- No refresh logic in the SPA (the backend has `/api/v1/auth/refresh`, auth.py:277). No 401 handling (see I5).

### 4.3 hooks/use-studies.ts (91 lines)

- Defaults (lines 5-20): page 1, limit 20, sort `date`, order `desc`, all filters empty or false.
- State: filters, studies, total, counts (zeros), loading (initial **false**, :27), error, plus an `AbortController` ref (:30).
- Setters: `updateFilters` merges and resets page to 1 unless `page` is given (:32-34). setSearch, setModality, setSort, setStatusFilter, setDateRange go through it. setPage sets page. `toggleSpecialFilter` flips one boolean and resets page (:41-43).
- Fetch trigger: `useEffect` on `[filters, loadData]` sets a 300 ms debounce timer and clears it on the next change (:68-73). Page changes and sort changes are also debounced.
- Cancellation: `loadData` aborts the previous controller (:47-50) and starts a new one. The effect cleanup only clears the timer; no abort on unmount.
- Caching: none. Every filter change refetches. No keyed cache, no memoized results.
- Pagination: server-side (page/limit), `total` from the response. No infinite scroll.
- Counts: replaced on every successful response (:58), not otherwise.
- Refresh: no polling, no focus refetch, no manual refresh control. `refresh` (:89) is exported and unused. KPI numbers only change as a side effect of filter or page actions.
- Race analysis (asked for explicitly): see I6. In short, the abort prevents a stale response from overwriting newer data in the cases I could trace, but the `finally` block clears `loading` while a newer request is still in flight.

### 4.4 types/study.ts (60 lines) against the backend

`Study` (lines 1-21) vs. the backend item builder in `dashboard.py` (the `items.append({...})` block, inside `list_studies` at dashboard.py:415):

| Field | TypeScript | Backend | Mismatch |
|---|---|---|---|
| patient_name | `string` (:3) | `s.patient.patient_name`; column `Mapped[str or None]` (app/db/models.py:199) | Yes, can be null (B6) |
| description | `string` (:10) | `s.study_description`, nullable (models.py:336) | Yes |
| accession | `string` (:11) | `s.accession_number`, nullable (models.py:334) | Yes |
| study_date | `string` (:8) | `s.study_date`, nullable (models.py:332) | Yes |
| report_id / report_status / oru_status | optional, no null (:18-20) | `rep.id if rep else None` etc. (emits null when no report) | Yes (null) |
| tenant_name / tenant_id | optional (:6-7) | `tenant_names.get(...)`, `s.tenant_id` | Possibly null |
| modalities | `string[]` | `a.get("modalities", [])` | No |
| lifecycle_state | `'ONLINE' or 'ARCHIVED'` | `s.lifecycle_state` | Not verified |
| patient_source | `string` | `s.patient.source` (non-null column) | No |

Unused type members: `tenant_id`, `report_id`, `oru_status`, and `ModalityCode` (:60).
`StudyResponse` (:23-31) and `StudyFilters` (:43-58) match the query and response contracts.

---

## 5. User workflows (UI level)

1. **Search.** Type in the search box (SearchToolbar.tsx:18-23). `setSearch` runs `updateFilters` with page reset (use-studies.ts:36, :32-34). The effect debounces 300 ms (:68-73), aborts the prior request (:47-50), and calls `GET /api/v1/studies?...&search=...`. On success the table, pagination, and KPI counts update.
2. **Filter by modality or date.** Pick a modality (SearchToolbar.tsx:27-37) or a from/to date (:40-52). Same path as step 1. Dates become YYYYMMDD in the query (api.ts:19-20).
3. **Filter by status chips.** All, Online, Archived set `state` (StatusChips.tsx:27-32). Critical, Reported, EMR, HIS toggle booleans (`toggleSpecialFilter`) and reset page (use-studies.ts:41-43).
4. **Sort.** Click a header (StudyTable.tsx:172-174). The same key flips asc/desc; a new key starts at desc (:32-38). Patient sends `patient_name`, which the backend ignores (I2). Date, Accession, and the arrow state work.
5. **Switch views.** Table or grid icon (StudyListPage.tsx:210-221). Local state only, no refetch.
6. **Paginate.** Previous/next (Pagination.tsx:24-47). `setPage(page +/- 1)` (use-studies.ts:39) then the same debounced fetch. The page size is fixed at 20 (no selector). "Page X of Y - N studies" comes from `total`.
7. **Open the detail drawer.** Click a row or card (StudyTable.tsx:87, :209), or the Inspect or Info button (:154, :295). `handleStudyClick` sets selectedStudy and opens the drawer (StudyListPage.tsx:79-82). The drawer renders only list-row data plus fabricated report text and demographics (B4). Closing: X, Escape, or overlay (`onOpenChange`, StudyDetailDrawer.tsx:27).
8. **Open in viewer.** The grid card's Eye button (StudyTable.tsx:136) and the drawer's Open Viewer button (StudyDetailDrawer.tsx:55) call `window.open(getViewerUrl(uid), '_blank')`. The URL is `<origin>/viewer/viewer?StudyInstanceUIDs=<uid>`, which is the legacy viewer. The table view has no viewer button. The "VIEW" overlay on the table thumbnail (StudyTable.tsx:223-225) opens the drawer, not the viewer.
9. **Share.** Share button in the grid (:144), the list (:311), or the drawer (StudyDetailDrawer.tsx:61). Opens ShareModal (StudyListPage.tsx:84-88). The URL is `https://paxs.local/share/<uid>?exp=<expiry>` (ShareModal.tsx:21). Copy calls `navigator.clipboard.writeText` (:24) and shows a check for 2 s. **No network call** (B3).
10. **Forward to node.** Forward button in the list (StudyTable.tsx:303) or the drawer (StudyDetailDrawer.tsx:64). ForwardModal: pick a destination (hard-coded, ForwardModal.tsx:57-59) and a transfer syntax (never sent). "Send C-STORE" sets loading, waits 1.5 s, shows the success toast, closes (:24-28). **No network call** (B2).
11. **Delete.** Trash button in the list (StudyTable.tsx:319) or the drawer (StudyDetailDrawer.tsx:67). DeleteModal: Confirm waits 1 s, then `onConfirm(uid)` shows "deleted successfully" (StudyListPage.tsx:96-98), and the list is not refreshed. **No network call** (B1).
12. **KPI header refresh.** No control exists. Counts update only as a side effect of a successful list fetch (use-studies.ts:58). The "DICOM Server" and "VNA Tenant" cards never change (I4).
13. **Diagnostic report.** Header button (StudyListPage.tsx:123-128) opens the report for `studies[0]` (B5). Row icons (StudyTable.tsx:128, :287), drawer buttons (StudyDetailDrawer.tsx:58, :142, :150) call `window.paxsOpenReportModal?.(uid)`. The global is defined only by `app/static/viewer/paxs-study-list-enhance.js:656`, which the deployed `index.html` loads but the source `index.html` does not (I1). If the global is missing, the buttons do nothing silently.
14. **Logout.** Calls `POST /api/v1/auth/logout`, removes `paxs_token`, redirects to `/login` (StudyListPage.tsx:66-77). It redirects even on failure.

---

## 6. Code review findings

### 6.1 Findings

#### B1 [BLOCKING] Delete is simulated; the UI reports success for a destructive action
- Where: DeleteModal.tsx:19-27; StudyListPage.tsx:96-98; api.ts:31-37 (`deleteStudy`, never imported).
- Quote: `setTimeout(() => { setLoading(false); onConfirm(study.study_uid); onClose(); }, 1000);` and `showToast(\`Study ${uid} deleted successfully.\`)`.
- What is wrong: no request is sent. The study stays in the table (no refetch), but the toast says it was deleted. The real endpoint exists: `DELETE /api/v1/studies/{study_uid}` (dashboard.py:834) requires `require_destructive_role`, deletes files from storage, deletes DB rows, and writes audit entries. The client function that calls it is never used.
- Second defect: DeleteModal.tsx:30 `onOpenChange={(open) => !open && onClose()}` is not gated by `loading`. Pressing Escape during the 1 s wait closes the dialog, but the timer still runs and `onConfirm` still fires. The "deleted" toast then appears after the user cancelled.
- Failure scenario: a clinician confirms deletion and sees "deleted successfully". The study is still there, or the user assumes it was removed. Either way the UI misreports a destructive action.
- Fix: `await deleteStudy(uid)` inside try/catch. On success call `refresh()` and close. On failure keep the modal open with the error. Gate `onOpenChange` on `!loading`. Keep the timer out of the flow entirely. Consider a typed confirmation for hard deletes.

#### B2 [BLOCKING] Forward to node is simulated
- Where: ForwardModal.tsx:22-29 (timer and toast), :57-59 (hard-coded destinations with IP addresses), :66-75 (transfer syntax never sent), :80 (Cancel does not stop the timer).
- Quote: `onToast(\`Successfully forwarded study to ${node}\`)`.
- What is wrong: no request is sent. The backend has `POST /api/v1/nodes/{ae_title}/send` (nodes.py:184) with body `SendScope` (nodes.py:39-44: study, series, and instance UIDs plus `our_ae_title`; no transfer-syntax field), gated by `require_destructive_role` and audit-logged as FORWARDED. Destinations should come from `GET /api/v1/nodes` (nodes.py:88).
- Failure scenario: the user picks PACS_BACKUP and sees "Successfully forwarded". Nothing was sent. If the user clicks Cancel within the 1.5 s window, the success toast still appears.
- Fix: load nodes from the API, POST to `/send`, surface the real response, and remove the transfer-syntax select unless the backend contract adds it. Make Cancel abort the pending operation.

#### B3 [BLOCKING] Share link and QR code are fabricated
- Where: ShareModal.tsx:21 `const shareUrl = \`https://paxs.local/share/${study.study_uid}?exp=${expiry}\`;`; :47-52 "Fake QR Code" grid; :51 `${Math.random() > 0.5 ? 'opacity-100' : 'opacity-0'}`; :55 caption "Scan to open in zero-footprint viewer".
- What is wrong: the URL is a hard-coded host and nothing is created server side. The QR is random noise, regenerated on every render (for example when the expiry select changes). The backend `POST /api/v1/share` (share.py:27-50) creates a real token, returns `url`, `qr_base64`, and `expires_at`, and writes an AuditLog entry with action SHARED (share.py:37-41). The UI bypasses both. `expiry_days` is an integer from 1 to 365 (share.py:23), so the "1 Hour" option (ShareModal.tsx:65) has no backend equivalent.
- Failure scenario: a clinician copies the link and sends it to a referring physician. It does not resolve. The QR scans to nothing. No share audit record exists.
- Fix: POST `{study_uid, expiry_days}` to `/api/v1/share`, show the returned `url` and `qr_base64`, and drop or redefine the 1-hour option.

#### B4 [BLOCKING] Detail drawer shows invented clinical content and demographics
- Where: StudyDetailDrawer.tsx:40-42 `<span>DOB: 1980-01-01</span>` and `<span>M</span>` (shown for every patient); :130-141 fixed report text; :160-173 placeholder series with counts `Math.floor((i + 1) * 15)`.
- Quote: `<p className="text-sm text-slate-400 line-clamp-2">Patient presents with acute pain. Follow-up from previous examination.</p>` and `1. Unremarkable examination.`
- What is wrong: the report text (Clinical History, Findings, Impression) is shown for any study whose `report_status` is set, and it has no relation to the study. The DOB and sex are constants. The series list is generated placeholders, not real series. The list API returns neither DOB nor sex. Real data exists: `GET /api/v1/studies/{uid}` (dashboard.py:697) and the radiology report records.
- Confirmed in the deployed bundle: `DOB: 1980-01-01` and `Patient presents with acute pain` each occur in `app/static/studies-ui/assets/index-Bxwcl_Y6.js`. This content ships to users.
- Failure scenario: a finalized study's drawer shows "Impression: 1. Unremarkable examination." A reader may take it as the actual report. Every patient appears as a 1980-born male.
- Fix: remove all placeholders. Bind demographics and report to the detail and report endpoints. Show "not available" when data is absent.

#### B5 [BLOCKING] Header "Diagnostic Report" opens the report for the first row, not the selected study
- Where: StudyListPage.tsx:123-128.
- Quote: `const firstUid = studies[0]?.study_uid; if (firstUid) (window as any).paxsOpenReportModal?.(firstUid); else alert('No studies available to report.');`
- What is wrong: the button ignores selection and always uses the first row of the current page and sort order.
- Failure scenario: the user wants another patient's study, or does not notice which patient is first, and enters findings against the wrong study.
- Fix: remove the header shortcut, or require an explicit study choice shown with patient name, date, and accession. Never default to `studies[0]`.

#### B6 [BLOCKING, conditional on data] One study with a null patient name blanks the whole app
- Where: nullable column app/db/models.py:199 `patient_name: Mapped[str | None]`; list serializer emits `"patient_name": s.patient.patient_name` (dashboard.py item builder); typed non-null at types/study.ts:3; unguarded `.replace` at StudyTable.tsx:102 and :233, StudyDetailDrawer.tsx:35, ShareModal.tsx:43, ForwardModal.tsx:45, DeleteModal.tsx:44. (`getInitials`, StudyTable.tsx:21, guards falsy values; `.replace` does not.)
- What is wrong: no error boundary exists (grep for `ErrorBoundary` and `componentDidCatch` in src returns nothing; main.tsx:6-13 and App.tsx:6-18 have none). `strict` is off, so `tsc -b` does not flag `string | null` used as `string`.
- Failure scenario: a row whose patient name is NULL causes a TypeError during render. React unmounts the tree and the page is blank until that row leaves the current page. Whether ingest actually stores NULL (rather than an empty string) for an empty PatientName was not verified. The schema allows it.
- Fix: type `patient_name` as `string | null`, add a `displayName(p)` helper with `?? ''`, enable `strict`, and wrap the list and drawer in an error boundary.

#### I1 [IMPORTANT] Source index.html and deployed index.html differ; a rebuild silently disables the Report buttons
- Where: study-list-ui/index.html:11-12 loads only `/src/main.tsx`. Deployed `app/static/studies-ui/index.html:10` loads `/viewer/paxs-study-list-enhance.js?v=37`. That script defines `window.paxsOpenReportModal` (app/static/viewer/paxs-study-list-enhance.js:656).
- What is wrong: all report entry points call `(window as any).paxsOpenReportModal?.(...)` (StudyTable.tsx:128, :287; StudyDetailDrawer.tsx:58, :142, :150; StudyListPage.tsx:126). With the optional chain, a missing global produces no error and no feedback. `build_standalone.bat:44-46` copies `dist` with xcopy and does not inject the script. A fresh build from source therefore drops the script tag and silently breaks report entry. The enhance script also expects the React header's `data-paxs-react-user-chip` attribute (enhance.js:881; StudyListPage.tsx:122), so the two are coupled in both directions.
- Failure scenario: after the next `build_standalone.bat` run, every Diagnostic Report button does nothing.
- Fix: put the script tag in the source index.html (or a Vite `transformIndexHtml` plugin), or port `openReportModal` into React. Add a build check that asserts the global is present in the output.

#### I2 [IMPORTANT] Patient sort sends a key the backend does not recognize and silently sorts by date
- Where: StudyTable.tsx:172 `<SortHeader label="Patient" sortKey="patient_name" />`. Backend: `if sort == "patient"` (dashboard.py near line 535); anything else falls to the default date branch (dashboard.py near line 566).
- What is wrong: the Patient header still shows the active arrow (StudyTable.tsx:41, :51 compare `filters.sort === sortKey`), while the results are ordered by date.
- Fix: `sortKey="patient"`. Keep the sort keys in one shared constant on both sides.

#### I3 [IMPORTANT] Copy buttons throw on plain-HTTP origins
- Where: StudyDetailDrawer.tsx:21-24 `navigator.clipboard.writeText(study.study_uid); onToast(...)`; ShareModal.tsx:23-28 (same, plus `setCopied`).
- What is wrong: the Clipboard API is available only in secure contexts. PAXS runs plain HTTP unless HTTPS is configured with certificates (run_paxs.py:30-40). On `http://<lan-host>:8000/studies-ui/`, `navigator.clipboard` is undefined, so `.writeText` throws a TypeError inside the click handler. Neither the toast nor the "copied" state runs. The promise is also not awaited or caught.
- Fix: feature-detect; fall back to a hidden textarea and `document.execCommand('copy')`, or show the value for manual copy. `await` and catch.

#### I4 [IMPORTANT] KPI header shows hard-coded status text, not server state
- Where: KpiHeader.tsx:23 `DIMSE :10400 Listening` (static text, no fetch); KpiHeader.tsx:76 `Multi-Tenant Mode Active` (static); KpiHeader.tsx:61-64 "Critical Unread" badge shown whenever `counts.critical > 0`.
- What is wrong: the first and last cards make claims about server state that the UI never checks. For "Critical Unread": the critical count and filter use every `TriageFlag` row with no review-state condition (dashboard.py, counts near lines 493-497 and filter near lines 515-518), although a review endpoint exists (dashboard.py:684). Whether "unread" is accurate depends on whether reviewed flags remain as rows (not verified).
- Fix: fetch health and config (dashboard.py:143 `/health`, :223 `/stats`), or remove the claims. Rename the badge to match what the count measures.

#### I5 [IMPORTANT] Bearer token in localStorage; 401 and auth failures are not handled
- Where: api.ts:22-24 and :32-34; StudyListPage.tsx:53-56; set by app/static/login.html:241 and app/static/app.js:52, :603.
- What is wrong: any script on the origin can read `paxs_token`. The token is sent on every request in addition to the cookie, so the app relies on both mechanisms. Thumbnails (`<img>`, StudyTable.tsx:96, :219) carry only the cookie. A 401 shows "Failed to load studies (401)" (api.ts:27) and gives no route back to `/login`. The `auth/me` failure is swallowed (StudyListPage.tsx:63), so the header silently shows no user.
- Fix: rely on the HttpOnly cookie only for the SPA. On 401, redirect to `/login?next=<current path>`. Surface `auth/me` failures.

#### I6 [IMPORTANT] Race: aborted requests clear the loading flag while a newer request is in flight
- Where: use-studies.ts:46-66. Abort at :47-50, `setLoading(true)` at :52, `finally { setLoading(false) }` at :63-65.
- What is wrong: when a request is aborted, its `finally` runs after the newer request has already set loading to true, so `loading` is cleared while the newer request is still pending. The skeleton disappears and old rows look current.
- Stale overwrite: not reproduced from the code. The abort prevents the older response from calling `setStudies`. The guarantee depends only on abort timing, with no sequence-number guard.
- Fix: keep a `requestIdRef`. In `finally`, clear loading only if the id is the latest. Ignore results whose id is not the latest.

#### I7 [IMPORTANT] Dev proxy targets port 8042; PAXS listens on 8000
- Where: vite.config.ts:16-19 `'/api': 'http://localhost:8042'`, `'/dicom-web': 'http://localhost:8042'`.
- What is wrong: `run_paxs.py:38` uses `PAXS_PORT` defaulting to 8000, and the README uses :8000 throughout (README.md:512-519, :748). 8042 is Orthanc's default (app/services/admin_panel_service.py:7, :52). On a default install, `npm run dev` sends every `/api` call to a non-PAXS service, and the list shows an error. The `/dicom-web` proxy is not used by the app.
- Related: `getViewerUrl` uses `location.origin` (api.ts:40). In dev that is port 5174, and `/viewer` is not proxied, so "Open Viewer" returns 404 unless `PAXS_PUBLIC_BASE_URL` is set.
- Fix: default the proxy target to `process.env.PAXS_PORT` or 8000. Remove the unused `/dicom-web` entry. Document the dev setup.

#### I8 [IMPORTANT] Full mock viewer with fake patient data ships in the bundle and cannot be reached
- Where: PacsViewerDashboard.tsx:207-213 `ELEANOR SMITH`, `54Y / FEMALE`, `MRN: 904-8120`; :247 `Dr. Harrison`; :544 `PATIENT: ELEANOR SMITH`; :564 `AI NODULE DETECTED: RUL SUBPLEURAL 6.2mm (94.2% CONF)`; :512-515 `Math.random()` for displayed HU values on mouse move.
- Unreachable: `onOpenViewer` is declared (StudyListPage.tsx:24) and destructured (:27) but never called. App.tsx:7 `setView` is only called from the `onOpenViewer` prop (App.tsx:14), which never fires. The component is still imported (App.tsx:3) and bundled; the deployed bundle contains `ELEANOR SMITH` twice.
- Dead state: 41 useState identifiers appear exactly once in the file, and 14 lucide-react icon imports are unused (for example Layers, Scissors, Spline, Flame, Compass, Download, RefreshCw; PacsViewerDashboard.tsx:2-44).
- Fix: delete `PacsViewerDashboard.tsx`, the `view` state in App.tsx, and the `onOpenViewer` prop. That removes 662 lines of mock code.

#### I9 [IMPORTANT] Study table clips below about 760 px
- Where: StudyTable.tsx:170, :184, :208 `grid-cols-[64px_2fr_3fr_150px_120px_100px_180px]` with `gap-4` and `px-6`; container at :168 has `overflow-hidden` and no horizontal scroll.
- What is wrong: the fixed tracks sum to 614 px, gaps to 96 px, and padding to 48 px, about 758 px before the fr columns. Below that width the rightmost Actions column is clipped and cannot be reached.
- Fix: wrap in `overflow-x-auto` with a minimum width on the grid, or switch to the stacked card layout below `md` (the grid view already does this).

#### I10 [IMPORTANT] Keyboard and screen-reader access gaps
- (a) Rows and cards are not focusable. StudyTable.tsx:84-88 and :206-210 are `div`s with `onClick` and no `tabIndex`, `role`, or key handler. Keyboard users can reach only the icon buttons.
- (b) Icon-only buttons have no accessible name: StudyTable.tsx:136-138, :144-146, :295-297, :303-305, :311-313, :319-321; Pagination.tsx:24-35 and :36-47; view toggles StudyListPage.tsx:210-221. Radix Tooltip content does not label the trigger.
- (c) Hover-only controls: StudyTable.tsx:153 (Inspect), StudyDetailDrawer.tsx:108 (copy UID), and :169 (series eye) use `opacity-0 group-hover:opacity-100` with no `focus-visible` reveal. Focus is invisible.
- (d) `SortHeader` is declared inside `StudyTable` (StudyTable.tsx:40-54), so it is a new component type on every render. After a sort click the button is unmounted and keyboard focus is lost.
- (e) Status chips (StatusChips.tsx:25-40) have no `aria-pressed`. The search and date inputs have no label (SearchToolbar.tsx:18-23, :40-52).
- (f) The toast (StudyListPage.tsx:105-109) and the error banner (:192-196) have no `role="status"` or `role="alert"`.
- Fix: use table semantics, or make the first cell a button. Add aria-labels, `aria-pressed`, `focus-visible:opacity-100`, hoist `SortHeader` out of the render function, add live regions.

#### I11 [IMPORTANT] Terms and Privacy dialog is hand-rolled without dialog semantics
- Where: PaxsFooter.tsx:74-76 overlay and container are plain `div`s. No `role="dialog"`, `aria-modal`, or `aria-labelledby`; no Escape handling; no focus trap or initial focus. The close button (:89-94) has no accessible name.
- Fix: use the existing Radix Dialog (components/ui/dialog.tsx), as the other modals do.

#### I12 [IMPORTANT] Type and compiler settings let runtime nulls through
- Where: tsconfig.app.json (no `strict`; :27-28 turn off unused checks, so unused code passes `tsc -b`). types/study.ts vs backend (section 4.4). api.ts:26-28 returns `res.json()` as `StudyResponse` with no validation. Eleven `as any` casts (section 6.2).
- What is wrong: several fields the backend emits as null are typed without null, and strict checks are off, so the compiler cannot see it. A non-JSON 200 response (for example a misrouted proxy, see I7) throws a SyntaxError that is shown as the error text. A shape change surfaces later as a crash in `.replace` or `.length`.
- Fix: enable `strict` and `noUnusedLocals`, make types nullable where the API is nullable, and validate responses at the fetch boundary (zod or valibot).

#### I13 [IMPORTANT] Google Fonts loaded on every page view
- Where: index.css:1 `@import url('https://fonts.googleapis.com/css2?family=Inter...&family=JetBrains+Mono...')`.
- What is wrong: each page view sends the client IP and Referer to Google. In a PHI-handling app this is avoidable third-party disclosure. The request also fails on networks without internet access, which is common in hospitals, and the UI falls back to system fonts. Backend CSP was not checked (see section 7).
- Fix: self-host the fonts (for example `@fontsource`), or use the system font stack.

### 6.2 NITs

- **N1 Dead code.** `deleteStudy` (api.ts:31-37, see B1); `refresh` (use-studies.ts:89); `STATUS_FILTERS` and `SPECIAL_FILTERS` (constants.ts:13-24); `ModalityCode` (types/study.ts:60); the unused type fields `tenant_id`, `report_id`, `oru_status`; `.glass-panel`, `.animate-shimmer`, `.animate-slide-up` (index.css:62-68, :105-113); `ui/avatar.tsx`, `ui/dropdown-menu.tsx`, `ui/separator.tsx` (no importers); `Eye` import in StudyListPage.tsx:14 (unused); `onOpenViewer` prop (StudyListPage.tsx:24, :27); the whole viewer (I8).
- **N2 Unused dependencies** in package.json: date-fns (:29); @radix-ui/react-checkbox (:14), react-label (:17), react-popover (:18), react-toggle (:23), react-toggle-group (:24) are never imported; react-avatar (:13), react-dropdown-menu (:16), react-separator (:21) are only imported by unused ui files.
- **N3 Unreferenced assets.** `src/assets/hero.png`, `src/assets/vite.svg`, and `public/icons.svg` (copied into dist, not referenced). The app uses `/static/logo/logo.ico` (index.html:7).
- **N4 Animation classes with no plugin.** `animate-in`, `fade-in`, `slide-in-from-bottom-5`, `zoom-in-95`, `data-[state=open]:animate-in`, and similar are used at StudyListPage.tsx:106, PaxsFooter.tsx:75, ui/dialog.tsx:22 and :39, ui/select.tsx:76, ui/tooltip.tsx:21. No `tailwindcss-animate` or `tw-animate-css` in package.json and no `@plugin` in index.css, so these are no-ops. Dialogs open and close without transitions.
- **N5 Build output accumulates.** `emptyOutDir: false` (vite.config.ts:23) and the xcopy without a clean (build_standalone.bat:46). `app/static/studies-ui/assets/` holds 22 entries from earlier builds.
- **N6 `as any` casts** (11): use-studies.ts:59, api.ts:40, PacsViewerDashboard.tsx:286 and :417, StatusChips.tsx:29, StudyTable.tsx:128 and :287, StudyListPage.tsx:126, StudyDetailDrawer.tsx:58, :142, :150. The `window` globals should be declared with `declare global` instead.
- **N7 Toast and timer overlap.** StudyListPage.tsx:91-94 does not clear the previous 3 s timer, so the first timer hides a second toast early. ShareModal.tsx:27, ForwardModal.tsx:24, DeleteModal.tsx:22 use `setTimeout` with no cleanup.
- **N8 Stacked modals.** StudyDetailDrawer.tsx:61-69 calls `onAction`, and StudyListPage.tsx:84-89 opens Share, Forward, or Delete without closing the drawer. Two Radix dialogs end up open together.
- **N9 `window.open` without `noopener`.** StudyTable.tsx:136, StudyDetailDrawer.tsx:55 and :169. The viewer is same-origin, so the risk is low.
- **N10 Labels and language.** ShareModal.tsx:42, :59, :73 `<label>` elements are not associated with their controls. Arabic strings (StudyTable.tsx:128, :132, :287, :291) appear on a page declared `lang="en"` (index.html:2).
- **N11 Redundant provider.** `TooltipProvider` is created per row (StudyTable.tsx:125, :284); App.tsx:10 already provides one.
- **N12 Duplicated logic.** The table and grid branches of StudyTable repeat action and modality logic (StudyTable.tsx:56-165 and :167-333). The auth fetch in StudyListPage.tsx:53-64 duplicates the header logic in api.ts and has no AbortController.
- **N13 Debounce and unmount.** use-studies.ts:68-73 debounces every change, including page and sort, and the cleanup does not abort an in-flight request on unmount.
- **N14 Empty-state flash on load.** use-studies.ts:27 starts `loading` as false and the first fetch waits 300 ms (:69-71), so StudyTable.tsx:194-199 shows "No studies found" before the first request. Start with `loading: true`.
- **N15 Hash navigation leaves the SPA.** StudyListPage.tsx:135, :143, :151 set `window.location.href = '/#nodes' | '/#workflow' | '/#studies'`. The `/#workflow` and `/#studies` anchors were not verified in `app/static/index.html`. `/login` at :75.
- **N16 Pagination after a shrink.** Pagination.tsx:12-14 shows "Page 3 of 2" if total drops while on a later page. Only reachable once delete works.
- **N17 Missing root.** main.tsx:7 renders only `if (rootElement)`, so a missing `#root` renders nothing and gives no error.
- **N18 README.** README.md is Vite boilerplate. It does not describe the app, the build and deploy into `app/static/studies-ui`, the simulated features, or the API contract.
- **N19 Footer copy.** PaxsFooter.tsx:132 "SHA-256 digital signatures", :138 "AES-TLS encryption in transit", :141 role list. These are policy claims shown to users and not verified against the backend. "AES-TLS" is not a standard term (TLS uses AES as a cipher). :41 links to a personal domain.

### 6.3 Checked, not an issue

- No `dangerouslySetInnerHTML`, `innerHTML`, `eval`, or `new Function` anywhere in src.
- Hooks: all hooks run before early returns (ShareModal.tsx:15-19, ForwardModal.tsx:15-20, DeleteModal.tsx:14-17). No conditional hooks.
- Effect dependencies are correct: use-studies.ts `[filters, loadData]` and `loadData` on `[filters]`. No stale closure found. StudyListPage's `[]` effect reads no closure values.
- Radix Select 2.3.7 accepts an empty-string `SelectItem` value (the downloaded dist has `hasEmptyValueOption` at about line 1095 and no throw). SearchToolbar.tsx:32 (`value=""`) is **not** a crash. This was a suspicion I checked and dropped.
- Radix Dialog 1.1.23 emits no console warning for a missing `DialogDescription` (it only sets `aria-describedby`). Not reported.
- Query parameters match the backend: state, critical/reported/oru/his as `1`, from/to as YYYYMMDD, search, modality, page, limit, sort, order. Count keys match.
- The auth endpoints exist: `/api/v1/auth/me` (auth.py:352), `/api/v1/auth/logout` (auth.py:334). Thumbnail route exists (dashboard.py:773).
- Radix dialogs supply focus trapping and Escape handling. All Radix dialogs have a `DialogTitle`.
- Default sort `date desc` matches the backend default.
- Whether `require_destructive_role` restricts delete and forward to admins was not verified.

### 6.4 Specific answers to the review focus areas

- **Hooks correctness:** clean, see 6.3.
- **Stale closures and effects without cleanup:** no stale closures found. Timers without cleanup: N7. The debounce effect does clear its timer (use-studies.ts:72).
- **Race where an older search response overwrites a newer one:** not reproduced (I6). The residual problem is the loading flag.
- **dangerouslySetInnerHTML:** none.
- **Token handling:** I5.
- **Accessibility of modals and tables:** I10 and I11.
- **Error and loading states:** I6, N14, I5, and the silent failures in B1-B3.
- **Type safety (`any`):** 11 casts (N6), plus I12.
- **Unnecessary re-renders:** every keystroke updates `filters` in the hook, which re-renders StudyListPage and all children. There are no memoized children, and SortHeader remounts each render (I10d). The cost is small at 20 rows.
- **Hardcoded URLs:** StudyListPage.tsx:58, :68, :75, :135, :143, :151; api.ts:3; ShareModal.tsx:21; ForwardModal.tsx:57-59; PacsViewerDashboard.tsx:185, :194; index.css:1; vite.config.ts:17-18; PaxsFooter.tsx:41.

---

## 7. Open questions

1. **Intent of the simulated features.** B1-B4 and the mock viewer are in the deployed bundle. Are they intentional demo scaffolding or unfinished work? If they are meant to ship, the UI needs a clear distinction, or the feature needs to be removed.
2. **Source versus deployed index.html.** Is the `paxs-study-list-enhance.js` include meant to be part of the build? Who edits `app/static/studies-ui/index.html` by hand (I1)?
3. **Guest access to the list API.** `GET /api/v1/studies` uses `get_optional_user` (dashboard.py:415-417), and the code comment says guests see the "legacy unscoped data". Is it intended that unauthenticated API callers can list PHI? This is backend scope but affects how far the UI's login gate actually protects data.
4. **Anchors.** Do `/#nodes`, `/#workflow`, and `/#studies` exist in the landing page (`app/static/index.html`)? Not verified.
5. **Null patient names.** Does ingest store NULL or an empty string for an empty PatientName? A query against the patients table would settle whether B6 happens in practice.
6. **Drawer scrolling.** The drawer's `ScrollArea` (StudyDetailDrawer.tsx:73, root `max-h-[60vh]`) inside a `max-h-[90vh]` flex dialog may not scroll, because the Radix viewport has no definite height. Needs a browser check. Not verified.
7. **Stacked modals.** Whether two open dialogs behave acceptably with Radix focus management (N8). Needs a browser check.
8. **Critical count semantics.** Do reviewed `TriageFlag` rows stay in the table? If so, "Critical Unread" overstates (I4).
9. **Dev port.** Is the intended dev backend port 8000 or 8042 (I7)? The env var `PAXS_PORT` may be set on some machines.
10. **Share expiry.** Is a 1-hour share needed? The backend supports whole days only (share.py:23).
11. **CSP.** Does the backend CSP allow `fonts.googleapis.com` (I13)? `browser_console_network_audit.md` mentions a CSP, but it was not checked here.
12. **Footer claims.** The Terms text (PaxsFooter.tsx:132, :138, :141) makes specific security and compliance claims. Verify against the backend before shipping.
13. **Build status.** Does `tsc -b && vite build` and `oxlint` currently pass? Not run (see section 0).
