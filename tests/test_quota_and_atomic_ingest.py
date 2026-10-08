"""H08 (quota enforcement) and H09 (atomic staged ingest).

Covers the three required evidences:
  * quota exhaustion refuses the write (STOW 507 + 0x0111, C-STORE 0xA700);
  * a failure leaves no orphaned `.tmp_*` and never touches a pre-existing
    canonical object;
  * a reimport charges only the size delta.
"""

from __future__ import annotations

import asyncio
import glob
import os
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.core.config import settings
from app.db.models import StorageQuota, Tenant
from app.db.session import AsyncSessionLocal, SyncSessionLocal
from app.services import quota_service, storage_service
from app.services.quota_service import QuotaExceeded, release_quota, reserve_quota
from tests.helpers import build_stow_body, create_user, ds_bytes, make_instance, stow_headers


def _root() -> Path:
    return Path(settings.STORAGE_ROOT)


def _temp_files() -> list[str]:
    return glob.glob(str(_root() / "**" / ".tmp_*"), recursive=True)


def _set_quota(tenant_id: int | None, max_bytes: int, backend: str | None = None) -> int:
    with SyncSessionLocal() as session:
        quota = StorageQuota(
            tenant_id=tenant_id, storage_backend_name=backend,
            max_bytes=max_bytes, used_bytes=0, enabled=True,
        )
        session.add(quota)
        session.commit()
        return quota.id


def _used(quota_id: int) -> int:
    with SyncSessionLocal() as session:
        return session.get(StorageQuota, quota_id).used_bytes


def _make_tenant(code: str = "h08") -> int:
    async def run():
        async with AsyncSessionLocal() as session:
            async with session.begin():
                tenant = Tenant(name=code, code=code)
                session.add(tenant)
                await session.flush()
                return tenant.id

    return asyncio.run(run())


# --------------------------------------------------------------------------- #
# H08 — the reservation primitive
# --------------------------------------------------------------------------- #
def test_reservation_is_atomic_and_released(client):
    tenant_id = _make_tenant("h08_atomic")
    quota_id = _set_quota(tenant_id, 1000)

    reserve_quota(tenant_id, None, 600)
    assert _used(quota_id) == 600

    # 600 + 600 > 1000 -> refused, and the counter must be unchanged.
    with pytest.raises(QuotaExceeded):
        reserve_quota(tenant_id, None, 600)
    assert _used(quota_id) == 600, "a refused reservation must not consume quota"

    # It fits after a release.
    release_quota(tenant_id, None, 600)
    assert _used(quota_id) == 0
    reserve_quota(tenant_id, None, 900)
    assert _used(quota_id) == 900


def test_release_never_goes_negative(client):
    tenant_id = _make_tenant("h08_floor")
    quota_id = _set_quota(tenant_id, 500)
    reserve_quota(tenant_id, None, 100)
    release_quota(tenant_id, None, 999)
    assert _used(quota_id) == 0


# --------------------------------------------------------------------------- #
# H09 — staged writes
# --------------------------------------------------------------------------- #
_REIMPORT_UIDS = (generate_uid(), generate_uid(), generate_uid())


def _stage_one(tenant_id: int | None = None, charge: int | None = None,
               uids: tuple[str, str, str] | None = None):
    """Stage one instance; `uids` pins the key so a reimport targets it."""
    study_uid, series_uid, sop_uid = uids or (generate_uid(), generate_uid(), generate_uid())
    ds = make_instance(patient_id="H08_PAT", study_uid=study_uid,
                       series_uid=series_uid, sop_uid=sop_uid,
                       instance_num=1, rows=8, cols=8,
                       pixel=np.full((8, 8), 7, dtype=np.uint16))
    return storage_service.stage_dataset_with_quota(
        ds, tenant_id, backend_name=None, charge_bytes=charge
    )


def test_staging_does_not_publish_until_promoted(client):
    staged = _stage_one()
    canonical = Path(storage_service.local_path(staged.key))
    staged_path = Path(staged.staged_path)

    assert staged_path.exists()
    assert not canonical.exists(), "the canonical object must not exist before promote"

    staged.promote()
    assert canonical.exists()
    assert not staged_path.exists(), "promote must consume the staged file"


