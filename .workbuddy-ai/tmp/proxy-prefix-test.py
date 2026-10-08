"""V2-3 / V2-4 verification: reverse-proxy prefix, forwarding, and sub-path shells.

Covers:
  * normalise_prefix hardening
  * prefix_markup (quote-anchored, no double-prefix, external URLs untouched)
  * inject_boot (markup + runtime shim, no-op without a prefix)
  * ForwardedPrefixMiddleware over http AND websocket scopes
  * trusted-proxy gating
  * end-to-end: a sub-path request still routes, and generated URLs carry the prefix
  * root-mounted behaviour is byte-identical (zero regression)
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.abspath("."))

from fastapi import FastAPI, Request
from starlette.testclient import TestClient

from app.core.config import (
    DEFAULT_PUBLIC_BASE_URL,
    Settings,
    resolve_public_base_url,
)
from app.core.proxy_prefix import (
    ForwardedPrefixMiddleware,
    inject_boot,
    normalise_prefix,
    prefix_markup,
)

pass_n = 0
fail_n = 0


def ok(name, cond, extra=""):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print(f"  PASS  {name}")
    else:
        fail_n += 1
        print(f"  FAIL  {name}" + (f"  -> {extra}" if extra else ""))


def test_normalise():
    print("\n[1] normalise_prefix hardening")
    ok("'paxs/' -> '/paxs'", normalise_prefix("paxs/") == "/paxs")
    ok("'/paxs/' -> '/paxs'", normalise_prefix("/paxs/") == "/paxs")
    ok("'/paxs' -> '/paxs'", normalise_prefix("/paxs") == "/paxs")
    ok("'' -> ''", normalise_prefix("") == "")
    ok("None -> ''", normalise_prefix(None) == "")
    ok("nested '//a//b/' -> '/a/b'", normalise_prefix("//a//b/") == "/a/b")
    ok("scheme rejected", normalise_prefix("http://evil/x") == "")
    ok("traversal rejected", normalise_prefix("/a/../b") == "")


def test_prefix_markup():
    print("\n[2] prefix_markup — rewrites app paths, leaves everything else alone")
    src = (
        '<link href="/viewer/a.css">'
        '<script src="/viewer/b.js"></script>'
        '<img src="/static/logo/logo.png">'
        '<a href="/studies-ui">list</a>'
        "'/api/v1/studies'"
        '"https://cdn.example.com/viewer/c.js"'
        '"/unrelated/path"'
    )
    out = prefix_markup(src, "/paxs")
    ok("viewer css prefixed", '"/paxs/viewer/a.css"' in out)
    ok("viewer js prefixed", '"/paxs/viewer/b.js"' in out)
    ok("static prefixed", '"/paxs/static/logo/logo.png"' in out)
    ok("studies-ui prefixed", '"/paxs/studies-ui"' in out)
    ok("single-quoted api prefixed", "'/paxs/api/v1/studies'" in out)
    ok("EXTERNAL url untouched", '"https://cdn.example.com/viewer/c.js"' in out)
    ok("unrelated path untouched", '"/unrelated/path"' in out)

    twice = prefix_markup(out, "/paxs")
    ok("idempotent (no double prefix)", twice == out, twice[:120])

    ok("no prefix is a no-op", prefix_markup(src, "") == src)
    ok("None prefix is a no-op", prefix_markup(src, None) == src)


def test_inject_boot():
    print("\n[3] inject_boot — markup + runtime shim")
    html = '<!doctype html><html><head><title>x</title></head><body>'
    html += '<script defer src="/viewer/app.bundle.js"></script></body></html>'
    out = inject_boot(html, "/paxs", "https://pacs.example.org/paxs")
    ok("boot script injected", 'id="paxs-runtime-boot"' in out)
    ok("injected right after <head>",
       out.index("paxs-runtime-boot") < out.index("<title>"))
    ok("BASE_PATH set", '"/paxs"' in out)
    ok("PUBLIC base injected", '"https://pacs.example.org/paxs"' in out)
    ok("bundle path prefixed", '"/paxs/viewer/app.bundle.js"' in out)
    ok("fetch shim present", "window.fetch =" in out)
    ok("XHR shim present", "XMLHttpRequest.prototype.open" in out)
    ok("already-prefixed guard present", "already prefixed" in out)

    ok("no prefix is a no-op", inject_boot(html, "") == html)
    ok("no boot script when no prefix", "paxs-runtime-boot" not in inject_boot(html, ""))


def build_app(trusted="127.0.0.1"):
    app = FastAPI()

    @app.get("/probe")
    async def probe(request: Request):
        return {
            "path": request.scope.get("path"),
            "root_path": request.scope.get("root_path"),
            "scheme": request.scope.get("scheme"),
            "host": request.headers.get("host"),
            "base_url": str(request.base_url),
            "public_base": resolve_public_base_url(request),
        }

    app.add_middleware(ForwardedPrefixMiddleware, trusted_hosts=trusted)
    return app


def test_http_scope():
    print("\n[4] ForwardedPrefixMiddleware — http scope")
    client = TestClient(build_app(), client=("127.0.0.1", 12345))

    print("  --- stripping proxy: path has no prefix, header carries it ---")
    r = client.get("/probe", headers={"X-Forwarded-Prefix": "/paxs"})
    body = r.json()
    ok("route still matched (200)", r.status_code == 200, r.status_code)
    ok("path unchanged", body["path"] == "/probe", body["path"])
    ok("root_path published", body["root_path"] == "/paxs", body["root_path"])
    ok("base_url carries the prefix", body["base_url"].endswith("/paxs/"), body["base_url"])
    ok("public_base carries the prefix",
       body["public_base"].endswith("/paxs"), body["public_base"])

    print("  --- non-stripping proxy: path already carries the prefix ---")
    r = client.get("/paxs/probe", headers={"X-Forwarded-Prefix": "/paxs"})
    body = r.json()
    ok("route matched after prefix strip (200)", r.status_code == 200, r.status_code)
    ok("path stripped to /probe", body["path"] == "/probe", body["path"])
    ok("root_path published", body["root_path"] == "/paxs", body["root_path"])

    print("  --- X-Forwarded-Host / Proto ---")
    r = client.get("/probe", headers={
        "X-Forwarded-Prefix": "/paxs",
        "X-Forwarded-Host": "pacs.hospital.org",
        "X-Forwarded-Proto": "https",
        "Host": "internal:8000",
    })
    body = r.json()
    ok("scheme is https", body["scheme"] == "https", body["scheme"])
    ok("host replaced by X-Forwarded-Host",
       body["host"] == "pacs.hospital.org", body["host"])
    ok("base_url fully correct",
       body["base_url"] == "https://pacs.hospital.org/paxs/", body["base_url"])
    ok("public_base fully correct",
       body["public_base"] == "https://pacs.hospital.org/paxs", body["public_base"])

    print("  --- no forwarded headers => untouched ---")
    r = client.get("/probe")
    body = r.json()
    ok("root_path empty", body["root_path"] == "", body["root_path"])
    # TestClient sends Host: testserver, which IS routable, so the resolver
    # legitimately prefers it over the loopback fallback.
    ok("public_base derives from the request Host",
       body["public_base"] == "http://testserver", body["public_base"])


def test_trusted_gating():
    print("\n[5] Trusted-proxy gating")
    untrusted = TestClient(build_app(), client=("203.0.113.9", 5555))
    r = untrusted.get("/paxs/probe", headers={"X-Forwarded-Prefix": "/paxs"})
    ok("untrusted peer's prefix is IGNORED (404, path not stripped)",
       r.status_code == 404, r.status_code)

    wildcard = TestClient(build_app(trusted="*"), client=("203.0.113.9", 5555))
    r = wildcard.get("/paxs/probe", headers={"X-Forwarded-Prefix": "/paxs"})
    ok("'*' trusts everyone", r.status_code == 200, r.status_code)

    cidr = TestClient(build_app(trusted="10.0.0.0/8"), client=("10.1.2.3", 5555))
    r = cidr.get("/paxs/probe", headers={"X-Forwarded-Prefix": "/paxs"})
    ok("CIDR match trusts", r.status_code == 200, r.status_code)

    outside = TestClient(build_app(trusted="10.0.0.0/8"), client=("192.168.1.5", 5555))
    r = outside.get("/paxs/probe", headers={"X-Forwarded-Prefix": "/paxs"})
    ok("CIDR miss ignores", r.status_code == 404, r.status_code)

    glob = TestClient(build_app(trusted="172.16.*"), client=("172.16.5.5", 5555))
    r = glob.get("/paxs/probe", headers={"X-Forwarded-Prefix": "/paxs"})
    ok("glob match trusts", r.status_code == 200, r.status_code)


def test_websocket_scope():
    print("\n[6] WebSocket scope honours the prefix (task requirement)")
    captured = {}

    async def inner(scope, receive, send):
        captured["type"] = scope["type"]
        captured["root_path"] = scope.get("root_path")
        captured["scheme"] = scope.get("scheme")
        captured["path"] = scope.get("path")
        captured["host"] = dict(scope.get("headers", [])).get(b"host")

    mw = ForwardedPrefixMiddleware(inner, trusted_hosts="*")
    scope = {
        "type": "websocket",
        "path": "/paxs/ws/events",
        "root_path": "",
        "scheme": "ws",
        "client": ("10.0.0.1", 1234),
        "headers": [
            (b"host", b"internal:8000"),
            (b"x-forwarded-prefix", b"/paxs"),
            (b"x-forwarded-host", b"pacs.hospital.org"),
            (b"x-forwarded-proto", b"wss"),
        ],
    }
    asyncio.run(mw(scope, None, None))
    ok("websocket scope reached the app", captured.get("type") == "websocket")
    ok("ws root_path set", captured.get("root_path") == "/paxs", captured.get("root_path"))
    ok("ws scheme upgraded to wss", captured.get("scheme") == "wss", captured.get("scheme"))
    ok("ws path stripped", captured.get("path") == "/ws/events", captured.get("path"))
    ok("ws host replaced", captured.get("host") == b"pacs.hospital.org", captured.get("host"))

    # An http scope must NOT be forced to a ws scheme.
    captured.clear()
    http_scope = dict(scope)
    http_scope["type"] = "http"
    http_scope["path"] = "/paxs/probe"
    asyncio.run(mw(http_scope, None, None))
    ok("http scope keeps https (not wss)", captured.get("scheme") == "https",
       captured.get("scheme"))


def test_root_path_setting():
    print("\n[7] ROOT_PATH (static config) is honoured without any header")
    s = Settings(PUBLIC_BASE_URL="", ROOT_PATH="/paxs")
    ok("Settings keeps ROOT_PATH", s.ROOT_PATH == "/paxs", s.ROOT_PATH)
    ok("resolver appends ROOT_PATH",
       resolve_public_base_url(settings_obj=s) == DEFAULT_PUBLIC_BASE_URL + "/paxs",
       resolve_public_base_url(settings_obj=s))

    s2 = Settings(PUBLIC_BASE_URL="https://pacs.example.org", ROOT_PATH="/paxs")
    ok("explicit PUBLIC_BASE_URL gains the prefix",
       resolve_public_base_url(settings_obj=s2) == "https://pacs.example.org/paxs",
       resolve_public_base_url(settings_obj=s2))

    s3 = Settings(PUBLIC_BASE_URL="https://pacs.example.org/paxs", ROOT_PATH="/paxs")
    ok("prefix not duplicated when already present",
       resolve_public_base_url(settings_obj=s3) == "https://pacs.example.org/paxs",
       resolve_public_base_url(settings_obj=s3))

    s4 = Settings(PUBLIC_BASE_URL="", ROOT_PATH="")
    ok("no ROOT_PATH => plain loopback",
       resolve_public_base_url(settings_obj=s4) == DEFAULT_PUBLIC_BASE_URL,
       resolve_public_base_url(settings_obj=s4))

    ok("FORWARDED_ALLOW_IPS defaults to loopback",
       Settings().FORWARDED_ALLOW_IPS == "127.0.0.1", Settings().FORWARDED_ALLOW_IPS)


def test_shipped_shells():
    print("\n[8] The shipped viewer shell rewrites cleanly")
    p = "app/static/viewer/index.html"
    if os.path.exists(p):
        raw = open(p, encoding="utf-8").read()
        out = inject_boot(raw, "/paxs", "https://host/paxs")
        ok("bundle src prefixed", '"/paxs/viewer/app.bundle' in out)
        ok("PUBLIC_URL prefixed", '"/paxs/viewer/"' in out)
        ok("no stale root-absolute /viewer src left", 'src="/viewer/' not in out,
           'found src="/viewer/"')
        ok("app-config path prefixed", '"/paxs/viewer/app-config.js' in out)
    else:
        ok("viewer index.html present", False, p)

    p2 = "app/static/viewer/app-config.js"
    if os.path.exists(p2):
        cfg = open(p2, encoding="utf-8").read()
        out = prefix_markup(cfg, "/paxs")
        ok("routerBasename prefixed", "routerBasename: '/paxs/viewer'" in out)
        ok("wadoRoot prefixed", "wadoRoot: '/paxs/dicom-web'" in out)
        ok("qidoRoot prefixed", "qidoRoot: '/paxs/dicom-web'" in out)
    else:
        ok("app-config.js present", False, p2)

    p3 = "app/static/studies-ui/index.html"
    if os.path.exists(p3):
        sh = open(p3, encoding="utf-8").read()
        out = inject_boot(sh, "/paxs", "https://host/paxs")
        ok("studies-ui asset base prefixed", '"/paxs/studies-ui/assets/' in out)
        ok("studies-ui enhance script prefixed",
           '"/paxs/viewer/paxs-study-list-enhance.js' in out)
        ok("studies-ui favicon prefixed", '"/paxs/static/logo/logo.ico"' in out)
    else:
        ok("studies-ui index.html present", False, p3)


def test_prefix_path_and_landing():
    print("\n[9] prefix_path + role_landing carry the mount prefix (V2-4)")
    from app.api.v1.auth import role_landing
    from app.core.proxy_prefix import prefix_path

    ok("prefixes a plain path", prefix_path("/paxs", "/login") == "/paxs/login")
    ok("idempotent", prefix_path("/paxs", "/paxs/login") == "/paxs/login")
    ok("no prefix -> unchanged", prefix_path("", "/login") == "/login")
    ok("None prefix -> unchanged", prefix_path(None, "/login") == "/login")
    ok("protocol-relative refused", prefix_path("/paxs", "//evil.com") == "//evil.com")
    ok("absolute URL refused",
       prefix_path("/paxs", "https://evil.com") == "https://evil.com")
    ok("root path prefixed", prefix_path("/paxs", "/") == "/paxs/")

    ok("role_landing default is prefixed",
       role_landing("RADIOLOGIST", None, "/paxs") == "/paxs/viewer",
       role_landing("RADIOLOGIST", None, "/paxs"))
    ok("role_landing admin default prefixed",
       role_landing("ADMIN", None, "/paxs") == "/paxs/",
       role_landing("ADMIN", None, "/paxs"))
    ok("role_landing honours ?next= with prefix",
       role_landing("PHYSICIAN", "/studies-ui?x=1", "/paxs") == "/paxs/studies-ui?x=1",
       role_landing("PHYSICIAN", "/studies-ui?x=1", "/paxs"))
    ok("role_landing still blocks open-redirect",
       role_landing("PHYSICIAN", "//evil.com", "/paxs") == "/paxs/viewer",
       role_landing("PHYSICIAN", "//evil.com", "/paxs"))
    ok("role_landing unchanged at root",
       role_landing("RADIOLOGIST", None, "") == "/viewer")


test_normalise()
test_prefix_markup()
test_inject_boot()
test_http_scope()
test_trusted_gating()
test_websocket_scope()
test_root_path_setting()
test_shipped_shells()
test_prefix_path_and_landing()

print("\n===============================")
print(f"  PASS: {pass_n}   FAIL: {fail_n}")
print("===============================\n")
sys.exit(1 if fail_n else 0)
