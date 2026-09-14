"""Webhooks — notify external systems when PAXS events occur (e.g. a new
study was received). URLs are configured via the WEBHOOK_URLS setting
(comma-separated). Delivery is fire-and-forget from a worker thread so the
receiver/API never block on slow webhook targets.

Payload schema (all events): {\"event\": ..., \"occurred_at\": ISO, ...event data}
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request
from datetime import datetime, timezone

from app.core.config import settings

log = logging.getLogger("paxs.webhooks")

_MAX_RETRIES = 2
_TIMEOUT = 8.0


def configured_urls() -> list[str]:
    raw = getattr(settings, "WEBHOOK_URLS", "") or ""
    return [u.strip() for u in raw.split(",") if u.strip()]


def _deliver(url: str, payload: dict) -> None:
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
                log.warning("webhook %s -> HTTP %s", url, resp.status)
        except Exception as exc:
            if attempt < _MAX_RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
            else:
                log.warning("webhook %s failed after %d tries: %s",
                            url, _MAX_RETRIES, exc)


def fire(event: str, **data) -> None:
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
        threading.Thread(
            target=_deliver, args=(url, payload), daemon=True, name=f"webhook-{event[:12]}"
        ).start()
