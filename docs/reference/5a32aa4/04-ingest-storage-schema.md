# Ingest, storage, data integrity and schema — changes in 5a32aa4

> **Re-review of commit `5a32aa4`** (baseline `e90d32c`), part of the [PAXS Archive documentation](../../README.md). Static analysis, 2026-10-08; line numbers refer to `5a32aa4`. Baseline area reference: [02-dicom-network-ingest.md](../02-dicom-network-ingest.md), [05-nodes-routing-storage.md](../05-nodes-routing-storage.md). Findings are consolidated and prioritized in [CODE_REVIEW.md](../../CODE_REVIEW.md), which takes precedence where the two differ. Finding IDs here (N1, N-01, V4-01, …) are local to this file; CODE_REVIEW.md maps them to NEW-xx IDs.

Old commit: `e90d32c`. New commit: `HEAD` = `5a32aa4` ("Sync local project state to GitHub").
Method: read-only. Every claim comes from code read at HEAD. Line numbers refer to HEAD unless marked "old".
Not executed: pytest and alembic (both would write caches or need the missing `app/services/storage/` package). Items that need a runtime check are marked **verify**.
Severity labels: RED = data loss, corruption, cross-tenant exposure or ingest outage. YELLOW = defect with a clear failure path but narrower trigger. GREEN = hygiene or low impact.

## 0. Headline

- The commit is one large sync (`5a32aa4`): 295 files, +47,377 / -3,436 overall. In-scope changes are concentrated in ingest, quota, C10 tenant identity, split/merge, lossy, routing and four new migrations.
- Good progress: the C-STORE/STOW stage, index, commit, promote flow exists; `..` path escape is rejected at the key layer; lossy now encodes native pixels with matched codecs; prefetch calls a real function; C-GET/C-MOVE, priors and lifecycle now apply tenant scope or admin-only checks.
- Still broken at HEAD: `app/services/storage/` is missing from git (app cannot import); auto-routing for new studies never matches (F-01); the quota counter never decreases and its tenant/backend scoping is wrong; C10 tenant-scoped identity leaves NULL-tenant rows non-unique and, on MySQL, keeps the old global unique.
- New RED findings: N-01 to N-10 (section 3). The two most urgent are the MySQL migration problems (N-02 and N-03) and split/merge commit ordering (N-07).

## 1. Change summary

### 1.1 Migration chain (verified by parsing all 42 revision files; `(local verification file)`)

| Revision | down_revision | Status | Content |
|---|---|---|---|
| `c0d1e2f3a4b5` | `b3456789c0d1` | old head | auth sessions and lockout |
| `a3456789b0c1` | `c0d1e2f3a4b5` | NEW | `report_addenda` (C06); no `updated_at` (fixed later) |
| `b4567890c1d2` | `a3456789b0c1` | NEW | C10: `assigning_authorities`, `patient_identities`, `patient_identity_conflicts`; `patients.tenant_id`; composite unique replaces global unique; data report (no data moved) |
| `c5678901d2e3` | `b4567890c1d2` | NEW | creates `lossless_compression_rules`, `organization_settings`, `report_templates` from `Base.metadata`; adds `report_addenda.updated_at` |
| `d6789012e3f4` | `c5678901d2e3` | NEW, **HEAD** | `storage_quotas.used_bytes` + backfill from `instances` |

- Single head at HEAD: `d6789012e3f4`. Old head was `c0d1e2f3a4b5`. Merges `s9013243n4o5` and `t9013243n4o5` are unchanged.
- `migrations/env.py`: adds `config.attributes["paxs_migration_url"]` override (H12). No schema effect.

### 1.2 Settings

- New in this commit, in scope: `LOSSY_COMPRESSION_ENABLED: bool = False` (`app/core/config.py:161`, fail-closed); `STORAGE_ALLOW_LEGACY_PATHS: bool = False` (config diff; read by the missing storage backend).
- Read by in-scope code, pre-existing: `ROUTING_QUEUE_ENABLED`, `DR_REPLICATION_ENABLED`, `COMPRESSION_ENABLED`, `STATS_USAGE_CACHE_SECONDS`, `STOW_MAX_PART_BYTES`, `DICOMWEB_PUBLIC`, `SCRIPTS_ENABLED`.

### 1.3 Per-file changes

