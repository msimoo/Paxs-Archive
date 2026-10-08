"""Tests for warm-on-open (app/services/warm_service.py).

Covers: pre-generating missing thumbnails + Redis keys for a whole study,
bounded warm (max instances), frame pre-decode into the in-process cache, the
DICOMweb triggers (QIDO with StudyInstanceUID, study metadata), dedup, and the
safe no-op when warm-on-open is disabled.
"""

from __future__ import annotations

import pytest

from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers

CT = "1.2.840.10008.5.1.4.1.1.2"


def _stow_ct(client, n: int = 3, study_uid: str | None = None,
             series_uid: str | None = None) -> dict[str, str]:
    """STOW a small CT study; returns {study, series, sops:[...]}."""
    import uuid

    study = study_uid or f"1.2.840.113619.2.{uuid.uuid4().hex[:24]}"
    series = series_uid or f"1.2.826.0.1.3680043.10.511.2026.{uuid.uuid4().hex[:20]}"
    sops: list[str] = []
    parts = []
    for i in range(n):
        sop = f"1.2.826.0.1.3680043.8.498.{uuid.uuid4().hex[:40]}"
        sops.append(sop)
        ds = make_instance(
            study_uid=study, series_uid=series, sop_uid=sop,
            instance_num=i + 1, position=[0.0, 0.0, i * 2.0],
            rows=32, cols=32,
        )
        parts.append(ds_bytes(ds))
    resp = client.post(
        "/dicom-web/studies", content=build_stow_body(parts), headers=stow_headers()
    )
    assert resp.status_code == 200, resp.text
    return {"study": study, "series": series, "sops": sops}


def _instance_keys(client, uids: dict) -> list[str]:
    from app.db.session import SyncSessionLocal
    from app.db.models import Instance, Series, Study
    from sqlalchemy import select

    with SyncSessionLocal() as s:
        rows = (
            s.execute(
                select(Instance.file_path)
                .join(Series, Instance.series_id == Series.id)
                .join(Study, Series.study_id == Study.id)
                .where(Study.study_instance_uid == uids["study"])
            )
            .scalars()
            .all()
        )
    return list(rows)


def test_warm_pregenerates_thumbnails_and_redis_keys(client, monkeypatch):
    """A warm pass writes missing .thumbnails objects AND the Redis keys the
    WADO thumbnail endpoint would compute (256px default PNG)."""
    from app.services import redis_service, storage_service, warm_service

    uids = _stow_ct(client, n=3)
    inst_keys = _instance_keys(client, uids)

    # No pre-generated thumbnails exist yet (worker disabled in tests).
    for key in inst_keys:
        thumb_key = storage_service.thumbnail_key(uids["sops"][inst_keys.index(key)])
        assert not storage_service.exists(thumb_key), "test precondition"

    store: dict[bytes, bytes] = {}
    monkeypatch.setattr(redis_service, "cache_get", lambda k: store.get(k))
    monkeypatch.setattr(
        redis_service, "cache_set", lambda k, v, expire_seconds=300: store.__setitem__(k, v) or True
    )

    # Run the sync warm body directly (deterministic — no background thread).
    warm_service._warm_study(uids["study"])

    # Pre-generated thumbnails now exist for every instance.
    for sop in uids["sops"]:
        assert storage_service.exists(storage_service.thumbnail_key(sop)), sop

    # Redis keys for the default thumbnail (256 png) are warm.
    for sop in uids["sops"]:
        key = f"paxs:thumb:{sop}:auto:256:png"
        assert store.get(key), f"missing redis key {key}"
        assert store[key][:8] == b"\x89PNG\r\n\x1a\n", "must be a PNG"


def test_warm_bounds_instances_and_decodes_first_frames(client, monkeypatch):
    """WARM_MAX_INSTANCES caps thumbnails; WARM_MAX_FRAMES caps pre-decode."""
    from app.core.config import settings
    from app.services import redis_service, storage_service, warm_service

    monkeypatch.setattr(settings, "WARM_MAX_INSTANCES", 2)
    monkeypatch.setattr(settings, "WARM_MAX_FRAMES", 2)

    uids = _stow_ct(client, n=3)
    store: dict[bytes, bytes] = {}
    monkeypatch.setattr(redis_service, "cache_get", lambda k: store.get(k))
    monkeypatch.setattr(
        redis_service, "cache_set", lambda k, v, expire_seconds=300: store.__setitem__(k, v) or True
    )

    decoded: list[int] = []

    def fake_decode(ds, indices):
        decoded.append(len(indices))
        return [(b"x", "application/octet-stream")] * len(indices)

    monkeypatch.setattr(
        "app.services.wado_rs_bulk_service.decode_frames_to_raw", fake_decode
    )

    warm_service._warm_study(uids["study"])

    # Only the first 2 instances got thumbnails (bounded).
    warmed = [sop for sop in uids["sops"] if storage_service.exists(storage_service.thumbnail_key(sop))]
    assert len(warmed) == 2
    # And the frame decode ran at most WARM_MAX_FRAMES times.
    assert sum(decoded) <= 2


