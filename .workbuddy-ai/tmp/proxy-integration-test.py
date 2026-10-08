"""V2-3 / V2-4 end-to-end: the REAL app serves prefix-aware SPA shells.

Asserts against app.main.app (not a synthetic app), with the auth gate disabled
so the shell handlers are reachable, and checks:

  * root-mounted behaviour is unchanged (no boot shim, original paths)
  * a sub-path request gets a prefixed shell + the runtime shim
  * X-Forwarded-Host/Proto drive the injected absolute base
  * ROOT_PATH alone (no header) also produces a prefixed shell
  * the API keeps working under the prefix (non-stripping proxy shape)
"""
import os
import sys

sys.path.insert(0, os.path.abspath("."))

from starlette.testclient import TestClient

from app.core import config as config_module
from app.core.config import settings

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


# The gate would 303 an anonymous TestClient to /login before the shell handler
# runs; it is orthogonal to prefix handling, so turn it off for this test only.
settings.AUTH_GATE_ENABLED = False

from app.main import app  # noqa: E402  (import after the flag is set)

client = TestClient(app, client=("127.0.0.1", 4242))


def get(path, headers=None):
    r = client.get(path, headers=headers or {}, follow_redirects=False)
    return r


print("\n[1] Root-mounted behaviour is unchanged (zero regression)")
if True:
    for path in ("/", "/studies-ui", "/viewer", "/settings"):
        r = get(path)
        if r.status_code != 200:
            ok(f"{path} serves (200)", False, r.status_code)
            continue
        body = r.text
        ok(f"{path} serves 200", True)
        ok(f"{path} has NO boot shim", "paxs-runtime-boot" not in body)
        ok(f"{path} keeps original absolute paths",
           '"/viewer' in body or '"/static' in body or '"/api' in body or '"/studies-ui' in body)
        ok(f"{path} has no accidental prefix", '"/paxs/' not in body)
        ok(f"{path} is no-cache", r.headers.get("cache-control") == "no-cache",
           r.headers.get("cache-control"))

print("\n[2] Stripping proxy: X-Forwarded-Prefix only")
if True:
    h = {"X-Forwarded-Prefix": "/paxs"}
    r = get("/studies-ui", h)
    ok("/studies-ui still routes (200)", r.status_code == 200, r.status_code)
    body = r.text
    ok("boot shim injected", "paxs-runtime-boot" in body)
    ok("BASE_PATH is /paxs", '"/paxs"' in body and 'window.PAXS_BASE_PATH' in body)
    ok("studies-ui assets prefixed", '"/paxs/studies-ui/assets/' in body)
    ok("enhance script prefixed", '"/paxs/viewer/paxs-study-list-enhance.js' in body)
    ok("favicon prefixed", '"/paxs/static/logo/logo.ico"' in body)
    ok("no un-prefixed studies-ui asset left",
       '"/studies-ui/assets/' not in body, "found /studies-ui/assets/")

    # /viewer with no query serves the STUDY LIST shell (documented behaviour);
    # a ?StudyInstanceUIDs= deep link serves the PaxsViewer bundle shell.
    r = get("/viewer", h)
    ok("/viewer routes with prefix (200)", r.status_code == 200, r.status_code)
    ok("/viewer (no query) serves the study-list shell",
       '"/paxs/studies-ui/assets/' in r.text)

    r = get("/viewer?StudyInstanceUIDs=1.2.840.1", h)
    body = r.text
    ok("viewer deep-link routes with prefix (200)", r.status_code == 200, r.status_code)
    ok("viewer bundle prefixed", '"/paxs/viewer/app.bundle' in body)
    ok("PUBLIC_URL prefixed", '"/paxs/viewer/"' in body)
    ok("no un-prefixed viewer script left", 'src="/viewer/' not in body)
    ok("viewer app-config prefixed", '"/paxs/viewer/app-config.js' in body)

    r = get("/", h)
    ok("dashboard shell prefixed", '"/paxs/static/app.js' in r.text, r.status_code)
    ok("dashboard stylesheet prefixed", '"/paxs/static/styles.css' in r.text, r.status_code)

    r = get("/settings", h)
    ok("settings shell prefixed", "'/paxs/api/v1/license/status'" in r.text, r.status_code)

