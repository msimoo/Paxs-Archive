"""V1-5 verification: SpaStaticFiles must serve index.html only for navigation
routes, and return a genuine 404 for missing *assets*.

Regression context: the old handler fell back to index.html for EVERY 404 under
the mount, so a missing .wasm returned `200 text/html`. The browser then reported
a cryptic WebAssembly/MIME error instead of a plain 404 — which is what made an
absent codec file look like a decoding bug during the audit.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.abspath("."))

from fastapi import FastAPI
from starlette.testclient import TestClient

from app.main import SpaStaticFiles

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


tmp = tempfile.mkdtemp(prefix="paxs-spa-test-")
with open(os.path.join(tmp, "index.html"), "w", encoding="utf-8") as fh:
    fh.write("<!doctype html><title>SPA SHELL</title>")
with open(os.path.join(tmp, "real.js"), "w", encoding="utf-8") as fh:
    fh.write("console.log('real');")

app = FastAPI()
app.mount("/viewer", SpaStaticFiles(directory=tmp, html=True), name="viewer")
client = TestClient(app)

print("\n[1] Real assets still serve normally (no regression)")
r = client.get("/viewer/real.js")
ok("existing .js -> 200", r.status_code == 200, r.status_code)
ok("existing .js body intact", "console.log" in r.text)
r = client.get("/viewer/index.html")
ok("index.html -> 200", r.status_code == 200, r.status_code)

print("\n[2] Missing ASSETS must 404 honestly (the V1-5 fix)")
for asset in ("charlswasm_decode.wasm", "openjphjs.wasm", "app.bundle.deadbeef.js",
              "theme.css", "data.json", "logo.png", "bundle.js.map"):
    r = client.get(f"/viewer/{asset}")
    ok(f"missing {asset} -> 404", r.status_code == 404, f"got {r.status_code}")
    ok(f"missing {asset} is not HTML",
       "text/html" not in (r.headers.get("content-type") or ""),
       r.headers.get("content-type"))

print("\n[3] Navigation routes still fall back to the SPA shell")
for route in ("/viewer/deep/link", "/viewer/some-route", "/viewer/studies/1.2.840"):
    r = client.get(route)
    ok(f"navigation {route} -> 200", r.status_code == 200, r.status_code)
    ok(f"navigation {route} serves the shell", "SPA SHELL" in r.text)

print("\n[4] Edge cases")
r = client.get("/viewer/")
ok("directory request -> 200 shell", r.status_code == 200, r.status_code)
r = client.get("/viewer/UPPER.WASM")
ok("uppercase .WASM also 404s", r.status_code == 404, r.status_code)
r = client.get("/viewer/archive.tar.gz")
ok("unknown extension -> 404 (asset-looking)", r.status_code == 404, r.status_code)
r = client.get("/viewer/route.with.dots")
ok("dotted navigation route -> 200 shell", r.status_code == 200, r.status_code)

print("\n===============================")
print(f"  PASS: {pass_n}   FAIL: {fail_n}")
print("===============================\n")
sys.exit(1 if fail_n else 0)
