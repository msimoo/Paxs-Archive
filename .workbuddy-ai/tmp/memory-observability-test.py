"""Round 3 verification: memory budgets (V1-2 / V3-6), cache telemetry, and
TransferSyntaxUID decode diagnostics.

Covers:
  * the two new settings + their defaults
  * validate_memory_budgets() coherence warnings
  * settings_service schema: registration, group, type, min/max enforcement
  * the config API surfacing min/max
  * the frame cache's byte budget, LRU eviction and oversized-entry guard
  * the volume cache's budget reader
  * Prometheus exposition of cache counters/gauges
  * decode-context formatting and its absence of PHI
  * decode-failure logging actually carrying the transfer syntax
"""
import logging
import os
import sys

import numpy as np

sys.path.insert(0, os.path.abspath("."))

from fastapi import Depends

from app.core import cache_stats
from app.core.config import Settings, settings, validate_memory_budgets
from app.core.dicom_diagnostics import decode_context, transfer_syntax_label
from app.services import settings_service, volume_service, wado_rs_bulk_service

pass_n = 0
fail_n = 0

# Geometry for the synthetic volumes used in the budget tests.
ROWS, COLS, NSLICES = 8, 8, 12


def ok(name, cond, extra=""):
    global pass_n, fail_n
    if cond:
        pass_n += 1
        print(f"  PASS  {name}")
    else:
        fail_n += 1
        print(f"  FAIL  {name}" + (f"  -> {extra}" if extra else ""))


# --------------------------------------------------------------------------- #
def test_settings_defaults():
    print("\n[1] The two budget settings exist with the specified defaults")
    s = Settings()
    ok("DICOM_FRAME_CACHE_MB defaults to 256", s.DICOM_FRAME_CACHE_MB == 256, s.DICOM_FRAME_CACHE_MB)
    ok("VOLUME_CACHE_BUDGET_MB defaults to 1024", s.VOLUME_CACHE_BUDGET_MB == 1024, s.VOLUME_CACHE_BUDGET_MB)
    ok("both are ints",
       isinstance(s.DICOM_FRAME_CACHE_MB, int) and isinstance(s.VOLUME_CACHE_BUDGET_MB, int))
    ok("volume budget is below the hybrid budget by default",
       s.VOLUME_CACHE_BUDGET_MB < s.HYBRID_SERVER_VOL_BUDGET_MB,
       (s.VOLUME_CACHE_BUDGET_MB, s.HYBRID_SERVER_VOL_BUDGET_MB))
    ok("env override is honoured",
       Settings(DICOM_FRAME_CACHE_MB=64).DICOM_FRAME_CACHE_MB == 64)


def test_validate_memory_budgets():
    print("\n[2] validate_memory_budgets() coherence warnings")
    ok("the shipped defaults are coherent", validate_memory_budgets(Settings()) == [],
       validate_memory_budgets(Settings()))

    over = Settings(VOLUME_CACHE_BUDGET_MB=8192, HYBRID_SERVER_VOL_BUDGET_MB=4096)
    w = validate_memory_budgets(over)
    ok("cache above the hybrid budget warns", any("exceeds" in x for x in w), w)

    ok("cache below the hybrid budget is quiet",
       validate_memory_budgets(
           Settings(VOLUME_CACHE_BUDGET_MB=512, HYBRID_SERVER_VOL_BUDGET_MB=4096)) == [])

    # Deliberate: a budget too small for the session cap is NOT warned about.
    # The byte budget is the authoritative memory constraint and the session cap
    # is an admission limit it need not fully serve; warning here fired on the
    # shipped defaults and would train operators to ignore it. Real pressure is
    # measured via paxs_cache_utilisation_ratio instead.
    small = Settings(VOLUME_CACHE_BUDGET_MB=64, HYBRID_MAX_SERVER_MPR_SESSIONS=20)
    ok("a small budget alone does not warn (measured, not guessed)",
       validate_memory_budgets(small) == [], validate_memory_budgets(small))

    ok("zero volume budget is a documented disable, not a warning",
       validate_memory_budgets(Settings(VOLUME_CACHE_BUDGET_MB=0)) == [],
       validate_memory_budgets(Settings(VOLUME_CACHE_BUDGET_MB=0)))
    ok("zero frame budget is a documented disable, not a warning",
       validate_memory_budgets(Settings(DICOM_FRAME_CACHE_MB=0)) == [],
       validate_memory_budgets(Settings(DICOM_FRAME_CACHE_MB=0)))

    ok("negative volume budget warns",
       any("VOLUME_CACHE_BUDGET_MB must be >= 0" in x
           for x in validate_memory_budgets(Settings(VOLUME_CACHE_BUDGET_MB=-1))),
       validate_memory_budgets(Settings(VOLUME_CACHE_BUDGET_MB=-1)))
    ok("negative frame budget warns",
       any("DICOM_FRAME_CACHE_MB must be >= 0" in x
           for x in validate_memory_budgets(Settings(DICOM_FRAME_CACHE_MB=-5))),
       validate_memory_budgets(Settings(DICOM_FRAME_CACHE_MB=-5)))

    big = Settings(VOLUME_CACHE_BUDGET_MB=4096, HYBRID_MAX_SERVER_MPR_SESSIONS=4)
    ok("a generous budget with a low session cap is quiet",
       validate_memory_budgets(big) == [], validate_memory_budgets(big))