| File | What changed | New functions / signatures | New model / schema |
|---|---|---|---|
| `app/services/indexing_service.py` | C10 identity resolution; demographic conflicts quarantined; tenant set on study creation only; C04 cross-tenant guard; GSPS/KIN/RDSR hooks; DR replication flag; auto-route call moved into study creation | `resolve_patient_identity(session, meta, tenant_id=None, source=None) -> PatientResolution` (114); `default_authority_oid(tenant_id) -> str` (89); `_ensure_authority(session, oid)` (96); `get_or_create_patient(...)` (282); `get_or_create_study(session, meta, patient, tenant_id=None)` (290); `get_or_create_series` (370); `upsert_instance(session, series, meta, file_path, file_size, storage_backend=None)` (398); `index_new_instance(session, meta, file_path, sha256=None, performed_by, tenant_id, storage_backend, read_path, ds, file_size_bytes) -> Instance` (433); `class CrossTenantIngestError(ValueError)` (41) | none (uses `PatientIdentity`, `PatientIdentityConflict`, `AssigningAuthority`) |
| `app/services/storage_service.py` | Key validation (`..`, separators, Windows device names, length); staged write with quota reservation before any byte is written; tenant/backend resolution; `move_study_to_backend` | `class InvalidStorageKey(StorageError)` (48); `unsafe_reason(value) -> str or None` (78); `validate_component(value, *, field, fallback) -> str` (101); `instance_key(study, series, sop)` (125); `thumbnail_key(sop)` (133); `resolve_study_backend(study_uid) -> str or None` (159); `default_backend_name() -> str or None` (145); `@dataclass StagedObject` with `promote()` (359) and `discard()` (366); `stage_dataset_with_quota(ds, tenant_id, backend_name=None, charge_bytes=None) -> StagedObject` (382); `purge_stale_temp_files` (438); `move_study_to_backend(study_uid, to_backend) -> dict` (531); `cached_storage_usage_bytes` (267); `invalidate_usage_cache` (302). `save_bytes_with_quota` (323) and `save_dataset_with_quota` (474) exist but have no callers | none |
| `app/services/quota_service.py` | Atomic reservation via conditional UPDATE; release; recompute (no callers) | `class QuotaExceeded(RuntimeError)` (269); `reserve_quota(tenant_id, backend_name, size) -> list[QuotaInfo]` (278); `release_quota(tenant_id, backend_name, size) -> None` (336); `recompute_used_bytes(tenant_id=None) -> int` (380); `_info_for(session, q)` (360); `get_effective_quotas(tenant_id, backend_name)` (71) unchanged logic | `StorageQuota.used_bytes` (models.py:1150) |
| `app/dicom/receiver.py` | C-STORE: validate present UIDs; resolve tenant from calling AE; stage (reserve quota); index in one transaction; promote after commit; new status codes | `_resolve_tenant(source_ae) -> int or None` (57); `_index_async(meta, path, sha256, source_ae, backend, read_path, ds, file_size_bytes)` (67); `handle_store(event) -> int` (85); C-GET/C-MOVE gain tenant scope via `_matching_instance_keys(query, level, tenant_id)` (479) | none |
| `app/dicom/hl7.py` | `_ensure_patient` limited to `tenant_id IS NULL` (documented gap: no AE-to-tenant mapping on the HL7 listener) | (changed body of `_ensure_patient`, ~255-290) | none |
| `app/dicom/parser.py` | Adds `issuer_of_patient_id` (IssuerOfPatientID 0010,0021) to parsed metadata | (`_META_FIELDS`, `_KEYWORD_MAP`) | none |
| `app/services/split_merge_service.py` | Stage-verify-flush-unlink relocation (C-1); split and merge write verified rewritten objects | `split_series_to_new_study(session, source_study_uid, series_uids, target_patient_id=None, target_patient_name=None, new_study_description=None, performed_by) -> Study` (43); `merge_studies(session, source_study_uids, target_study_uid, performed_by) -> Study` (157); `_rewrite_instance_study_uid(file_key, new_study_uid, new_patient_id, new_patient_name=None, backend=None) -> bytes` (231); `_verify_rewritten_instance(key, backend, new_study_uid, new_patient_id) -> None` (261); `async _rewrite_and_relocate(session, targets, *, new_study_uid, new_patient_id, new_patient_name, performed_by, action) -> None` (286) | none |
| `app/api/v1/split_merge.py` | Auth raised to destructive role (`split` 48, `merge` 84) | none | none |
| `app/services/lossy_compression_service.py` | Native-pixel transcode; eligibility gate; encoder match; geometry and decode checks; staged publish | `class LossyCompressionError(RuntimeError)` (61); `is_eligible_source(uid) -> bool` (117); `encoder_available(uid) -> bool` (142); `validate_target_transfer_syntax(uid) -> str` (152); `_codec_kwargs(ts, q) -> dict` (171); `_assert_geometry_preserved(before, ds)` (196); `transcode_dataset_lossy(ds, target_ts_uid, quality_factor=85) -> (Dataset, int)` (217); `async transcode_instance_lossy(session, instance, target_ts_uid, quality_factor, *, study_uid, tenant_id) -> bool` (284); `async evaluate_and_run_lossy_compression(session, rule_id=None) -> dict` (391); CRUD helpers at 498-537. Constants: `LOSSY_TARGETS` (94), `_LOSSY_UIDS` (71), `_LOSSLESS_COMPRESSED_UIDS` (81) | `LossyCompressionRule.transfer_syntax_uid` default changed to 1.2.840.10008.1.2.4.81 (models.py:1043) |
| `app/api/v1/lossy_compression.py` | Default target JPEG-LS near-lossless; baseline rejected with 400; auth to destructive role; run reports `disabled`, `skipped_instances` | none | none |
| `app/services/routing_service.py` | Temp file for remote (S3) objects removed in `finally` | none (`import os` added) | none |
| `app/api/v1/routing.py` | Rule create/update/delete require admin; evaluate requires destructive role | none | none |
| `app/services/prefetch_service.py` | Prior fetch now calls `sender.send_move` (F-03); max-age filter on priors | `_study_move_query(study_uid) -> Dataset` (72); `_within_max_age(study_date, max_age_days, now) -> bool` (83) | none |
| `app/services/prior_service.py` | Priors tenant-scoped | `find_prior_studies(session, study_instance_uid, patient_id=None, *, tenant_id=None, include_all=False)` (37) | none |
| `app/api/v1/priors.py` | Anchor study must be visible (`require_study_access`); tenant scope passed | none | none |
| `app/services/query_service.py` | `include_all` default flipped to `False` on `study_query`, `series_query`, `instance_query` | `study_query(filters, tenant_id, include_all=False)` (162); `series_query(..., include_all=False)` (169); `instance_query(..., include_all=False)` (182) | none |
| `app/services/triage_service.py` | Decode failures logged at DEBUG with codec context (no logic change) | none | none |
| `app/api/v1/lifecycle.py` | All rule CRUD, run, archive, restore, purge require admin (purge was destructive role) | none | none |
| `app/db/models.py` | C10 tenant scoping; quarantine table; report addenda; quota counter | `class ReportAddendum(Base, TimestampMixin)` (1469); `class PatientIdentityConflict(Base, TimestampMixin)` (1492) | `patients.tenant_id` (203); `patients.patient_id` no longer unique (206); `UniqueConstraint(tenant_id, patient_id)` (228); `patient_identities.tenant_id` (299); identity unique index now (tenant_id, AA, identifier) (330); `patient_identity_conflicts` (new); `report_addenda` (new); `storage_quotas.used_bytes` (1150) |
| `migrations/env.py` | Target URL override via config attributes | none | none |
| `app/services/storage/` | Expected by storage_service.py:35, 43 and replication_service.py:39 | — | **missing** (B-01) |
| `app/services/storage_commit_service.py`, `app/services/worker.py`, `pix_pdq_service.py`, `lifecycle_service.py`, `warm_service.py`, `dicom/tls.py`, `scripting_service.py` | Not changed in this commit (checked for old bugs in section 2) | — | — |

Additional changed files found outside the scope list but relevant: `app/api/v1/dicomweb.py` (STOW stages, promotes and quota-reserves per part; `_validate_ingest_uids` 269; `_reject_cross_tenant_ingest` 249; `staged = None` once at 973), `app/api/v1/quota.py` (unchanged route order, see F-04), `app/core/config.py` (new settings above).

## 2. Prior findings status