def test_discard_leaves_no_orphan_and_preserves_the_existing_object(client):
    """The decisive H09 assertion: failure must not touch a stored object."""
    # A pre-existing instance, as a reimport would find it.
    original = _stage_one(uids=_REIMPORT_UIDS)
    original.promote()
    canonical = Path(storage_service.local_path(original.key))
    before = canonical.read_bytes()

    # A reimport stages new bytes for the *same* key, then fails.
    replacement = _stage_one(uids=_REIMPORT_UIDS)
    assert replacement.key == original.key
    replacement_path = Path(replacement.staged_path)
    replacement.discard()

    assert canonical.read_bytes() == before, "a failed reimport destroyed the object"
    assert not replacement_path.exists(), "a discarded ingest left its staged file"


def test_reimport_charges_only_the_delta(client):
    tenant_id = _make_tenant("h08_delta")
    quota_id = _set_quota(tenant_id, 10_000_000)

    first = _stage_one(tenant_id=tenant_id)
    first.promote()
    assert _used(quota_id) == first.size

    # Reimport: the same object replaced by a slightly larger one.
    delta = 25
    again = _stage_one(tenant_id=tenant_id, charge=delta)
    again.promote()
    assert _used(quota_id) == first.size + delta, "a reimport double-charged"


def test_sweeper_purges_only_stale_temp_files(client):
    staged = _stage_one()
    tmp = Path(staged.staged_path)
    assert tmp.exists()

    # A canonical object must never be swept.
    keeper = _stage_one()
    keeper.promote()
    canonical = Path(storage_service.local_path(keeper.key))

    assert storage_service.purge_stale_temp_files(max_age_seconds=3600) == 0
    assert tmp.exists(), "a fresh staged file must not be swept"

    old = time.time() - 7200
    os.utime(tmp, (old, old))
    assert storage_service.purge_stale_temp_files(max_age_seconds=3600) >= 1
    assert not tmp.exists()
    assert canonical.exists(), "the sweeper must never touch a published object"


# --------------------------------------------------------------------------- #
# H08 — end-to-end refusal
# --------------------------------------------------------------------------- #
def test_stow_returns_507_and_out_of_resources_when_quota_is_full(client, auth_headers):
    before_temps = set(_temp_files())
    tenant_id = _make_tenant("h08_stow")
    _set_quota(tenant_id, 10)  # far smaller than one instance
    user = create_user(client, auth_headers, "h08_stow_user", "RADIOLOGIST",
                       tenant_id=tenant_id)

    ds = make_instance(patient_id="H08_STOW", study_uid=generate_uid(),
                       series_uid=generate_uid(), sop_uid=generate_uid(),
                       instance_num=1, rows=8, cols=8,
                       pixel=np.full((8, 8), 9, dtype=np.uint16))
    resp = client.post("/dicom-web/studies", content=build_stow_body([ds_bytes(ds)]),
                       headers={**stow_headers(), **user})

    assert resp.status_code == 507, resp.text
    body = resp.json()
    codes = [item["00081197"]["Value"][0] for item in body["00081198"]["Value"]]
    assert 0x0111 in codes, f"expected Out of Resources (0x0111), got {codes}"
    assert set(_temp_files()) <= before_temps, "a refused STOW left a staged file"


def test_cstore_returns_a700_when_quota_is_full(client):
    """C-STORE maps a full quota to 0xA700 Refused: Out of Resources."""
    from app.dicom import receiver

    before_temps = set(_temp_files())
    tenant_id = _make_tenant("h08_cstore")
    _set_quota(tenant_id, 10)

    ds = make_instance(patient_id="H08_CSTORE", study_uid=generate_uid(),
                       series_uid=generate_uid(), sop_uid=generate_uid(),
                       instance_num=1, rows=8, cols=8,
                       pixel=np.full((8, 8), 5, dtype=np.uint16))
    event = SimpleNamespace(
        dataset=ds, file_meta=ds.file_meta,
        assoc=SimpleNamespace(requestor=SimpleNamespace(ae_title="H08_AE")),
    )
    status = receiver.handle_store(event)
    assert status == 0xA700, f"expected 0xA700, got 0x{status:04X}"
    assert set(_temp_files()) <= before_temps, "a refused C-STORE left a staged file"