def test_settings_schema():
    print("\n[3] settings_service schema registration + range enforcement")
    for key, lo, hi in (("DICOM_FRAME_CACHE_MB", 16, 16384),
                        ("VOLUME_CACHE_BUDGET_MB", 64, 65536)):
        spec = settings_service.EDITABLE_KEYS.get(key)
        ok(f"{key} is registered", spec is not None)
        if spec is None:
            continue
        ok(f"{key} group is viewer", spec["group"] == "viewer", spec["group"])
        ok(f"{key} type is int", spec["type"] == "int", spec["type"])
        ok(f"{key} declares min", spec.get("min") == lo, spec.get("min"))
        ok(f"{key} declares max", spec.get("max") == hi, spec.get("max"))
        ok(f"{key} has a description", bool(spec.get("description")))
        ok(f"{key} is in the viewer group listing",
           key in [k for k, v in settings_service.EDITABLE_KEYS.items()
                   if v["group"] == "viewer"])

    # Coercion: valid, out-of-range, and wrong type.
    ok("valid value coerces", settings_service._coerce("VOLUME_CACHE_BUDGET_MB", 2048) == "2048")
    ok("valid frame value coerces", settings_service._coerce("DICOM_FRAME_CACHE_MB", 512) == "512")

    for key, bad in (("VOLUME_CACHE_BUDGET_MB", 0), ("VOLUME_CACHE_BUDGET_MB", -1),
                     ("VOLUME_CACHE_BUDGET_MB", 999999),
                     ("DICOM_FRAME_CACHE_MB", 0), ("DICOM_FRAME_CACHE_MB", 999999)):
        try:
            settings_service._coerce(key, bad)
            ok(f"{key}={bad} rejected", False, "accepted")
        except ValueError:
            ok(f"{key}={bad} rejected", True)

    try:
        settings_service._coerce("VOLUME_CACHE_BUDGET_MB", "not-a-number")
        ok("non-numeric rejected", False, "accepted")
    except ValueError:
        ok("non-numeric rejected", True)

    ok("_parse round-trips",
       settings_service._parse("VOLUME_CACHE_BUDGET_MB", "2048") == 2048)
    ok("a key without min/max still coerces",
       settings_service._coerce("HYBRID_MAX_SERVER_MPR_SESSIONS", 5) == "5")


def test_config_api_field():
    print("\n[4] The config API surfaces min/max for bounded keys")
    from app.api.v1.config import _field

    f = _field("VOLUME_CACHE_BUDGET_MB", {})
    ok("min surfaced", f.get("min") == 64, f.get("min"))
    ok("max surfaced", f.get("max") == 65536, f.get("max"))
    ok("value present", f.get("value") == 1024, f.get("value"))
    ok("group-relevant metadata intact", f["type"] == "int" and bool(f["label"]))

    g = _field("HYBRID_MAX_SERVER_MPR_SESSIONS", {})
    ok("unbounded key has no min/max", "min" not in g and "max" not in g, g)


