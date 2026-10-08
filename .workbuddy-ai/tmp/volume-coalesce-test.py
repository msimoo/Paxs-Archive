"""V3-1 verification: concurrent cold-cache volume materialisation is coalesced.

Regression context: load_volume_array's L1/L2 lookup and its cache insert were
both locked, but the expensive decode loop between them was not, and there was no
in-flight registry. N concurrent /mpr/slice or /tiles requests for one uncached
series therefore each re-read and re-decoded the whole stack (~500 MB of voxels
for a 512x512x500 CT) in parallel.
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import numpy as np

sys.path.insert(0, os.path.abspath("."))

from app.core import cache_stats
from app.services import volume_service
from app.services.volume_service import VolumeSlice

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


ROWS, COLS, NSLICES = 8, 8, 12

# Captured before any patching, so tests can chain on the real implementations.
REAL_FRAME_ARRAY = volume_service._frame_array
REAL_READ_DATASET = volume_service.storage_service.read_dataset


class FakeDS:
    """Minimal stand-in for a decoded pydicom Dataset."""

    def __init__(self, value):
        self.Rows = ROWS
        self.Columns = COLS
        self.SamplesPerPixel = 1
        self.RescaleSlope = 1
        self.RescaleIntercept = 0
        self._arr = np.full((ROWS, COLS), value, dtype=np.int16)

    @property
    def pixel_array(self):
        time.sleep(0.01)              # a decode is never instantaneous
        return self._arr


def make_slices(n=NSLICES, series="1.2.3.4"):
    return [
        VolumeSlice(
            sop_uid=f"{series}.{i}",
            file_path=f"/fake/{series}/{i}.dcm",
            backend=None,
            position=[0.0, 0.0, float(i)],
            iop=[1.0, 0.0, 0.0, 0.0, 1.0, 0.0],
            z=float(i),
            instance_number=i,
            frame=None,
        )
        for i in range(n)
    ]


def setup():
    """Reset caches and install a decode counter that widens the race window.

    Redis is DISABLED for the duration: it is a live L2 tier in this deployment
    and would otherwise serve a volume cached by a previous run, making the
    decode count non-deterministic. We are testing the L1 single-flight path.

    cache_stats is reset too: the counters are process-lifetime by design
    (Prometheus counter semantics), so without this each test would see the
    totals of every test before it.
    """
    volume_service._VOL_CACHE.clear()
    volume_service._IN_FLIGHT_VOLUMES.clear()
    cache_stats.reset()
    _saved_redis = volume_service.settings.REDIS_CACHE_ENABLED
    volume_service.settings.REDIS_CACHE_ENABLED = False
    state = {"decodes": 0, "saved_redis": _saved_redis}

    def counting(s, cache=None):
        state["decodes"] += 1
        time.sleep(0.01)
        return REAL_FRAME_ARRAY(s, cache)

    volume_service._frame_array = counting
    volume_service.storage_service.read_dataset = lambda path, backend_name=None: FakeDS(7)
    return state


def teardown(state=None):
    volume_service._VOL_CACHE.clear()
    volume_service._IN_FLIGHT_VOLUMES.clear()
    volume_service._frame_array = REAL_FRAME_ARRAY
    volume_service.storage_service.read_dataset = REAL_READ_DATASET
    if state is not None:
        volume_service.settings.REDIS_CACHE_ENABLED = state["saved_redis"]


def test_concurrent_coalescing():
    print("\n[1] N concurrent cold-cache loads decode the stack exactly ONCE")
    state = setup()
    slices = make_slices()
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(volume_service.load_volume_array, slices, "1.2.3.4")
                       for _ in range(8)]
            results = [f.result(timeout=60) for f in futures]

        ok("all 8 callers returned an array", all(isinstance(r, np.ndarray) for r in results))
        ok("all 8 got the same shape",
           all(r.shape == (NSLICES, ROWS, COLS) for r in results),
           {r.shape for r in results})
        # Without coalescing this is 8 * NSLICES == 96.
        ok(f"decodes == {NSLICES} (one materialisation)",
           state["decodes"] == NSLICES,
           f"got {state['decodes']} (expected {NSLICES})")
        ok("_IN_FLIGHT_VOLUMES drained after completion",
           len(volume_service._IN_FLIGHT_VOLUMES) == 0,
           len(volume_service._IN_FLIGHT_VOLUMES))
    finally:
        teardown(state)


def test_warm_cache():
    print("\n[2] A warm cache short-circuits without touching the registry")
    state = setup()
    slices = make_slices()
    try:
        volume_service.load_volume_array(slices, "1.2.3.4")
        first_decodes = state["decodes"]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = [f.result(timeout=30) for f in
                       [pool.submit(volume_service.load_volume_array, slices, "1.2.3.4")
                        for _ in range(4)]]
        ok("warm loads decoded nothing extra", state["decodes"] == first_decodes,
           f"{first_decodes} -> {state['decodes']}")
        ok("warm loads still returned data",
           all(r.shape == (NSLICES, ROWS, COLS) for r in results))
    finally:
        teardown(state)


def test_failing_owner():
    print("\n[3] A failing owner releases waiters (no permanent wedge)")
    state = setup()
    slices = make_slices()
    calls = {"n": 0}

    def flaky(s, cache=None):
        calls["n"] += 1
        time.sleep(0.05)
        if calls["n"] == 1:
            raise RuntimeError("simulated decode failure")
        return REAL_FRAME_ARRAY(s, cache)

    volume_service._frame_array = flaky
    try:
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(volume_service.load_volume_array, slices, "1.2.3.4")
                       for _ in range(4)]
            outcomes = []
            for f in futures:
                try:
                    outcomes.append(f.result(timeout=30))
                except Exception as exc:  # noqa: BLE001
                    outcomes.append(exc)

        ok("the failed caller surfaced an error",
           any(isinstance(o, Exception) for o in outcomes),
           [type(o).__name__ for o in outcomes])
        ok("at least one caller still produced the volume",
           any(isinstance(o, np.ndarray) for o in outcomes),
           [type(o).__name__ for o in outcomes])
        # 1 failed attempt + exactly ONE re-materialisation. The bug this test
        # caught: every woken waiter re-claimed ownership, so a failed owner
        # produced 3 parallel re-decodes (37 calls instead of 13).
        ok(f"only ONE re-materialisation after the failure (calls == {NSLICES + 1})",
           calls["n"] == NSLICES + 1, f"got {calls['n']}")
        ok("registry is empty afterwards (no wedge)",
           len(volume_service._IN_FLIGHT_VOLUMES) == 0,
           len(volume_service._IN_FLIGHT_VOLUMES))
    finally:
        teardown(state)


def test_distinct_series():
    print("\n[4] Distinct series are NOT serialised against each other")
    state = setup()
    a = make_slices(series="1.1.1")
    b = make_slices(series="2.2.2")
    try:
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=2) as pool:
            fa = pool.submit(volume_service.load_volume_array, a, "1.1.1")
            fb = pool.submit(volume_service.load_volume_array, b, "2.2.2")
            fa.result(timeout=60)
            fb.result(timeout=60)
        elapsed = time.monotonic() - t0
        ok("both series materialised", state["decodes"] == NSLICES * 2, state["decodes"])
        # Fully serialised would be ~2 * NSLICES * 0.02s = ~0.48s.
        ok("the two series decoded concurrently, not serially", elapsed < 0.45,
           f"{elapsed:.3f}s")
    finally:
        teardown(state)


def test_empty():
    print("\n[5] Empty slice list is still a fast no-op")
    volume_service._VOL_CACHE.clear()
    out = volume_service.load_volume_array([], "1.2.3.4")
    ok("empty input -> empty array", out.shape == (0, 0, 0), out.shape)


def _unit_bytes():
    """Bytes of one seeded volume: 8 rows x 8 cols x 12 slices float32."""
    return ROWS * COLS * NSLICES * 4


def test_lru_eviction():
    print("\n[6] _VOL_CACHE is LRU-evicted under a BYTE budget (V3-6)")
    state = setup()
    real_budget = volume_service.volume_cache_budget_bytes
    try:
        unit = _unit_bytes()
        # Budget for exactly three seeded volumes.
        volume_service.volume_cache_budget_bytes = lambda: unit * 3
        with volume_service._VOL_CACHE_LOCK:
            for i in range(3):
                volume_service._vol_cache_put_locked(
                    f"seed:{i}", np.zeros((NSLICES, ROWS, COLS), dtype=np.float32)
                )
        ok("three entries fit the budget",
           list(volume_service._VOL_CACHE.keys()) == ["seed:0", "seed:1", "seed:2"],
           list(volume_service._VOL_CACHE.keys()))

        # Touch the OLDEST entry, promoting it to most-recently-used, then add a
        # fourth: the true LRU head ("seed:1") must go, not "seed:0".
        with volume_service._VOL_CACHE_LOCK:
            volume_service._VOL_CACHE.move_to_end("seed:0", last=True)
            volume_service._vol_cache_put_locked(
                "seed:3", np.zeros((NSLICES, ROWS, COLS), dtype=np.float32)
            )

        keys = list(volume_service._VOL_CACHE.keys())
        ok("recently-used entry survived", "seed:0" in keys, keys)
        ok("least-recently-used entry was evicted", "seed:1" not in keys, keys)
        ok("new entry cached", "seed:3" in keys, keys)
        ok("byte budget respected", len(keys) <= 3, len(keys))

        stats = volume_service.volume_cache_stats()
        ok("eviction counted", stats["evictions"] == 1, stats)
        ok("inserts counted", stats["inserts"] == 4, stats)
        ok("byte gauge matches the live dict",
           stats["bytes"] == sum(v.nbytes for v in volume_service._VOL_CACHE.values()),
           (stats["bytes"], sum(v.nbytes for v in volume_service._VOL_CACHE.values())))
        ok("entry gauge matches the live dict",
           stats["entries"] == len(volume_service._VOL_CACHE), stats)
    finally:
        volume_service.volume_cache_budget_bytes = real_budget
        teardown(state)


def test_oversized_entry_is_kept():
    print("\n[7] A volume larger than the whole budget is still cached (no thrash)")
    state = setup()
    real_budget = volume_service.volume_cache_budget_bytes
    try:
        volume_service.volume_cache_budget_bytes = lambda: 1     # 1 byte
        big = np.zeros((NSLICES, ROWS, COLS), dtype=np.float32)
        with volume_service._VOL_CACHE_LOCK:
            volume_service._vol_cache_put_locked("huge", big)
        ok("oversized entry retained", "huge" in volume_service._VOL_CACHE,
           list(volume_service._VOL_CACHE.keys()))
        ok("nothing else was evicted to make room",
           len(volume_service._VOL_CACHE) == 1, len(volume_service._VOL_CACHE))
    finally:
        volume_service.volume_cache_budget_bytes = real_budget
        teardown(state)


def test_lru_hit_refreshes_recency():
    print("\n[8] A cache HIT refreshes recency (so the working set stays resident)")
    state = setup()
    real_budget = volume_service.volume_cache_budget_bytes
    a = make_slices(series="aaa")
    b = make_slices(series="bbb")
    try:
        volume_service.load_volume_array(a, "aaa")
        volume_service.load_volume_array(b, "bbb")

        key_a = f"array:aaa:{len(a)}"
        key_b = f"array:bbb:{len(b)}"
        ok("both cached", key_a in volume_service._VOL_CACHE and key_b in volume_service._VOL_CACHE)

        before = volume_service.volume_cache_stats()
        # Touch A so it becomes most-recently-used.
        volume_service.load_volume_array(a, "aaa")
        after = volume_service.volume_cache_stats()
        ok("the second load was a HIT", after["hits"] == before["hits"] + 1,
           (before["hits"], after["hits"]))
        ok("A promoted to most-recently-used",
           list(volume_service._VOL_CACHE.keys())[-1] == key_a,
           list(volume_service._VOL_CACHE.keys()))

        # Budget for two volumes, then add a third: the stale one must go.
        unit = NSLICES * ROWS * COLS * 4
        volume_service.volume_cache_budget_bytes = lambda: unit * 2
        volume_service.load_volume_array(make_slices(series="ccc"), "ccc")

        ok("recently-hit A survived eviction",
           key_a in volume_service._VOL_CACHE, list(volume_service._VOL_CACHE.keys()))
        ok("stale B was evicted",
           key_b not in volume_service._VOL_CACHE, list(volume_service._VOL_CACHE.keys()))
    finally:
        volume_service.volume_cache_budget_bytes = real_budget
        teardown(state)
        teardown(state)


test_concurrent_coalescing()
test_warm_cache()
test_failing_owner()
test_distinct_series()
test_empty()
test_lru_eviction()
test_oversized_entry_is_kept()
test_lru_hit_refreshes_recency()

print("\n===============================")
print(f"  PASS: {pass_n}   FAIL: {fail_n}")
print("===============================\n")
sys.exit(1 if fail_n else 0)
