"""Tests for Study Lifecycle Management (item 14 — auto-archive, restore, purge).

Covers:
  * rule CRUD via the REST API (create/list/get/update/delete + validation)
  * real tiering: ARCHIVE moves instance objects under `.archive/` and marks
    the study ARCHIVED; RESTORE moves them back (verifiable on the backend)
  * PURGE removes objects (hot + cold) and the study row
  * evaluate_and_run — the same engine the worker sweeps with
  * role gating — purge requires a destructive role
  * dashboard stats counters (lifecycle.rules / archived / online)
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AuditLog, Study
from app.db.session import AsyncSessionLocal, sync_engine
from app.services import indexing_service, lifecycle_service, storage_service
from tests.helpers import make_instance

ARCHIVE_PREFIX = lifecycle_service.ARCHIVE_PREFIX


def _ingest_study(patient_id: str, *, modality: str = "CT", body_part: str = "CHEST",
                  study_date: str = "20240110") -> str:
    """Save a study (1 instance) to the archive; returns its StudyInstanceUID."""
    import asyncio

    from app.dicom.parser import parse_metadata

    ds = make_instance(patient_id=patient_id, modality=modality, body_part=body_part,
                       study_date=study_date)
    key, sha = storage_service.save_dataset(ds)

    async def _index():
        async with AsyncSessionLocal() as s:
            async with s.begin():
                await indexing_service.index_new_instance(
                    s, parse_metadata(ds), key, sha, performed_by="test"
                )

    asyncio.run(_index())
    return str(ds.StudyInstanceUID)


def _instance_key_of(study_uid: str) -> str:
    with Session(sync_engine) as session:
        from app.db.models import Instance, Series

        key = (
            session.execute(
                select(Instance.file_path)
                .join(Series, Series.id == Instance.series_id)
                .where(Series.study_id == select(Study.id)
                       .where(Study.study_instance_uid == study_uid).scalar_subquery())
            )
            .scalars()
            .first()
        )
    return key


def _study_state(study_uid: str) -> str:
    with Session(sync_engine) as session:
        st = session.execute(
            select(Study.lifecycle_state).where(Study.study_instance_uid == study_uid)
        ).scalar_one()
    return st


def _get_auth_headers(client, username="admin_lifecycle", password="adminpassword",
                      role="ADMIN"):
    reg = client.post(
        "/api/v1/auth/register",
        json={"username": username, "password": password,
              "full_name": "Admin Lifecycle", "role": role},
    )
    if reg.status_code == 201:
        token = reg.json()["access_token"]
    else:
        login = client.post(
            "/api/v1/auth/login", json={"username": username, "password": password},
        )
        token = login.json()["access_token"]
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------------- #
# Rule CRUD via REST
# --------------------------------------------------------------------------- #
def test_lifecycle_rule_crud(client):
    headers = _get_auth_headers(client)

    # 1. Create
    resp = client.post(
        "/api/v1/lifecycle/rules",
        json={
            "name": "Archive CT after 1y",
            "description": "Move aged CTs to the cold tier",
            "enabled": True,
            "priority": 5,
            "action": "ARCHIVE",
            "min_age_days": 365,
            "modality": "CT",
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    rule = resp.json()
    assert rule["action"] == "ARCHIVE"
    assert rule["modality"] == "CT"
    rule_id = rule["id"]

    # 2. Invalid action rejected
    resp = client.post(
        "/api/v1/lifecycle/rules",
        json={"name": "Bad", "action": "EXPLODE"},
        headers=headers,
    )
    assert resp.status_code == 422

    # 3. List
    resp = client.get("/api/v1/lifecycle/rules", headers=headers)
    assert resp.status_code == 200
    ids = [r["id"] for r in resp.json()]
    assert rule_id in ids

    # 4. Get one
    resp = client.get(f"/api/v1/lifecycle/rules/{rule_id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["name"] == "Archive CT after 1y"

    # 5. Update
    resp = client.put(
        f"/api/v1/lifecycle/rules/{rule_id}",
        json={"min_age_days": 180, "enabled": False},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["min_age_days"] == 180
    assert resp.json()["enabled"] is False

    # 6. Delete
    resp = client.delete(f"/api/v1/lifecycle/rules/{rule_id}", headers=headers)
    assert resp.status_code == 200
    assert client.get(f"/api/v1/lifecycle/rules/{rule_id}", headers=headers).status_code == 404


# --------------------------------------------------------------------------- #
# Real tiering: archive / restore moves objects on the backend
# --------------------------------------------------------------------------- #
def test_archive_and_restore_move_objects():
    study_uid = _ingest_study("LC_P1")
    key = _instance_key_of(study_uid)
    assert key and not key.startswith(ARCHIVE_PREFIX)
    assert storage_service.exists(key)

    # ARCHIVE
    result = lifecycle_service.archive_study(study_uid, "test")
    assert result["ok"] is True and result["moved"] == 1
    assert _study_state(study_uid) == "ARCHIVED"
    assert not storage_service.exists(key)          # hot copy gone
    cold_key = f"{ARCHIVE_PREFIX}{key}"
    assert storage_service.exists(cold_key)         # cold copy present

    # Idempotent: re-archiving an archived study is a no-op (0 moved)
    result = lifecycle_service.archive_study(study_uid, "test")
    assert result["ok"] is True and result["moved"] == 0

    # RESTORE
    result = lifecycle_service.restore_study(study_uid, "test")
    assert result["ok"] is True and result["moved"] == 1
    assert _study_state(study_uid) == "ONLINE"
    assert storage_service.exists(key)
    assert not storage_service.exists(cold_key)

    # Audit trail recorded both actions
    with Session(sync_engine) as session:
        actions = {
            a.action for a in session.execute(
                select(AuditLog).where(AuditLog.study_instance_uid == study_uid)
            ).scalars().all()
        }
    assert {"ARCHIVED", "RESTORED"} <= actions


def test_purge_deletes_objects_and_metadata():
    study_uid = _ingest_study("LC_P2")
    key = _instance_key_of(study_uid)

    # Archive it first, then purge — cold objects must be deleted too.
    lifecycle_service.archive_study(study_uid, "test")
    cold_key = f"{ARCHIVE_PREFIX}{key}"
    assert storage_service.exists(cold_key)

    result = lifecycle_service.purge_study(study_uid, "test")
    assert result["ok"] is True and result["purged"] is True
    assert not storage_service.exists(key)
    assert not storage_service.exists(cold_key)

    with Session(sync_engine) as session:
        assert session.execute(
            select(Study).where(Study.study_instance_uid == study_uid)
        ).scalar_one_or_none() is None


def test_purge_requires_destructive_role(client):
    # First registered user is forced to ADMIN — bootstrap one, then create a
    # PHYSICIAN whose purge attempt must be rejected.
    _get_auth_headers(client, username="root_lifecycle", role="ADMIN")
    headers = _get_auth_headers(client, username="phys_lifecycle", role="PHYSICIAN")
    study_uid = _ingest_study("LC_P3")
    resp = client.post(f"/api/v1/lifecycle/studies/{study_uid}/purge", headers=headers)
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# Rule engine sweep (worker path)
# --------------------------------------------------------------------------- #
def test_evaluate_and_run_archives_matching_studies():
    # One old CT (matches), one old MR (modality filter excludes it), one
    # recent CT (age filter excludes it).
    old_ct = _ingest_study("LC_P4", modality="CT", study_date="20230105")
    old_mr = _ingest_study("LC_P5", modality="MR", study_date="20230105")
    recent_ct = _ingest_study("LC_P6", modality="CT", study_date="20260701")

    with Session(sync_engine) as session:
        session.add(lifecycle_service.create_rule_sync(
            session, {
                "name": "Auto archive old CT",
                "action": "ARCHIVE",
                "min_age_days": 90,
                "modality": "CT",
            }))
        session.commit()

    counts = lifecycle_service.evaluate_and_run()
    assert counts["archived"] >= 1
    assert _study_state(old_ct) == "ARCHIVED"
    assert _study_state(old_mr) == "ONLINE"      # modality excluded
    assert _study_state(recent_ct) == "ONLINE"   # too young

    # A second sweep archives nothing new (idempotent — already ARCHIVED).
    counts = lifecycle_service.evaluate_and_run()
    assert counts["archived"] == 0


# --------------------------------------------------------------------------- #
# API run + archived-studies list + stats
# --------------------------------------------------------------------------- #
def test_lifecycle_api_run_and_list(client):
    headers = _get_auth_headers(client)
    study_uid = _ingest_study("LC_P7")

    client.post(
        "/api/v1/lifecycle/rules",
        json={"name": "Archive everything", "action": "ARCHIVE", "min_age_days": 0},
        headers=headers,
    )
    resp = client.post("/api/v1/lifecycle/run", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["archived"] >= 1

    # The study now appears in the cold-tier list.
    resp = client.get("/api/v1/lifecycle/studies?state=ARCHIVED", headers=headers)
    assert resp.status_code == 200
    uids = [r["study_uid"] for r in resp.json()]
    assert study_uid in uids

    # Per-study restore through the API.
    resp = client.post(f"/api/v1/lifecycle/studies/{study_uid}/restore", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["moved"] == 1

    # 404 for a missing study.
    resp = client.post("/api/v1/lifecycle/studies/1.2.999/archive", headers=headers)
    assert resp.status_code == 404


def test_stats_include_lifecycle_counters(client):
    headers = _get_auth_headers(client)
    _ingest_study("LC_P8")
    client.post(
        "/api/v1/lifecycle/rules",
        json={"name": "Count rule", "action": "ARCHIVE", "min_age_days": 0},
        headers=headers,
    )
    client.post("/api/v1/lifecycle/run", headers=headers)

    stats = client.get("/api/v1/stats").json()
    lc = stats["features"]["lifecycle"]
    assert lc["rules"] >= 1
    assert lc["archived"] >= 1
    assert lc["online"] >= 0