def test_frame_cache_budget():
    print("\n[5] Frame cache: byte budget, LRU eviction, oversized guard")
    real_budget = wado_rs_bulk_service.frame_cache_budget_bytes
    try:
        cache_stats.reset()
        wado_rs_bulk_service.clear_frame_cache()

        frame = b"x" * 1000
        wado_rs_bulk_service.frame_cache_budget_bytes = lambda: 3000   # 3 frames
        for i in range(3):
            wado_rs_bulk_service._cache_put((f"sop{i}", 1), frame)
        ok("three frames fit", len(wado_rs_bulk_service._FRAME_CACHE) == 3)

        # Touch the oldest, then insert a fourth: the true LRU must go.
        wado_rs_bulk_service._cache_get(("sop0", 1))
        wado_rs_bulk_service._cache_put(("sop3", 1), frame)
        keys = list(wado_rs_bulk_service._FRAME_CACHE.keys())
        ok("recently-used frame survived", ("sop0", 1) in keys, keys)
        ok("least-recently-used frame evicted", ("sop1", 1) not in keys, keys)
        ok("new frame cached", ("sop3", 1) in keys, keys)
        ok("byte budget respected", len(keys) <= 3, len(keys))

        stats = wado_rs_bulk_service.frame_cache_stats()
        ok("eviction counted", stats["evictions"] == 1, stats)
        ok("inserts counted", stats["inserts"] == 4, stats)
        ok("hits counted", stats["hits"] == 1, stats)
        ok("bytes gauge correct", stats["bytes"] == 3000, stats)
        ok("entries gauge correct", stats["entries"] == 3, stats)

        # Miss accounting.
        before = wado_rs_bulk_service.frame_cache_stats()["misses"]
        ok("absent key is a miss",
           wado_rs_bulk_service._cache_get(("nope", 9)) is None)
        ok("miss counted",
           wado_rs_bulk_service.frame_cache_stats()["misses"] == before + 1)

        # Oversized entry must be retained (otherwise the cache would never hold
        # anything when a single frame exceeds the budget).
        wado_rs_bulk_service.frame_cache_budget_bytes = lambda: 10
        wado_rs_bulk_service._cache_put(("huge", 1), b"y" * 5000)
        ok("oversized frame retained", ("huge", 1) in wado_rs_bulk_service._FRAME_CACHE)
        ok("oversized frame is the only entry",
           len(wado_rs_bulk_service._FRAME_CACHE) == 1,
           list(wado_rs_bulk_service._FRAME_CACHE.keys()))

        # Clearing resets the gauges.
        wado_rs_bulk_service.clear_frame_cache()
        s2 = wado_rs_bulk_service.frame_cache_stats()
        ok("clear resets bytes gauge", s2["bytes"] == 0, s2)
        ok("clear resets entries gauge", s2["entries"] == 0, s2)
    finally:
        wado_rs_bulk_service.frame_cache_budget_bytes = real_budget
        wado_rs_bulk_service.clear_frame_cache()
        cache_stats.reset()

    # The budget reader must reflect settings (and survive a bad value).
    ok("budget reader uses settings",
       wado_rs_bulk_service.frame_cache_budget_bytes() == 256 * 1024 * 1024,
       wado_rs_bulk_service.frame_cache_budget_bytes())
    real = wado_rs_bulk_service.settings.DICOM_FRAME_CACHE_MB
    try:
        wado_rs_bulk_service.settings.DICOM_FRAME_CACHE_MB = 64
        ok("budget reader follows a runtime override",
           wado_rs_bulk_service.frame_cache_budget_bytes() == 64 * 1024 * 1024,
           wado_rs_bulk_service.frame_cache_budget_bytes())
    finally:
        wado_rs_bulk_service.settings.DICOM_FRAME_CACHE_MB = real


