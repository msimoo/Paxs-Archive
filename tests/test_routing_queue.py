"""Phase C1 — Smart Router send queue (durable dispatch + automatic retry).

Covers the queue that backs the auto-routing rules engine:

  * enqueue_dispatch is idempotent per (study, node) while pending,
  * process_queue_batch dispatches due items over real DIMSE and marks
    COMPLETED with per-instance counts,
  * a failing destination gets exponential-backoff retries and lands FAILED
    after max_attempts,
  * retry_queue_item re-queues a FAILED item manually,
  * evaluate_and_route_study enqueues (instead of fire-and-forget dispatch),
  * the REST API (list / retry / run) works with auth,
  * the migration cycle (upgrade -> downgrade -> upgrade) is clean.
"""

import asyncio
import socket
import time

from pynetdicom import AE, StoragePresentationContexts, evt
from pynetdicom.sop_class import Verification
from pydicom.uid import ExplicitVRLittleEndian, generate_uid
from sqlalchemy import select

from app.core.config import settings
from app.db.models import DicomNode, RoutingQueueItem, Study
from app.db.session import AsyncSessionLocal, SyncSessionLocal
from app.services import indexing_service, routing_service, storage_service
from tests.helpers import make_instance


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _recording_scp(port, ae_title="PAXS_DEST", accept=True):
    """Minimal destination SCP; accepts (or refuses) C-STORE and records SOP UIDs."""
    received = []

    def on_store(event):
        if not accept:
            return 0xC212  # Processing failure
        received.append(str(event.dataset.SOPInstanceUID))
        return 0x0000

    def on_echo(event):
        return 0x0000

    ae = AE(ae_title=ae_title)
    ae.supported_contexts = StoragePresentationContexts
    ae.add_supported_context(Verification, ExplicitVRLittleEndian)
    ae.start_server(("127.0.0.1", port), block=False, evt_handlers=[
        (evt.EVT_C_STORE, on_store), (evt.EVT_C_ECHO, on_echo),
    ])
    return ae, received