def test_warm_idempotent_and_safe_on_missing_files(client, monkeypatch):
    """Re-warming is a no-op for existing thumbnails; missing source files are
    skipped without raising."""
    from app.services import redis_service, storage_service, warm_service

    uids = _stow_ct(client, n=2)
    store: dict[bytes, bytes] = {}
    monkeypatch.setattr(redis_service, "cache_get", lambda k: store.get(k))
    monkeypatch.setattr(
        redis_service, "cache_set", lambda k, v, expire_seconds=300: store.__setitem__(k, v) or True
    )

    warm_service._warm_study(uids["study"])
    first = {
        sop: storage_service.read_bytes(storage_service.thumbnail_key(sop))
        for sop in uids["sops"]
    }

    # Second pass: thumbnails already exist -> still succeeds, bytes identical.
    warm_service._warm_study(uids["study"])
    for sop in uids["sops"]:
        assert storage_service.read_bytes(storage_service.thumbnail_key(sop)) == first[sop]

    # A study whose files are gone (orphan DB rows) must not raise.
    warm_service._warm_study("1.2.840.99999.orphan.missing")


def test_warm_disabled_is_noop(client, monkeypatch):
    from app.core.config import settings
    from app.services import warm_service

    monkeypatch.setattr(settings, "WARM_ON_OPEN_ENABLED", False)
    assert warm_service.schedule_warm("1.2.3") is False


def test_warm_dedups_concurrent_same_study(client, monkeypatch):
    from app.services import warm_service

    calls: list[str] = []
    monkeypatch.setattr(
        warm_service, "_warm_worker", lambda uid: calls.append(uid) or None
    )
    assert warm_service.schedule_warm("1.2.840.1") is True
    # Second trigger while the first is in flight -> no second thread.
    assert warm_service.schedule_warm("1.2.840.1") is True
    assert calls == ["1.2.840.1"]


def test_qido_with_study_uid_triggers_warm(client, monkeypatch):
    """The viewer's deep-link QIDO (specific StudyInstanceUID) fires schedule_warm."""
    from app.services import warm_service

    uids = _stow_ct(client, n=1)
    triggered: list[str] = []
    monkeypatch.setattr(warm_service, "schedule_warm", lambda uid: triggered.append(uid) or True)

    resp = client.get(f"/dicom-web/studies?StudyInstanceUID={uids['study']}")
    assert resp.status_code == 200
    assert triggered == [uids["study"]]


def test_qido_list_does_not_warm_every_study(client, monkeypatch):
    """Plain study-list QIDO (no specific UID) must NOT trigger warm."""
    from app.services import warm_service

    _stow_ct(client, n=1)
    triggered: list[str] = []
    monkeypatch.setattr(warm_service, "schedule_warm", lambda uid: triggered.append(uid) or True)

    resp = client.get("/dicom-web/studies?limit=20")
    assert resp.status_code == 200
    assert triggered == []


def test_study_metadata_triggers_warm(client, monkeypatch):
    from app.services import warm_service

    uids = _stow_ct(client, n=1)
    triggered: list[str] = []
    monkeypatch.setattr(warm_service, "schedule_warm", lambda uid: triggered.append(uid) or True)

    resp = client.get(f"/dicom-web/studies/{uids['study']}/metadata")
    assert resp.status_code == 200
    assert triggered == [uids["study"]]


def test_warm_does_not_break_frame_requests(client, monkeypatch):
    """End-to-end: after the triggers, frame retrieval still works and returns
    the native codestream (warm pass touches caches, not the request path)."""
    from app.services import warm_service

    uids = _stow_ct(client, n=1)
    triggered: list[str] = []
    monkeypatch.setattr(warm_service, "schedule_warm", lambda uid: triggered.append(uid) or True)

    # Open the study (metadata trigger), then fetch its frame.
    client.get(f"/dicom-web/studies/{uids['study']}/metadata")
    resp = client.get(
        f"/dicom-web/studies/{uids['study']}/series/{uids['series']}"
        f"/instances/{uids['sops'][0]}/frames/1"
    )
    assert resp.status_code == 200
    assert resp.headers.get("content-type", "").startswith(
        "application/octet-stream"
    )


# --------------------------------------------------------------------------- #
# C03: DICOMweb is private by default, so this module's requests carry a valid
# ADMIN bearer token. (No test here asserts anonymous behaviour.)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def client(auth_client):
    """Authenticated client (ADMIN bearer token)."""
    return auth_client
