# PACS_Sys — Archive layer, archive-svc (.NET), infrastructure and plans

> **Area analysis of PACS_Sys** for the [PAXS Archive vs PACS_Sys comparison](../../COMPARISON_PACS_Sys.md). Static, read-only analysis of the `PACS_Sys` repository at HEAD `2ebdbf4` **plus its uncommitted working tree** (2026-10-10). Nothing was built, run or modified. Secret values are never reproduced. Severity labels (🔴/🟡/🟢, RED/YELLOW, CRITICAL/MAJOR) are the analyst's; the comparison document carries the consolidated view.

**Repo:** `C:\Users\dody1\source\repos\PACS_Sys` (branch `main`, HEAD `2ebdbf4`, uncommitted work present)
**Method:** read-only. Read/Grep/Glob plus read-only `git`. No dotnet, docker, npm, build, test or server was run. Secret values are not reproduced; secrets are cited by kind and location only. Paths are relative to the repo root unless marked absolute.

---

## 0. Corrections to the brief, and coverage

- **Size.** The brief estimated about 7k lines for archive-svc. Measured: **38,210 lines** of non-generated C# (src 21,137 across 8 projects; tests 17,073 across 2 projects). Also 1,859 lines of generated EF migrations, a 5,309-line DICOM dictionary TSV and two scripts.
- **"Empty scaffolds" is true at HEAD only.** `git ls-files archive-svc` shows that Codecs, Dimse, Hl7, Persistence, Storage and Workers each contain only a `.csproj` in HEAD. The working tree adds about 14k lines of implementation to Dimse, Storage, Persistence, Codecs, Workers and Api/Hosting, plus most of the tests, all **uncommitted**. Hl7 is still empty: 0 `.cs` files, one csproj that references `nhapi` (`src/PacsSys.Archive.Hl7/PacsSys.Archive.Hl7.csproj:14`).
- **Coverage.** Read directly: `src/PacsSys.Archive.Api/Program.cs` (all), `Api/Hosting/ArchiveComposition.cs` (all), `Api/Hosting/DicomListener.cs` (all), `Workers/OrphanSweepWorker.cs` (all), `Codecs/CodecRegistry.cs` (all), `Dimse/Scp/CStoreScp.cs` (30–120), `Dimse/Scp/DicomServer.cs` (96–185), `Dicom/Part10/DatasetReader.cs` (1–140), `Dicom/Iso2022Decoder.cs` (1–70), `Storage/InstanceStore.cs` (1–130), `Persistence/PostgresStorageLedger.cs` (1–60), the archive plan (full), `archive-svc/docs/hosting.md` (full). The rest was checked by class inventory, grep and test-method inventory. No claim about the PDU codec, query matching, EF catalogue, JSON writer or most test bodies rests on line-by-line reading.
- **Uncommitted state.** `git diff --stat`: 13 tracked archive-svc files modified (442 insertions, 65 deletions), plus root `.gitignore` and `AGENTS.md`. Untracked: the Dimse, Storage, Persistence, Codecs, Workers, Api/Hosting and most test trees; `archive-svc/docs/`; `archive-svc/scripts/`; `plan/feature-archive-engine-rebuild.md`; `.obsidian/`; `.codex/hooks.json`.

---

## 1. Archive layer: what provides DICOM services today

Two stacks exist. **Deployed:** phase-0 compose (dcm4chee-arc-light), plus the prod overlay's HAProxy/Traefik layer. **Not deployed or wired:** archive-svc. The BFF and viewer talk only to arc-light.

| Service | Provider | Exposure (host port) | Auth / TLS | Evidence |
|---|---|---|---|---|
| DIMSE C-ECHO/C-STORE/C-FIND/C-MOVE/C-GET | arc-light `dcm4che/dcm4chee-arc-psql:${ARC_TAG}`, pinned **5.33.0** in `.env.example:7` | **11112 published plain** (`phase-0/docker-compose.yml:73`). Prod adds DIMSE-TLS on 2762 via HAProxy (`infra/docker-compose.prod.yml:227`) | No TLS on 11112. AE-title acceptance lives in arc LDAP config, which is **not in the repo**. HAProxy `verify optional`, no CA (`infra/haproxy/haproxy.cfg:37`) | `phase-0/docker-compose.yml:62-73` |
| DICOMweb QIDO/WADO/STOW | Same arc-light, served under `/dcm4chee-arc/aets/DCM4CHEE/rs` (`phase-0/README.md:141`) | **8080 plain HTTP published** (`phase-0/docker-compose.yml:71`). Through Traefik at `/dicom-web` (`infra/traefik/dynamic/routers.yml:16-22`) | **None.** Image is `-psql`, not `-secure` (`phase-0/docker-compose.yml:62`; `phase-0/README.md:186` says the flip is future work). Traefik router has no auth middleware. `frontends/packages/dicomweb-sdk` sends no Authorization header. BFF returns the user's bearer token for DICOMweb use (`bff-svc/src/app.ts:336-337`); the viewer sends it (`frontends/apps/viewer/src/dicom/loadSeriesIndex.ts:82`) and the archive ignores it | as listed |
| HL7 v2 ADT/ORM/ORU | arc-light listener, 2575 (`phase-0/docker-compose.yml:75`). Separate **BFF ORU MLLP listener**, enabled by default | **2575 published on host** (arc). BFF listener binds `0.0.0.0:2575` by default | No TLS, no allow-list in repo. BFF listener has no peer allow-list, timeout or size cap (`bff-svc/src/clinical/hl7OruMllpListener.ts:20-24`) | `bff-svc/src/config.ts:163-165` |
| Modality Worklist | Claimed for arc-light only (`conformance/pacs-conformance-statement-v1.md:37, 86`). Not configured or tested in repo | none | none | archive-svc has MWL written but unregistered (`Api/Hosting/ArchiveComposition.cs:33-38`) |
| Arc config (LDAP) | `dcm4che/slapd-dcm4chee` | **389 published** (`phase-0/docker-compose.yml:23`) | Plain LDAP. Admin password is a literal in the healthcheck (`:32`) | `:14-36` |
| Index DB | `dcm4che/postgres-dcm4chee:${DB_TAG}` | **5432 published** (`:47`) | Password from env. Weak default in `phase-0/.env.example:12` (value not reproduced) | `:38-59` |
| WildFly admin | arc | **9990 published** (`:77`) | None | `:77` |
| Pixel storage | arc local volume `arc_storage:/storage` (`:96`). MinIO S3 descriptor exists only as an LDIF that no script imports | none | MinIO root creds are used as S3 keys (`infra/ldap/storage-descriptors.ldif:20-21`) | see §3 |
| CORS | arc CLI permits `http://localhost:5173` with credentials (`phase-0/arc-init/dcm4chee-arc-cors.cli:4-5`) | n/a | dev only | `phase-0/docker-compose.yml:88` |

**Arc image and version.** `dcm4chee-arc-psql:5.33.0`, `dcm4che/postgres-dcm4chee:16.5-33`, `dcm4che/slapd-dcm4chee:2.6.7-33.0` (`phase-0/.env.example:5-7`, all pinned via env). The conformance statement cites **5.31.2** (`conformance/pacs-conformance-statement-v1.md:11, 15`). The archive plan calls 5.33.0 "the" archive (`plan/feature-archive-engine-rebuild.md:15`).

**Phase 1 (Keycloak).** `phase-1/docker-compose.yml` runs only `keycloak` in `start-dev` mode (`:6`), published on 8081 (`:13`), with the realm imported from `phase-1/keycloak/realm-pacs.json:15`. It does **not** include the archive. Keycloak therefore protects the BFF and the SPAs only. The archive's DICOMweb is not gated by it.

---

## 2. archive-svc status