| ID | Status | Evidence (HEAD) | Notes |
|---|---|---|---|
| B-01 | OPEN | `app/services/storage/` absent on disk and not tracked (`git ls-files app/services/storage` returns 0). `git check-ignore -v app/services/storage/__init__.py` gives `.gitignore:9:storage/`. Imported at `storage_service.py:35` (`from app.services.storage import get_storage_backend`) and `:43` (`from app.services.storage.backend import StorageError`), `replication_service.py:39`, `api/v1/storage.py:34`. | Because `receiver.py` and `indexing_service.py` import `storage_service`, the app import chain fails. Backend containment (`STORAGE_ALLOW_LEGACY_PATHS`) cannot be reviewed. |
| D-01 | PARTIAL | C-STORE error path no longer deletes an existing object: `receiver.py:123` and `:161` discard only the staged file; STOW: `dicomweb.py:1073-1074`. Check-then-insert still open: `indexing_service.py:295-311` (study), `:372-387` (series), `:402-421` (instance), no savepoint or IntegrityError handling. S3 writes the canonical key before commit: `storage_service.py:417-420`. | Residual: N-18. Race outcome: N-01, N-21. |
| D-02 | PARTIAL (key layer FIXED) | `_UID_OK` removed (no occurrence in `app/`). `_DOT_SEGMENTS` (`storage_service.py:75`) and `unsafe_reason` (`:85-86` rejects `..`/`.`; `:87-95` separators, control chars, trailing dot/space, device names). `validate_component` (`:101-119`) used by `instance_key` (`:125-130`), `thumbnail_key` (`:133-134`), `delete_study_files` (`:245-247`). | Backend-level containment unverifiable (package missing). Validation skips empty values: N-09. |
| D-03 | FIXED (residual gaps N-08, N-19, N-20) | Native-pixel path: `lossy_compression_service.py:217-281`; `out.compress(..., generate_instance_uid=False)` (`:253-257`); targets restricted to JPEG-LS NR (`4.81`) and JPEG 2000 lossy (`4.91`) (`:94-97`); baseline excluded (`:90-93`); eligibility gate (`:117-139`); geometry and decode checks (`:269`, `:274-279`); rescale tags preserved because `copy.deepcopy(ds)` (`:245`). | No 8-bit path remains. Production gate has no fidelity check (test-only, `tests/test_lossy_compression_safety.py:107-110`). |
| D-04 | PARTIAL | C-1 half FIXED: rewritten bytes are written, read back and verified before any unlink (`split_merge_service.py:286-347`; verifier `:261-283`). Still OPEN: `merge_studies` (`:157-228`) has no tenant or patient equality check, and `split_merge.py:48`, `:84` are role-only. `IssuerOfPatientID` not rewritten (`:231-258`). | New: N-06, N-07. |
| D-05 | OPEN | `pix_pdq_service.py:423-427`: `for study in source.studies: study.patient_id = target.id` then `session.delete(source)`. `Patient.studies` cascades `all, delete-orphan` (`models.py:232`, `:240`). | Escalated: N-10. |
| D-07 | OPEN | `tls.py:53-57`: on `(ssl.SSLError, OSError)` the existing cert and key are deleted (`cert.unlink`, `key.unlink`). `tls.py:61-62`: a single existing file is deleted when only one of the pair exists. | A passphrase-protected operator key is deleted and replaced by a self-signed one. |
| S-10 | OPEN (reachable by any authenticated user) | `scripting_service.py:107-130` `_load` calls `spec.loader.exec_module` (`:123`) on first load or mtime change; `list_scripts` (`:133-137`) loads every file. Called from `dashboard.py:108-112` `_script_count`, which feeds `dashboard.py:406` inside the `/stats` route (`:242-244`, dependency `get_current_user_any`). The scripts API list is admin-only (`api/v1/scripts.py:36`). | Fix: admin-only stats count, or read script metadata without importing modules. |
| S-11 | OPEN | (a) No calling-AE allowlist: `receiver.py:631` `AE(ae_title=...)`, no `require_calling_aet`. (b) C-FIND SERIES/INSTANCE discard parent UIDs: `receiver.py:320-321` and `:334-335` pass `None`; `_SERIES_KEYS` (`:191`) and `SERIES_FILTERS` (`query_service.py:31`) have no `StudyInstanceUID`. (c) C-GET/C-MOVE with empty or missing UID fall through to `instance_query(None, None, ...)` (`receiver.py:479-511`, `query_service.py:182-196`), returning everything in the caller's tenant scope. | Improvement: tenant scope now applied to C-GET and C-MOVE (`receiver.py:549-553`, `:598-604`). |
| S-12 | PARTIAL | FIXED: priors tenant-scoped (`priors.py` 37-78; `prior_service.py:95`); lifecycle actions admin-only (`api/v1/lifecycle.py` throughout); routing rule CRUD admin-only (`routing.py:88-150`). OPEN: split and merge (`split_merge.py:48`, `:84`; `split_merge_service.py:43-154`, `:157-228` no tenant check); routing evaluate (`routing.py:161-173` destructive role, no tenant check). | N-06, N-10, N-11. |
| F-01 | OPEN | `routing_service.py:49-56`: `evaluate_rules_for_study` selects the Study row from its own session. `indexing_service.py:340-350` calls `evaluate_and_route_study` from inside the still-uncommitted ingest transaction. `routing_service.py:55-56`: `if not study: return []`. | New study never matches. Source-AET fail-open: N-11. |
| F-02 | OPEN | `worker.py:234` `lifecycle_service.evaluate_and_run("lifecycle-worker")` passes the string as positional `rule_id`. `lifecycle_service.py:235-236` filters `StudyLifecycleRule.id == "lifecycle-worker"`, so no rules match and the sweep is a no-op. | Unchanged file. |
| F-03 | FIXED | `prefetch_service.py:190-203` calls `sender.send_move(host, port, ae, dest_ae_title, query=..., our_ae_title=...)`; defined at `sender.py:238-239` with matching signature; returns dict with `ok` (`:275` etc.). | Operational: the remote must have this archive's AE configured as a node to accept the C-MOVE destination. |
| F-04 | PARTIAL | Enforcement is wired: `receiver.py:106-111`, `dicomweb.py:1019-1023`, `lossy:344-349` call `stage_dataset_with_quota` → `reserve_quota`; refusal returns 0xA700 (`receiver.py:140-142`) or 0x0111 (`dicomweb.py:1051-1060`). OPEN: scoping and counter defects (N-04, N-05). Route shadowing still present: `api/v1/quota.py:145` `@router.get("/{quota_id}")` is declared before `:183` `@router.get("/status")`, so `GET /api/v1/quotas/status` matches `{quota_id}` (int) and returns 422. | `save_bytes_with_quota` and `save_dataset_with_quota` remain unused. |
| F-05 | OPEN | `hl7.py:748-770` `_handle_connection` only reads and calls `_handle_message`; no response is written (no MSA/ACK). `hl7.py:616` `# TODO: Send response back over MLLP connection`. MSA is built only for PDQ (`hl7.py:656`, `:680`) and not sent. | "ORM without OBR crashes connection" not re-verified in this pass. |
| F-06 | OPEN | `warm_service.py:211` `vol_ok = asyncio.run(_warm_vol_series())`. No `import asyncio` in `warm_service.py` (imports at `:22-34`). The NameError is caught by the surrounding broad `except` and logged at DEBUG. | Volume pre-warm never runs. |
| F-08 | OPEN | `worker.py:79-87`: the guarded UPDATE claims `ids`, but the re-select (`:86-87`) returns every row in `ids` still RUNNING, including rows another worker claimed, so jobs can be processed twice. `worker.py:367-383` `_recover_stuck_jobs` resets every RUNNING job to PENDING; called at `worker.py:424` at each worker start, which can reset live jobs of other workers. | Unchanged file. |
| F-10 | PARTIAL | Five model tables now created: `assigning_authorities`, `patient_identities` (`b4567890c1d2:73-120`); `organization_settings`, `report_templates`, `lossless_compression_rules` (`c5678901d2e3:31-62`). The BIGINT-PK-without-SQLite-variant defect is repeated in new migrations: `a3456789b0c1:25`, `b4567890c1d2:77`, `:90`, `:126` (models use the variant at `models.py:37`). | N-15. |
| F-14 | OPEN | `lifecycle_service.py:155-160` deletes objects before the DB commit; `purge_study` (`:217-226`) commits after deletes; `_tier_study` (`:108-145`) moves objects (`:125`) before `archive_study` commits (`:193-203`). `evaluate_and_run` loads every study (`:245`). `dashboard.py:934` deletes files (`delete_study_files`) before the DB delete. | No compensation on failure. Also no quota release (N-05). |
| 02-F-13 | PARTIAL | Demographics no longer overwritten: conflicts quarantined (`indexing_service.py:238-261`); backfill only fills empty fields (`:264-271`); HIS source is authoritative (`:238`, `:264`). OPEN: missing UIDs still become `"UNKNOWN"` (`:294`, `:371`, `:401`); `study_description` still overwritten on each instance (`:364-366`). | N-09. |

