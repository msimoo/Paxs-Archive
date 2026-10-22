I’ve written the comparison to docs/COMPARISON_PACS_Sys.md, with the four detailed PACS_Sys analyses in docs/comparison/pacs-sys/. It’s also linked from the docs index. Nothing in PACS_Sys was touched, and its uncommitted work is intact.

Bottom line: PACS_Sys has the sounder foundation, and PAXS has more features working end to end. Neither is ready for a hospital yet, but they fail in different places.

	PAXS Archive	PACS_Sys
Archive core	Custom Python; data-integrity problems; doesn’t build from git	Unmodified dcm4chee-arc-light 5.33 (mature), plus your own C# archive in progress
Identity	Home-grown login (JWT + LDAP)	Keycloak OIDC
Code quality	Uneven; tests can’t run as committed	Strict TypeScript, typed API client, builds and tests pass
Features	Very broad and actually wired: HL7 in and out, FHIR, IHE profiles, routing, lifecycle, replication, quotas, multi-tenancy, real admin pages, server-side MPR	Fewer, but more carefully built: concurrency-safe worklist and report actions, a conformance statement, SOP-class allow-list
Biggest risk	Its own archive core	An open archive edge (see below)

Problems both projects share:

Signing can commit stale text. I confirmed this in your clinical app: sign() reuses the last autosaved draft (ReportAuthorPage.tsx:502), and the server signs that stored version. Edits typed after the last autosave are silently dropped, the same class of bug as PAXS NEW-01.
3D/MPR correctness. I confirmed one in your viewer: multi-phase series pass the geometry check and get mixed into one volume (geometry.ts:217-221). PAXS has the GPU wrong-rows bug.
Unauthenticated HL7 listener. Yours is on by default and bound to all interfaces. Your own code-health audit flagged this in August, and it’s still open.
Mocked features shown as real. Your admin pages are mostly in-memory stand-ins. The “Signed … SR SOP” message appears even though the SR store step is mocked by default.

What needs fixing in PACS_Sys first:

Your archive edge is open. I confirmed you use the unsecured arc-light image. Traefik’s /dicom-web route has no auth, and port 8080 (the archive’s REST API and admin UI) is published to the host. Images and archive operations are reachable without a login. Use the -secure image or add a Traefik ForwardAuth through the BFF, and stop publishing 8080, 9990, 5432 and 389.
Commit archive-svc. About 40k lines of C#, of which about 14k lines and ~400 test methods exist only on your disk.
Basic viewer tools are missing. Mouse window/level, zoom and pan aren’t enabled. There are no orientation markers, and rotate/flip are CSS-only, so measurements use the wrong pixels afterwards.

Ideas worth borrowing from PAXS: its study-scoped share token is exactly the “scoped-credential exchange” your share links are waiting for. Its test that checks every route for an auth guard would have caught your three ungated BFF routes. A config check that refuses to start with insecure defaults, and server-side MPR for low-end workstations, could also help a field pilot.

Before you commit these files: you’ve already committed docs/ in 3f8b4f2. If you add these new files and push, the security findings about your own PACS_Sys project go to that repo’s GitHub remote. Keep them uncommitted or move them into PACS_Sys if that matters. Should I move them?





-------------------------------------------------------------------
from your analysis of both projects whats best to continue developing PACS_SYS or PACX-ARCHIVE?
-------------------------------------------------------------------


Keep developing PACS_Sys. Use PAXS Archive as a source of ideas, not as a codebase to continue or merge.

Why PACS_Sys
Factor	PACS_Sys	PAXS Archive
The hardest part, the archive	Already solved by dcm4chee-arc-light, which is mature and conforms to the standards	Custom-built, with data-integrity bugs: overwrites, missing savepoints, a migration that breaks MRN uniqueness
What’s left to fix	Mostly configuration and wiring: lock down the archive edge, the HL7 listener default, the sign bug, viewer tools. Weeks of work	Deep, spread across 278 routes and the ingest core. It doesn’t even build from git. Months of work
Closing the feature gap	Cheap: arc-light already has MPPS, storage commitment, routing, retention and HL7. You mostly need real admin adapters to its REST API instead of writing features	The features exist, but you inherit their bugs
Code quality	Strict TypeScript, typed contracts, tests that run, CI, your own audits and phased plans	Uneven. Arrived as large dumps, with AI-tool logs committed, so it’s hard to trust and maintain
Ownership	Yours: sole author, so you know every decision	Mostly another developer’s code (9 of 10 commits), on their GitHub remote
Clinical-safety posture	Conservative: SOP-class allow-list, geometry gating, unsupported objects listed rather than rendered	Broad but risky: demo AI on by default, MPR shows wrong pixels, fusion misaligned