def test_volume_budget_reader():
    print("\n[6] Volume cache: budget reader reflects settings")
    ok("default budget is 1024 MB",
       volume_service.volume_cache_budget_bytes() == 1024 * 1024 * 1024,
       volume_service.volume_cache_budget_bytes())
    real = volume_service.settings.VOLUME_CACHE_BUDGET_MB
    try:
        volume_service.settings.VOLUME_CACHE_BUDGET_MB = 2048
        ok("follows a runtime override",
           volume_service.volume_cache_budget_bytes() == 2048 * 1024 * 1024,
           volume_service.volume_cache_budget_bytes())

        # 0 must stay 0 — an `or` fallback would turn a deliberate disable into
        # the full default budget.
        volume_service.settings.VOLUME_CACHE_BUDGET_MB = 0
        ok("0 means disabled and is NOT masked by a default",
           volume_service.volume_cache_budget_bytes() == 0,
           volume_service.volume_cache_budget_bytes())

        volume_service.settings.VOLUME_CACHE_BUDGET_MB = -5
        ok("negative clamps to 0",
           volume_service.volume_cache_budget_bytes() == 0,
           volume_service.volume_cache_budget_bytes())

        volume_service.settings.VOLUME_CACHE_BUDGET_MB = "garbage"
        ok("non-numeric falls back to the default",
           volume_service.volume_cache_budget_bytes() == 1024 * 1024 * 1024,
           volume_service.volume_cache_budget_bytes())
    finally:
        volume_service.settings.VOLUME_CACHE_BUDGET_MB = real

    # A disabled cache must actually store nothing.
    cache_stats.reset()
    volume_service._VOL_CACHE.clear()
    real = volume_service.settings.VOLUME_CACHE_BUDGET_MB
    try:
        volume_service.settings.VOLUME_CACHE_BUDGET_MB = 0
        with volume_service._VOL_CACHE_LOCK:
            volume_service._vol_cache_put_locked(
                "disabled", np.zeros((NSLICES, ROWS, COLS), dtype=np.float32)
            )
        ok("budget 0 stores nothing", len(volume_service._VOL_CACHE) == 0,
           list(volume_service._VOL_CACHE.keys()))
        ok("budget 0 reports an empty gauge",
           volume_service.volume_cache_stats()["bytes"] == 0,
           volume_service.volume_cache_stats())
        ok("budget 0 does not count an insert",
           volume_service.volume_cache_stats()["inserts"] == 0,
           volume_service.volume_cache_stats())
    finally:
        volume_service.settings.VOLUME_CACHE_BUDGET_MB = real
        volume_service._VOL_CACHE.clear()
        cache_stats.reset()

    ok("_VOL_CACHE_MAX is gone (byte budget replaced it)",
       not hasattr(volume_service, "_VOL_CACHE_MAX"))


def test_metrics_exposition():
    print("\n[7] Prometheus exposition of cache metrics")
    from app.api.v1.metrics import _cache_budgets, _cache_metric_lines, _fmt_help_type

    cache_stats.reset()
    cache_stats.incr("volume", "hits", 7)
    cache_stats.incr("volume", "misses", 3)
    cache_stats.incr("frame", "hits", 11)
    cache_stats.set_gauges("volume", bytes=1024 * 1024 * 512, entries=1)

    lines = _cache_metric_lines()
    text = "\n".join(lines)
    ok("hit counter emitted", 'paxs_cache_operations_total{cache="volume",result="hit"} 7' in text)
    ok("miss counter emitted", 'paxs_cache_operations_total{cache="volume",result="miss"} 3' in text)
    ok("insert counter emitted", 'result="insert"' in text)
    ok("evict counter emitted", 'result="evict"' in text)
    ok("bytes gauge emitted", 'paxs_cache_bytes{cache="volume"} 536870912' in text)
    ok("entries gauge emitted", 'paxs_cache_entries{cache="volume"} 1' in text)
    ok("budget gauge emitted", 'paxs_cache_budget_bytes{cache="volume"} 1073741824' in text)
    ok("frame cache series present even with no traffic",
       'paxs_cache_operations_total{cache="frame",result="hit"} 11' in text)
    ok("utilisation ratio emitted", 'paxs_cache_utilisation_ratio{cache="volume"} 0.5' in text,
       [ln for ln in lines if "utilisation" in ln])
    ok("no NaN/inf in the exposition",
       not any(("nan" in ln.lower() or "inf" in ln.lower()) for ln in lines))

    help_text = _fmt_help_type()
    for name in ("paxs_cache_operations_total", "paxs_cache_bytes",
                 "paxs_cache_entries", "paxs_cache_budget_bytes",
                 "paxs_cache_utilisation_ratio"):
        ok(f"HELP/TYPE declared for {name}",
           f"# HELP {name} " in help_text and f"# TYPE {name} " in help_text)

    ok("budgets read from settings",
       _cache_budgets() == {"volume": 1024 * 1024 * 1024, "frame": 256 * 1024 * 1024},
       _cache_budgets())

    # A zero budget must not divide by zero.
    real = volume_service.settings.VOLUME_CACHE_BUDGET_MB
    try:
        volume_service.settings.VOLUME_CACHE_BUDGET_MB = 0
        from app.api.v1 import metrics as metrics_mod
        lines2 = metrics_mod._cache_metric_lines()
        ratio_line = [ln for ln in lines2 if "utilisation_ratio" in ln and "volume" in ln][0]
        ok("zero budget yields ratio 0, not a crash", ratio_line.endswith(" 0.000000"), ratio_line)
    finally:
        volume_service.settings.VOLUME_CACHE_BUDGET_MB = real
    cache_stats.reset()