## 3. New findings

### RED

**N-01 RED. NULL-tenant patients and identities are no longer unique (duplicate MRN rows in the default bucket).**
- Evidence: `models.py:206` `patient_id ... nullable=False` (no unique). `models.py:228` `UniqueConstraint("tenant_id", "patient_id", name="uq_patients_tenant_patient_id")`. `models.py:330-337` identity unique index on `(tenant_id, assigning_authority_id, identifier)`. Old: `56d9f882c2e4:57` `sa.UniqueConstraint('patient_id')` (global).
- Code: `indexing_service.py:143-153` identity lookup `.scalar_one_or_none()`; `:166-174` legacy patient lookup `.scalar_one_or_none()`; `:190-204` `except IntegrityError` adopts the winner of a race.
- Problem: SQL UNIQUE treats NULLs as distinct (MySQL and SQLite). The `tenant_id IS NULL` bucket is the default for single-tenant installs, unmapped AEs and HL7. It has no DB uniqueness, so the IntegrityError race handler can never fire for it.
- Failure: two modalities send the first images of the same new MRN at the same time. Both pass step 2, both INSERT, and two `Patient` rows with NULL tenant now exist. From then on every ingest for that MRN raises `MultipleResultsFound` at `:166-174` or `:143-153`, so each C-STORE returns 0xC212 until someone deduplicates by hand. The same pattern exists in `hl7.py` `_ensure_patient` (~255-290) and `pix_pdq_service.py:340-341`.
- Fix: keep uniqueness for the legacy bucket. Options: a real sentinel tenant row for legacy; a unique index on `(COALESCE(tenant_id, 0), patient_id)` (MySQL functional index, SQLite expression index); or per-MRN serialization (`SELECT ... FOR UPDATE` on a lock row). Treat `MultipleResultsFound` as an explicit alarm.

**N-02 RED. On MySQL, the C10 migration never removes the global `UNIQUE(patient_id)`.**
- Evidence: origin `56d9f882c2e4:57` `sa.UniqueConstraint('patient_id')` (unnamed). `b4567890c1d2:150-155`: `with op.batch_alter_table("patients", copy_from=_legacy_patients_table()) as batch:` then `add_column`, `create_index`, `create_unique_constraint`. No drop operation anywhere in `upgrade()`. The comment at `:146-149` claims the rebuild removes the old constraint.
- Why: Alembic batch recreates the table only on SQLite (`recreate="auto"`). On MySQL the operations are emitted as ALTER statements, so the unnamed unique survives (MySQL names it after the column: `patient_id`; **verify**). `db_admin.schema_drift` (`db_admin.py:237-262`) compares tables and columns only, so it cannot see the surviving constraint.
- Failure: on a MySQL install migrated from `c0d1e2f3a4b5`, tenant B cannot store an MRN that tenant A already has. The INSERT fails on the old index. The IntegrityError handler (`indexing_service.py:195-204`) re-selects with the tenant filter, finds nothing, and re-raises. C10 tenant isolation of identifiers does not work on MySQL, the default backend (`config.py` comment: "MySQL 8 is the default").
- Fix: in `upgrade()`, when `bind.dialect.name != "sqlite"`, inspect `get_unique_constraints("patients")` and `get_indexes("patients")` and drop the unnamed unique on `patient_id`. Add a post-upgrade assertion. **Verify:** `SHOW INDEX FROM patients` after upgrading a copy of a `c0d1e2f3a4b5` MySQL database.

**N-03 RED. `b4567890c1d2` gives a TEXT column a literal default; MySQL 8 rejects that. Chain blocked on MySQL.**
- Evidence: `b4567890c1d2:132` `sa.Column("detail", sa.Text(), nullable=False, server_default="{}")`. Model `models.py:1514` `detail: Mapped[str] = mapped_column(Text, nullable=False, default="{}")` (Python-side only).
- Why: MySQL 8.0.13 and later accept a default on BLOB/TEXT only as an expression (`DEFAULT ('{}')`). A literal default raises ERROR 1101 ("BLOB, TEXT, GEOMETRY or JSON column ... can't have a default value"). **Verify** on the local MySQL 8 under `.tools/` (not executed).
- Failure: `init_db` runs the whole chain on an empty DB (`session.py:255-263`); on MySQL 8 that stops at `b4567890c1d2`. Every MySQL upgrade from `c0d1e2f3a4b5` stops there too.
- Fix: drop the `server_default` (the Python default already covers the model), or use `server_default=sa.text("('{}')")`.

