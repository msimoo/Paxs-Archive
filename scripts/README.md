# PAXS custom server scripts (roadmap #43)

Drop `.py` files here to run custom logic on server events — **no restart
needed** (hot reload via mtime). Admin → **Scripts** lists, toggles, reloads
and test-runs them; `{name}.py.disabled` marker files disable a script.

## Script contract

```python
EVENTS = ["StudyReceived"]          # optional: restrict subscriptions

def handle(event, ctx): ...         # catch-all handler (optional)
def on_study_received(ctx): ...     # per-event handler (event lower-cased)
```

If both `handle` and `on_<event>` are defined, **both run** (handle first,
then the per-event handler).

> ⚠️ `StudyReceived` fires from the ingest transaction *before* it commits, so
> `ctx.study()` may not yet see the triggering study (or may hit a DB lock if
> the ingest is still writing). Design scripts to tolerate a `None` study;
> re-read on a later event or query on demand for fresh data.

`ctx` is a `ScriptContext`:

| Member | What it does |
| --- | --- |
| `ctx.log` | per-script logger (`paxs.scripts.<name>`) |
| `ctx.event`, `ctx.payload` | the event name and its data dict |
| `ctx.study(uid)` | read-only patient + study + series summary dict |
| `ctx.instances(uid)` | instance metadata rows for a study |
| `ctx.read_bytes(key)` | object bytes from the storage backend |
| `ctx.http_post(url, payload)` | outbound JSON POST, returns HTTP status |
| `ctx.fire(event, **data)` | re-dispatch to *other* scripts (chaining) |

## Events

* `StudyReceived` — `study_uid, patient_id, patient_name, accession_number, study_description`
* `StudyDeleted` — `study_uid, performed_by`
* `CriticalResultFlagged` — `study_instance_uid, patient_id, finding_level, description, reported_by`

## Notes

* Scripts are **trusted admin code** — they run in-process with the server's
  privileges (mirrors Orthanc Lua). Never place untrusted code here.
* A script that raises never breaks ingestion or other scripts; its error is
  logged under `paxs.scripts`.
* `00_demo.py` is a safe example; delete it whenever you like.