def test_decode_diagnostics():
    print("\n[8] Decode context formatting")
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import UID

    ok("known syntax gets its name",
       transfer_syntax_label(UID("1.2.840.10008.1.2.4.90")).startswith("1.2.840.10008.1.2.4.90 ("),
       transfer_syntax_label(UID("1.2.840.10008.1.2.4.90")))
    ok("JPEG-LS is labelled",
       "JPEG-LS" in transfer_syntax_label(UID("1.2.840.10008.1.2.4.80")),
       transfer_syntax_label(UID("1.2.840.10008.1.2.4.80")))
    ok("RLE is labelled",
       "RLE" in transfer_syntax_label(UID("1.2.840.10008.1.2.5")),
       transfer_syntax_label(UID("1.2.840.10008.1.2.5")))
    # pydicom's own name is authoritative and preferred over the built-in map;
    # for HTJ2K it spells the name out rather than using the "HTJ2K" initialism.
    ok("HTJ2K syntax is labelled",
       "High-Throughput" in transfer_syntax_label(UID("1.2.840.10008.1.2.4.201")),
       transfer_syntax_label(UID("1.2.840.10008.1.2.4.201")))
    ok("None -> unset", transfer_syntax_label(None) == "unset")
    ok("empty -> unset", transfer_syntax_label("") == "unset")
    ok("unknown UID still returns the value",
       transfer_syntax_label("9.9.9.9.9").startswith("9.9.9.9.9"))

    ds = Dataset()
    fm = FileMetaDataset()
    fm.TransferSyntaxUID = UID("1.2.840.10008.1.2.4.80")
    ds.file_meta = fm
    ds.Modality = "CT"
    ds.Rows = 512
    ds.Columns = 512
    ds.BitsAllocated = 16
    ds.BitsStored = 12
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.NumberOfFrames = 1
    ds.SeriesInstanceUID = "1.2.3.4"
    ds.SOPInstanceUID = "1.2.3.4.5"

    ctx = decode_context(ds, frame=3)
    ok("context names the transfer syntax", "JPEG-LS" in ctx, ctx)
    ok("context includes the UID", "1.2.840.10008.1.2.4.80" in ctx, ctx)
    ok("context includes the pixel layout",
       "bits=16/12" in ctx and "samples=1" in ctx and "512x512" in ctx, ctx)
    ok("context includes the modality", "modality=CT" in ctx, ctx)
    ok("context includes series + sop + frame",
       "series=1.2.3.4" in ctx and "sop=1.2.3.4.5" in ctx and "frame=3" in ctx, ctx)
    ok("context is a single line", "\n" not in ctx)

    # No patient identifiers may appear.
    ds.PatientName = "DOE^JOHN"
    ds.PatientID = "MRN123456"
    ds.PatientBirthDate = "19800101"
    ctx2 = decode_context(ds)
    ok("no PatientName", "DOE" not in ctx2, ctx2)
    ok("no PatientID", "MRN123456" not in ctx2, ctx2)
    ok("no PatientBirthDate", "19800101" not in ctx2, ctx2)

    # A bare dataset with no file_meta must not explode.
    bare = Dataset()
    ctx3 = decode_context(bare)
    ok("bare dataset -> 'unset' syntax", "transfer_syntax=unset" in ctx3, ctx3)
    ok("bare dataset does not raise", isinstance(ctx3, str) and bool(ctx3))


