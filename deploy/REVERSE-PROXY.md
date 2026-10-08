# Deploying PAXS behind a reverse proxy

PAXS serves four SPAs plus the API from one process. It works in two shapes:

| Shape | Public URL | Configuration |
|---|---|---|
| **Root-mounted** (recommended) | `https://pacs.example.org/` | nothing to do |
| **Sub-path** | `https://example.org/paxs/` | `ROOT_PATH` or `X-Forwarded-Prefix` |

Root-mounted is the default and needs no configuration. Sub-path deployment works
too, but requires the two settings below because the frontends (the PaxsViewer
bundle in particular) are built with root-absolute asset paths.

---

## 1. Configuration

Set these in `.env` (or the container environment):

```dotenv
# The sub-path the app is mounted under. Empty = mounted at the domain root.
# Use "/paxs" for https://example.org/paxs/
PAXS_ROOT_PATH=/paxs

# Which peers may set X-Forwarded-*. uvicorn defaults to loopback only, so a
# proxy in another container is IGNORED until it is listed here.
#   "*"                 trust anything (only on a trusted network)
#   "172.18.0.0/16"     a Docker bridge subnet
#   "10.0.0.5"          a single proxy address
PAXS_FORWARDED_ALLOW_IPS=172.18.0.0/16

# The browser-reachable origin used in share links, QR codes and FHIR URLs.
# Leave EMPTY to derive it from X-Forwarded-Host / Host (the prefix is appended
# automatically). Set it when the public host differs from the internal one.
PUBLIC_BASE_URL=https://example.org
```

`PAXS_ROOT_PATH` and `PUBLIC_BASE_URL` are read by `run_paxs.py`; `PUBLIC_BASE_URL`
is also surfaced through `/api/v1/health` so the dashboard and study list build
absolute links against the same origin.

**Prefer `X-Forwarded-Prefix` over `PAXS_ROOT_PATH` when you can.** The header is
per-request, so one deployment can be reached at several prefixes at once;
`PAXS_ROOT_PATH` is a single static value.

---

## 2. What the app does with the prefix

`ForwardedPrefixMiddleware` (`app/core/proxy_prefix.py`) runs before the auth gate
and the router. For a request from a **trusted** peer carrying
`X-Forwarded-Prefix: /paxs` it:

1. sets `scope["root_path"] = "/paxs"`, which is what Starlette's
   `request.base_url` and `url_for` use — so generated URLs carry the prefix;
2. **strips** the prefix from `scope["path"]` when the proxy did *not* already do
   so, so route matching is identical at any mount depth;
3. applies `X-Forwarded-Host` (replacing `Host`) and `X-Forwarded-Proto`, so
   `https://` is reported correctly behind TLS termination;
4. does the same for **WebSocket** scopes (scheme becomes `ws`/`wss`).

It also handles both proxy shapes:

* **Stripping** — `nginx: location /paxs/ { proxy_pass http://app/; }`. The
  request arrives as `/viewer/...`; the prefix lives only in the header.
* **Non-stripping** — Traefik without `StripPrefix`, or a raw TCP forward. The
  request arrives as `/paxs/viewer/...`; step 2 removes the prefix.

The SPA shells (`/`, `/viewer`, `/studies-ui`, `/admin`, `/settings`, `/login`)
and `app-config.js` are then served through `_spa_shell` / `_prefixed_text_asset`,
which:

* rewrite quote-anchored root-absolute app paths
  (`src="/viewer/app.bundle.js"` → `src="/paxs/viewer/app.bundle.js"`);
* inject a small boot shim that sets `window.PAXS_BASE_PATH` and prefixes
  root-absolute same-origin `fetch` / `XMLHttpRequest` URLs **at runtime** — this
  is what the dashboard's many literal `'/api/v1/...'` calls need;
* inject `window.PAXS_PUBLIC_BASE_URL` so absolute links (share links, viewer
  deep-links) point at the public origin.

The viewer bundle derives its own asset and route URLs from `window.PUBLIC_URL`
and `window.config.routerBasename`, both of which are rewritten, so its dynamic
chunk loading follows the prefix automatically.

With no prefix configured **all of this is inert** — the shells are served as
plain `FileResponse` with identical bytes.

---

## 3. nginx (sub-path, prefix stripped)

```nginx
location /paxs/ {
    proxy_pass         http://paxs:8000/;      # note the trailing slash: strips /paxs
    proxy_http_version 1.1;

    proxy_set_header Host              $host;
    proxy_set_header X-Real-IP         $remote_addr;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Forwarded-Host  $host;
    proxy_set_header X-Forwarded-Prefix /paxs;

    # Long-lived streaming reads (volume bytes, NDJSON node events).
    proxy_buffering off;
    proxy_read_timeout 300s;

    # WebSocket upgrade — harmless if unused today, correct for future endpoints.
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
}

# DIMSE / HL7 are TCP, not HTTP — route them separately if exposed.
```