| Project | Implemented? | Lines (non-generated .cs) | What it does | Tests (method attributes, see §5) |
|---|---|---|---|---|
| `PacsSys.Archive.Api` | **Yes**, host only | 883 (4 files) | Hosts the DIMSE listener and the sweep in one process. Maps only `GET /health` (`Program.cs:91`) and `GET /` (`:117`). DICOMweb is deferred to Phase 03 (`Program.cs:7-8`). Refuses to start on a volatile index (`:79-89`) and on an unmigrated DB | ArchiveHostTests (15) in UnitTests/Hosting |
| `PacsSys.Archive.Dicom` | **Yes** | 4,656 (20 files) | VRs (27), tags, dictionary (5,309 entries embedded as a TSV resource, `Dictionary/DataDictionary.cs:50, 161`), Part 10 reader and writer, DICOM JSON reader and writer, ISO 2022 decoder (`Iso2022Decoder.cs`), SopClass catalogue (45 classes), implicit and explicit VR reads | DataDictionary 13, DicomUid 8, Iso2022Decoder 18, DicomJsonWriter 8, DicomJsonReader 22, ArabicRoundTrip 6, RoundTrip 11, DatasetReader 10 + 6 synthetic, FileMetaReader 4, ImplicitVrEquivalence 6 |
| `PacsSys.Archive.Dimse` | **Yes** | 8,265 (29 files, all uncommitted except csproj) | PS3.8 Upper Layer (PDU codec, association, PDV framing). DIMSE command sets. SCP: C-ECHO, C-STORE, C-FIND, C-MOVE, C-GET, Storage Commitment, MPPS. MWL and IAN written but **not registered** (`ArchiveComposition.cs:102-116`). SCU: Echo, Store, Find, Move, Get, IAN. Accept loop on a plain `TcpListener` (`Scp/DicomServer.cs:108`). No `SslStream` anywhere in src | CFind 14, CGet 16, CMove 19, CStore 16, StorageCommitment 15, Mpps 19, ModalityWorklist 8, Ian 5, PduCodec 17, DimseCommandSet 18, Echo 11, RetrieveScu 11, StoreThenFind 5 |
| `PacsSys.Archive.Domain` | **Yes** | 1,246 (5 files) | Entities, PN matching, wildcards, date+time ranges, name folding | DateTimeRange 18, PersonName 18, Wildcard 10, NameFolding 9 |
| `PacsSys.Archive.Storage` | **Yes** | 3,513 (22 files, uncommitted) | `FileSystemBlobStore` (SHA-256 content-addressed). `InstanceStore` ordering (reserve, blob, metadata, resolve). In-memory ledger, catalogue and **MPPS store**. Query and retrieve sources. Worklist model and matcher. Has `AWSSDK.S3` referenced, but no S3 code | InstanceStore 15, FileSystemBlobStore 12, QuerySource 27, RetrieveSource 13, WorklistMatching 16 |
| `PacsSys.Archive.Persistence` | **Yes** (Postgres index) | 1,875 (15 files, uncommitted) + 1,859 generated migration lines | EF Core model: patient, study, series, instance, blob_location, blob_staging. `PostgresStorageLedger`, `EfInstanceCatalog`, EF projection and retrieve sources. Initial migration `20260920073623_InitialSchema`. Wired only when `Archive:Database:ConnectionString` is set (`ArchiveComposition.cs:68-82`) | 64 Postgres integration tests (gated by `ARCHIVE_DB`) |
| `PacsSys.Archive.Codecs` | **Partial**: JPEG-LS only | 501 (4 files, uncommitted) | `IDicomCodec`, `CodecRegistry`, `JpegLsCodec` (CharLS 4.1.1 managed binding), `NativeCodecLoader` (Linux natives redirect). Only JPEG-LS is registered (`CodecRegistry.cs:39-42`). Vendored `.so` files for OpenJPEG, OpenJPH and libjpeg-turbo have **no bindings** | JpegLs 10, NativeCodecLoader 7 |
| `PacsSys.Archive.Workers` | **Yes** | 198 (1 file, uncommitted) | `OrphanSweepWorker`: runs `RecoverAsync` on a 5-minute timer with a 15-minute grace. Failures are reported, not thrown (`OrphanSweepWorker.cs:151-163`) | OrphanSweepWorker 10 |
| `PacsSys.Archive.Hl7` | **No** (empty scaffold) | 0 | Only the csproj, with a comment that the Phase 04 ADT/ORM listener goes here | none |
| IntegrationTests | n/a | 5,533 (20 files) | Judges our SCP and SCU against arc-light and the dcm4che toolkit, plus Postgres suites. All gated by env vars (see §5) | 119 attributes |

**Tests, total.** 585 test-method attributes across 55 files (466 unit, 119 integration). This matches the scale the plan claims. I did **not** run them, per the rules. The plan's own counts are 551 unit passing and 2 skipped, and 27 integration passing and 28 skipped (`plan/feature-archive-engine-rebuild.md:51-53`). These are unverified.

**Stated goal** (`plan/feature-archive-engine-rebuild.md:15-19, 41-42`): replace the unmodified dcm4chee-arc-light with a C# archive in one process under systemd, Postgres installed natively, **no Docker anywhere** (CON-001, line 276; SC-08 line 523), no third-party DICOM library (CON-002, line 278), codecs as thin P/Invoke wrappers (REQ-004, line 268). Estimate: **32–42 engineer-weeks** for two senior .NET engineers, plus 20% (lines 41-42). Status in plan header: **In Progress**, v1.3, 2026-09-18.

**Target framework.** net10.0 for all projects (`archive-svc/Directory.Build.props`), `TreatWarningsAsErrors`, nullable enabled, `InvariantGlobalization=false` (needed for ICU Arabic folding, comment in Directory.Build.props). The Api csproj uses `Microsoft.NET.Sdk.Web`. EF Core 10.0.11, Npgsql EF 10.0.3, Dapper 2.1.79 (referenced in Persistence, not used in the code I checked), AWSSDK.S3 4.0.102.4 (unused), prometheus-net 8.2.1 (referenced in Api, nothing registers a metric: `Program.cs` has no `UseHttpMetrics` or `MapMetrics`).

**Configuration defaults.**
- `appsettings.json`: `Dimse.BindAddress 0.0.0.0`, port 11112, `AcceptAnyCalledAeTitle false`, `KnownCallingAeTitles []` (empty means any caller, `hosting.md:53`), `AcknowledgeVolatileIndex false`.
- `appsettings.Development.json`: `AcceptAnyCalledAeTitle true`, bind `127.0.0.1`, `AcknowledgeVolatileIndex true`, relative `BlobRoot "blobs"`.
- `archive-svc/src/PacsSys.Archive.Api/blobs/` exists on disk and is empty (0 files). It is gitignored.
- `archive-svc/tests/**/Fixtures` on disk: **14 files, about 7.9 MB**. They are gitignored, and the .gitignore comment says they contain real patient bytes (`.gitignore:21-24`). Not committed (verified with `git check-ignore`).

**Plan's own "not done" list** (`plan/feature-archive-engine-rebuild.md:218-252`):
- C-FIND with no UID reads every study's projections (performance, Decision 27).
- Reverse orphan sweep (blob without row, or missing blob) does not exist; `verified_at` is never re-confirmed.
- Three of four codecs have no managed binding.
- MWL and IAN are written but deliberately unwired (Decisions 7, 15, 21).
- Hosting: "Done" (lines 220-225).
- Durable ledger and catalogue: "Done" (lines 231-239, 374-385).

Plan items that are **not started** in the repo: Phase 03 DICOMweb (QIDO/WADO/STOW, Thumbnail `?viewport`), Phase 04 management API and HL7 ADT/ORM over MLLP (NHapi), Phase 05 migration and cutover. Also the Prometheus `dcm4chee_*` compatibility surface.

---

## 3. Infrastructure and deployment

