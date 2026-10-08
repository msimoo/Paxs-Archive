# PAXS Archive — Technical Documentation

Documentation of the whole PAXS Archive codebase: structure, runtime architecture, every workflow, the complete
HTTP API and a full code review. It describes commit **`5a32aa4`** (main) and was written from a static analysis on
2026-10-08. It started from a baseline review of `e90d32c` and was then re-reviewed against `5a32aa4`.

## Start here

| Document | Read it when you want to… |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | understand what the system is, how the repo is organized, the process/thread model, the middleware chain, the security model, data model, configuration, frontends, deployment (including reverse proxy) and tests |
| [APP_WORKFLOW.md](APP_WORKFLOW.md) | follow a real flow end to end: startup, sign-in and LDAP, staged C-STORE ingest, worker, Q/R, viewing with MPR client/server/hybrid modes, STOW, order→MWL→MPPS→report→addenda→EMR, routing, migration/prefetch, lifecycle/quotas, share links, export, admin, licensing, hooks, reverse proxy. Includes 18 sequence, state and flow diagrams |
| [API_REFERENCE.md](API_REFERENCE.md) | look up any of the 278 routes: method, path, auth level, handler and summary, plus a computed **Changes since e90d32c** section. Generated from the router source |
| [CODE_REVIEW.md](CODE_REVIEW.md) | see the status of every earlier finding (fixed / partial / open), the 6 new blockers and 26 new important findings, the phased remediation plan, and the tests to add |

## Area deep-dives (`reference/`)

Baseline (`e90d32c`) module-by-module references are below. Each one has a banner linking to what changed in
`5a32aa4`.

| # | Area | Scope | Changes in `5a32aa4` |
|---|---|---|---|
| 01 | [Core, config, DB](reference/01-core-config-db.md) | `main.py`, `core/**`, `db/**`, rate limit, deps, migrations, requirements: **settings table**, **data model**, **migration chain** | [5a32aa4/01](reference/5a32aa4/01-core-config-auth.md), [5a32aa4/04](reference/5a32aa4/04-ingest-storage-schema.md) |
| 02 | [DICOM network & ingest](reference/02-dicom-network-ingest.md) | `app/dicom/*`, storage/indexing/query/commitment/worker/compression/warm/triage | [5a32aa4/04](reference/5a32aa4/04-ingest-storage-schema.md) |
| 03 | [DICOMweb & imaging](reference/03-dicomweb-imaging.md) | QIDO/WADO/STOW, volume/MPR/MIP, fusion, GSPS, KIN, DICOMDIR, export, share: **Redis key catalog** | [5a32aa4/02](reference/5a32aa4/02-dicomweb-volume.md) |
| 04 | [Auth, admin & ops](reference/04-auth-admin-ops.md) | auth, users, dashboard, settings, DB/Redis/admin panels, config, license, metrics, LDAP | [5a32aa4/01](reference/5a32aa4/01-core-config-auth.md), [5a32aa4/05](reference/5a32aa4/05-clinical-ldap-frontend.md) |
| 05 | [Nodes, routing & storage](reference/05-nodes-routing-storage.md) | nodes, routing, replication, storage, quota, lifecycle, lossy, migration, prefetch, tenants, split/merge, scripts | [5a32aa4/04](reference/5a32aa4/04-ingest-storage-schema.md) |
| 06 | [Clinical, IHE & AI](reference/06-clinical-ihe-ai.md) | reports, worklists, MPPS, critical results, peer review, dose, IAN, FHIR, PIX/PDQ, XDS-I, AI | [5a32aa4/05](reference/5a32aa4/05-clinical-ldap-frontend.md) |
| 07 | [Server-served web UI](reference/07-web-ui-static.md) | dashboard `app.js`, login, settings page, fallback viewer, PaxsViewer add-ons: **every fetch call** | [5a32aa4/03](reference/5a32aa4/03-viewer-js.md), [5a32aa4/05](reference/5a32aa4/05-clinical-ldap-frontend.md) |
| 08 | [React study list](reference/08-study-list-ui.md) | `study-list-ui/`: stack, build, components, data layer | [5a32aa4/05](reference/5a32aa4/05-clinical-ldap-frontend.md) |
| 09 | [Deploy, scripts & licensing](reference/09-deploy-scripts-licensing.md) | Docker, HAProxy, monitoring, Windows scripts, vendor portal: **ops runbook**, **license API contract** | [5a32aa4/07](reference/5a32aa4/07-deploy-licensing-docs.md) (new env vars, reverse proxy, doc claim checks) |
| 10 | [Tests](reference/10-tests.md) | fixtures, inventory, coverage map, quality | [5a32aa4/06](reference/5a32aa4/06-tests-hygiene.md) (new suites, auth matrix, repo hygiene) |

[history/](history/) archives the `e90d32c` code review and API reference.

## Quick facts

- **One process:** FastAPI (HTTP + DICOMweb + volume/MPR) + a pynetdicom SCP thread (`:10400`) + an optional HL7
  listener (`:2575`) + a background worker thread. MySQL 8 (or SQLite) + local/S3 object storage + Redis.
- **Entry point:** `python run_paxs.py` → `app.main:app` on `0.0.0.0:8000`. UIs: `/login`, `/` (dashboard), `/viewer`
  (study list → PaxsViewer + 3D MPR), `/settings`, `/admin`. API docs at `/api/docs`.
- **Before you run it:**
  - The committed tree is still missing `app/services/storage/` (gitignored), and `redis` is not in
    `requirements.txt` ([CODE_REVIEW.md §1.1](CODE_REVIEW.md#11-build-and-deploy)).
  - Set `ENVIRONMENT=production` and a random `SECRET_KEY` of at least 32 characters. Without
    `ENVIRONMENT=production` the security gate is skipped.
  - Read [CODE_REVIEW.md §2](CODE_REVIEW.md#2--new-blocking-findings) before clinical use.

## How this documentation was produced

- **Baseline:** ten parallel area analyses of `e90d32c` read every in-scope source file (minified bundles were
  inventoried, not read).
- **Re-review:** seven parallel analyses of the `e90d32c` → `5a32aa4` diff.
- Route and auth tables are generated mechanically from the router ASTs.
- The lead reviewer re-verified the blocking findings in the source; these are marked ✅ in the review.
- Nothing was executed against a running server. The review environment has no Python dependencies installed, and
  the tree does not import as committed.
- Line numbers refer to the commit named in each document. Regenerate [API_REFERENCE.md](API_REFERENCE.md) after
  route changes.

The repository's own design notes (`README.md`, `PROJECT_STRUCTURE.md`, `RUN_AND_BUILD_GUIDE.md`,
`audit_and_roadmap/`, `PAXS_*.md`, `feature-plans/`) describe intent and plans. Where they disagree with the code,
these documents describe the code. Known drift is listed in CODE_REVIEW NEW-32.
