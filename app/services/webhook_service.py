"""Webhooks — notify external systems when PAXS events occur (e.g. a new
study was received). URLs are configured via the WEBHOOK_URLS setting
(comma-separated). Delivery is fire-and-forget from a worker thread so the
receiver/API never block on slow webhook targets.

Payload schema (all events): {\"event\": ..., \"occurred_at\": ISO, ...event data}
"""

from __future__ import annotations

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app.core.concurrency import submit_background
from app.core.config import settings

log = logging.getLogger("paxs.webhooks")

_MAX_RETRIES = 2
_TIMEOUT = 8.0


def configured_urls() -> list[str]:
    raw = getattr(settings, "WEBHOOK_URLS", "") or ""
    return [u.strip() for u in raw.split(",") if u.strip()]


def _deliver(url: str, payload: dict[str, Any]) -> None:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": "PAXS-Webhook/1.0"},
        method="POST",
    )
    for attempt in range(_MAX_RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
                if 200 <= resp.status < 300:
                    return
                log.warning("webhook_delivery_failed status=%d", resp.status)
        except Exception as exc:
            # Delivery is best effort; exception strings can contain the
            # configured URL, credentials, response data, or patient data.
            if attempt < _MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
            else:
                log.warning(
                    "webhook_delivery_failed attempts=%d error_type=%s",
                    _MAX_RETRIES,
                    type(exc).__name__,
                )


def fire(event: str, **data: Any) -> None:
    """Queue a webhook delivery (non-blocking). Emits nothing if no webhooks\n    are configured."""
    urls = configured_urls()
    if not urls:
        return
    payload = {
        "event": event,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        **data,
    }
    for url in urls:
        if not submit_background(_deliver, url, payload):
            log.warning("webhook_dispatch_dropped reason=background_pool_saturated")
