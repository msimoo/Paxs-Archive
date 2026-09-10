# PAXS Archive — Browser Console / Network / Snapshot Audit

> Date: 2026-08-18 · Method: live browser session against `http://localhost:8000`
> (MySQL backend, auth enabled) — console, network, DOM snapshots, screenshots.
> Scope: find and fix real issues **without changing core features or the
> code lifecycle**. Server was restarted after each fix; fixes verified live.

---

## Summary

| Severity | Count | Status |
|----------|-------|--------|
| Critical | 1 | ✅ Fixed |
| High     | 1 | ✅ Fixed |
| Medium   | 1 | ✅ Fixed |
| Observations (not code bugs) | 3 | — documented |
| Regressions introduced by fixes | 0 | verified (53 tests pass) |

---

## BUG-01 — CRITICAL: Registration returns HTTP 500 (MissingGreenlet)

### Symptom (browser)
Signing up on `/login` → **HTTP 500** ("HTTP 500" toast). Reproduction was
deterministic: `POST /api/v1/auth/register` → 500 every time.

### Root cause
`app/api/v1/auth.py` — `register()`:
```python
db.add(user)
await db.flush()
await write_audit(...)          # adds auth_audit_log row (pending)
... _auth_response(user, ...)   # → _user_dict(user) → user.created_at
```
`User.created_at` is `server_default=func.now()`. After `db.flush()`,
SQLAlchemy does **not** know the generated value on MySQL (MySQL has no
`INSERT..RETURNING`; SQLite does, which is why the test suite never caught
this). Accessing `user.created_at` triggered a lazy refresh through the sync
path inside the async handler → `sqlalchemy.exc.MissingGreenlet`, and the
subsequent `session.rollback()` in `get_db` hit the same error (broken
session state).

```
[SQL: INSERT INTO auth_audit_log (user_id, action, ip_address, user_agent, details) ...]
sqlalchemy.exc.MissingGreenlet: greenlet_spawn has not been called; can't call await_only() here.
```

### Fix (minimal, same pattern already used in `app/api/v1/users.py`)
```python
db.add(user)
await db.flush()
# MySQL has no INSERT..RETURNING: server-generated columns (created_at)
# are unloaded after the flush and accessing them triggers a sync lazy
# refresh -> MissingGreenlet in the async handler. Reload inside the
# async context (same pattern as users.py admin create).
await db.refresh(user)
await write_audit(db, user.id, "login_success", request, {"via": "register"})
```

### Verification
`POST /api/v1/auth/register` → **201** with `access_token`, `user` (incl.
`created_at`) and `redirect_to`. Failed pre-fix attempts left no orphaned
rows (MySQL rolled them back on restart). `test_auth.py` still passes.

---

## BUG-02 — HIGH: OHIF viewer cannot decode any image (WASM blocked by CSP)

### Symptom (browser console)
The viewer loaded the study, fetched frames over WADO-RS (`.../frames/1 →
200`), but every image failed to decode and the Error Boundary showed
"Something went wrong":
```
[error] wasm streaming compile failed: CompileError: WebAssembly.instantiateStreaming():
Refused to compile or instantiate WebAssembly module because 'unsafe-eval' is not an
allowed source of script in the following Content Security Policy directive:
"script-src 'self' 'unsafe-inline'"
[error] Error executing method 'decodeTask' on worker 'dicomImageLoader': RuntimeError: Aborted(...)
[warning] Couldn't decode ...
```

### Root cause
The global security-headers CSP (auth plan §4.2) in `app/main.py` allowed
only `script-src 'self' 'unsafe-inline'`. The OHIF DICOM image decoder
compiles **WebAssembly** in a worker; CSP requires `'wasm-unsafe-eval'`
for WASM compilation. Result: the archive's central feature — viewing
images — was completely broken under the auth gate.

### Fix (minimal, standard practice)
`app/main.py` `_SECURITY_HEADERS`:
```python
"script-src 'self' 'unsafe-inline' 'wasm-unsafe-eval'; "
```
(`'wasm-unsafe-eval'` only permits WASM compilation — it does **not** allow
JavaScript `eval()`.)

### Verification
- `curl -I /viewer` and `/viewer/viewer?...` now return the CSP with
  `'wasm-unsafe-eval'`.
- In-page `WebAssembly.compile(...)` → **WASM-COMPILE-OK** under the new
  policy.
- The viewer renders the study: toolbar, series thumbnails, viewport Canvas,
  "I: 1 (1/2)".
- Note: an open browser tab keeps serving the pre-restart bundle from its
  HTTP cache (old CSP in the cached worker response). **A hard refresh
  (Ctrl+Shift+R) after restart clears it** — permanent for new sessions.

---

## BUG-03 — MEDIUM: Google Fonts blocked by CSP (console error)

### Symptom (browser console)
```
Refused to load the stylesheet 'https://fonts.googleapis.com/css2?...'
because it violates the following Content Security Policy directive:
"style-src 'self' 'unsafe-inline'"
```
The UI links Inter + JetBrains Mono from Google Fonts but the CSP blocked
them → font fallback, plus a console error on every page load.

### Fix (minimal)
`app/main.py` `_SECURITY_HEADERS`:
```python
"style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
"font-src 'self' data: https://fonts.gstatic.com; "
```

### Verification
Console clean after restart; network shows `fonts.gstatic.com/*.woff2 →
200`. No other CSP origins were loosened.

---

## Observations (NOT code bugs — left unchanged)

1. **`PatientID` = "BRAIN +PA"** for study
   `1.3.12.2.1107.5.1.7.111265.30000026072810492146500000381`: the raw DICOM
   `(0010,0020)` contains the study description. The archive correctly
   stores/returns what the source HIS sent — data-quality issue at the
   source, not an archive bug. Affects the study list and the report modal's
   Patient ID field.
2. **Admin console "Overview" tab is visible to PHYSICIANs** — by design:
   it renders `/api/v1/health` + `/stats`, the same shared dashboard data.
   All sensitive endpoints are correctly gated (verified: `/api/v1/users` →
   403 for PHYSICIAN). Settings fields render 🔒 read-only for non-admins.
3. **Study list "12459"** on one row is that study's **accession number**
   (column renders empty when absent), not a rendering bug.
4. Upload buttons open native file/folder dialogs (untestable via the a11y
   snapshot); the colored upload panel and its flow were verified in earlier
   sessions and are unaffected by these fixes.

---

## Verification after fixes

| Check | Result |
|-------|--------|
| `pytest tests/test_auth.py test_sessions.py test_login_gate.py test_viewer.py` | **53 passed** |
| `POST /api/v1/auth/register` (live) | **201** |
| `WebAssembly.compile` under new CSP (live) | **OK** |
| Console on study list / dashboard / settings / admin | clean |
| Fonts load (network) | `fonts.gstatic.com → 200` |
| RBAC (PHYSICIAN → `/api/v1/users`) | **403** (correct) |

## Files changed

| File | Change |
|------|--------|
| `app/api/v1/auth.py` | `await db.refresh(user)` after flush in `register()` (BUG-01) |
| `app/main.py` | CSP: `'wasm-unsafe-eval'` in script-src (BUG-02); Google Fonts origins in style-src/font-src (BUG-03) |

## Reminder

Two throwaway PHYSICIAN accounts were created during testing
(`fresh_ok_1`, `curl_test_2`); delete them from Admin → Users if unwanted.
