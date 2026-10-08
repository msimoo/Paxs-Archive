"""Reverse-proxy / sub-path (X-Forwarded-*) support.

WHY THIS MODULE EXISTS
----------------------
Neither uvicorn's ``ProxyHeadersMiddleware`` (0.52) nor Starlette (1.3) reads
``X-Forwarded-Prefix`` — uvicorn only honours ``X-Forwarded-Proto`` and
``X-Forwarded-For``. So an archive mounted under a sub-path
(``https://host/paxs/``) generated every absolute URL *without* the prefix, and
its SPA shells referenced root-absolute assets that 404'd.

Three things are handled here:

1. :class:`ForwardedPrefixMiddleware` — trusts ``X-Forwarded-Prefix`` /
   ``X-Forwarded-Host`` / ``X-Forwarded-Proto`` from a known proxy, publishes the
   prefix as ``scope["root_path"]`` (which is what Starlette's ``request.base_url``
   and ``url_for`` use), and strips the prefix from the incoming path when the
   proxy did *not* already do so. Handles ``http`` **and** ``websocket`` scopes.
2. :func:`prefix_markup` — rewrites quote-anchored root-absolute app paths in the
   HTML/JS shells we serve (``"/viewer/...`` → ``"/paxs/viewer/...``).
3. :func:`inject_boot` — injects a small runtime shim that prefixes
   root-absolute same-origin requests made *at runtime* (``fetch`` /
   ``XMLHttpRequest``), which is what the dashboard's hundreds of literal
   ``'/api/v1/...'`` calls need.

Everything is gated on a non-empty prefix, so a root-mounted deployment
(the default) is byte-for-byte unchanged.

TWO PROXY SHAPES ARE SUPPORTED
------------------------------
* **Stripping** (nginx ``location /paxs/ { proxy_pass http://app/; }``) — the
  request arrives as ``/viewer/...``; the prefix lives only in the header.
* **Non-stripping** (Traefik without ``StripPrefix``, or a plain TCP forward) —
  the request arrives as ``/paxs/viewer/...``; the prefix is stripped from
  ``scope["path"]`` so routing still matches.

See ``deploy/REVERSE-PROXY.md`` for ready-made nginx / Traefik / Caddy blocks.
"""

from __future__ import annotations

import ipaddress
import json
import re
from fnmatch import fnmatch

# Root-absolute path roots owned by the app. Only these are ever prefixed, so a
# genuine external URL (``https://host/viewer``) or an unrelated path is left
# alone. Keep in sync with the FastAPI mounts in app/main.py.
APP_MOUNT_ROOTS = (
    "/viewer",
    "/studies-ui",
    "/admin",
    "/settings",
    "/static",
    "/dicom-web",
    "/api",
    "/login",
    "/logout",
    "/paxs-viewer-guide",
    "/PaxsViewer-logo.png",
)


def normalise_prefix(value: str | None) -> str:
    """``"paxs/"`` / ``"/paxs/"`` / ``"/paxs"`` → ``"/paxs"``; empty → ``""``."""
    text = (value or "").strip().strip("/")
    if not text:
        return ""
    # Collapse interior duplicates and reject anything with a scheme or a
    # traversal segment — a prefix must be a plain path fragment.
    if "://" in text or ".." in text.split("/"):
        return ""
    parts = [p for p in text.split("/") if p]
    return "/" + "/".join(parts)


class TrustedHosts:
    """Matcher for trusted peers: ``*``, glob, exact host/IP, or CIDR.

    Used for two related decisions — which peers may set ``X-Forwarded-*``
    headers, and which may scrape ``/metrics`` without credentials.
    """

    def __init__(self, patterns: str | list[str] | tuple[str, ...]) -> None:
        if isinstance(patterns, str):
            patterns = [p.strip() for p in patterns.split(",")]
        self.patterns = [p for p in (str(p).strip() for p in patterns) if p]
        self.networks = []
        for p in self.patterns:
            if "/" in p:
                try:
                    self.networks.append(ipaddress.ip_network(p, strict=False))
                except ValueError:
                    pass

    def __contains__(self, host: str | None) -> bool:
        if host is None:
            return False
        for p in self.patterns:
            if p == "*" or p == host or fnmatch(host, p):
                return True
        for net in self.networks:
            try:
                if ipaddress.ip_address(host) in net:
                    return True
            except ValueError:
                pass
        return False