**N-04 RED. Quota scope resolution treats `None` as a wildcard; tenant and backend quotas leak across scopes and can be bypassed.**
- Evidence: `quota_service.py:79-83` filters only when the argument is not None (`if tenant_id is not None: stmt.where(tenant_id == ...)`, `if backend_name is not None: stmt.where(storage_backend_name == ...)`). `reserve_quota` (`:298-333`) increments every returned quota; `release_quota` (`:340-357`) mirrors it.
- Three consequences:
  - (a) `tenant_id=None` returns every enabled quota of every tenant (no filter). Sources of `None`: unmapped AE (`receiver.py:106-108` → `tenant_service.py:49-64`, "None = unmapped"); ADMIN or unassigned STOW (`dicomweb.py:1021` and `:1032`); lossy with an unloaded study (`lossy:329-339`). One tenant at its limit refuses every unmapped ingest (0xA700, `receiver.py:140-142`), and all other tenants' counters are inflated.
  - (b) A tenant-wide quota (backend NULL) is excluded whenever a backend name is passed. Once a default backend is registered, `default_backend_name()` (`storage_service.py:145-156`) returns its name, and `resolve_study_backend` (`:159-173`) passes it into `receiver.py:98-101` and `dicomweb.py:1012-1014`. Tenant-wide quotas are then never enforced for new studies.
  - (c) Backend-scoped quotas (tenant NULL) are excluded for tenant writes (`tenant_id == T` does not match NULL).
- Failure: a site with "tenant A total 1 TB" and a registered default backend ingests without limit for tenant A. An admin STOW fills tenant B's quota.
- Fix: define the applicable set explicitly, treating `None` as the NULL bucket (`IS NULL`), not a wildcard, and always resolve tenant-wide and backend-wide quotas for each write. Add a 3x3 test matrix. Current tests (`tests/test_quota_and_atomic_ingest.py`) call with `backend=None` only.

**N-05 RED. The quota counter only grows: nothing decrements it on delete, split/merge, tier move or re-import, and recompute never runs.**
- Evidence:
  - `release_quota` runs only inside `StagedObject.discard()` (`storage_service.py:375-379`).
  - Deletes do not touch quota: `dashboard.py:934` (`delete_study_files`), `lifecycle_service.py:148-169` (`_purge_study`), `:217-226` (`purge_study`), `split_merge_service.py:144` and `:219` (cascade deletes of source studies), `storage_service.py:531-578` (`move_study_to_backend` does not adjust per-backend quotas).
  - Re-import is charged in full: `receiver.py:109-111` and `dicomweb.py:1019-1023` pass no `charge_bytes`, so `charge = len(data)` (`storage_service.py:407`). Promote then overwrites the object and `upsert_instance` sets the new size (`indexing_service.py:423-426`).
  - Lossy shrink: `lossy:348` charges `max(0, len(new PixelData) - old_size)` (encapsulated bytes minus full file size). The instance size then drops (`:357`), so the counter ends above the real sum.
  - `recompute_used_bytes` (`quota_service.py:380-398`) has zero callers (grep of `app/`). For a tenant id, `q not in quotas` (`:387`) compares ORM objects loaded in different sessions with no `__eq__` (`models.py` has none), so it skips every quota. Even if repaired, `SUM(instances)` excludes reservations still in flight.
- Failure: C-STORE retransmission after a timeout charges the same object again every time. Deletions never return space. Eventually every C-STORE for that tenant returns 0xA700 and every STOW part returns 0x0111 (`dicomweb.py:1051-1060`) with no real capacity in use.
- Fix: adjust `used_bytes` in the same transaction as each delete or replace (new size minus old size, or release). For re-import pass `charge_bytes = new - old` (look up the old size before the upsert). Repair recompute: load quotas in the same session and compare by id, under the row lock, and count committed plus reserved bytes. Add tests through the real C-STORE and STOW paths; the current test (`tests/test_quota_and_atomic_ingest.py:112`) calls the helper with an explicit `charge_bytes`.

**N-06 RED. Split creates the new study with no tenant, moving series out of their tenant.**
- Evidence: `split_merge_service.py:87-96` `new_study = Study(patient_id=..., study_instance_uid=new_study_uid, ...)` has no `tenant_id`. Nothing sets it later (`:153-154` set only the patient).
- Failure: a tenant-A user splits series out of a tenant-A study. The moved series now sit in a `tenant_id IS NULL` study: invisible to tenant A (`query_service.py:146-156`), visible to unscoped readers and to unmapped AEs (C-GET/C-MOVE, `receiver.py:598-604`).
- Fix: `tenant_id=source_study.tenant_id` on the new `Study`. Add a cross-tenant split test.

**N-07 RED. Split and merge unlink superseded objects before the transaction commits, so a commit failure destroys the study's pixel data.**
- Evidence: `_rewrite_and_relocate` flushes at `split_merge_service.py:359` and then unlinks superseded sources at `:376-383`. The DB commit happens later in the API: `split_merge.py:64` and `:97` (`await db.commit()`). The docstring (`:15-17`) says unlink happens after the flush, which is not the same as after commit.
- Failure: the flush succeeds, COMMIT fails (MySQL lock-wait timeout, deadlock, dropped connection). The DB rolls back to the old keys, but the old objects are already deleted. The study's images are unreadable and cannot be recovered.
- Fix: defer unlinks until after commit. Return a post-commit action list from the service and execute it in the endpoint after `commit()`, or record tombstones and sweep later. Apply the same to merge.

**N-08 RED. Lossy: the canonical object is replaced before the DB commit; a later failure leaves the originals destroyed and metadata wrong.**
- Evidence: `lossy_compression_service.py:344-354` `staged.promote()` overwrites the canonical key. `:356-360` updates the instance and flushes, with no commit. `evaluate_and_run_lossy_compression` (`:430-489`) processes the whole candidate set and flushes once (`:491`). The only commit is in the API after the run (`api/v1/lossy_compression.py:179-180`).
- Failure: 300 instances are promoted. The final commit (or any later statement) fails and rolls back. The DB still says uncompressed with the old size, but the files are now JPEG-LS or J2K. The uncompressed originals are gone (no versioning). The quota reservation for the lossy delta was already taken.
- Fix: write the lossy object to a new key, commit per instance or per batch with the new transfer syntax and size, and only then swap the pointer and delete the old object after commit (the post-commit pattern from N-07). Keep originals until the commit succeeds.