For a hospital pilot, a proven core with weak edges is far easier to make safe than a broad feature set on a weak core.

When PAXS would be the better choice

Only if you needed an all-in-one, single-process, no-Docker install for a demo right now. You’d also have to accept rewriting its archive core, and have clear rights to the code. Neither applies to a clinical pilot.

Suggested plan for PACS_Sys
Week 1, stop-ship fixes:
Secure the archive edge: use the -secure arc-light image or add a Traefik ForwardAuth through the BFF, and stop publishing 8080, 9990, 5432 and 389.
Turn the HL7 listener off by default and restrict who can reach it.
Fix the stale-draft sign.
Commit archive-svc to a branch (about 14k lines exist only on your disk).
Add a typecheck step to CI.
Weeks 2–3, viewer basics:
Mouse window/level, zoom and pan tools.
Orientation markers.
Rotate and flip done in the camera rather than CSS.
A fixed geometry check for multi-phase series.
Real SR storage instead of the mock.
Weeks 3–5, unlock arc-light:
Replace the local_mvp admin mocks with arc-light REST and the Keycloak admin API, or hide them.
Configure MPPS, storage commitment, routing and retention in arc-light.
Complete the production overlay.
Set up off-site backups and run and record a restore drill.
Borrow from PAXS (ideas only):
The study-scoped share token, which would enable image sharing.
A test that checks every route has an auth guard.
A config check that fails closed in production.
Later, server-side MPR for low-end workstations.
On archive-svc

It’s your riskiest roadmap item. PAXS shows how much a single-process archive has to get right. Don’t block the pilot on it:

Run arc-light at the site.
If no-Docker is a hard requirement, evaluate Orthanc: a native single binary with a DICOMweb plugin.
Keep archive-svc on a parallel track, and only cut over once it passes your conformance suite, including DICOMweb.

If you want, I can start on the week-1 fixes in PACS_Sys, beginning with committing archive-svc and securing the DICOMweb edge.




-------------------------------
BUT I AM working on custom archive not depenedat on the arc-light
-------------------------------








**Keep developing PACS_Sys.** Use PAXS Archive as a source of ideas, not as a codebase to continue or merge.

## Why PACS_Sys

| Factor | PACS_Sys | PAXS Archive |
|---|---|---|
| **The hardest part, the archive** | Already solved by dcm4chee-arc-light, which is mature and conforms to the standards | Custom-built, with data-integrity bugs: overwrites, missing savepoints, a migration that breaks MRN uniqueness |
| **What's left to fix** | Mostly configuration and wiring: lock down the archive edge, the HL7 listener default, the sign bug, viewer tools. Weeks of work | Deep, spread across 278 routes and the ingest core. It doesn't even build from git. Months of work |
| **Closing the feature gap** | Cheap: arc-light already has MPPS, storage commitment, routing, retention and HL7. You mostly need real admin adapters to its REST API instead of writing features | The features exist, but you inherit their bugs |
| **Code quality** | Strict TypeScript, typed contracts, tests that run, CI, your own audits and phased plans | Uneven. Arrived as large dumps, with AI-tool logs committed, so it's hard to trust and maintain |
| **Ownership** | Yours: sole author, so you know every decision | Mostly another developer's code (9 of 10 commits), on their GitHub remote |
| **Clinical-safety posture** | Conservative: SOP-class allow-list, geometry gating, unsupported objects listed rather than rendered | Broad but risky: demo AI on by default, MPR shows wrong pixels, fusion misaligned |

For a hospital pilot, a proven core with weak edges is far easier to make safe than a broad feature set on a weak core.

## When PAXS would be the better choice

Only if you needed an all-in-one, single-process, no-Docker install for a demo *right now*. You'd also have to accept rewriting its archive core, and have clear rights to the code. Neither applies to a clinical pilot.

## Suggested plan for PACS_Sys

1. **Week 1, stop-ship fixes:**
   - Secure the archive edge: use the `-secure` arc-light image or add a Traefik ForwardAuth through the BFF, and stop publishing 8080, 9990, 5432 and 389.
   - Turn the HL7 listener off by default and restrict who can reach it.
   - Fix the stale-draft sign.
   - **Commit `archive-svc` to a branch** (about 14k lines exist only on your disk).
   - Add a typecheck step to CI.
2. **Weeks 2–3, viewer basics:**
   - Mouse window/level, zoom and pan tools.
   - Orientation markers.
   - Rotate and flip done in the camera rather than CSS.
   - A fixed geometry check for multi-phase series.
   - Real SR storage instead of the mock.