# Retained for callers that imported the original private name.
_TrustedHosts = TrustedHosts


class ForwardedPrefixMiddleware:
    """Publish a trusted proxy's ``X-Forwarded-Prefix`` as ``scope["root_path"]``.

    Pure ASGI (not ``BaseHTTPMiddleware``) so it can mutate ``scope["path"]``
    before routing, and so streaming/File responses pass through untouched.
    """

    def __init__(self, app, trusted_hosts: str | list[str] = "127.0.0.1") -> None:
        self.app = app
        self.trusted = TrustedHosts(trusted_hosts)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)

        client = scope.get("client")
        client_host = client[0] if client else None
        if client_host not in self.trusted:
            # An untrusted hop must not be able to move our routes around.
            return await self.app(scope, receive, send)

        headers = {
            k.decode("latin1").lower(): v.decode("latin1")
            for k, v in scope.get("headers", ())
        }

        prefix = normalise_prefix(headers.get("x-forwarded-prefix", ""))
        if not prefix:
            return await self.app(scope, receive, send)

        new_scope = dict(scope)
        root_path = normalise_prefix(new_scope.get("root_path") or "")
        new_scope["root_path"] = root_path + prefix

        # Non-stripping proxy: the path still carries the prefix. Remove it so
        # route/mount matching behaves exactly as it does at the root.
        path = new_scope.get("path", "")
        if path == prefix or path.startswith(prefix + "/"):
            new_scope["path"] = path[len(prefix):] or "/"
            raw = new_scope.get("raw_path")
            if isinstance(raw, (bytes, bytearray)):
                new_scope["raw_path"] = raw[len(prefix):] or b"/"

        # X-Forwarded-Host / Proto so request.base_url and url_for are correct.
        fwd_host = headers.get("x-forwarded-host", "").split(",")[0].strip()
        if fwd_host:
            new_scope["headers"] = [
                (k, v) for (k, v) in new_scope.get("headers", ()) if k.lower() != b"host"
            ] + [(b"host", fwd_host.encode("latin1"))]

        fwd_proto = headers.get("x-forwarded-proto", "").split(",")[0].strip().lower()
        if fwd_proto in ("http", "https", "ws", "wss"):
            # Normalise across scope types: a websocket scope must get ws/wss and
            # an http scope must get http/https, whatever spelling the proxy sent.
            secure = fwd_proto in ("https", "wss")
            if new_scope["type"] == "websocket":
                new_scope["scheme"] = "wss" if secure else "ws"
            else:
                new_scope["scheme"] = "https" if secure else "http"

        return await self.app(new_scope, receive, send)


def prefix_path(root_path: str | None, path: str) -> str:
    """Prefix a browser-facing app-relative path with the mount prefix.

    Used for redirects and landing paths, which the browser follows directly and
    therefore must carry the prefix (``/login`` -> ``/paxs/login``). Idempotent,
    and it refuses to touch anything that is not a plain same-origin path, so a
    protocol-relative or absolute URL can never be rewritten into one.
    """
    prefix = normalise_prefix(root_path or "")
    if not prefix or not isinstance(path, str):
        return path
    if not path.startswith("/") or path.startswith("//"):
        return path
    if path == prefix or path.startswith(prefix + "/"):
        return path
    return prefix + path


