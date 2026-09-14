from pathlib import Path

from app.core.config import settings
from app.services import storage_service


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


def test_local_backend_accepts_legacy_absolute_path():
    """Rows written before the backend abstraction stored absolute paths under
    STORAGE_ROOT — the local backend must keep serving them."""
    ds = make_ds()
    key, _ = storage_service.save_dataset(ds)
    absolute = storage_service.local_path(key)
    # Simulate a legacy row: absolute path in the DB.
    assert storage_service.read_bytes(absolute) == storage_service.read_bytes(key)
    assert storage_service.exists(absolute)
    assert storage_service.size(absolute) == storage_service.size(key)
    storage_service.delete_file(absolute)
    assert not storage_service.exists(key)


def test_local_backend_reads_legacy_relative_to_cwd_key(tmp_path):
    """Real legacy rows (STORAGE_ROOT default "./storage") stored keys like
    "storage/1.2.3/..." relative to the process CWD. The local backend must
    resolve those too, not only absolute paths."""
    import os

    rel = Path("paxs_legacy_rel_test")
    legacy = rel / "1.2.3" / "1.2.4" / "x.dcm"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    try:
        legacy.write_bytes(b"legacy-object")
        # The root-relative candidate does NOT exist; the CWD-relative one does.
        assert not (Path(settings.STORAGE_ROOT) / str(rel) / "1.2.3" / "1.2.4" / "x.dcm").exists()
        assert storage_service.exists(str(legacy))
        assert storage_service.read_bytes(str(legacy)) == b"legacy-object"
        assert storage_service.size(str(legacy)) == 13
        assert storage_service.delete_file(str(legacy)) is True
    finally:
        import shutil

        shutil.rmtree(rel, ignore_errors=True)
        os.chdir(os.getcwd())


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


def test_safe_component_sanitizes_untrusted_uids():
    # A malicious SOP UID must not escape the storage root: path separators
    # are stripped from every component, so nothing can traverse upward.
    key = storage_service.instance_key("1.2.3", "1.2.4", "../../etc/passwd")
    assert key == "1.2.3/1.2.4/.._.._etc_passwd.dcm"
    # No component contains a path separator (dots are legal in UIDs).
    assert all("/" not in part for part in key.split("/"))
    storage_service.save_bytes(key, b"data")
    assert storage_service.exists(key)
    assert not Path(settings.STORAGE_ROOT, "..", "etc", "passwd").exists()
    resolved = Path(storage_service.local_path(key))
    assert resolved.is_relative_to(Path(settings.STORAGE_ROOT))


def make_ds():
    from tests.helpers import make_instance

    return make_instance()