**N-09 RED. Missing UIDs collapse onto one storage key and one DB row, causing silent overwrite.**
- Evidence: `storage_service.py:111-119` in `validate_component`: `text = str(...).strip()`, then `if not text: if fallback: return f"{fallback}_{hashlib.sha1(text.encode(...)).hexdigest()[:20]}"`. `text` is always `""` here, so the suffix is the constant `da39a3ee5e6b4b0d3255` (sha1 of empty string). Validation skips empty values: `receiver.py:96-99` (`if _value not in (None, ""):`) and `dicomweb.py:280-285` ("Missing UIDs keep the pre-existing deterministic fallback", comment at `:283`). DB side: `indexing_service.py:294` (study `"UNKNOWN"`), `:371` (series), `:401` (instance).
- Failure: two different instances with no SOPInstanceUID in the same series both map to `.../sop_da39a3ee5e6b4b0d3255.dcm`. The second promote overwrites the first's pixels, and `upsert_instance` (`indexing_service.py:403-426`) finds the existing "UNKNOWN" row and repoints it. Unrelated studies missing StudyInstanceUID merge into one "UNKNOWN" study.
- Trigger: malformed or empty UIDs from a modality or STOW client. Reachability depends on the calling-AE policy (S-11a).
- Fix: reject empty StudyInstanceUID, SeriesInstanceUID and SOPInstanceUID at ingest (C-STORE 0xC000, STOW 400). If a fallback is wanted, derive it from the dataset content hash (the sha256 is already computed), never from the empty string. Fix `validate_component` accordingly.

**N-10 RED. PIX/HL7 feeds match patients by MRN only across tenants; A40 can merge across institutions and deletes studies (D-05 still live).**
- Evidence: `pix_pdq_service.py:340-341` (A04/A08 lookup by `Patient.patient_id` only, `scalar_one_or_none`). `:397-401` (A40 source and target by MRN only). `:423-427` (`for study in source.studies: study.patient_id = target.id` then `session.delete(source)`; `Patient.studies` cascades, `models.py:232-240`). `hl7.py` `_ensure_patient` (~255-290) is tenant-NULL only.
- Failure (cross-tenant): patient "456" exists only in tenant B and "123" only in tenant A. An A40 "456 into 123" moves B's identities and studies into A. If both MRNs exist in two tenants, `MultipleResultsFound` is raised instead. Failure (D-05): the studies collection is still loaded, so the cascade deletes the moved studies and their series and instance rows on flush while the files are orphaned.
- Fix: scope PIX lookups by tenant and assigning authority (C10 model). Refuse A40 across tenants. Move studies by explicit UPDATE and remove them from the source collection before deleting, or delete only an emptied source (`passive_deletes=True`). Add tests.

### YELLOW

**N-11 YELLOW. Routing: ingest-time routing still cannot match; manual evaluation fails open on source AE; evaluate endpoint has no tenant check.**
- `routing_service.py:60` `source_aet = meta.get("source_aet") if meta else ""`; `:88-89` `if rule.source_aet and source_aet:`. When the AE is empty the restriction is skipped, so the rule matches every source AE. `parse_metadata` never sets `source_aet` (`parser.py` `_META_FIELDS`), so even a fixed F-01 path would fail open.
- `routing.py:161-173` evaluate requires only the destructive role and calls `evaluate_and_route_study(study_uid)` (`routing_service.py:233-248`) with no tenant check. A tenant-scoped destructive-role user can dispatch another tenant's study to any node.
- Fix: fail closed when a rule requires a source AE and none is known. Tenant-check the study before evaluation. Fix F-01 by evaluating with the ingest session after flush, or by passing the Study into the evaluator.

**N-12 YELLOW. STOW: a stale `staged` from an earlier part is discarded when a later part fails before staging; partial-batch behaviour contradicts its comment.**
- `dicomweb.py:973` `staged = None` (once per request, before the loop). `:1019` creates a new staged object per part; after a successful part `staged` still refers to it. A later part that fails before `:1019` (`dataset_from_bytes` at `:994`, `_validate_ingest_uids` at `:997`, cross-tenant check at `:999`) reaches the generic handler `:1068-1075`: `if staged is not None: await db.rollback(); staged.discard(); staged = None`. `discard()` releases the reservation of the previous, already committed and promoted part (`storage_service.py:375-379`), while that object remains published.
- `dicomweb.py:1061-1067`: `InvalidStorageKey` and `CrossTenantIngestError` raise HTTP 400 for the whole request, but earlier parts were already committed and promoted. The comment at `:1062-1063` says "refuse the whole STOW rather than store anything", which is not what happens.
- Fix: reset `staged = None` at the top of each part. Either pre-validate every part before storing any, or document partial success and return per-part results.

**N-13 YELLOW. A promote failure after commit leaves a durable row pointing at a missing object and releases the quota.**
- `receiver.py:114-122` commits the index transaction on exit of `session.begin()`; `:126` `staged.promote()`. A failure goes to the outer `except Exception` (`:153-166`), which calls `staged.discard()` (`:161`) and releases the reservation. `dicomweb.py:1044-1045` then `:1073-1075` do the same.
- Failure: disk full or an S3 timeout during promote after commit. The instance row exists, the canonical object does not, and the quota is released. The modality receives 0xC212 and may retry or give up; if it gives up, the row dangles.
- Fix: never release quota after commit. Retry promote with backoff, or write a pending-promote marker that a sweeper resolves. Alternatively promote before commit and compensate by deleting the new canonical object if the commit fails (acceptable because of staging keys).

**N-14 YELLOW. Migration hygiene: historical migrations import live models, and downgrades drop tables they did not create.**
- `c5678901d2e3:39` `from app.db.models import Base` and `:49` `Base.metadata.tables[name].create(bind, checkfirst=True)`. Future model changes will change this historical migration; later migrations that add columns will then fail as "already exists".
- `c5678901d2e3:65-75` downgrade drops `lossless_compression_rules`, `organization_settings`, `report_templates` unconditionally, even when upgrade skipped creating them because they pre-existed (data loss on downgrade).
- `b4567890c1d2:202-223` downgrade drops `patient_identity_conflicts`, `patient_identities`, `assigning_authorities` unconditionally. Same pattern.
- Fix: freeze the DDL inside each migration (explicit `sa.Table` definitions), and in downgrade drop only what this revision created (record existence at upgrade time).

**N-15 YELLOW. SQLite: new migrations create non-autoincrement primary keys and drop indexes during the `patients` rebuild.**
- PK: `a3456789b0c1:25` `sa.Column("id", sa.BigInteger(), primary_key=True)`; `b4567890c1d2:77`, `:90`, `:126` the same. Models use `BigIntPK = BigInteger().with_variant(Integer, "sqlite")` (`models.py:37`). On SQLite a BIGINT PRIMARY KEY is not a rowid alias, so an INSERT without an explicit id fails with NOT NULL. Affected tables: `report_addenda`, `assigning_authorities`, `patient_identities`, `patient_identity_conflicts`.
- Indexes: `b3456789c0d1:16-17` creates `ix_patients_name_id` (patient_name, patient_id) and `ix_patients_birth_date`. `_legacy_patients_table()` (`b4567890c1d2:43-66`) lists only `ix_patients_patient_name` and `ix_patients_source`. The SQLite recreate drops the others. The docstring at `:49-50` says "The existing indexes are listed so the rebuild keeps them" and is false for these two.
- Fix: use `sa.BigInteger().with_variant(sa.Integer(), "sqlite")` in the migrations; add the two missing indexes to `_legacy_patients_table`. **Verify:** `PRAGMA index_list(patients)` and an INSERT into `report_addenda` on a migrated SQLite copy.

