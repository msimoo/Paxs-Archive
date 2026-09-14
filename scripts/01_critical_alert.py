"""Sample script — forward critical results to an external pager/webhook.

Set CRITICAL_WEBHOOK_URL below (or leave it blank to only log). Demonstrates
ctx.http_post and per-event handlers.
"""

EVENTS = ["CriticalResultFlagged"]

# Set to e.g. "https://example.com/pager" to receive critical alerts.
CRITICAL_WEBHOOK_URL = ""


def on_critical_result_flagged(ctx):
    p = ctx.payload
    ctx.log.info(
        "CRITICAL %s on study %s (reported by %s)",
        p.get("finding_level"), p.get("study_instance_uid"), p.get("reported_by"),
    )
    if CRITICAL_WEBHOOK_URL:
        status = ctx.http_post(CRITICAL_WEBHOOK_URL, {
            "event": "CriticalResultFlagged",
            "study_uid": p.get("study_instance_uid"),
            "patient_id": p.get("patient_id"),
            "finding_level": p.get("finding_level"),
            "description": p.get("description"),
            "occurred_at": p.get("occurred_at"),
        })
        ctx.log.info("pager notified (HTTP %s)", status)
