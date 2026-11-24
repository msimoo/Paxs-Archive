"""Speed plan (metadata): keep the gzipped WADO-RS /metadata body per series/study.

Measured on the 1,834-image series: reading 19.6 MB of stored ``metadata_json``
from MySQL costs 600-900 ms and gzip another ~330 ms, on every open of the
study. The body only changes when an instance of the series is added, removed
or rewritten, so this module keeps the *compressed* body and revalidates it with
one aggregate query.

Freshness (the "stamp"): ``(count, sum(id), max(id), sum(content_version))`` over
the caller's tenant-scoped instance rows. Every path that rewrites an
instance's stored metadata also bumps ``content_version`` (C-04: re-ingest,
compression, lossy transcode); adding or removing an instance changes the
count / id sums. If the stamp differs, the body is rebuilt as before.

Safety:
- the key includes the caller's tenant scope, so one tenant's body is never
  served to another; and the stamp query itself is the scoped query;
- bounded by compressed bytes (LRU); process-local (no PHI leaves the process);
- the ETag is derived from the stamp, so a browser revalidating an unchanged
  series gets a body-less 304.
"""
from __future__ import annotations

import gzip
import hashlib
import threading
from collections import OrderedDict
from dataclasses import dataclass

MAX_BYTES = 128 * 1024 * 1024  # compressed bodies (≈ 400 large series)
GZIP_LEVEL = 6

Stamp = tuple[int, int, int, int]
Key = tuple[int | None, bool, str, str | None]


@dataclass(frozen=True)
class CachedBody:
    stamp: Stamp
    gzipped: bytes
    etag: str


_entries: OrderedDict[Key, CachedBody] = OrderedDict()
_bytes = 0
_lock = threading.Lock()


def etag_for(key: Key, stamp: Stamp) -> str:
    digest = hashlib.sha256(repr((key, stamp)).encode()).hexdigest()[:32]
    return f'"meta-{digest}"'


def get(key: Key, stamp: Stamp) -> CachedBody | None:
    with _lock:
        entry = _entries.get(key)
        if entry is None or entry.stamp != stamp:
            return None
        _entries.move_to_end(key)
        return entry


def build(key: Key, stamp: Stamp, body: bytes) -> CachedBody:
    """Compress ``body`` and remember it (called on a worker thread)."""
    global _bytes
    entry = CachedBody(stamp, gzip.compress(body, compresslevel=GZIP_LEVEL), etag_for(key, stamp))
    if len(entry.gzipped) > MAX_BYTES:
        return entry
    with _lock:
        previous = _entries.pop(key, None)
        if previous is not None:
            _bytes -= len(previous.gzipped)
        _entries[key] = entry
        _bytes += len(entry.gzipped)
        while _bytes > MAX_BYTES:
            _old, evicted = _entries.popitem(last=False)
            _bytes -= len(evicted.gzipped)
    return entry


def clear() -> None:
    global _bytes
    with _lock:
        _entries.clear()
        _bytes = 0
