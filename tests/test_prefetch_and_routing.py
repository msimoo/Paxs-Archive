"""Regression tests for H-1 (prefetch dead code), H-2 (routing temp-file leak)
and H-13 (blocking calls on the async event loop).

* H-1: ``evaluate_and_trigger_prefetch`` called the non-existent
  ``sender.c_move_study``; every prefetch raised ``AttributeError`` which was
  swallowed, so priors were never fetched.
* H-2: ``dispatch_study_to_node_sync`` wrote a ``.dcm`` temp file per S3 object
  with ``delete=False`` and never unlinked it.
* H-13: ``ping_node`` ran a blocking DIMSE C-ECHO directly on the event loop,
  stalling every other request.
"""

from __future__ import annotations

import asyncio
import os
import threading
import time

import httpx
import pytest
from pydicom.uid import generate_uid
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db.models import DicomNode, PrefetchRule
from app.db.session import AsyncSessionLocal, sync_engine
from app.dicom import sender
from app.dicom.parser import parse_metadata
from app.main import app
from app.services import indexing_service, prefetch_service, routing_service, storage_service
from tests.conftest import _bearer_token, _create_test_user
from tests.helpers import make_instance


@pytest.fixture(autouse=True)
def _no_scripts(monkeypatch, tmp_path):
    """Keep ingest from running the real scripts/ hooks."""
    monkeypatch.setattr(settings, "SCRIPTS_DIR", str(tmp_path / "scripts"))


@pytest.fixture(autouse=True)
def _clean_prefetch_rules():
    """Start every test with no prefetch rules, so a run only evaluates the rule
    the test itself creates (the DB is session-scoped and shared across files)."""
    with sync_engine.begin() as conn:
        conn.execute(PrefetchRule.__table__.delete())
    yield


async def _ingest(ds) -> str:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            key, sha = storage_service.save_dataset(ds)
            await indexing_service.index_new_instance(
                session, parse_metadata(ds), key, sha
            )
    return key


def _make_node(ae_title: str, host: str = "10.0.0.9", port: int = 11112) -> int:
    """Insert (idempotently) a destination node; return its id."""
    with Session(sync_engine) as session:
        session.execute(
            DicomNode.__table__.delete().where(DicomNode.__table__.c.ae_title == ae_title)
        )
        node = DicomNode(ae_title=ae_title, host=host, port=port, enabled=True)
        session.add(node)
        session.commit()
        return node.id


# --------------------------------------------------------------------------- #
# H-1 — prefetch must actually issue a C-MOVE
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_prefetch_invokes_send_move(monkeypatch):
    calls: list[dict] = []

    def fake_send_move(host, port, ae_title, dest_ae_title, query=None,
                       our_ae_title="PAXS_SCU"):
        calls.append({
            "host": host, "port": port, "ae_title": ae_title,
            "dest_ae_title": dest_ae_title, "query": query,
            "our_ae_title": our_ae_title,
        })
        return {"ok": True, "status": 0, "completed": 1, "failed": 0, "remaining": 0}

    monkeypatch.setattr(sender, "send_move", fake_send_move)

    prior_uid = generate_uid()
    monkeypatch.setattr(
        prefetch_service,
        "query_remote_priors_sync",
        lambda *a, **k: [
            {"study_instance_uid": prior_uid, "study_date": "20260101", "modalities": "CT"}
        ],
    )

    node_ae, rule_name = "PREFETCH_SRC_H1", "H1 Prefetch CT"
    node_id = _make_node(node_ae)
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                PrefetchRule.__table__.delete().where(PrefetchRule.__table__.c.name == rule_name)
            )
            session.add(PrefetchRule(
                name=rule_name, enabled=True, priority=1, modality="CT",
                max_prior_age_days=730, max_priors_count=3, source_node_id=node_id,
            ))

    async with AsyncSessionLocal() as session:
        async with session.begin():
            jobs = await prefetch_service.evaluate_and_trigger_prefetch(
                session, patient_id="H1_PATIENT", modality="CT"
            )

    assert len(jobs) == 1
    job = jobs[0]
    assert job.priors_found == 1
    assert job.priors_retrieved == 1
    assert job.status == "COMPLETED"

    # send_move was invoked with the correct mapping (dest = our own AE).
    assert len(calls) == 1, "prefetch must issue exactly one C-MOVE per prior"
    call = calls[0]
    assert call["host"] == "10.0.0.9"
    assert call["port"] == 11112
    assert call["ae_title"] == node_ae
    assert call["dest_ae_title"] == settings.DICOM_AE_TITLE
    assert call["our_ae_title"] == settings.DICOM_AE_TITLE
    assert str(call["query"].QueryRetrieveLevel) == "STUDY"
    assert str(call["query"].StudyInstanceUID) == prior_uid


