"""Tests for the Prometheus /metrics endpoint (deploy/MONITORING.md)."""

from app.services.compression_service import codec_status
from tests.test_dicomweb import _upload_study


def test_codec_status_reports_every_method_and_rle_available():
    """The codec probe covers all four lossless methods; RLE is built into
    pydicom so it must always report available, and each entry carries the
    transfer-syntax UID + a note. Anything else depends on installed codecs."""
    status = codec_status()
    by_method = {entry["method"]: entry for entry in status}
    assert set(by_method) == {"RLE", "JPEG-LS", "JPEG2000", "HTJ2K"}
    for entry in status:
        assert entry["transfer_syntax_uid"].startswith("1.2.840.10008.1.2")
        assert entry["note"]
    assert by_method["RLE"]["available"] is True


def test_health_exposes_codec_availability(client):
    """/api/v1/health reports per-method codec availability under
    compression.codecs, and /api/v1/config's readonly block mirrors it."""
    r = client.get("/api/v1/health")
    assert r.status_code == 200
    codecs = r.json()["compression"]["codecs"]
    by_method = {entry["method"]: entry["available"] for entry in codecs}
    assert set(by_method) == {"RLE", "JPEG-LS", "JPEG2000", "HTJ2K"}
    assert by_method["RLE"] is True  # built-in, never false
    assert r.json()["compression"]["method"] in {"RLE", "JPEG-LS", "JPEG2000", "HTJ2K"}

    client.post("/api/v1/auth/register", json={"username": "monitor_user", "password": "secret123"})
    login = client.post("/api/v1/auth/login", json={"username": "monitor_user", "password": "secret123"})
    h = {"Authorization": f"Bearer {login.json()['access_token']}"}
    cfg = client.get("/api/v1/config", headers=h)
    assert cfg.status_code == 200, cfg.text
    assert cfg.json()["readonly"]["compression"]["codecs"] == codecs


def test_metrics_exposes_core_series(client):
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "text/plain" in r.headers["content-type"]
    assert "version=0.0.4" in r.headers["content-type"]

    body = r.text
    # Instance identity + process/DB health
    assert 'paxs_info{instance="node-1"' in body
    assert "paxs_up 1" in body
    assert "paxs_db_up 1" in body
    # Worker queue depth (both states present with values)
    assert 'paxs_worker_queue_depth{state="pending"} ' in body
    assert 'paxs_worker_queue_depth{state="running"} ' in body
    # Routing queue
    assert 'paxs_routing_queue_depth{state="pending"} ' in body
    assert 'paxs_routing_queue_depth{state="failed"} ' in body
    # Replica series always present (configured=0 without a replica)
    assert "paxs_replica_configured 0" in body
    assert "paxs_replica_connected 0" in body
    assert "paxs_replica_lag_seconds 0" in body
    # HELP/TYPE lines for the exposition format
    assert "# HELP paxs_worker_queue_depth" in body
    assert "# TYPE paxs_worker_queue_depth gauge" in body


def test_metrics_reflects_worker_queue_after_upload(client):
    """A STOW upload creates a PENDING compression job; /metrics must show it."""
    _upload_study(client, n=1)
    body = client.get("/metrics").text
    for line in body.splitlines():
        if line.startswith("paxs_worker_queue_depth{state=\"pending\"} "):
            pending = int(line.rsplit(" ", 1)[1])
            assert pending >= 1, f"expected pending job, got {pending}"
            break
    else:  # pragma: no cover - line missing entirely
        raise AssertionError("paxs_worker_queue_depth{state=\"pending\"} missing")


def test_metrics_http_counter_increments(client):
    client.get("/metrics")
    client.get("/metrics")
    body = client.get("/metrics").text
    for line in body.splitlines():
        if line.startswith("paxs_http_requests_total "):
            total = int(line.rsplit(" ", 1)[1])
            assert total >= 3  # the three /metrics calls above
            break
    else:  # pragma: no cover
        raise AssertionError("paxs_http_requests_total missing")