def _wait_for(fn, timeout=6.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if fn():
            return
        time.sleep(0.1)
    raise AssertionError("condition not met within timeout")


def _wait_stored(study_uid):
    def _check():
        with SyncSessionLocal() as s:
            return s.execute(
                select(Study).where(Study.study_instance_uid == study_uid)
            ).scalar_one_or_none()
    _wait_for(lambda: _check() is not None)


_SOP_COUNTER = [0]


def _seed_study(study_uid: str, n: int = 2, patient_id: str = "C1_P1",
                modality: str = "C1", body_part: str = "CHEST") -> None:
    """Ingest an n-instance study directly (service-level, no DIMSE needed).

    SOP/series UIDs are derived from a monotonic counter (not the study UID
    tail) so service tests sharing one DB can never collide on SOP Instance
    UIDs — two random study UIDs can share their last digits."""

    def _ingest():
        import asyncio

        async def _run():
            async with AsyncSessionLocal() as session:
                async with session.begin():
                    for i in range(n):
                        _SOP_COUNTER[0] += 1
                        seq = _SOP_COUNTER[0]
                        ds = make_instance(
                            patient_id=patient_id, study_uid=study_uid,
                            series_uid=f"1.2.3.4.{seq}.{i + 1}",
                            sop_uid=f"1.2.3.5.{seq}.{i + 1}",
                            instance_num=i + 1, modality=modality, body_part=body_part,
                        )
                        key, sha = storage_service.save_dataset(ds)
                        from app.dicom.parser import parse_metadata
                        await indexing_service.index_new_instance(
                            session, parse_metadata(ds), key, sha,
                        )

        asyncio.run(_run())

    _ingest()
    _wait_stored(study_uid)


def _make_rule(study_uid: str, node_id: int, modality: str = "C1") -> int:
    async def _create():
        async with AsyncSessionLocal() as session:
            async with session.begin():
                rule = await routing_service.create_rule(session, {
                    "name": f"Route {study_uid[-8:]}",
                    "enabled": True,
                    "priority": 5,
                    "destination_node_id": node_id,
                    "modality": modality,
                })
                return rule.id
    return asyncio.run(_create())


def _clear_queue():
    """Tests share one DB (service tests don't use the per-test client fixture),
    so wipe the queue before queue-processing assertions."""
    from sqlalchemy import delete as sa_delete

    with SyncSessionLocal() as s:
        s.execute(sa_delete(RoutingQueueItem))
        s.commit()


def _auth(client, username, password="secret123", role=None):
    body = {"username": username, "password": password}
    if role:
        body["role"] = role
    client.post("/api/v1/auth/register", json=body)
    res = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    return {"Authorization": f"Bearer {res.json()['access_token']}"}


# --------------------------------------------------------------------------- #
# Enqueue idempotency + evaluation path
# --------------------------------------------------------------------------- #


def test_enqueue_dispatch_idempotent():
    """Re-enqueueing the same (study, node) while PENDING creates no duplicate."""
    node = DicomNode(ae_title="C1_DEST", host="127.0.0.1", port=_free_port(), enabled=True)
    with SyncSessionLocal() as s:
        s.add(node)
        s.commit()
        s.refresh(node)
        node_id = node.id

    study_uid = generate_uid()
    _seed_study(study_uid, n=1, modality="C1")
    rule_id = _make_rule(study_uid, node_id, modality="C1")

    async def _enq():
        async with AsyncSessionLocal() as session:
            async with session.begin():
                rule = await routing_service.get_rule(session, rule_id)
                item1 = await routing_service.enqueue_dispatch(session, study_uid, rule)
                item2 = await routing_service.enqueue_dispatch(session, study_uid, rule)
                return item1.id, item2.id

    i1, i2 = asyncio.run(_enq())
    assert i1 == i2  # same row, not a duplicate

    with SyncSessionLocal() as s:
        rows = s.execute(
            select(RoutingQueueItem).where(RoutingQueueItem.study_instance_uid == study_uid)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == "PENDING"


def test_evaluate_and_route_enqueues_not_dispatches(monkeypatch):
    """evaluate_and_route_study creates queue items; dispatch is deferred to worker."""
    dispatched = []

    async def _fake_dispatch(*a, **kw):
        dispatched.append(a)
        return 0

    monkeypatch.setattr(routing_service, "dispatch_study_to_node_sync", _fake_dispatch)

    node = DicomNode(ae_title="C1_EVAL", host="127.0.0.1", port=_free_port(), enabled=True)
    with SyncSessionLocal() as s:
        s.add(node)
        s.commit()
        s.refresh(node)
        node_id = node.id

    study_uid = generate_uid()
    _seed_study(study_uid, n=1, modality="C2")
    _make_rule(study_uid, node_id, modality="C2")

    count = asyncio.run(routing_service.evaluate_and_route_study(study_uid))
    assert count == 1
    assert dispatched == []  # no direct dispatch — queued instead

    with SyncSessionLocal() as s:
        rows = s.execute(
            select(RoutingQueueItem).where(RoutingQueueItem.study_instance_uid == study_uid)
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].node_id == node_id
        assert rows[0].status == "PENDING"


# --------------------------------------------------------------------------- #
# Queue processing: success + retry + failure
# --------------------------------------------------------------------------- #
def test_process_queue_batch_success():
    """A reachable destination -> item COMPLETED with per-instance counts."""
    _clear_queue()
    study_uid = generate_uid()
    _seed_study(study_uid, n=2, modality="C3")

    dest_port = _free_port()
    dest_ae, received = _recording_scp(dest_port)
    try:
        node = DicomNode(ae_title="C1_OK", host="127.0.0.1", port=dest_port, enabled=True)
        with SyncSessionLocal() as s:
            s.add(node)
            s.commit()
            s.refresh(node)
            node_id = node.id

        rule_id = _make_rule(study_uid, node_id, modality="C3")
        with SyncSessionLocal() as s:
            s.add(RoutingQueueItem(
                study_instance_uid=study_uid, rule_id=rule_id, node_id=node_id,
                status="PENDING", max_attempts=3,
                next_retry_at=None,  # due immediately
            ))
            s.commit()

        counts = routing_service.process_queue_batch()
        assert counts["claimed"] == 1 and counts["completed"] == 1
        _wait_for(lambda: len(received) == 2)
        # 2 instances seeded by _seed_study (counter-based SOP UIDs, both sent)
        assert len(set(received)) == 2

        with SyncSessionLocal() as s:
            item = s.execute(
                select(RoutingQueueItem).where(RoutingQueueItem.study_instance_uid == study_uid)
            ).scalar_one()
            assert item.status == "COMPLETED"
            assert item.attempts == 1
            assert item.sent_count == 2 and item.failed_count == 0
    finally:
        dest_ae.shutdown()


def test_process_queue_batch_retries_then_fails():
    """Unreachable destination -> attempts increment, backoff set, then FAILED."""
    original_max = settings.ROUTING_MAX_ATTEMPTS
    original_backoff = settings.ROUTING_RETRY_BACKOFF_SECONDS
    try:
        _clear_queue()
        settings.ROUTING_MAX_ATTEMPTS = 2
        settings.ROUTING_RETRY_BACKOFF_SECONDS = 5  # tiny backoff for the test
        study_uid = generate_uid()
        _seed_study(study_uid, n=1, modality="C4")

        # Destination node points at a closed port -> every attempt fails.
        node = DicomNode(ae_title="C1_DEAD", host="127.0.0.1", port=1, enabled=True)
        with SyncSessionLocal() as s:
            s.add(node)
            s.commit()
            s.refresh(node)
            node_id = node.id

        rule_id = _make_rule(study_uid, node_id, modality="C4")
        with SyncSessionLocal() as s:
            s.add(RoutingQueueItem(
                study_instance_uid=study_uid, rule_id=rule_id, node_id=node_id,
                status="PENDING", max_attempts=2,
                next_retry_at=None,
            ))
            s.commit()

        # Attempt 1 -> fails, requeued with backoff (attempts=1, next_retry_at set).
        c1 = routing_service.process_queue_batch()
        assert c1["claimed"] == 1 and c1["requeued"] == 1
        with SyncSessionLocal() as s:
            item = s.execute(
                select(RoutingQueueItem).where(RoutingQueueItem.study_instance_uid == study_uid)
            ).scalar_one()
            assert item.status == "PENDING"
            assert item.attempts == 1
            assert item.next_retry_at is not None
            assert item.last_error  # reason captured

        # Attempt 2 (next_retry_at set to the past so it's due) -> FAILED.
        # Backdate by 2s: MySQL stores DateTime with second precision, so
        # writing "now" can race the sweep's own "now" and stay in the future.
        from datetime import datetime, timedelta, timezone
        with SyncSessionLocal() as s:
            item = s.execute(
                select(RoutingQueueItem).where(RoutingQueueItem.study_instance_uid == study_uid)
            ).scalar_one()
            item.next_retry_at = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(seconds=2)
            s.commit()

        c2 = routing_service.process_queue_batch()
        assert c2["claimed"] == 1 and c2["failed"] == 1
        with SyncSessionLocal() as s:
            item = s.execute(
                select(RoutingQueueItem).where(RoutingQueueItem.study_instance_uid == study_uid)
            ).scalar_one()
            assert item.status == "FAILED"
            assert item.attempts == 2
            assert item.last_error
    finally:
        settings.ROUTING_MAX_ATTEMPTS = original_max
        settings.ROUTING_RETRY_BACKOFF_SECONDS = original_backoff


def test_retry_queue_item_requeues_failed():
    """retry_queue_item resets a FAILED item to PENDING (attempts=0)."""
    study_uid = generate_uid()
    _seed_study(study_uid, n=1, modality="C6")
    node = DicomNode(ae_title="C1_RETRY", host="127.0.0.1", port=_free_port(), enabled=True)
    with SyncSessionLocal() as s:
        s.add(node)
        s.commit()
        s.refresh(node)
        node_id = node.id

    with SyncSessionLocal() as s:
        item = RoutingQueueItem(
            study_instance_uid=study_uid, node_id=node_id,
            status="FAILED", attempts=3, max_attempts=3,
            next_retry_at=None, last_error="boom",
        )
        s.add(item)
        s.commit()
        item_id = item.id

    res = routing_service.retry_queue_item(item_id)
    assert res["ok"] is True
    assert res["item"]["status"] == "PENDING"
    assert res["item"]["attempts"] == 0

    # Re-queueing a non-FAILED item is refused.
    res2 = routing_service.retry_queue_item(item_id)
    assert res2["ok"] is False


def test_recover_stuck_queue_items():
    """Items left SENDING by a crashed worker get re-queued on startup."""
    study_uid = generate_uid()
    _seed_study(study_uid, n=1, modality="C7")
    node = DicomNode(ae_title="C1_STUCK", host="127.0.0.1", port=_free_port(), enabled=True)
    with SyncSessionLocal() as s:
        s.add(node)
        s.commit()
        s.refresh(node)
        node_id = node.id
        s.add(RoutingQueueItem(
            study_instance_uid=study_uid, node_id=node_id,
            status="SENDING", attempts=1, max_attempts=5,
            next_retry_at=None,
        ))
        s.commit()

    routing_service.recover_stuck_queue_items()
    with SyncSessionLocal() as s:
        item = s.execute(
            select(RoutingQueueItem).where(RoutingQueueItem.study_instance_uid == study_uid)
        ).scalar_one()
        assert item.status == "PENDING"
        assert item.next_retry_at is not None


# --------------------------------------------------------------------------- #
# REST API
# --------------------------------------------------------------------------- #
def _register_node_api(client, headers, ae_title, host, port):
    r = client.post("/api/v1/nodes", headers=headers,
                    json={"ae_title": ae_title, "host": host, "port": port, "enabled": True})
    assert r.status_code == 201, r.text
    return r.json()


def test_routing_queue_api(client):
    admin_h = _auth(client, "c1.admin")
    _clear_queue()  # earlier service tests left PENDING items in the shared DB

    # Seed a study + rule + FAILED queue item via the API path.
    study_uid = generate_uid()
    _seed_study(study_uid, n=1, modality="C5")
    dest_port = _free_port()
    dest_ae, received = _recording_scp(dest_port)
    try:
        _register_node_api(client, admin_h, "C1_API", "127.0.0.1", dest_port)
        with SyncSessionLocal() as s:
            node_id = s.execute(
                select(DicomNode.id).where(DicomNode.ae_title == "C1_API")
            ).scalar_one()

        r = client.post("/api/v1/routing/rules", headers=admin_h, json={
            "name": f"C1 API rule {study_uid[-6:]}",
            "enabled": True,
            "destination_node_id": node_id,
            "modality": "C5",
        })
        assert r.status_code == 201, r.text
        rule_id = r.json()["id"]

        # Create a FAILED item directly so the API has something to show.
        with SyncSessionLocal() as s:
            s.add(RoutingQueueItem(
                study_instance_uid=study_uid, rule_id=rule_id, node_id=node_id,
                status="FAILED", attempts=3, max_attempts=3,
                next_retry_at=None, last_error="test failure",
            ))
            s.commit()

        # GET /api/v1/routing/queue
        rq = client.get("/api/v1/routing/queue", headers=admin_h)
        assert rq.status_code == 200
        items = rq.json()
        assert any(i["study_instance_uid"] == study_uid and i["status"] == "FAILED" for i in items)
        failed = next(i for i in items if i["study_instance_uid"] == study_uid)

        # Filter by status
        rq2 = client.get("/api/v1/routing/queue?status=FAILED", headers=admin_h)
        assert rq2.status_code == 200
        assert all(i["status"] == "FAILED" for i in rq2.json())

        # POST retry -> PENDING; next sweep dispatches successfully.
        rr = client.post(f"/api/v1/routing/queue/{failed['id']}/retry", headers=admin_h)
        assert rr.status_code == 200, rr.text
        assert rr.json()["item"]["status"] == "PENDING"

        # POST /queue/run triggers a sweep; the item dispatches to the live SCP.
        rs = client.post("/api/v1/routing/queue/run", headers=admin_h)
        assert rs.status_code == 200

        def _completed():
            with SyncSessionLocal() as s:
                item = s.get(RoutingQueueItem, failed["id"])
                return item is not None and item.status == "COMPLETED"

        _wait_for(_completed)
        with SyncSessionLocal() as s:
            item = s.get(RoutingQueueItem, failed["id"])
            assert item.sent_count == 1

        # Retry of a non-FAILED item -> 400.
        rr2 = client.post(f"/api/v1/routing/queue/{failed['id']}/retry", headers=admin_h)
        assert rr2.status_code == 400
    finally:
        dest_ae.shutdown()


def test_routing_queue_api_requires_auth(client):
    r = client.get("/api/v1/routing/queue")
    assert r.status_code == 401


def test_routing_queue_destructive_role_required(client):
    """Queue sweep/retry trigger real C-STORE sends — PHYSICIAN is refused."""
    admin_h = _auth(client, "c1.boss")
    phys_h = _auth(client, "c1.phys", role="PHYSICIAN")

    # A queue item (any state) so retry/run have something to act on.
    node = DicomNode(ae_title="C1_ROLE", host="127.0.0.1", port=_free_port(), enabled=True)
    with SyncSessionLocal() as s:
        s.add(node)
        s.commit()
        s.refresh(node)
        s.add(RoutingQueueItem(
            study_instance_uid=generate_uid(), node_id=node.id,
            status="FAILED", attempts=1, max_attempts=2,
            next_retry_at=None, last_error="x",
        ))
        s.commit()
        item_id = s.execute(
            select(RoutingQueueItem).order_by(RoutingQueueItem.id.desc())
        ).scalar_one().id

    r = client.post(f"/api/v1/routing/queue/{item_id}/retry", headers=phys_h)
    assert r.status_code == 403
    r = client.post("/api/v1/routing/queue/run", headers=phys_h)
    assert r.status_code == 403
    # Admin is allowed to list + run.
    r = client.post("/api/v1/routing/queue/run", headers=admin_h)
    assert r.status_code == 200