@pytest.mark.anyio
async def test_prefetch_applies_max_prior_age(monkeypatch):
    """L-13: priors older than the rule's window are not fetched."""
    calls: list[str] = []

    def fake_send_move(host, port, ae_title, dest_ae_title, query=None,
                       our_ae_title="PAXS_SCU"):
        calls.append(str(query.StudyInstanceUID))
        return {"ok": True, "status": 0, "completed": 1, "failed": 0, "remaining": 0}

    monkeypatch.setattr(sender, "send_move", fake_send_move)

    fresh_uid, stale_uid = generate_uid(), generate_uid()
    monkeypatch.setattr(
        prefetch_service,
        "query_remote_priors_sync",
        lambda *a, **k: [
            {"study_instance_uid": fresh_uid, "study_date": "20260101", "modalities": "CT"},
            {"study_instance_uid": stale_uid, "study_date": "20000101", "modalities": "CT"},
        ],
    )

    rule_name = "H1 Age Window"
    node_id = _make_node("PREFETCH_AGE_H1")
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                PrefetchRule.__table__.delete().where(PrefetchRule.__table__.c.name == rule_name)
            )
            session.add(PrefetchRule(
                name=rule_name, enabled=True, priority=1, modality="CT",
                max_prior_age_days=365, max_priors_count=5, source_node_id=node_id,
            ))

    async with AsyncSessionLocal() as session:
        async with session.begin():
            jobs = await prefetch_service.evaluate_and_trigger_prefetch(
                session, patient_id="H1_AGE", modality="CT"
            )

    assert len(jobs) == 1
    assert jobs[0].priors_found == 2          # both matched remotely
    assert jobs[0].priors_retrieved == 1      # only the fresh one was fetched
    assert calls == [fresh_uid]


# --------------------------------------------------------------------------- #
# H-2 — S3 routing must not leak temp files
# --------------------------------------------------------------------------- #
def _mock_s3_object(monkeypatch, payload: bytes = b"FAKE-DICOM-BYTES") -> None:
    """Make every storage lookup behave like a remote (S3) object."""
    monkeypatch.setattr(storage_service, "local_path", lambda key, backend_name=None: None)
    monkeypatch.setattr(storage_service, "exists", lambda key, backend_name=None: True)
    monkeypatch.setattr(storage_service, "read_bytes", lambda key, backend_name=None: payload)


@pytest.mark.anyio
async def test_routing_s3_temp_files_cleaned(monkeypatch):
    study_uid = generate_uid()
    ds = make_instance(patient_id="H2_S3", study_uid=study_uid, modality="CT")
    await _ingest(ds)
    node_id = _make_node("ROUTE_S3_DEST_H2", host="10.0.0.7")

    _mock_s3_object(monkeypatch)

    seen: list[str] = []

    def fake_send_file(host, port, ae_title, file_path):
        seen.append(str(file_path))
        assert os.path.exists(file_path), "the temp file must exist while sending"
        return {"ok": True}

    monkeypatch.setattr(routing_service, "send_file", fake_send_file)

    result = await asyncio.to_thread(
        routing_service.dispatch_study_to_node_sync, study_uid, node_id
    )
    assert result["ok"] is True
    assert result["sent"] >= 1
    assert seen, "the S3 branch must have materialized a temp file"

    for path in seen:
        assert not os.path.exists(path), f"routing temp file leaked: {path}"