# --------------------------------------------------------------------------- #
# Serve-time rewriting
# --------------------------------------------------------------------------- #
def prefix_markup(markup: str, prefix: str) -> str:
    """Rewrite quote-anchored root-absolute app paths to include ``prefix``.

    Anchoring on the opening quote is what makes this safe:

    * ``src="/viewer/a.js"``        -> rewritten
    * ``"https://cdn/x/viewer/a"``  -> NOT rewritten (the char before ``/viewer``
      is ``m``, not a quote)
    * ``"/paxs/viewer/a.js"``       -> NOT rewritten again (the quote is followed
      by ``/paxs``, so the ``"/viewer`` pattern does not occur) — no double prefix
    """
    p = normalise_prefix(prefix)
    if not p or not markup:
        return markup
    for root in APP_MOUNT_ROOTS:
        for quote in ('"', "'"):
            markup = markup.replace(quote + root, quote + p + root)
    return markup


_BOOT_TEMPLATE = r"""
<script id="paxs-runtime-boot">
(function () {
  var BASE = __PAXS_BASE_PATH__;
  var PUBLIC = __PAXS_PUBLIC_BASE__;
  window.PAXS_BASE_PATH = BASE;
  if (PUBLIC) { window.PAXS_PUBLIC_BASE_URL = PUBLIC; }
  if (!BASE) { return; }

  // ---------------------------------------------------------------------
  // Sub-path deployment shim.
  //
  // The bundles hardcode root-absolute paths in places we cannot rewrite
  // (hundreds of literal '/api/v1/...' calls in the dashboard). Rather than
  // edit every call site, prefix same-origin root-absolute requests at the
  // transport layer. Only paths under our own mounts are touched, and an
  // already-prefixed path is left alone, so this cannot double-apply.
  // ---------------------------------------------------------------------
  var P = BASE;
  var ROOTS = __PAXS_ROOTS__;

  function fixUrl(u) {
    if (typeof u !== 'string' || u.charAt(0) !== '/' || u.charAt(1) === '/') return u;
    for (var i = 0; i < ROOTS.length; i++) {
      var r = ROOTS[i];
      if (u === r || u.indexOf(r + '/') === 0) {
        if (u === P || u.indexOf(P + '/') === 0) return u;   // already prefixed
        return P + u;
      }
    }
    return u;
  }
  window.__paxsFixUrl = fixUrl;

  var origFetch = window.fetch;
  if (origFetch) {
    window.fetch = function (input, init) {
      try {
        if (typeof input === 'string') {
          input = fixUrl(input);
        } else if (input && typeof input.url === 'string') {
          var fixed = fixUrl(input.url);
          if (fixed !== input.url) {
            try { input = new Request(fixed, input); } catch (e) { input = fixed; }
          }
        }
      } catch (e) { /* never break the request over a rewrite */ }
      return origFetch.call(this, input, init);
    };
  }

  if (window.XMLHttpRequest) {
    var origOpen = window.XMLHttpRequest.prototype.open;
    window.XMLHttpRequest.prototype.open = function (method, url) {
      try {
        arguments[1] = fixUrl(url);
      } catch (e) { /* non-writable arguments object — leave as-is */ }
      return origOpen.apply(this, arguments);
    };
  }
})();
</script>
"""

_HEAD_RE = re.compile(r"<head[^>]*>", re.IGNORECASE)


def build_boot_script(prefix: str, public_base: str = "") -> str:
    p = normalise_prefix(prefix)
    if not p:
        return ""
    return (
        _BOOT_TEMPLATE.replace("__PAXS_BASE_PATH__", json.dumps(p))
        .replace("__PAXS_PUBLIC_BASE__", json.dumps(public_base or ""))
        .replace("__PAXS_ROOTS__", json.dumps(list(APP_MOUNT_ROOTS)))
    )


def inject_boot(html: str, prefix: str, public_base: str = "") -> str:
    """Prefix the markup and inject the runtime boot shim.

    Injected immediately after ``<head>`` so ``window.PAXS_BASE_PATH`` exists
    before any bundle script runs (all of which are ``defer``).
    """
    p = normalise_prefix(prefix)
    if not p or not html:
        return html
    html = prefix_markup(html, p)
    boot = build_boot_script(p, public_base)
    match = _HEAD_RE.search(html)
    if match:
        return html[: match.end()] + boot + html[match.end():]
    return boot + html