**N-16 YELLOW. `worker.py:420` runs `Base.metadata.create_all` outside Alembic.**
- `worker.py:416-422`: `from app.db.models import Base` and `Base.metadata.create_all(sync_engine)` inside a try that logs and continues. This contradicts `session.py:237-243`: "create_all is now used only by the test fixtures."
- Failure: a standalone worker starts before the app on a `c0d1e2f3a4b5` database. `create_all` adds `report_addenda`, `patient_identities` and other new tables but does not add `patients.tenant_id`. The app's `init_db` sees a non-empty DB and skips migrations (`session.py:264-276`). `alembic upgrade head` fails at `a3456789b0c1` with "already exists" (`db_admin.py:333-344` raises RuntimeError), and `stamp_head` refuses because `schema_drift` still reports `patients.tenant_id` (`db_admin.py:352-368`). Manual reconciliation required.
- Fix: remove `create_all` from the worker.

**N-17 YELLOW. Migration advisory lock is ineffective and not enforced (db_admin.py, adjacent to scope).**
- `db_admin.py:275-291` `_advisory_lock` runs `GET_LOCK` inside `with engine.connect()`. The engine is `NullPool` (`:327`), so the connection closes when the block exits and MySQL releases the lock at that point. `_release_advisory_lock` (`:294-301`) runs on another connection.
- When `got` is 0 the function returns False, but `run_migrations` continues (`:330-331`).
- Fix: hold a dedicated connection for the duration of `command.upgrade`, and abort if the lock is not acquired.

**N-18 YELLOW. S3 path writes the canonical object before the DB commit (D-01 residual).**
- `storage_service.py:413-420`: when the backend has no `stage_bytes`, `bk.save_bytes(key, data)` writes the canonical key immediately. `StagedObject.discard()` (`:366-379`) cannot restore an overwritten object, so a rollback leaves the old object replaced.
- Fix: for S3, stage to a temporary key and copy to canonical after commit, or rely on bucket versioning and restore on discard.

**N-19 YELLOW. Lossy publish gate lacks a fidelity check; sign and rescale are not compared; the modality filter fails open; the charge units are inconsistent.**
- The gate is geometry and decode only (`lossy_compression_service.py:269-279`). `_codec_kwargs` (`:181`) maps the default quality 85 to `jls_error = 2`, i.e. up to plus or minus 2 grey levels on 16-bit data. Nothing checks `max|orig - decoded| <= jls_error`. `PixelRepresentation` (signedness) is not compared, so a sign flip would pass the geometry check.
- Fail-open filter: `:462-468` `if rule.modality and inst.series.modality:` skips the check when the series has no modality, so the rule applies. Same for body part.
- Charge: `:348` mixes encapsulated pixel bytes with the full file size.
- `tests/test_lossy_compression_safety.py:107-110` checks fidelity in tests, not in the production gate.
- Fix: compare the decoded array against the original within the codec bound (JPEG-LS: max abs error at most `jls_error`; J2K: PSNR floor). Assert `PixelRepresentation`, `RescaleSlope` and `RescaleIntercept` are unchanged. Require a non-null match for modality and body part. Charge `new_file_size - old_file_size`.

**N-20 YELLOW. The lossy run blocks the event loop and loads the whole candidate set at once.**
- `transcode_instance_lossy` is `async` but calls synchronous work directly: `read_dataset` (`lossy:316`), `transcode_dataset_lossy` (`:322`, pydicom encode and decode, CPU-bound), `stage_dataset_with_quota` (`:344`), `promote` (`:351`), `size` (`:357`). Compare `split_merge_service.py:323-326`, which correctly uses `asyncio.to_thread`.
- The API runs the whole batch in one request (`api/v1/lossy_compression.py:169-181`). The candidate query has no LIMIT and eager-loads (`lossy:420-430`) and is materialised with `.all()` (`:430`).
- Failure: a large run freezes the event loop, stalling every HTTP request, and holds one transaction plus all ORM objects in memory.
- Fix: run per-instance work via `asyncio.to_thread`, iterate by id range with `yield_per`, commit per batch, or move the run to the worker queue.

**N-21 YELLOW. Priors (C10) ignore the assigning authority.**
- `prior_service.py:78-90` matches `Patient.patient_id == target_patient_id` only. C10 defines identity as (tenant, authority, MRN) (`indexing_service.py:59-69`, `:143-153`). The explicit comment at `indexing_service.py:159-165` says a different authority is a different person.
- Failure: within one tenant, the same MRN string under two authorities is merged into one priors list, so radiologists see another person's priors.
- Fix: restrict to patients linked through `PatientIdentity` with the anchor's authority, or document the MRN-only semantics.

**N-22 YELLOW. Side effects fire before commit, and sub-indexers can break the ingest transaction.**
- `indexing_service.py:321-339`: `webhook_fire("StudyReceived")` and `script_fire(...)` run inside the uncommitted ingest transaction. A rollback leaves the external notification sent.
- `indexing_service.py:515-561`: GSPS, KIN and RDSR sub-indexers are wrapped in `try/except Exception` without `begin_nested()`. A DB error inside one leaves the session failed, and the final `await session.flush()` (`:563`) raises, so an ingest that could succeed fails with 0xC212.
- Fix: emit events after commit. Wrap each sub-indexer in `async with session.begin_nested()`.

### GREEN

**N-23 GREEN.** `indexing_service.py:96-106` `_ensure_authority` creates `AssigningAuthority` rows from client-supplied IssuerOfPatientID without bounds. A concurrent duplicate `oid` raises an unhandled IntegrityError at `:105`. Fix: savepoint with retry, and validate length and format.

**N-24 GREEN.** `api/v1/split_merge.py` (~63-65 and ~96-99) returns raw exception text: `f"Split failed: {exc}"`, `f"Merge failed: {exc}"`. Fix: generic message; log the detail.

**N-25 GREEN.** `split_merge_service.py:214-219` deletes `src_study` before the re-parented series are flushed (flush at `:221`). This relies on autoflush before the cascade loads `series_list` (`models.py:389-390`, `cascade="all, delete-orphan"`). Fix: explicit `await session.flush()` before the delete.

**N-26 GREEN.** `.workbuddy-ai/backups-phase2/` was added in `5a32aa4` and includes copies of `app/api/v1/volume.py` and `app/services/volume_service.py`. Duplicate app code in the repo, likely unintended. Fix: remove or ignore.