**Topology in words, as configured:**
1. **Browser → Traefik :443.** TLS 1.2 minimum, SNI strict, cipher list (`infra/traefik/traefik.yml:44-52`). HTTP 80 redirects to HTTPS (`:18-26`). Self-signed dev CA generated by `infra/scripts/generate-dev-certs.sh`. Routers: `/api → bff-svc:3000`, `/dicom-web → arc:8080` (**no path rewrite**), `/auth → keycloak:8080`, `/admin|/clinical|/viewer → SPAs`, `/ → viewer` (`infra/traefik/dynamic/routers.yml:7-102`). **The overlay defines none of bff-svc, the three SPAs, or keycloak** (service list: `infra/docker-compose.prod.yml`, services at lines 30, 50, 69, 89, 109, 129, 150, 172, 198, 223, 243, 267 only).
2. **Modality → HAProxy :2762 (DIMSE-TLS) or :11112 (plain) → arc:11112.** TLS terminates at HAProxy and the hop to arc is plaintext (`haproxy.cfg:37, 40`). The comment at `haproxy.cfg:3-5` says "after TLS termination (or passthrough)". `infra/README.md:192` says "TCP-mode TLS passthrough". These two statements contradict each other. Client certificates are optional with no CA (`:37`). **Host-port collision**: HAProxy publishes 11112 (`infra/docker-compose.prod.yml:228`) and arc publishes 11112 (`phase-0/docker-compose.yml:73`). The overlay is additive, so both containers try to bind the same host port.
3. **External Postgres and LDAP: not wired.** `db-sidecar` and `ldap-sidecar` run `echo ...; sleep infinity` (`infra/docker-compose.prod.yml:33, 53`). arc still gets `POSTGRES_HOST: db` from phase-0 (`phase-0/docker-compose.yml:79`). `infra/README.md:146-168` describes arc connecting to an external Postgres, Patroni and LDAP syncrepl. That is design intent, not what the compose files do.
4. **MinIO.** Standalone for pilot, 4-node distributed for full (`docker-compose.prod.yml:69-150`). `minio-init` creates the bucket and sets anonymous access to none (`:181`). This fixes the anonymous-download problem recorded as R-09 in the archive plan. Arc is **not** pointed at MinIO by any compose file or script. The descriptor exists only as an LDIF (`infra/ldap/storage-descriptors.ldif`).
5. **Monitoring.** Prometheus on host 9090 with `--web.enable-lifecycle` (`docker-compose.prod.yml:250, 252`). Grafana on host 3001 with **anonymous Viewer access enabled** (`:273, 277`). Scrape targets: arc `:9990/metrics` (`infra/prometheus/prometheus.yml:34-45`; no JMX exporter exists in repo), BFF `:3000/metrics` (real, `bff-svc/src/app.ts:61`), MinIO `:9000/minio/v2/metrics/cluster` (`prometheus.yml:73-79`), Traefik `:8082/metrics` (`:84-90`; `traefik.yml` has no metrics block). No alert rules (`prometheus.yml:15, 20`). Grafana's `pacs-overview.json` panels read `dcm4chee_*` names that nothing in the repo produces.
6. **Backups.** `infra/scripts/backup-all.sh`: pgBackRest full backup (`:32`). MinIO "backup" mirrored to a bucket on the **same MinIO endpoint** (`:45-53`). LDAP exported as **plaintext LDIF** (`:78`). The pgBackRest template uses a **local** repo (`infra/backup/pgbackrest.conf.template:17`), with AES-256-CBC repo encryption and the passphrase from env (`:20-21`). Retention 30 full, 14 diff (`:18-19`). Differential and WAL cron jobs are outside the repo (`:37-40`). No off-site target.
7. **Restore drill.** Procedure written (`infra/docs/restore-drill.md`, 241 lines, with pass criteria). The checklist is an **unfilled template**: drill #, dates, results and sign-off are blank (`infra/docs/restore-drill-checklist.md:8-16, 114-119`). Troubleshooting refers to a container `pacs-arc` (`restore-drill.md:239`); the actual name is `phase0-arc` (`phase-0/docker-compose.yml:63`).
8. **Secrets handling.**
   - Good: `.env` and `.env.*` are gitignored (`.gitignore:7-10`). Only `.env.example` files are tracked. Overlay uses `${VAR:?...}` for MinIO distributed credentials (`docker-compose.prod.yml:75, 95, 115, 135`). Deploy script requires PG and LDAP secrets (`infra/scripts/deploy-prod.sh:71-74`).
   - Bad: many fallbacks to default credentials remain (see F-05). `deploy-prod.sh:288` prints the default Grafana login in its final message.
9. **Deploy orchestration.** `infra/scripts/deploy-prod.sh`: sources `.env` (`:48-50`), runs `npm run build` and `npm test` (`:152, :161`), **auto-generates self-signed certs if missing** (`:183-187`), starts `phase-0 + prod` compose with profiles (`:193-202, :251`), then smoke-tests with `curl -k` (`:264, :267`). It does **not** import either seed LDIF, and it hides `psql` audit-schema errors (`:244`). The cron line is printed, not installed (`:273`).

**Scaffolded vs validated.**
- *Validated by the authors' own logs, not by this review:* phase-1 compose config and Keycloak discovery returning 200 (`phase-1/README.md:434-435`); a live viewer smoke against a dev archive (`phase-1/README.md:421-433`).
- *Scaffolded (config exists, not wired or not run):* external Postgres and LDAP (placeholders), MinIO-backed storage (no LDIF import), ATNA audit (LDIF not imported, and it uses UDP syslog by default, `infra/ldap/atna-audit.ldif:20`), DIMSE-TLS (optional client cert, no CA), HA (single instance everywhere; HAProxy has one backend server at `haproxy.cfg:55`), monitoring (arc metrics not exported), backups (never executed, no off-site, no cron installed), restore drill (never recorded).
- *Plan-only:* replication/DR, Patroni, Keycloak 2-node, LDAP syncrepl (`plan/...pacs-architecture-plan.md:326-337`).
- `plan/feature-phase-7-production-hardening.md` has 39 ✅ markers and 0 open. The phase-7 README says "Implemented — local-dev infrastructure scaffolded" (`plan/feature-phase-7-production-hardening.md:7`). That is accurate for the code. The phase-7 README's claims of "externalized HA Postgres and LDAP" (`phase-7/README.md:11`) are not.
- `phase-8/README.md:9`: **Planned**. The plan has 51 open ⬜ markers, 0 done.

---

## 4. Conformance (`conformance/pacs-conformance-statement-v1.md`, v1.0 DRAFT, 2026-05-27)

**Claims, and what the repo supports:**

| Area | Claimed (file:line) | Repo evidence |
|---|---|---|
| Archive | arc-light 5.31.2; C-STORE SCP, C-FIND SCP, C-MOVE SCP, C-GET SCP, QIDO-RS, WADO-RS, WADO-URI, STOW-RS, UPS-RS, MWL SCP, HL7 ADT/ORM/ORU (`:11, 37`) | Version mismatch with 5.33.0 pin. Nothing in repo configures or tests UPS-RS, MWL, WADO-URI or GET. Upstream capability, unverified here |
| Storage SOP classes | CT, MR, Enhanced CT/MR (**supported**); CR, DX, US, SC, SR, PDF (accepted) (`:64-73`) | Viewer allowlist matches (`conformance/phase-1-ct-mr-allowlist.md:9-17`) |
| Q/R SOP classes | Study Root FIND and MOVE (`:79-80`) | **C-GET is missing here** but claimed in §2.2 (`:37`) |
| Worklist | MWL FIND (`:86`) | Not registered in archive-svc; not configured in arc |
| Verification (C-ECHO) | **Not listed** | archive-svc does declare Verification (`hosting.md:65`). Arc supports it by default. Closure doc A.1 flags the omission |
| MPPS, Storage Commitment, IAN | **Not listed** | archive-svc has MPPS and Storage Commitment; not in arc's CS. Closure doc A.2–A.4 |
| Transfer syntaxes | Explicit VR LE supported; Implicit VR LE "if required"; JPEG baseline, JPEG P14, JPEG-LS and J2K lossless **accepted by archive** (`:94-101`). WADO transcodes on the fly (`:105`) | No compression rules or transcoding config in repo. archive-svc accepts Explicit LE, Implicit LE, Explicit BE (`CStoreScp.cs:41-46`) and offers JPEG-LS only via the codec registry (not in its accept list) |
| DICOMweb auth | **JWT (Keycloak `pacs-viewer` client) for all QIDO, WADO, STOW, WADO-URI, Rendered** (`:111-118`). "DICOMweb endpoints accept JWT bearer tokens" (`:142`) | **Not enforced.** See F-01 |
| TLS | "All DICOMweb traffic uses HTTPS in production" (`:120`). DIMSE-TLS 2762 "recommended for production" (`:143`) | Port 8080 is published plain (`phase-0:71`). DIMSE-TLS is optional with no CA (`haproxy.cfg:37`). archive-svc has no TLS (`hosting.md:142-143`) |
| HL7 | ADT, ORM, ORU on 2575 (`:128-130`) | Arc-light's listener is published (`phase-0:75`). Not configured in repo |
| Audit | ATNA to syslog via `infra/ldap/atna-audit.ldif` (`:146`) | LDIF is never imported (F-14) |
| Mutations | "lossy compression not enabled in the storage/compression rules" (`:160`) | No compression rules exist in repo |
| Single AE | Archive is single AE `DCM4CHEE` (`:163`) | Consistent with repo |