@pytest.mark.anyio
async def test_routing_temp_cleaned_even_when_send_fails(monkeypatch):
    """The finally-block removes the temp file even if send_file raises."""
    study_uid = generate_uid()
    ds = make_instance(patient_id="H2_FAIL", study_uid=study_uid, modality="CT")
    await _ingest(ds)
    node_id = _make_node("ROUTE_S3_FAIL_H2", host="10.0.0.7")

    _mock_s3_object(monkeypatch, payload=b"X")

    seen: list[str] = []

    def boom(host, port, ae_title, file_path):
        seen.append(str(file_path))
        raise RuntimeError("association failed")

    monkeypatch.setattr(routing_service, "send_file", boom)

    with pytest.raises(RuntimeError):
        await asyncio.to_thread(
            routing_service.dispatch_study_to_node_sync, study_uid, node_id
        )

    assert seen
    for path in seen:
        assert not os.path.exists(path), f"temp file leaked after a failed send: {path}"


# --------------------------------------------------------------------------- #
# H-13 — a slow C-ECHO must not stall the event loop
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_ping_node_does_not_block_event_loop(monkeypatch):
    _create_test_user("ping_probe_h13", "ADMIN")
    headers = {"Authorization": f"Bearer {_bearer_token('ping_probe_h13', 'ADMIN')}"}

    node_ae = "PING_UNREACHABLE_H13"
    async with AsyncSessionLocal() as session:
        async with session.begin():
            await session.execute(
                DicomNode.__table__.delete().where(DicomNode.__table__.c.ae_title == node_ae)
            )
            session.add(DicomNode(
                ae_title=node_ae, host="10.255.255.1", port=11112, enabled=True
            ))

    # Stand in for an unreachable peer's connect timeout — deterministic, no real
    # network. Runs in a worker thread once the fix is in place.
    started = threading.Event()
    echo_seconds = 1.0

    def slow_echo(host, port, ae_title, our_ae_title="PAXS_SCU"):
        started.set()
        time.sleep(echo_seconds)
        return {"ok": False, "reason": "simulated unreachable"}

    monkeypatch.setattr(sender, "send_echo", slow_echo)

    max_lag_ms = 0.0
    stop = False

    async def _loop_probe():
        nonlocal max_lag_ms
        while not stop:
            t = time.perf_counter()
            await asyncio.sleep(0.01)
            max_lag_ms = max(max_lag_ms, (time.perf_counter() - t) * 1000)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
        probe = asyncio.create_task(_loop_probe())
        ping_task = asyncio.create_task(
            ac.post(f"/api/v1/nodes/{node_ae}/ping", headers=headers)
        )

        # Wait (without blocking the loop) until the C-ECHO is genuinely running.
        for _ in range(400):
            if started.is_set():
                break
            await asyncio.sleep(0.005)

        # A real request must complete promptly WHILE the C-ECHO is in flight.
        t0 = time.perf_counter()
        health = await ac.get("/api/v1/health/live")
        health_ms = (time.perf_counter() - t0) * 1000

        ping = await ping_task
        stop = True
        await probe

    assert started.is_set(), "the C-ECHO never ran"
    assert health.status_code == 200
    # The explicit requirement: a concurrent /health/live stays under 50 ms.
    assert health_ms < 50, f"/api/v1/health/live took {health_ms:.1f} ms during a C-ECHO"
    # Loop-lag guard: with the bug the loop is blocked for ~echo_seconds (1000 ms);
    # off-loaded it stays in the tens of ms.
    assert max_lag_ms < 250, f"event loop stalled for {max_lag_ms:.1f} ms during a C-ECHO"

    assert ping.status_code == 200
    assert ping.json()["ok"] is False