def test_decode_failure_logging():
    print("\n[9] Decode failures log the transfer syntax")
    from pydicom.dataset import Dataset, FileMetaDataset
    from pydicom.uid import UID

    class ExplodingDS(Dataset):
        """Dataset whose decode always fails — deterministic codec-gap simulation."""
        @property
        def pixel_array(self):
            raise RuntimeError("no decoder installed for this syntax")

    def make(ts):
        ds = ExplodingDS()
        fm = FileMetaDataset()
        fm.TransferSyntaxUID = UID(ts)
        ds.file_meta = fm
        ds.Modality = "CT"
        ds.Rows = 64
        ds.Columns = 64
        ds.BitsAllocated = 16
        ds.BitsStored = 16
        ds.SamplesPerPixel = 1
        ds.PhotometricInterpretation = "MONOCHROME2"
        ds.NumberOfFrames = 2
        ds.SeriesInstanceUID = "1.2.3"
        ds.SOPInstanceUID = "1.2.3.4"
        ds.PixelData = b"\x00" * 256
        return ds

    captured = []

    class Capture(logging.Handler):
        def emit(self, record):
            captured.append(record.getMessage())

    logger = logging.getLogger("paxs.wado_bulk")
    handler = Capture()
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    real_budget = wado_rs_bulk_service.frame_cache_budget_bytes
    wado_rs_bulk_service.frame_cache_budget_bytes = lambda: 1024 * 1024
    try:
        # --- Case A: pixel_array fails but the raw codestream is still served.
        # A valid encapsulated frame lets extract_frame_bytes succeed, which is
        # exactly the graceful-degradation path decode_frames_to_raw promises.
        from pydicom.encaps import encapsulate

        def make_encapsulated(ts):
            ds = make(ts)
            ds.NumberOfFrames = 1
            ds.PixelData = encapsulate([b"\x00" * 64])
            return ds

        frames = wado_rs_bulk_service.decode_frames_to_raw(
            make_encapsulated("1.2.840.10008.1.2.4.80"), [1]
        )
        ok("the request still returned data (graceful fallback)", bool(frames), frames)
        blob = "\n".join(captured)
        ok("a warning was logged", bool(captured), captured)
        ok("the log carries the transfer syntax UID",
           "1.2.840.10008.1.2.4.80" in blob, blob)
        ok("the log carries the human codec name", "JPEG-LS" in blob, blob)
        ok("the log carries the exception type", "RuntimeError" in blob, blob)
        ok("the log carries the pixel layout", "bits=16/16" in blob, blob)
        ok("the log carries the series/sop identity",
           "series=1.2.3" in blob and "sop=1.2.3.4" in blob, blob)

        # A different syntax must produce a correspondingly different label.
        captured.clear()
        wado_rs_bulk_service.decode_frames_to_raw(
            make_encapsulated("1.2.840.10008.1.2.5"), [1]
        )
        blob2 = "\n".join(captured)
        ok("RLE syntax labelled in the log", "RLE" in blob2, blob2)
        ok("RLE UID present", "1.2.840.10008.1.2.5" in blob2, blob2)

        # --- Case B: BOTH paths fail (no encapsulation to fall back to).
        # The request must fail loudly, and BOTH handlers must log context.
        captured.clear()
        raised = None
        try:
            wado_rs_bulk_service.decode_frames_to_raw(make("1.2.840.10008.1.2.4.90"), [1])
        except ValueError as exc:
            raised = exc
        ok("both decode paths failing surfaces an error", raised is not None, raised)
        blob3 = "\n".join(captured)
        ok("pixel_array failure logged with the syntax",
           "decode_frames_to_raw: pixel_array failed" in blob3
           and "1.2.840.10008.1.2.4.90" in blob3, blob3)
        ok("frame-extraction failure also logged with the syntax",
           "Failed to extract pixel array frames" in blob3
           and "JPEG 2000" in blob3, blob3)
        ok("both log lines name the codec", "JPEG 2000" in blob3, blob3)
    finally:
        wado_rs_bulk_service.frame_cache_budget_bytes = real_budget
        wado_rs_bulk_service.clear_frame_cache()
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        cache_stats.reset()