**Conformance-related gaps the authors already list:** closure doc v1.1 targets (`PACS_Sys_DICOM_IHE_Workflow_Closure.md` §A–§N). The "Conformance Statement v1.1" is not published.

**Summary.** The statement mixes arc-light's upstream capabilities with PACS Sys's own configuration, without separating them. Several security claims are not implemented (JWT, mTLS, HTTPS-only DICOMweb). It should not go to a modality vendor in this form.

---

## 5. CI and testing

**Workflow: `.github/workflows/ci.yml`** (only workflow in repo).
- Triggers: push to `main`, pull requests, manual (`:3-8`).
- Job `phase-1` (`:14-39`): Node 22, `npm ci`, `npm run build`, `npm test` (root `vitest run tests`, `package.json:21`), `docker compose -f phase-1/docker-compose.yml config`.
- Job `phase-0-viewer` (`:41-66`): viewer `npm ci` and build, `docker compose config` in phase-0.

**Not run in CI:** `npm run typecheck` (script exists, `package.json:20`), any Playwright e2e (three specs), .NET build or tests (zero dotnet references in `.github`), the prod overlay (no `docker compose -f infra/...` validation), lint (none), SAST, secret scanning, `npm audit`, shellcheck and PSScriptAnalyzer. Push triggers only on `main`. Node 22 in CI vs `phase-0/README.md:176`, which says "Use Node 20, not 22+".

**Unit and component tests.** Root `tests/` (vitest), 12 files and 133 tests according to `phase-1/README.md:418-424` (author-reported). Not verified here.

**E2E.**
- `e2e/admin-mocked.spec.ts` and `e2e/clinical-mocked.spec.ts` are **mocked**. They intercept `/api/v1/*` in Playwright and start the Vite dev server through `playwright.admin.config.ts:16-17` and `playwright.clinical.config.ts:16-17`. Not run in CI.
- `e2e/viewer-live.spec.ts` is **live** and opt-in. It skips unless `E2E_VIEWER_STUDY_UID` is set (`:3, :13`). It needs a running viewer, BFF, archive and Keycloak. It falls back to a default dev login (`:9-10`, see F-31).

**archive-svc tests** (not in CI; gated by environment):
- `DCM4CHE_HOME` for the dcm4che toolkit suites (`Dcm4cheToolkit.cs:55`).
- `ARC_DIMSE_HOST` and `ARC_DIMSE_PORT` for the arc-light suites, with `ARC_MOVE_DEST` for C-MOVE (`ArcLight*Tests.cs`).
- `ARC_ALLOW_STORE=1` for suites that write to the archive (`ArcLightStoreTests.cs:67`).
- `ARCHIVE_DB` for the Postgres suites (`PostgresFixture.cs:59`).

The "judged by a third party in both directions" doctrine (plan CON-009) is real: our SCUs are checked against arc-light, and our SCPs are checked against the dcm4che toolkit. The authors' R-10 and Decision 19 record real bugs that this caught. Those assertions check files written, not exit codes. Decision 19 says the dcm4che toolkit rejected the Storage Commitment context, so the test found a real defect.

---

## 6. Plans and self-assessment

**Plan folder** (`plan/`, status from front matter):

| File | Status |
|---|---|
| `feature-archive-engine-rebuild.md` | **In Progress** (v1.3, 2026-09-18) |
| `feature-his-integration-and-promotions.md` | Draft, ready to implement |
| `feature-phase-1-viewer-bff-1.md` | Completed |
| `feature-phase-2-clinical-portal-mvp.md` | Implemented, automated validation passed |
| `feature-phase-3-admin-console-mvp.md` | MVP complete, automated and browser validation passed |
| `feature-phase-4-viewer-v1-1.md` | Completed |
| `feature-phase-5-clinical-advanced-surfaces.md` | Implemented, local-dev validation passed |
| `feature-phase-6-admin-advanced-surfaces-1.md` | In Progress |
| `feature-phase-7-production-hardening.md` | Implemented, local-dev infrastructure scaffolded (39 ✅, 0 open) |
| `feature-phase-8-field-pilot.md` | **Planned** (51 open, 0 done) |
| `feature-screenshot-parity-viewer-clinical-2.md` | Completed |
| `feature-viewer-clinical-modernization-1.md` | In progress |

