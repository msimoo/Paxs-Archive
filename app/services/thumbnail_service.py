"""Generates small PNG thumbnails for fast study-list browsing.

Thumbnails are best-effort: missing decode handlers, odd pixel data or corrupt
files just return False — never crash the worker. Reads/writes go through the
active storage backend (local filesystem or S3), so thumbnails live next to
the archive regardless of backend.
"""

from __future__ import annotations

import io
import logging

from app.services import storage_service
from app.services.imaging import dataset_to_image

log = logging.getLogger("paxs.thumbnail")

DEFAULT_SIZE = 192


def generate_thumbnail(src_key: str, dst_key: str, size: int = DEFAULT_SIZE,
                       backend_name: str | None = None) -> bool:
    """Generate a PNG thumbnail next to the source object on the same backend
    (`backend_name` = the multi-storage backend holding the object)."""
    try:
        ds = storage_service.read_dataset(src_key, backend_name=backend_name)
        img = dataset_to_image(ds, max_size=size)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        storage_service.save_bytes(dst_key, buf.getvalue(), backend_name=backend_name)
        return True
    except Exception as exc:
        # WARNING (not debug): a failed thumbnail usually means a missing pixel
        # decode plugin (e.g. pylibjpeg not installed) or an unreadable file —
        # surface the reason in the server log so it is diagnosable.
        log.warning("Thumbnail failed for %s (backend=%s): %s", src_key, backend_name, exc)
        return False
