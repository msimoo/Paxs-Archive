"""Demo server script (roadmap #43).

Logs every archive event and, on new studies, prints the study metadata the
ScriptContext exposes. Safe to delete — it only writes log lines.
"""

# Optional: restrict to specific events. When omitted the script runs for
# every event it has a handler for.
EVENTS = ["StudyReceived", "StudyDeleted", "CriticalResultFlagged"]


def handle(event, ctx):
    ctx.log.info("[%s] %s — %d payload keys", ctx._name, event, len(ctx.payload))


def on_study_received(ctx):
    study_uid = ctx.payload.get("study_uid")
    study = ctx.study(study_uid) if study_uid else None
    if study:
        n_series = len(study["series"])
        patient = study["patient"]
        ctx.log.info(
            "Study %s received: patient=%s (%s), series=%d, desc=%r",
            study_uid, patient["name"], patient["id"], n_series,
            study["study_description"],
        )
        instances = ctx.instances(study_uid)
        ctx.log.info("  %d instance(s) indexed", len(instances))
    else:
        ctx.log.info("Study %s not yet visible in the DB (indexing in progress)",
                     study_uid)