3. **Weeks 3–5, unlock arc-light:**
   - Replace the `local_mvp` admin mocks with arc-light REST and the Keycloak admin API, or hide them.
   - Configure MPPS, storage commitment, routing and retention in arc-light.
   - Complete the production overlay.
   - Set up off-site backups and run and record a restore drill.
4. **Borrow from PAXS (ideas only):**
   - The study-scoped share token, which would enable image sharing.
   - A test that checks every route has an auth guard.
   - A config check that fails closed in production.
   - Later, server-side MPR for low-end workstations.

## On `archive-svc`

It's your riskiest roadmap item. PAXS shows how much a single-process archive has to get right. Don't block the pilot on it:
- Run arc-light at the site.
- If no-Docker is a hard requirement, evaluate **Orthanc**: a native single binary with a DICOMweb plugin.
- Keep `archive-svc` on a parallel track, and only cut over once it passes your conformance suite, including DICOMweb.

If you want, I can start on the week-1 fixes in PACS_Sys, beginning with committing `archive-svc` and securing the DICOMweb edge.

















**Keep developing PACS_Sys.** Use PAXS Archive as a source of ideas, not as a codebase to continue or merge.

## Why PACS_Sys

| Factor | PACS_Sys | PAXS Archive |
|---|---|---|
| **The hardest part, the archive** | Already solved by dcm4chee-arc-light, which is mature and conforms to the standards | Custom-built, with data-integrity bugs: overwrites, missing savepoints, a migration that breaks MRN uniqueness |
| **What's left to fix** | Mostly configuration and wiring: lock down the archive edge, the HL7 listener default, the sign bug, viewer tools. Weeks of work | Deep, spread across 278 routes and the ingest core. It doesn't even build from git. Months of work |
| **Closing the feature gap** | Cheap: arc-light already has MPPS, storage commitment, routing, retention and HL7. You mostly need real admin adapters to its REST API instead of writing features | The features exist, but you inherit their bugs |
| **Code quality** | Strict TypeScript, typed contracts, tests that run, CI, your own audits and phased plans | Uneven. Arrived as large dumps, with AI-tool logs committed, so it's hard to trust and maintain |
| **Ownership** | Yours: sole author, so you know every decision | Mostly another developer's code (9 of 10 commits), on their GitHub remote |
| **Clinical-safety posture** | Conservative: SOP-class allow-list, geometry gating, unsupported objects listed rather than rendered | Broad but risky: demo AI on by default, MPR shows wrong pixels, fusion misaligned |

For a hospital pilot, a proven core with weak edges is far easier to make safe than a broad feature set on a weak core.

## When PAXS would be the better choice

Only if you needed an all-in-one, single-process, no-Docker install for a demo *right now*. You'd also have to accept rewriting its archive core, and have clear rights to the code. Neither applies to a clinical pilot.

## Suggested plan for PACS_Sys

1. **Week 1, stop-ship fixes:**
   - Secure the archive edge: use the `-secure` arc-light image or add a Traefik ForwardAuth through the BFF, and stop publishing 8080, 9990, 5432 and 389.
   - Turn the HL7 listener off by default and restrict who can reach it.
   - Fix the stale-draft sign.
   - **Commit `archive-svc` to a branch** (about 14k lines exist only on your disk).
   - Add a typecheck step to CI.
2. **Weeks 2–3, viewer basics:**
   - Mouse window/level, zoom and pan tools.
   - Orientation markers.
   - Rotate and flip done in the camera rather than CSS.
   - A fixed geometry check for multi-phase series.
   - Real SR storage instead of the mock.
3. **Weeks 3–5, unlock arc-light:**
   - Replace the `local_mvp` admin mocks with arc-light REST and the Keycloak admin API, or hide them.
   - Configure MPPS, storage commitment, routing and retention in arc-light.
   - Complete the production overlay.
   - Set up off-site backups and run and record a restore drill.
4. **Borrow from PAXS (ideas only):**
   - The study-scoped share token, which would enable image sharing.
   - A test that checks every route has an auth guard.
   - A config check that fails closed in production.
   - Later, server-side MPR for low-end workstations.

## On `archive-svc`

It's your riskiest roadmap item. PAXS shows how much a single-process archive has to get right. Don't block the pilot on it:
- Run arc-light at the site.
- If no-Docker is a hard requirement, evaluate **Orthanc**: a native single binary with a DICOMweb plugin.
- Keep `archive-svc` on a parallel track, and only cut over once it passes your conformance suite, including DICOMweb.

If you want, I can start on the week-1 fixes in PACS_Sys, beginning with committing `archive-svc` and securing the DICOMweb edge.