`map $http_upgrade $connection_upgrade { default upgrade; '' close; }` must be
declared once at the `http` level.

## 4. nginx (root-mounted)

```nginx
location / {
    proxy_pass http://paxs:8000;
    proxy_set_header Host              $host;
    proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

No `X-Forwarded-Prefix`, no `PAXS_ROOT_PATH`. This is the simplest correct setup.

## 5. Traefik (Docker labels)

```yaml
labels:
  - traefik.enable=true
  - traefik.http.routers.paxs.rule=PathPrefix(`/paxs`)
  - traefik.http.routers.paxs.middlewares=paxs-strip,paxs-fwd
  - traefik.http.middlewares.paxs-strip.stripprefix.prefixes=/paxs
  - traefik.http.middlewares.paxs-fwd.headers.customrequestheaders.X-Forwarded-Prefix=/paxs
  - traefik.http.services.paxs.loadbalancer.server.port=8000
```

Traefik sets `X-Forwarded-Host` and `X-Forwarded-Proto` itself. Add
`/paxs` to `PAXS_FORWARDED_ALLOW_IPS` (or the Docker subnet) or the prefix header
is ignored.

> If you omit `stripprefix`, that is fine — the middleware strips it instead.
> Do not use both.

## 6. Caddy

```caddy
example.org {
    handle_path /paxs/* {          # strips the prefix
        reverse_proxy paxs:8000 {
            header_up X-Forwarded-Prefix /paxs
        }
    }
}
```

## 7. Docker Compose

```yaml
services:
  paxs:
    environment:
      PAXS_ROOT_PATH: /paxs
      PAXS_FORWARDED_ALLOW_IPS: 172.18.0.0/16
      PUBLIC_BASE_URL: https://example.org
    expose: ["8000"]
```

With HAProxy (`deploy/haproxy.cfg`) add the two missing headers — HAProxy sets
`X-Forwarded-For` via `option forwardfor` and `X-Forwarded-Proto` explicitly, but
never `X-Forwarded-Host` or `X-Forwarded-Prefix`:

```haproxy
http-request set-header X-Forwarded-Host   %[req.hdr(Host)]
http-request set-header X-Forwarded-Prefix /paxs
```

---

## 8. Verifying

```bash
# 1. The prefix is honoured and the shell is rewritten
curl -s -H 'X-Forwarded-Prefix: /paxs' http://localhost:8000/studies-ui \
  | grep -o 'paxs-runtime-boot\|"/paxs/studies-ui/assets/' | sort -u

# 2. A root-mounted request is untouched
curl -s http://localhost:8000/studies-ui | grep -c 'paxs-runtime-boot'   # expect 0

# 3. Generated URLs carry the prefix
curl -s -H 'X-Forwarded-Prefix: /paxs' \
        -H 'X-Forwarded-Host: pacs.example.org' \
        -H 'X-Forwarded-Proto: https' \
        http://localhost:8000/api/v1/health | grep public_base_url
# expect https://pacs.example.org/paxs

# 4. An untrusted peer's prefix is ignored (404 on the prefixed path)
curl -s -o /dev/null -w '%{http_code}\n' \
     -H 'X-Forwarded-Prefix: /paxs' http://localhost:8000/paxs/studies-ui

# 5. Through the proxy, end to end
curl -sI https://example.org/paxs/studies-ui | head -1     # expect 200
curl -s  https://example.org/paxs/api/v1/health | head -c 80
```

In the browser: open `https://example.org/paxs/studies-ui`, then check
DevTools → Network — every request should start with `/paxs/`, and there should
be no 404s for `/viewer/...`, `/studies-ui/assets/...` or `/api/v1/...`.
In the console, `window.PAXS_BASE_PATH` should read `"/paxs"`.

---

## 9. Known limitations

* **The prefix must be a plain path fragment.** `/paxs` is fine; a value with a
  scheme or `..` is rejected (`normalise_prefix` returns `""`).
* **Dynamic `<script>`/`<link>` injection is not rewritten** — the shim covers
  `fetch` and `XMLHttpRequest` only. In practice the bundles either derive those
  URLs from `window.PUBLIC_URL` (rewritten) or use static markup (rewritten).
* **Serving the same deployment at two different prefixes simultaneously** works
  via `X-Forwarded-Prefix` but not via `PAXS_ROOT_PATH`, which is a single static
  value.
* If you front PAXS with a CDN that rewrites asset URLs, disable that for
  `/paxs/*` — the app already emits prefix-correct paths.