def test_metrics_access_control():
    print("\n[10] /metrics access: trusted networks scrape without credentials")
    from fastapi import FastAPI, Request
    from starlette.testclient import TestClient

    from app.api.v1.metrics import require_metrics_access

    app = FastAPI()

    @app.get("/metrics")
    async def _m(request: Request, _a=Depends(require_metrics_access)):
        return {"ok": True}

    def client_from(ip):
        return TestClient(app, client=(ip, 44444))

    # Trusted peers (the shipped default covers loopback + RFC1918, which is what
    # the Docker/HAProxy topology needs).
    for ip in ("127.0.0.1", "10.1.2.3", "172.18.0.5", "192.168.9.9"):
        r = client_from(ip).get("/metrics")
        ok(f"trusted {ip} scrapes without credentials", r.status_code == 200, r.status_code)

    # Untrusted peers must still authenticate.
    for ip in ("203.0.113.9", "8.8.8.8"):
        r = client_from(ip).get("/metrics")
        ok(f"public {ip} is refused (401)", r.status_code == 401, r.status_code)

    # A wildcard opens it up (documented opt-in).
    real = settings.METRICS_ALLOW_IPS
    try:
        settings.METRICS_ALLOW_IPS = "*"
        r = client_from("8.8.8.8").get("/metrics")
        ok("METRICS_ALLOW_IPS='*' allows any peer", r.status_code == 200, r.status_code)

        # An empty allowlist means "authenticate always".
        settings.METRICS_ALLOW_IPS = ""
        r = client_from("127.0.0.1").get("/metrics")
        ok("empty allowlist refuses even loopback", r.status_code == 401, r.status_code)
    finally:
        settings.METRICS_ALLOW_IPS = real

    ok("the default allowlist is not wide open",
       "*" not in settings.METRICS_ALLOW_IPS, settings.METRICS_ALLOW_IPS)
    ok("the default allowlist includes the RFC1918 ranges",
       "10.0.0.0/8" in settings.METRICS_ALLOW_IPS
       and "172.16.0.0/12" in settings.METRICS_ALLOW_IPS
       and "192.168.0.0/16" in settings.METRICS_ALLOW_IPS,
       settings.METRICS_ALLOW_IPS)


def test_metrics_end_to_end():
    print("\n[11] /metrics renders end to end through the real app")
    from starlette.testclient import TestClient

    from app.main import app as real_app

    real_gate = settings.AUTH_GATE_ENABLED
    settings.AUTH_GATE_ENABLED = False
    try:
        c = TestClient(real_app, client=("127.0.0.1", 4242))
        r = c.get("/metrics")
        ok("loopback scrape succeeds (200)", r.status_code == 200, r.status_code)
        ok("content type is Prometheus text",
           "text/plain" in (r.headers.get("content-type") or ""),
           r.headers.get("content-type"))
        body = r.text
        ok("cache counters present", "paxs_cache_operations_total" in body)
        ok("cache budget gauge present", "paxs_cache_budget_bytes" in body)
        ok("volume budget is 1024 MB",
           'paxs_cache_budget_bytes{cache="volume"} 1073741824' in body,
           [ln for ln in body.splitlines() if 'budget_bytes{cache="volume"' in ln])
        ok("frame budget is 256 MB",
           'paxs_cache_budget_bytes{cache="frame"} 268435456' in body,
           [ln for ln in body.splitlines() if 'budget_bytes{cache="frame"' in ln])
        ok("existing metrics still present",
           "paxs_up 1" in body and "paxs_http_requests_total" in body)
        ok("no store cache header",
           r.headers.get("cache-control") == "no-store", r.headers.get("cache-control"))
    finally:
        settings.AUTH_GATE_ENABLED = real_gate


test_settings_defaults()
test_validate_memory_budgets()
test_settings_schema()
test_config_api_field()
test_frame_cache_budget()
test_volume_budget_reader()
test_metrics_exposition()
test_decode_diagnostics()
test_decode_failure_logging()
test_metrics_access_control()
test_metrics_end_to_end()

print("\n===============================")
print(f"  PASS: {pass_n}   FAIL: {fail_n}")
print("===============================\n")
sys.exit(1 if fail_n else 0)