**N-27 GREEN.** `receiver.py:149-152` comment says the generic handler removes the object for `CrossTenantIngestError`. The `except CrossTenantIngestError` branch re-raises before the generic cleanup, but the inner handler at `:122-124` already discards, so behaviour is correct and the comment is wrong.

**N-28 GREEN.** `quota_service.py:75-76` says results are "strictest first". `order_by(max_bytes asc)` puts 0 (unlimited) first, and no caller uses the order.

**N-29 GREEN.** C-MOVE and C-GET materialise every matching dataset before yielding (`receiver.py:557-563`, `:604-612`). Memory grows with study size. Scalability issue.

**N-30 GREEN.** `api/v1/priors.py` (~55-60) returns 200 `[]` for an unknown anchor UID and 404 for a UID in another tenant, so the response reveals whether another tenant's study UID exists.

## 4. Documentation deltas

- **C-STORE ingest (receiver.py:85-166), new order:** (1) validate present UIDs with `validate_component` (`:96-99`); (2) resolve backend via `resolve_study_backend` (`:101-104`); (3) resolve tenant from the calling AE, unmapped = None (`:106-108`); (4) stage and reserve quota for the full object size (`:109-111`); (5) parse metadata; (6) index in one DB transaction (`_index_async`, `:67-82`, `:114-122`); on any failure discard the staged file and release the reservation (`:123`, `:161`); (7) promote to the canonical key (`:126`); (8) storage-commit hook; return 0x0000.
- **C-STORE statuses:** quota refused 0xA700 (`:140-142`); invalid identifier 0xC000 (`:144-148`); cross-tenant append re-raised (`:149-152`); other failures 0xC212 (`:153-166`).
- **STOW-RS (api/v1/dicomweb.py ~941-1100), per part:** `_validate_ingest_uids` (`:269-285`); cross-tenant precheck (`:249-266`); stage; index; `db.commit()` (`:1044`); promote (`:1045`). Quota refusal is recorded per part as 0x0111 and surfaced as HTTP 507 when the batch completes (`:1051-1060`). Invalid identifier or cross-tenant raises HTTP 400 for the whole request (`:1061-1067`); earlier parts remain stored (N-12).
- **Patient identity (C10):** identity is (tenant, assigning authority, MRN). Missing PatientID becomes `UNIDENTIFIED-<StudyInstanceUID>` (`indexing_service.py:127-134`). Demographic conflicts are quarantined in `patient_identity_conflicts` and never overwrite validated values (`:238-261`). NULL tenant is the legacy bucket and is not unique in the DB (N-01).
- **Quota behaviour as documented vs. actual:** the module docstring (`quota_service.py:12-19`) says `commit_quota` is a no-op and `recompute_used_bytes` reconciles drift. Actual: no commit function exists; recompute has no callers and is broken for tenant scope (N-05). Document that the counter currently only increases.
- **Routing:** rule create, update and delete require admin (`routing.py:88-150`); evaluate requires destructive role (`:161-173`). Auto-route on ingest does not match new studies (F-01). Remote-S3 temp files are removed (`routing_service.py:136-175`).
- **Lossy (LOSSY_COMPRESSION_ENABLED=false by default, config.py:161):** targets are JPEG-LS near-lossless (1.2.840.10008.1.2.4.81, default in `models.py:1043` and the API) and JPEG 2000 lossy (...4.91). JPEG Baseline (...4.50) is rejected (`lossy:90-93`). Eligible sources: uncompressed, or verified-lossless compressed (`:117-139`). Gates: encoder must exist (`:152-168`), geometry preserved (`:269`), output must decode (`:274-279`). Quota charge is a delta (`:348`). Trigger endpoint requires destructive role (`api/v1/lossy_compression.py:169-173`).
- **Migrations:** head `d6789012e3f4`. Chain `c0d1e2f3a4b5` → `a3456789b0c1` → `b4567890c1d2` → `c5678901d2e3` → `d6789012e3f4`. New tables: `report_addenda`, `patient_identities`, `assigning_authorities`, `patient_identity_conflicts`, `lossless_compression_rules`, `organization_settings`, `report_templates`. New columns: `patients.tenant_id`, `patient_identities.tenant_id`, `storage_quotas.used_bytes`, `report_addenda.updated_at`. New constraints: `uq_patients_tenant_patient_id` (added, but the old global unique survives on MySQL, N-02), `ix_patient_identities_tenant_aa_identifier`. Known gaps: N-02, N-03, N-14, N-15.
- **Authorization changes (documentation drift risk):** lifecycle run, archive, restore and purge are admin-only (`api/v1/lifecycle.py`); split, merge and lossy endpoints are destructive-role; routing rule CRUD is admin-only; priors tenant-scoped.

## 5. Open questions and verification to run

1. **Confirm the MySQL findings (N-02, N-03, N-15) by running the chain.** Copy a `c0d1e2f3a4b5` database for MySQL 8 (local `.tools/`) and SQLite, run `alembic upgrade head`, then check `SHOW INDEX FROM patients`, `SHOW CREATE TABLE patient_identity_conflicts`, `PRAGMA index_list(patients)`, and an INSERT into `report_addenda` on SQLite. I did not run these.
2. **Legacy bucket semantics.** Is `tenant_id IS NULL` meant to be a real bucket with uniqueness (N-01), or a wildcard? The current code treats it as both.
3. **Quota semantics.** Should `used_bytes` be committed-only or committed plus reserved? Who owns reconciliation (N-05)? Should a delete decrement quota, or is the counter meant to be a cumulative-ingest budget?
4. **STOW partial-batch contract.** Is a 400 for one bad part intended to abort the request after earlier parts were stored (N-12)? The comment says the opposite.
5. **Lossy clinical acceptance.** Is an error of plus or minus 2 grey levels (JPEG-LS at default quality 85) acceptable for 16-bit CT, and is a production fidelity gate required (N-19)?
6. **Missing storage package.** Is `app/services/storage/` intentionally excluded from the repo (the `.gitignore:9` `storage/` rule matches it)? If it exists elsewhere, B-01 is a repo-hygiene problem; if not, the app cannot run.
7. **Worker `create_all`.** Is it kept intentionally for standalone workers (N-16)?
8. **PIX and HL7 cross-tenant reachability.** Can a real feed send A40 across tenants in production (N-10)?
9. **`.workbuddy-ai/backups-phase2/`.** Should these copies be in the repo (N-26)?
10. **Tests.** `tests/test_quota_and_atomic_ingest.py` exercises the helper with explicit `charge_bytes` and backend=None only. Nothing I found exercises production reimport charge, tenant-wide or backend-wide quota combinations, or delete-time decrement. Recommend adding these before relying on the quota path.
