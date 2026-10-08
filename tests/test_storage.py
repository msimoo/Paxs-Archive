from pathlib import Path

import pytest

from app.core.config import settings
from app.services import storage_service
from app.services.storage.backend import StorageError


def test_save_dataset_layout_and_sha():
    ds = make_ds()
    key, sha = storage_service.save_dataset(ds)

    study = str(ds.StudyInstanceUID)
    series = str(ds.SeriesInstanceUID)
    sop = str(ds.SOPInstanceUID)
    expected_key = f"{study}/{series}/{sop}.dcm"
    assert key == expected_key
    assert storage_service.exists(key)
    # local backend resolves the key under STORAGE_ROOT
    local = Path(storage_service.local_path(key))
    assert local == Path(settings.STORAGE_ROOT) / study / series / f"{sop}.dcm"
    assert local.exists()

    assert len(sha) == 64
    assert sha == storage_service.sha256_of(key)

    # round-trip
    from tests.helpers import ds_bytes

    assert storage_service.read_bytes(key) == ds_bytes(ds)
    # read_dataset returns a usable pydicom Dataset
    ds_back = storage_service.read_dataset(key)
    assert str(ds_back.SOPInstanceUID) == sop


def test_legacy_absolute_keys_are_refused_by_default():
    """C05: pre-abstraction rows stored absolute paths. Serving any absolute
    path from a DB value is exactly the escape we are closing, so it is
    refused unless the compatibility flag is explicitly enabled."""
    ds = make_ds()
    key, _ = storage_service.save_dataset(ds)
    absolute = storage_service.local_path(key)  # absolute, like a legacy row
    assert Path(absolute).is_absolute()
    with pytest.raises(StorageError):
        storage_service.read_bytes(absolute)
    with pytest.raises(StorageError):
        storage_service.exists(absolute)


def test_legacy_absolute_keys_still_work_with_the_flag(monkeypatch):
    """The compatibility escape hatch keeps an unmigrated archive readable."""
    monkeypatch.setattr(settings, "STORAGE_ALLOW_LEGACY_PATHS", True, raising=False)
    ds = make_ds()
    key, _ = storage_service.save_dataset(ds)
    absolute = storage_service.local_path(key)
    assert storage_service.read_bytes(absolute) == storage_service.read_bytes(key)
    assert storage_service.exists(absolute)
    assert storage_service.size(absolute) == storage_service.size(key)
    storage_service.delete_file(absolute)
    assert not storage_service.exists(key)


def test_legacy_cwd_relative_keys_are_refused_by_default(tmp_path, monkeypatch):
    """CWD-relative "storage/1.2.3/..." rows are the other legacy shape."""
    monkeypatch.chdir(tmp_path)
    rel = Path("paxs_legacy_rel_test")
    legacy = rel / "1.2.3" / "1.2.4" / "x.dcm"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"legacy-object")
    assert not (Path(settings.STORAGE_ROOT) / str(rel) / "1.2.3" / "1.2.4" / "x.dcm").exists()
    # The key is a plain relative key, so it is *contained* — it simply no
    # longer resolves to the copy sitting next to the CWD:
    assert storage_service.exists(str(legacy)) is False


def test_legacy_cwd_relative_keys_work_with_the_flag(tmp_path, monkeypatch):
    """With the flag on, the CWD-relative row resolves exactly as before."""
    monkeypatch.setattr(settings, "STORAGE_ALLOW_LEGACY_PATHS", True, raising=False)
    monkeypatch.chdir(tmp_path)
    rel = Path("paxs_legacy_rel_test")
    legacy = rel / "1.2.3" / "1.2.4" / "x.dcm"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(b"legacy-object")
    assert storage_service.exists(str(legacy))
    assert storage_service.read_bytes(str(legacy)) == b"legacy-object"
    assert storage_service.size(str(legacy)) == 13
    assert storage_service.delete_file(str(legacy)) is True

def test_delete_study_files():
    ds = make_ds()
    key, _ = storage_service.save_dataset(ds)
    storage_service.delete_study_files(str(ds.StudyInstanceUID))
    assert not storage_service.exists(key)
    # deleting a missing study is a no-op
    storage_service.delete_study_files("1.2.3.999")


def test_delete_instance_files_removes_thumb_too():
    ds = make_ds()
    key, _ = storage_service.save_dataset(ds)
    thumb = storage_service.thumbnail_key(str(ds.SOPInstanceUID))
    storage_service.save_bytes(thumb, b"png")
    storage_service.delete_instance_files(str(ds.StudyInstanceUID),
                                          str(ds.SeriesInstanceUID),
                                          str(ds.SOPInstanceUID))
    assert not storage_service.exists(key)
    assert not storage_service.exists(thumb)


def test_storage_usage_counts_files():
    before = storage_service.storage_usage_bytes()
    storage_service.save_bytes("usage-test/one.dcm", b"x" * 100)
    storage_service.save_bytes("usage-test/two.dcm", b"y" * 50)
    assert storage_service.storage_usage_bytes() == before + 150
    storage_service.delete_tree("usage-test")
    assert storage_service.storage_usage_bytes() == before


def test_untrusted_uids_are_rejected_not_rewritten():
    """C05: a malicious SOP UID is refused outright.

    This replaces an earlier test that asserted the value was *sanitised*
    into ".._.._etc_passwd.dcm". Silently rewriting an identifier hides a
    malformed (or hostile) ingest; the fix rejects it at the door instead, so
    nothing is written and no key is invented.
    """
    from app.services.storage_service import InvalidStorageKey

    with pytest.raises(InvalidStorageKey):
        storage_service.instance_key("1.2.3", "1.2.4", "../../etc/passwd")
    with pytest.raises(InvalidStorageKey):
        storage_service.instance_key("..", "1.2.4", "1.2.5")
    # Nothing escaped the root and nothing was created.
    assert not Path(settings.STORAGE_ROOT, "..", "etc", "passwd").exists()
    # A benign key still resolves inside the root.
    key = storage_service.instance_key("1.2.3", "1.2.4", "1.2.5")
    resolved = Path(storage_service.local_path(key))
    import os
    assert Path(os.path.realpath(str(resolved))).is_relative_to(
        Path(os.path.realpath(settings.STORAGE_ROOT))
    )

def make_ds():
    from tests.helpers import make_instance

    return make_instance()