**Archive rebuild (authors' own statements).** The plan is candid. §0 "Current Implementation Checkpoint" (lines 46-68) lists what is complete, in flight and not done. Phase 00 (DICOM foundation) is complete. Phase 01 (DIMSE) is in flight, and the table at lines 329-353 marks the SCPs and SCUs done. Phase 02 (durable index) is partial: the outbox is marked "Done, landed with the hosted sweep" (line 386), and the reverse sweep is "Queued" (line 387). Phases 03, 04 and 05 are not started. §8 (lines 1053-1070) records corrections measured against the live phase-0 archive. Decision 22 (lines 866-896) refuses a volatile index, which is good engineering. Risks R-01 (frame offset table, a patient-safety risk, line 473) and R-02 (IOCM cutover blocker, line 474) are recorded with mitigations. The plan states that **Phase 02 is a hard gate on production** (lines 505-507).

**Where the plan and the code disagree:**
- `hosting.md:34-35` says production "should not [acknowledge the volatile index] until Phase 02 lands the Postgres ledger". The plan and code say Phase 02 is wired. Stale doc.
- The plan says "12 projects" (line 51). `archive-svc/PacsSys.Archive.slnx` and the csproj count give **11**.
- The plan says MPPS is "Done" (line 349). It does not mention that MPPS state is in-memory even when Postgres is configured (F-23).
- The plan's cutover criteria (SC-07, line 522) do not mention DIMSE TLS or DICOMweb auth parity with the current stack (F-24).

**PACS_Sys_Plan_Audit.md** (2026-05-27, "Auditor: Claude"). This is a plan-level audit, not a code audit. It asks for P0 items the repo does not have: a regulatory folder (IEC 62304, ISO 14971, IFU, SOUP list), a threat model (`docs/threat-model` does not exist, line 80), a pen-test plan, `SECURITY.md`, CVE watch, image signing, CI security gates, backup encryption, PHI redaction pattern list, CSP/HSTS at Traefik, rate limiting, and a JWT lifecycle policy. Line 74 says "You have TLS, mTLS, ATNA audit, and Keycloak". **mTLS is not implemented** (no client cert code in `bff-svc`, F-17).

**CODE_HEALTH_AUDIT.md** (2026-08-12, commit `4c74157`):
- §1 Critical, unauthenticated HL7 MLLP listener: **still present** (`bff-svc/src/config.ts:163-165`; listener has no allow-list, timeout or cap). F-06.
- §2 High, 219 files flipped LF to CRLF: **no `.gitattributes` exists**. `git status` now shows no mass churn, so the immediate damage seems to have been cleaned up, but the guard is absent (F-21).
- §3 High, CI gaps: **still present** (`ci.yml` unchanged in shape). F-18.
- §4 High, 20 dependency advisories: not re-audited (no npm run). Root `package.json:41` still has `vitest ^2.1.8`, which is inside the critical advisory range the audit recorded (≤3.2.5).
- §8 Medium, `.env.development` not gitignored: **resolved**. `.gitignore:10` has `.env.*`, and `git ls-files` shows it untracked.
- §10 Low, about 700 KB of terminal logs committed: **still present**, 706,138 bytes across 9 root `.txt` files (F-19).

**PACS_Sys_DICOM_IHE_Workflow_Closure.md** (2026-05-27). A gap-closure plan: conformance v1.1 declarations (A.1–A.7), PIR patient merge and issuer rules (C), IOCM reject and QC (D), hanging protocols and priors (E), critical findings (F), walk-in MWL (G), sign-off attestation (H), lossy policy (I), legacy-PACS migration (J) and a validation suite (K). Effort estimated at 10–12 calendar weeks (§M). Nothing in the closure doc is marked done. The conformance statement shows none of its items.

**pacs-architecture-plan.md** (rev 5). §8.3 HA and §8.4 backup describe a Patroni cluster, a 4-node MinIO, LDAP syncrepl, a 2-node Keycloak, and off-site WAL and MinIO replication. None of this is implemented (§3 above). §2.1 line 81 specifies the `dcm4chee-arc-psql-secure` image, which the repo does not use.

**Root README.md.** Accurately says Phase 7 is "scaffolded" (line 30) and Phase 8 is "planned" (line 31). Line 117 says arc-light "remains an unmodified upstream dependency", which is consistent with the repo.

---

## 7. Capability checklist

Status vocabulary: **Real via arc-light** (upstream, deployed); **Real custom** (our code); **Scaffolded** (config or code exists, not wired or not run); **Planned** (plan only); **Absent**. Where the deployed stack and archive-svc differ, both are given.

| Capability | Status | Evidence | Notes |
|---|---|---|---|
| DIMSE C-STORE/C-FIND/C-MOVE/C-GET | **Real via arc-light** (deployed). **Real custom** in archive-svc (not deployed) | `phase-0/docker-compose.yml:62-73`; `phase-0/README.md:98-112` (storescu, author output); CS `:37`; archive-svc `Dimse/Scp/{CStore,CFind,CMove,CGet}Scp.cs`; `ArcLight*Tests.cs`, `Dcm4cheToolkit*Tests.cs` | C-GET is in CS §2.2 but not in §3.2. Plain TCP, AE-title only. No arc AE ACL in repo |
| MWL | **Real via arc-light** (claimed, unverified). archive-svc **Scaffolded** (unwired) | CS `:37, :86`; `ArchiveComposition.cs:33-38`; `hosting.md:70-74`; `Dimse/Scp/ModalityWorklistScp.cs`; `Dcm4cheToolkitWorklistTests.cs` | BFF has a local MVP MWL queue for admin and walk-in entries (`phase-1/README.md:139-143, 368`). Not DIMSE |
| MPPS | **Absent** on the deployed stack (arc does not claim it). **Real custom** in archive-svc (not deployed) | `ArchiveComposition.cs:100, 115`; `Dimse/Scp/MppsScp.cs`; `MppsScpTests.cs` (19) | Store is in memory even with Postgres (F-23). Not covered by the volatile-index gate |
| Storage Commitment | **Absent** on the deployed stack. **Real custom** in archive-svc (not deployed) | `ArchiveComposition.cs:111-114`; `Dimse/Scp/StorageCommitmentScp.cs`; `StorageCommitmentScpTests.cs` (15) | Report delivery is inline and unretried (archive plan R-11, line 483). Refuses unless bytes are on disk (Decision 14) |
| DICOMweb QIDO/WADO/STOW | **Real via arc-light, open** (no auth). archive-svc **Absent** (Phase 03 not started) | `phase-0/docker-compose.yml:62, :71`; `infra/traefik/dynamic/routers.yml:16-22`; `frontends/packages/dicomweb-sdk` (no auth); `bff-svc/src/app.ts:336-337`; `archive-svc/src/PacsSys.Archive.Api/Program.cs:7-8, 117-120` | Traefik does not strip `/dicom-web`; arc serves `/dcm4chee-arc/aets/...` (F-08). STOW for SR is `REPORT_SR_STORE_MODE=mock` by default (`phase-1/README.md:345`) |
| DICOM TLS | **Scaffolded** (HAProxy DIMSE-TLS on 2762, client cert optional, no CA). archive-svc **Absent** | `infra/haproxy/haproxy.cfg:36-40`; `infra/docker-compose.prod.yml:226-227`; `archive-svc/.../Dimse/Scp/DicomServer.cs:108` (plain `TcpListener`); `archive-svc/docs/hosting.md:142-143` ("not in any phase yet") | F-09. Plain 11112 still published |
| HTTPS | **Scaffolded** (Traefik TLS 1.2+, HSTS, HTTP to HTTPS redirect, self-signed CA generated). Application containers missing from overlay | `infra/traefik/traefik.yml:18-52`; `infra/traefik/dynamic/routers.yml:114-126`; `infra/scripts/deploy-prod.sh:166-190` | Arc 8080, WildFly 9990, Grafana 3001 and Prometheus 9090 bypass Traefik. F-02, F-04 |
| Compression/transcoding | **Real via arc-light, claimed** (CS `:98-105`; no rules in repo). archive-svc **Real custom** for JPEG-LS only, not in default accept list. J2K, HTJ2K, JPEG baseline **Absent** | `archive-svc/.../Codecs/CodecRegistry.cs:39-42`; `Codecs/JpegLsCodec.cs`; `Dimse/Scp/CStoreScp.cs:41-46`; codecs csproj `:5-17` (over-claims) | Decision 5: uncompressed-only ingest until a codec exists. The pilot CT stores uncompressed (plan §0) |
| Storage tiers/lifecycle | **Scaffolded** (MinIO S3 descriptor as LDIF, never imported; no lifecycle or retention rules in repo). archive-svc: filesystem blobs only, S3 **Planned** | `infra/ldap/storage-descriptors.ldif:14-27`; `infra/docker-compose.prod.yml:69-185`; `archive-svc/.../Storage/IBlobStore.cs` (FileSystemBlobStore); plan OQ-03 (line 1086) retention policy unknown | Retention policy is an open question for the archive plan |
| Routing/forwarding | **Scaffolded**. BFF admin routing rules are local MVP placeholders. archive-svc has static `MoveDestinations` and `CommitmentPeers` tables only | `phase-1/README.md:367, 376`; `archive-svc/src/PacsSys.Archive.Api/appsettings.json` (empty tables); `hosting.md:58-59` | archive-svc `StoreScu` exists but nothing forwards with it. Arc's own forwarding rules live in LDAP, not in repo |
| Replication/DR | **Absent** (plan only: `pacs-architecture-plan.md:333-337`; `infra/README.md:211-214` lists it as next step) | `infra/backup/pgbackrest.conf.template:17` (local repo only); `infra/scripts/backup-all.sh:45-53` (MinIO backup on the same endpoint) | No cross-site copy anywhere |
| Backup/restore | **Scaffolded**. Scripts and procedure exist; drill never recorded; no off-site; no cron installed | `infra/scripts/backup-all.sh`; `infra/backup/pgbackrest.conf.template`; `infra/docs/restore-drill.md`; `infra/docs/restore-drill-checklist.md:8-16, 114-119` | F-10, F-11, F-12. pgBackRest encryption is configured (`pgbackrest.conf.template:20-21`) |
| HL7 | **Real via arc-light** (listener on 2575, published, not configured in repo). **Real custom** BFF ORU MLLP (on by default, unauthenticated). archive-svc **Absent** (empty project) | `phase-0/docker-compose.yml:75`; CS `:126-134`; `bff-svc/src/config.ts:115, 163-165`; `bff-svc/src/clinical/hl7OruMllpListener.ts:20-24`; `Hl7/PacsSys.Archive.Hl7.csproj` | F-06, F-07, F-32. Port map inconsistent: ORU 2575, HIS-to-arc 2576, arc published 2575 |
| IHE profiles | **Planned** (closure doc only). Partial: BFF reject and IOCM are shadow state, no KOS written (archive plan R-02, line 474) | `PACS_Sys_DICOM_IHE_Workflow_Closure.md` §A–§N; CS §10 (cites IHE RAD rev 21 only) | Blocks cutover (archive plan R-02). CS declares no profile |
| Multi-tenancy | **Absent**. Single AE title, single archive | CS `:163`; `archive-svc/.../appsettings.json` (one `AeTitle`) | Declared as a limitation (CS §8 item 5) |
| HA/load balancing | **Scaffolded** (single instance everywhere; HAProxy one backend `haproxy.cfg:55`; Traefik one server per service). Patroni, syncrepl and Keycloak HA are **Planned** | `infra/docker-compose.prod.yml:30-67` (sidecars are placeholders); `infra/traefik/dynamic/routers.yml:70-102`; `pacs-architecture-plan.md:326-332` | Pilot is 2-server active/passive per plan, not evidenced in compose |
| Monitoring | **Scaffolded** (Prometheus and Grafana provisioned; BFF `/metrics` real). Arc JMX and Traefik metrics not enabled; no alert rules; Grafana anonymous | `infra/prometheus/prometheus.yml:15, 20, 34-45, 84-90`; `infra/docker-compose.prod.yml:250-277`; `bff-svc/src/metrics.ts`, `app.ts:61`; `infra/grafana/dashboards/pacs-overview.json` | F-15, F-34. archive-svc has `prometheus-net` but no metrics (`hosting.md:139-141`). BFF admin system views include hard-coded certificate data (`bff-svc/src/admin/systemDetailsClient.ts:155-157`) |
| Conformance statement | **Real** as a document (v1.0 DRAFT, 2026-05-27). **Partly inaccurate.** No archive-svc statement | `conformance/pacs-conformance-statement-v1.md` (§2–§9) | F-17. Pinned 5.33.0 vs stated 5.31.2. JWT, mTLS and HTTPS claims not implemented. Verification, MPPS, Storage Commitment, IAN not declared |
| CI | **Real, minimal**: build, unit tests, compose config for phase-0 and phase-1 only | `.github/workflows/ci.yml:3-66` | F-18. No .NET, e2e, typecheck, lint, security scans, or prod overlay check |

---

## 8. Findings

Severity: 🔴 high (security exposure or a deployment that will fail), 🟡 medium (real gap or misleading claim), 🟢 low (hygiene or minor drift). Line references are relative to the repo root. Secret values are described by kind only.

| ID | Sev | Where | Issue | Why it matters | Fix |
|---|---|---|---|---|---|
| F-01 | 🔴 | `phase-0/docker-compose.yml:62, :71`; `infra/traefik/dynamic/routers.yml:16-22`; `frontends/packages/dicomweb-sdk` (no Authorization); `bff-svc/src/app.ts:336-337` | **DICOMweb has no authentication anywhere in the deployed path.** Image is `-psql`, not `-secure`. The Traefik router has no auth middleware. The viewer sends a bearer token that the archive ignores. | Anyone who reaches 8080, or 443 once the path is fixed, can read studies. The conformance statement says the opposite (F-17) | Use `dcm4chee-arc-psql-secure` with Keycloak, or add forwardAuth on `/dicom-web` at Traefik. Remove the 8080 publish in prod. Correct the CS |
| F-02 | 🔴 | `phase-0/docker-compose.yml:23, :47, :71, :73, :75, :77`; `infra/docker-compose.prod.yml` header (additive) and `deploy-prod.sh:194-195` | The prod overlay is **additive** to phase-0, so the stack publishes **LDAP 389, Postgres 5432, arc HTTP 8080, plain DIMSE 11112, HL7 2575, WildFly management 9990** on the host. Traefik and HAProxy TLS can be bypassed. | Database and admin ports open to the network. WildFly management port exposed | Give prod services `ports: []` for dev-only publishes, or split dev and prod compose files |
| F-03 | 🔴 | `phase-0/docker-compose.yml:73` and `infra/docker-compose.prod.yml:228` | **Host port 11112 is published by two services** (arc and HAProxy). By inspection, the second container to start cannot bind. (Not run, per the rules.) | `deploy-prod.sh` with the `tls` profile (`:199`) will fail at `up -d` | Drop the arc publish in prod, or the HAProxy plain frontend |
| F-04 | 🔴 | `infra/traefik/dynamic/routers.yml:70-102` (targets `bff-svc:3000`, `admin-spa`, `clinical-spa`, `viewer-spa`, `keycloak:8080`); `infra/docker-compose.prod.yml` (services) | **The overlay defines none of the application services** that Traefik routes to. The "one-command deployment" cannot serve the product. | `deploy-prod.sh:264` smoke test (`/api/healthz`) cannot pass from this overlay | Add the services, or document the external stack and gate the deploy on it |
| F-05 | 🔴 | `infra/docker-compose.prod.yml:156-157, :177, :181`; `infra/scripts/backup-all.sh:50, :75`; `infra/scripts/deploy-prod.sh:235, :288`; `infra/ldap/storage-descriptors.ldif:20-21`; `infra/haproxy/haproxy.cfg:65`; `phase-0/docker-compose.yml:32`; `phase-0/.env.example:12`; `phase-1/docker-compose.yml:10`; `infra/docker-compose.prod.yml:272` | **Default-credential fallbacks in production paths.** Kinds: MinIO root user and password fallback (standalone pilot profile runs with it); LDAP admin password fallback in the backup script; Grafana admin fallback and the deploy script printing the default login; HAProxy stats fallback; literal LDAP admin password in phase-0 healthcheck; weak default Postgres password in `.env.example`; Keycloak bootstrap admin literal in phase-1; MinIO root creds reused as the S3 access key | Any run that does not override these uses known credentials. Distributed MinIO already uses `:?` (`docker-compose.prod.yml:75`), so the standalone path is the weak one | Replace every fallback with `${VAR:?...}`. Generate per-site secrets. Add a secret scanner (gitleaks) to CI. Do not print defaults |
| F-06 | 🔴 | `bff-svc/src/config.ts:163-165`; `bff-svc/src/clinical/hl7OruMllpListener.ts:20-24`; `CODE_HEALTH_AUDIT.md` §1 | **Unauthenticated HL7 ORU listener, enabled by default, bound to `0.0.0.0:2575`.** No allow-list, no timeout, no size cap, no connection limit. Still unfixed. | Any host that can reach 2575 can inject report content into patient records. The audit reproduced a 30 MB unterminated frame that blocked the event loop for 13 s | Make it opt-in. Default the host to `127.0.0.1`. Cap buffer and connections. Add an allow-list or mTLS. Serialise ACKs |
| F-07 | 🟡 | `phase-0/docker-compose.yml:75` | arc-light's HL7 listener is **published on the host** with no allow-list or TLS in repo. Its config lives in LDAP. | A second unauthenticated HL7 entry point | Do not publish 2575. Restrict at the network layer. Verify arc's HL7 application config |
| F-08 | 🟡 | `infra/traefik/dynamic/routers.yml:16-22, :78`; `bff-svc/src/config.ts:128`; `phase-0/README.md:141` | **Traefik `/dicom-web` route does not rewrite the path.** Arc's DICOMweb lives at `/dcm4chee-arc/aets/DCM4CHEE/rs`. The rewrite exists only in the Vite dev proxy. | By inspection, viewer DICOMweb calls through 443 return 404. `deploy-prod.sh:267` smoke check would fail. (Not run) | Add `replacePathRegex` or `stripPrefix` middleware. Test against a live stack |
| F-09 | 🟡 | `infra/haproxy/haproxy.cfg:3-5, :36-40, :45-49`; `infra/docker-compose.prod.yml:228`; `infra/README.md:192` | **DIMSE-TLS is not enforced.** `verify optional`, no `ca-file`, so no client authentication. TLS terminates at HAProxy and the hop to arc is plaintext. Plain 11112 frontend kept and published. Docs say "passthrough". | Confidentiality on one hop only. No peer authentication. CS `:143` says DIMSE-TLS is recommended | `verify required` with `ca-file`. Drop the plain frontend in prod. Fix the doc |
| F-10 | 🟡 | `infra/scripts/backup-all.sh:9, :45-53, :78`; `infra/backup/pgbackrest.conf.template:17`; `.gitignore` (no `backups/` rule) | **Backups are not off-site and some land inside the repo.** Default `BACKUP_DIR` is `../../backups/` relative to the repo, and `backups/` is not gitignored. The MinIO "backup" goes to the same MinIO endpoint. LDAP export is plaintext LDIF. pgBackRest repo is local only. | A single host loss takes data and backups together. Patient-bearing exports could be committed | Default backups outside the repo and add an ignore rule. Encrypt LDIF. Off-site pgBackRest repo (S3, encrypted). Separate MinIO backup target |
| F-11 | 🟡 | `infra/docs/restore-drill-checklist.md:8-16, 114-119`; `infra/docs/restore-drill.md:239` | **No restore drill has been recorded.** The checklist is blank. Troubleshooting names `pacs-arc`; the actual container is `phase0-arc` (`phase-0/docker-compose.yml:63`). | Plan requires a drill within the first week (`infra/README.md`, phase-7). No evidence of RTO or RPO | Run a drill in a sandbox, record it, fix the container name |
| F-12 | 🟢 | `infra/scripts/backup-all.sh:37-38` | Exit-code bug: `$?` is read after an `echo` in the failure branch, so the summary records exit 0. | Failed Postgres backup can be reported as code 0 | Capture `$?` immediately after the command |
| F-13 | 🟡 | `infra/docker-compose.prod.yml:27-67` (sidecars with `sleep infinity`); `phase-0/docker-compose.yml:79`; `infra/README.md:146-168`; `phase-7/README.md:11` | "Externalised HA Postgres and LDAP" is presented as delivered. The sidecars only echo and sleep. Arc still uses the phase-0 DB. | The pilot runs a single unreplicated database inside phase-0 compose | Relabel as scaffold. Wire arc's env to the external host, or remove the sidecars |
| F-14 | 🟡 | `infra/ldap/storage-descriptors.ldif:18-27`; `infra/ldap/atna-audit.ldif:18-20`; scripts | Both seed LDIFs contain `${VAR:-default}` placeholders. `ldapadd` does not expand them. **No script or runbook step imports them.** | MinIO storage and ATNA audit are not configured by any deploy path. If imported raw, the literal placeholder strings would be stored | Add an `envsubst` plus import step to `deploy-prod.sh`. Test the import |
| F-15 | 🟡 | `infra/prometheus/prometheus.yml:15, 20, 34-45, 73-79, 84-90`; `infra/docker-compose.prod.yml:250-252`; `infra/traefik/traefik.yml` (no metrics block) | Monitoring gaps. Arc `:9990/metrics` is not exposed (no JMX exporter). Traefik `:8082/metrics` is scraped but not enabled. No alert rules. MinIO scrape path may need auth (unverified). `--web.enable-lifecycle` on a published port allows unauthenticated config reload. | Dashboards show nothing for arc; no alerts for ingest failure or disk pressure | Enable exporters, add alert rules, bind Prometheus to localhost or behind auth |
| F-16 | 🟡 | `infra/docker-compose.prod.yml:211`; `infra/traefik/traefik.yml:39-42` | Docker socket mounted into Traefik (read-only mount), with the docker provider enabled. All routes are file-provider. | Socket access is root-equivalent on the host if Traefik is compromised | Remove the docker provider and the socket mount |
| F-17 | 🟡 | `conformance/pacs-conformance-statement-v1.md:11, 15, 37, 79-80, 86, 111-120, 142-143, 160`; `phase-0/.env.example:7`; `infra/README.md:193`; `PACS_Sys_Plan_Audit.md:74` | **Conformance statement and plans overstate the implementation.** Claims: JWT on all DICOMweb (not enforced). "All DICOMweb traffic uses HTTPS in production" (no restriction on 8080). Version 5.31.2 vs pinned 5.33.0. mTLS between BFF and arc (no code). C-GET in §2.2 but absent from §3.2. Verification, MPPS, Storage Commitment and IAN not declared. "Compression rules" that do not exist | Correct the statement before any vendor sees it. Separate upstream capability from PACS Sys configuration. Mark unimplemented items as planned |
| F-18 | 🟡 | `.github/workflows/ci.yml:3-66` | CI misses: no .NET build or tests (no dotnet anywhere in `.github`), no typecheck, no e2e, no prod overlay validation, no lint, no secret or SAST scan, no `npm audit`. Push runs only on `main`. Node 22 vs Node 20 advice (`phase-0/README.md:176`) | The 551+27 archive tests and the mocked e2e never run in CI. Regressions in archive-svc are invisible to the pipeline | Add a `dotnet test` job with the phase-0 stack as a service. Add typecheck, e2e-mocked, compose validation for prod overlay, gitleaks |
| F-19 | 🟡 | root `sent.txt` (187.6 KB, 3,276 lines of storescu output), `npm-test-summary.txt`, `clinical-plan-verify.txt`, `clinical-split-verify.txt`, `studylist-plan-verify.txt`, `task029-bulk-share-verify.txt`, `task029-sort-verify.txt`, `worklist-plan-verify.txt`, `.sync-probe.txt` | **Nine tracked root `.txt` logs, 706,138 bytes.** `sent.txt` contains local study-folder paths from a DICOM send. Content not reproduced here. | Can carry identifiers in git history. Bloat. Same as CODE_HEALTH_AUDIT §10 | `git rm --cached`, add to `.gitignore`. Check for identifiers before publishing the repo |
| F-20 | 🟢 | `phase-0/viewer/tsconfig.tsbuildinfo`, `frontends/apps/{admin,clinical,viewer}/tsconfig.tsbuildinfo` | Four `tsbuildinfo` files are tracked even though `*.tsbuildinfo` is gitignored (`.gitignore:6`) | Churn | `git rm --cached` |
| F-21 | 🟢 | repo root (no `.gitattributes`) | No `.gitattributes`. The earlier LF/CRLF flip is not present in `git status` now, but nothing prevents a recurrence | Recurrence risk | Add `* text=auto eol=lf` |
| F-22 | 🟢 | `.obsidian/` (5 files), `.codex/hooks.json`, `archive-svc/src/PacsSys.Archive.Api/PacsSys.Archive.Api.csproj.user`; `archive-svc/tests/**/Fixtures/` (14 files, 7.9 MB, real patient bytes per `.gitignore:21`) | Tool state and user files not ignored. Fixtures on disk are ignored, but must not travel with the repo | Accidental commit or sharing | Add ignores for tool state and `*.csproj.user`. Keep fixtures out of backups and shares |
| F-23 | 🟡 | `archive-svc/src/PacsSys.Archive.Api/Hosting/ArchiveComposition.cs:100, :115`; `archive-svc/src/PacsSys.Archive.Api/Hosting/ArchiveOptions.cs:79-86`; `Api/Program.cs:79-89` | **MPPS store is in memory even when Postgres is configured.** The volatile-index gate and warning cover only ledger and catalogue. | N-CREATE and N-SET state is silently lost on restart. A modality could see a procedure step vanish. The plan lists MPPS as Done without this caveat | Add a Postgres MPPS store, or extend the gate and warning to MPPS |
| F-24 | 🟡 | `archive-svc/src/PacsSys.Archive.Dimse/Scp/DicomServer.cs:108` (plain `TcpListener`); `archive-svc/src/PacsSys.Archive.Api/appsettings.json` (`BindAddress 0.0.0.0`, `KnownCallingAeTitles []`); `archive-svc/docs/hosting.md:53, :142-143` | archive-svc has **no TLS** and, by default, accepts any calling AE. The authors say "DIMSE over TLS is not in any phase yet". | The planned cutover (Phase 05) removes the HAProxy DIMSE-TLS path with nothing to replace it. SC-07 (plan line 522) does not mention TLS | Add `SslStream` support, or keep HAProxy in front and document it. Populate `KnownCallingAeTitles` in the pilot profile |
| F-25 | 🟡 | `archive-svc/src/PacsSys.Archive.Api/Hosting/...`; `phase-1/docker-compose.yml`; `phase-0/docker-compose.yml:73`; `bff-svc/src/config.ts:115` | HL7 port map is inconsistent: BFF ORU default 2575, HIS-to-arc 2576, arc publishes 2575. The archive plan says ORU stays on 2576 (`Hl7.csproj:4-5`). | Misrouted HL7 or a host port clash | Write the port map into `infra/README.md` and make one value canonical |
| F-26 | 🟢 | `archive-svc/src/PacsSys.Archive.Codecs/PacsSys.Archive.Codecs.csproj:5-17`; `archive-svc/src/PacsSys.Archive.Codecs/CodecRegistry.cs:39-42` | The codecs csproj header lists JPEG baseline, P14, J2K and HTJ2K as "supported". Only JPEG-LS is registered. No P/Invoke for OpenJPEG, OpenJPH or libjpeg-turbo | Over-claim in code comments | Reword to "planned" |
| F-27 | 🟢 | `archive-svc/src/PacsSys.Archive.Dicom/Part10/DatasetReader.cs:19-22` vs `:315-334` | Stale comment: says Implicit VR is deferred, but `ReadImplicitVrHeader` is implemented | Reviewer confusion | Update the comment |
| F-28 | 🟢 | `archive-svc/src/PacsSys.Archive.Storage/*.csproj` (`AWSSDK.S3`); `archive-svc/src/PacsSys.Archive.Api/*.csproj` (`prometheus-net`); `Persistence` (`Dapper`) | Unused package references. No S3 code. No metrics registered. Dapper not seen in code I checked | Supply-chain surface for no benefit | Remove until used |
| F-29 | 🟢 | `archive-svc/src/PacsSys.Archive.Persistence/ArchiveDbContextFactory.cs:29`; `archive-svc/tests/PacsSys.Archive.IntegrationTests/PostgresFixture.cs:38`; `archive-svc/tests/PacsSys.Archive.UnitTests/Hosting/ArchiveHostTests.cs:85` | Hard-coded database password literals. The factory is a design-time placeholder (`design-time.invalid`) but sits in the production assembly. Test fixtures use a superuser literal | Pattern that gets copied | Read from env. Move design-time factory out of the production assembly |
| F-30 | 🟡 | `phase-1/keycloak/realm-pacs.json:22, :51, :80` (public clients with direct grants), `:111-112` (BFF confidential client with a committed secret labelled dev-only), `:122, :131` (committed seeded-user passwords, also documented at `phase-1/README.md:118-121`); `phase-1/docker-compose.yml:15` | **Dev realm commits a client secret and seeded passwords, and enables password grants (ROPC) on the three SPA clients.** | If this realm is reused for pilot, credentials leak and SPAs handle passwords directly | Separate dev and prod realms. Disable direct grants. Rotate any reused secret |
| F-31 | 🟢 | `e2e/viewer-live.spec.ts:9-10` | Default dev login fallback in the live e2e spec | Only if the spec runs against a shared environment | Require the env var |
| F-32 | 🟡 | `infra/docker-compose.prod.yml:172-185` (`minio-init` uses `minio/mc:latest`) | Floating tag for the `mc` image, against the repo's own "pin every tag" rule (`phase-0/docker-compose.yml:3-4`) | Non-reproducible builds | Pin the digest |
| F-33 | 🟢 | `infra/docker-compose.prod.yml:206` | Traefik dashboard port 8082 published. `traefik.yml:12-13` sets `insecure: false`, so no dashboard route exists | Unnecessary exposure | Do not publish |
| F-34 | 🟡 | `infra/docker-compose.prod.yml:272-277`; `infra/grafana/dashboards/pacs-overview.json` | Grafana anonymous Viewer on published port 3001. Dashboards include "Audit event backlog" and "Clinical state entries" | Operational and clinical-workload data readable without login | `GF_AUTH_ANONYMOUS_ENABLED=false` |
| F-35 | 🟡 | `archive-svc/docs/hosting.md:34-35` | Doc drift: says production must not acknowledge the volatile index "until Phase 02 lands". Phase 02 is wired (plan lines 231-239) | Operators may keep the wrong config | Update the doc |

**Hygiene (E), summary.**
- Tracked env files: only `.env.example` (root and phase-0). `.env`, `.env.development` are untracked and ignored (`.gitignore:7-10`). ✅
- `node_modules`: 0 tracked. ✅
- `*.tsbuildinfo`: 4 tracked (F-20).
- Tracked logs: 9 root `.txt` files, 706 KB (F-19).
- `phase-0/viewer/dist` exists on disk and is gitignored. ✅
- `backups/` not ignored (F-10).
- `.sync-probe.txt` tracked (22 bytes), a sync probe artefact (F-19).

---

## 9. Strengths

- **Store ordering is the right design and is documented where it is enforced.** `archive-svc/src/PacsSys.Archive.Storage/InstanceStore.cs:15-36` says reserve, blob, metadata, resolve. A blob with no metadata is recoverable. Metadata with no blob is never created. A duplicate C-STORE whose blob still exists returns `AlreadyPresent` (`:71-89`). Decision 4 keeps both objects on a UID collision.
- **Volatile-index refusal and honest health reporting.** `Api/Program.cs:79-89`, `ArchiveOptions.cs:79-86`, and `/health` reports `durableIndex` (`Program.cs:108`).
- **Third-party judging of DIMSE.** Our SCUs are judged by arc-light and our SCPs by the dcm4che 5.33 toolkit (plan CON-009). The authors record the defects this found (Decisions 17, 19, 20). Assertions are on files written, not exit codes (plan R-10).
- **Careful DIMSE defaults.** Retrieve identifiers without unique keys are refused (plan Decision 9). Unknown C-MOVE destinations are refused, not resolved by DNS (Decision 10). Storage Commitment refuses unless the bytes are on disk (Decision 14). AE titles over 16 characters are refused, not truncated (Decision 22).
- **Arabic handling.** `Iso2022Decoder.cs:23-31` documents the designation-reset rules at `=` and `\`. `DicomEncodings.EnsureRegistered()` runs before any decode (`ArchiveComposition.cs:57-61`). `InvariantGlobalization=false` is set deliberately (`Directory.Build.props`).
- **Unidentified patients scoped per study** (Decision 24), avoiding a merge incident.
- **Authors' honesty.** The archive plan has a decision log, a "measured against the live archive" table with corrections (§8), and an explicit "not done" list. Several earlier audits (CODE_HEALTH_AUDIT) were written and kept.
- **Production hardening that is real:** TLS 1.2 minimum with strict SNI and HSTS (`traefik.yml:44-52`; `routers.yml:114-126`); MinIO bucket anonymous access set to none (`docker-compose.prod.yml:181`, fixing R-09); distributed MinIO uses required secrets; BFF defaults to OIDC (`bff-svc/src/config.ts:124`).
- **Backup script records per-step status** and writes a JSON summary (`backup-all.sh:98-107`). pgBackRest repo encryption is configured (`pgbackrest.conf.template:20-21`).
- **Restore procedure is specific,** with pass criteria per step (`restore-drill.md`).
- **Conformance policy.** "Documented + tested = allowed; anything else stays blocked" (`conformance/phase-1-ct-mr-allowlist.md:55`).
- **Secrets are not in git,** and `node_modules` is untracked.

---

## 10. Open questions

1. **Archive auth.** Is `dcm4chee-arc-psql-secure` (plan `pacs-architecture-plan.md:81`) meant to be used in production? Is an external reverse proxy in front of arc that adds auth or strips paths? If so, it is not in this repo.
2. **Where do BFF, SPAs and Keycloak run in production?** The overlay does not define them (F-04). Is there a separate stack, or is the overlay incomplete?
3. **Arc's LDAP configuration** (AE titles, ACLs, HL7 applications, storage descriptors, compression rules) is not version-controlled. Without it, every DIMSE and compression claim in the CS is unverifiable.
4. **Has any drill, pilot deploy or production deploy ever run?** The checklist is blank (F-11). `phase-8` is 0% done.
5. **HL7 ports.** Is arc's HL7 listener enabled? Which port does the BFF's HIS forwarder (`HIS_ARC_MLLP_PORT` default 2576, `bff-svc/src/config.ts:115`) actually reach?
6. **Archive-svc test results.** Did the author run the 551 unit and 27 integration tests with `DCM4CHE_HOME` and `ARCHIVE_DB` set? Not verified here (no run per rules).
7. **MPPS durability.** Is the in-memory store intended for the first cut? (F-23)
8. **Cutover intent.** Does Phase 05 retire the HAProxy DIMSE-TLS layer and Traefik `/dicom-web` route? If so, what replaces them (F-24)?
9. **Which `dcm4chee_*` metrics does arc actually emit** (archive plan OQ-05)? Grafana and the BFF's systemClient read them.
10. **MinIO metrics auth.** Does `/minio/v2/metrics/cluster` require a token in this MinIO version? (`prometheus.yml:73-79`; unverified)
11. **Retention policy** (archive plan OQ-03) is undefined. Storage-tier lifecycle depends on it.
12. **Dev realm.** Is `phase-1/keycloak/realm-pacs.json` used outside dev? (F-30)