print("\n[3] app-config.js is served prefix-aware")
if True:
    r = get("/viewer/app-config.js")
    ok("root-mounted config is untouched", "routerBasename: '/viewer'" in r.text)
    ok("root-mounted has no prefix", "'/paxs/viewer'" not in r.text)

    r = get("/viewer/app-config.js", {"X-Forwarded-Prefix": "/paxs"})
    ok("config still served (200)", r.status_code == 200, r.status_code)
    ok("routerBasename prefixed", "routerBasename: '/paxs/viewer'" in r.text)
    ok("wadoRoot prefixed", "wadoRoot: '/paxs/dicom-web'" in r.text)
    ok("javascript media type", "javascript" in (r.headers.get("content-type") or ""),
       r.headers.get("content-type"))

print("\n[4] X-Forwarded-Host / Proto reach the injected absolute base")
if True:
    h = {
        "X-Forwarded-Prefix": "/paxs",
        "X-Forwarded-Host": "pacs.hospital.org",
        "X-Forwarded-Proto": "https",
    }
    r = get("/studies-ui", h)
    ok("injected base is the public https origin",
       '"https://pacs.hospital.org/paxs"' in r.text,
       [ln for ln in r.text.splitlines() if "PAXS_PUBLIC_BASE" in ln][:1])

print("\n[5] Non-stripping proxy: the prefixed path still routes")
if True:
    h = {"X-Forwarded-Prefix": "/paxs"}
    for path in ("/paxs/studies-ui", "/paxs/viewer", "/paxs/settings", "/paxs/"):
        r = get(path, h)
        ok(f"{path} routes (200)", r.status_code == 200, r.status_code)
    r = get("/paxs/studies-ui", h)
    ok("non-stripped shell is prefixed", '"/paxs/studies-ui/assets/' in r.text)

print("\n[6] API endpoints keep working under the prefix")
if True:
    h = {"X-Forwarded-Prefix": "/paxs"}
    # /api/v1/health needs admin; 401/403 still proves the ROUTE matched
    # (a routing failure would be 404).
    for path in ("/api/v1/health", "/paxs/api/v1/health"):
        r = get(path, h)
        ok(f"{path} reached the app (not 404)", r.status_code != 404, r.status_code)
    r = get("/api/v1/health")
    ok("/api/v1/health without prefix also not 404", r.status_code != 404, r.status_code)

print("\n[7] ROOT_PATH alone (static config, no header) works too")
if True:
    settings.ROOT_PATH = "/paxs"
    try:
        r = get("/studies-ui")
        ok("shell prefixed from ROOT_PATH", '"/paxs/studies-ui/assets/' in r.text,
           r.status_code)
        ok("boot shim present", "paxs-runtime-boot" in r.text)
    finally:
        settings.ROOT_PATH = ""

print("\n[8] Auth-gate redirects carry the mount prefix (V2-4)")
if True:
    # Re-enable the gate: this is exactly the code path an anonymous browser hits.
    settings.AUTH_GATE_ENABLED = True
    try:
        r = get("/studies-ui")
        ok("root-mounted redirects to /login", r.headers.get("location") == "/login?next=%2Fstudies-ui",
           r.headers.get("location"))

        h = {"X-Forwarded-Prefix": "/paxs"}
        r = get("/studies-ui", h)
        ok("prefixed request 303s", r.status_code == 303, r.status_code)
        loc = r.headers.get("location") or ""
        ok("redirect target carries the prefix", loc.startswith("/paxs/login?"), loc)
        ok("next= carries the prefix too", "%2Fpaxs%2Fstudies-ui" in loc, loc)

        r = get("/paxs/studies-ui", h)
        loc = r.headers.get("location") or ""
        ok("non-stripped path also redirects under the prefix",
           loc.startswith("/paxs/login?"), loc)

        r = get("/logout", h)
        ok("/logout redirects to the prefixed /login",
           r.headers.get("location") == "/paxs/login", r.headers.get("location"))

        r = get("/logout")
        ok("/logout unchanged at the root",
           r.headers.get("location") == "/login", r.headers.get("location"))

        r = get("/settings", h)
        ok("/settings redirect prefixed",
           (r.headers.get("location") or "").startswith("/paxs/login?"),
           r.headers.get("location"))
    finally:
        settings.AUTH_GATE_ENABLED = False

print("\n===============================")
print(f"  PASS: {pass_n}   FAIL: {fail_n}")
print("===============================\n")
sys.exit(1 if fail_n else 0)
