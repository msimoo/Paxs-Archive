"""Stored-object content versions and the cache keys derived from them (C-04).

A SOP Instance UID is not a content address in this archive: re-ingest,
lossless compression, lossy transcode and split/merge all rewrite the object
behind an existing UID. ``Instance.content_version`` counts those rewrites, and
this module is the single place that turns (instance, version) into

* the HTTP entity tag the DICOMweb endpoints send, and
* every Redis / in-process cache key for frames, renders and thumbnails,

so a rewrite makes every older cached representation unreachable at once. The
old entries are never served again; they simply age out on their TTL.

Rejected alternative: hashing the response body for the ETag (the previous
behaviour). It is correct but must read and hash the whole object even to
answer a 304, and it says nothing about Redis entries keyed by SOP UID alone.
"""

from __future__ import annotations

import hashlib
from typing import Any


def version_of(instance: Any) -> int:
    """The instance's content version; rows from before the column default to 1."""
    value = getattr(instance, "content_version", None)
    try:
        return max(1, int(value)) if value is not None else 1
    except (TypeError, ValueError):
        return 1


def bump(instance: Any) -> int:
    """Advance the content version of an instance whose stored bytes changed.

    Call it in the same transaction that repoints or rewrites the object, so
    the new version becomes visible exactly when the new bytes do.
    """
    new_version = version_of(instance) + 1
    instance.content_version = new_version
    # S9: the per-series frame lookup cache holds versions; drop it so the new
    # version (ETag / Redis key) is used on the very next frame request (C-04).
    from app.services.frame_instance_cache import frame_instances

    frame_instances.clear()
    return new_version


def entity_tag(instance: Any, representation: str) -> str:
    """Strong ETag for one representation of one stored-object version.

    ``representation`` distinguishes the variants served for the same object
    (raw DICOM, a frame list in a given transfer-syntax mode, a rendered size
    and window, ...). Only identity goes into the tag, never PHI.
    """
    digest = hashlib.sha256(representation.encode("utf-8")).hexdigest()[:16]
    return f'"{getattr(instance, "id", 0)}-{version_of(instance)}-{digest}"'


def raw_frame_key(tenant_id: int | None, sop_uid: str, version: int, frame: int) -> str:
    """Redis key of one decoded raw frame (``transcode`` / warm path)."""
    scope = f"tenant:{tenant_id}:" if tenant_id is not None else ""
    return f"paxs:frame:{scope}{sop_uid}:v{version}:{frame}"


def formatted_frame_key(scope: str, sop_uid: str, version: int, frames: str,
                        mode: str, part: str) -> str:
    """Redis key of a formatted WADO-RS frame response body."""
    return f"paxs:frame:{scope}:{sop_uid}:v{version}:{frames}:{mode}:{part}"


def rendered_key(sop_uid: str, version: int, window: str, size: str,
                 presentation: str, fmt: str, frame: int = 0) -> str:
    """Redis key of a /rendered image (``frame`` is the 0-based frame, H-09)."""
    return (f"paxs:rendered:{sop_uid}:v{version}:f{frame}:{window}:{size}:"
            f"{presentation}:{fmt}")


def thumbnail_key(sop_uid: str, version: int, window: str, size: str, fmt: str) -> str:
    """Redis key of a rendered thumbnail (shared with the cache warmer)."""
    return f"paxs:thumb:{sop_uid}:v{version}:{window}:{size}:{fmt}"
