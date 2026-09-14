# Load Balancing — HAProxy + stateless nodes (roadmap #47)

PAXS scales horizontally: run N identical nodes behind HAProxy. Each node is
**stateless** — any request may be served by any node, and a node can die
without losing data or sessions.

## What makes a node stateless

| Concern | Where state lives | Stateless? |
| --- | --- | --- |
| Database | Shared MySQL/Postgres (`DATABASE_URL`) | ✅ every node reads/writes the same DB |
| DICOM objects | Shared object storage — S3/MinIO **named storage backend** (Admin → Storage) | ✅ objects are on S3, not a node-local disk |
| Auth sessions | JWT (stateless bearer token, `Authorization` header) | ✅ no sticky sessions needed |
| Background work | Compression/triage/lifecycle/routing/DR sweeps run in **every** node's worker; they are idempotent and DB-serialized by status flags | ✅ duplicates are safe |
| DICOM receiver | Each node binds `:10400` (and HL7 `:2575`); HAProxy TCP-balances associations | ✅ no single receiver |
| Thumbnails / temp files | Written next to objects on shared storage / `tempfile` | ✅ |

> ⚠️ **Do not** point nodes at SQLite or a local `STORAGE_ROOT`: that state would
> be node-local and invisible to the other nodes. Production requires the
> shared MySQL + S3 configuration (the default `.env` is single-node by design).

## Layout

```
                 ┌──────────────┐
   :8000  HTTP ─▶│              │──▶ paxs-node-1 (:8000/:10400/:2575)
   :10400 DIMSE─▶│   HAProxy    │──▶ paxs-node-2 (…)
   :2575  HL7  ─▶│              │──▶ paxs-node-3 (…)
                 └──────────────┘
                          └────────▶ MySQL 8 (shared) + MinIO/S3 (shared)
```

* `deploy/haproxy.cfg` — HTTP round-robin with `GET /api/v1/health/ready`
  checks (a node with a dead DB returns 503 and is taken out of rotation) plus
  DIMSE/HL7 TCP passthrough and HAProxy stats on `:8404`.
* `deploy/docker-compose.lb.yml` — HAProxy + 3 nodes + MySQL + MinIO in one
  command. `docker compose -f deploy/docker-compose.lb.yml up -d --build`.
* `deploy/Dockerfile` — one stateless node image; set `PAXS_INSTANCE_ID` per
  node so `/health` and the dashboard show which node answered (verify
  round-robin: `for i in $(seq 8); do curl -s localhost:8000/api/v1/health | grep instance; done`).

## Health probes

* `GET /api/v1/health/live` — always 200 while the process is up (liveness).
* `GET /api/v1/health/ready` — 200 when the node can serve (DB reachable),
  503 otherwise (readiness — used by HAProxy `httpchk` and container HEALTHCHECK).

## Operational notes

* New studies / forwarded studies land wherever HAProxy sends the association;
  because every node shares the DB + S3, all nodes see them immediately.
* In-flight DICOM associations drop if a node dies mid-C-STORE; the sending
  modality/SCU retries and HAProxy routes to a live node.
* Receiver AE title is identical on all nodes (`DICOM_AE_TITLE`), so modalities
  just point at the HAProxy address.
