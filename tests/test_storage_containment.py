"""C05 — storage path containment and traversal prevention.

Three layers are covered:

1. **Validator** — dangerous UID components are *rejected*, never rewritten;
   harmless-but-non-standard identifiers still pass (real archives contain them).
2. **Backend** — every path is canonicalised and required to stay inside the
   storage root; recursive deletes can never target the root or its parent.
3. **Ingest** — STOW-RS returns 400 and C-STORE returns 0xC000 for a malformed
   identifier, and nothing is written outside the root.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
from pydicom.uid import generate_uid

from app.core.config import settings
from app.services import storage_service
from app.services.storage.backend import StorageError
from app.services.storage.local import LocalStorageBackend
from app.services.storage_service import (
    InvalidStorageKey,
    instance_key,
    thumbnail_key,
    unsafe_reason,
    validate_component,
)
from tests.helpers import build_stow_body, ds_bytes, make_instance, stow_headers


# --------------------------------------------------------------------------- #
# 1. the validator
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "value",
    [
        "..",                    # the escape that started this
        ".",
        "...",                   # trailing dot -> Windows would trim it
        "a/b",
        "a\\b",
        "C:evil",                # drive-relative
        "x:y",                   # NTFS alternate data stream
        "a|b",
        'a"b',
        "a<b",
        "a\x00b",                # NUL
        "a\nb",                  # control char
        "trailing.",
        "CON", "con", "NUL", "AUX", "PRN", "COM1", "LPT9",
        "x" * 65,                # longer than a DICOM UID
    ],
)
def test_dangerous_components_are_rejected(value: str):
    with pytest.raises(InvalidStorageKey):
        validate_component(value, field="StudyInstanceUID")
    assert unsafe_reason(value) is not None


@pytest.mark.parametrize(
    "value",
    [
        "1.2.840.113619.2.408.900.77",
        # Real archives contain non-standard-but-harmless UIDs (this one is in
        # the live archive): they must keep working.
        "1.2.826.0.1.3680043.8.498.36e54e6b4c93400d8e4fcd3e9f169760",
        "1.2.3.claim2",
        "UNKNOWN",
        "Series_1",
        "x" * 64,
    ],
)
def test_harmless_identifiers_are_accepted(value: str):
    assert validate_component(value, field="StudyInstanceUID") == value
    assert unsafe_reason(value) is None


def test_empty_component_uses_the_deterministic_fallback():
    first = validate_component("", field="StudyInstanceUID", fallback="study")
    second = validate_component(None, field="StudyInstanceUID", fallback="study")
    assert first == second, "the fallback must be deterministic"
    assert first.startswith("study_")
    with pytest.raises(InvalidStorageKey):
        validate_component("", field="StudyInstanceUID")  # no fallback -> reject


def test_key_builders_reject_instead_of_rewriting():
    with pytest.raises(InvalidStorageKey):
        instance_key("..", "1.2.3", "1.2.4")
    with pytest.raises(InvalidStorageKey):
        instance_key("1.2.3", "..", "1.2.4")
    with pytest.raises(InvalidStorageKey):
        thumbnail_key("..")
    # A benign key is unchanged in shape.
    assert instance_key("1.2.3", "1.2.4", "1.2.5") == "1.2.3/1.2.4/1.2.5.dcm"


def test_delete_study_files_rejects_dot_segments(tmp_path):
    backend = LocalStorageBackend(str(tmp_path / "root"))
    with pytest.raises(InvalidStorageKey):
        storage_service.delete_study_files("..", backend_name=None)
    with pytest.raises(InvalidStorageKey):
        storage_service.delete_study_files(".", backend_name=None)
    assert backend.root.is_dir()


# --------------------------------------------------------------------------- #
# 2. backend containment
# --------------------------------------------------------------------------- #
@pytest.fixture()
def backend(tmp_path) -> LocalStorageBackend:
    return LocalStorageBackend(str(tmp_path / "storage"))


def test_contained_key_resolves_inside_the_root(backend):
    resolved = backend._resolve("1.2.3/1.2.4/1.2.5.dcm")
    assert resolved.is_relative_to(Path(os.path.realpath(backend.root)))


@pytest.mark.parametrize(
    "key",
    [
        "../outside.dcm",                    # parent escape
        "1.2.3/../../outside.dcm",
        "..",
        "/etc/passwd",                       # absolute (POSIX)
        "C:/Windows/Temp/evil.dcm",          # absolute (Windows drive)
        "//server/share/evil.dcm",           # UNC
    ],
)
def test_escaping_keys_are_refused(backend, key):
    with pytest.raises(StorageError):
        backend._resolve(key)


def test_write_outside_the_root_is_refused_and_leaves_nothing(backend, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(StorageError):
        backend.save_bytes("../outside/evil.dcm", b"payload")
    assert list(outside.iterdir()) == [], "nothing may be written outside the root"


def test_recursive_delete_cannot_target_the_root_or_its_parent(backend, tmp_path):
    parent = tmp_path
    (backend.root / "1.2.3").mkdir(parents=True, exist_ok=True)
    (backend.root / "1.2.3" / "x.dcm").write_bytes(b"x")
    marker = parent / "keep_me.txt"
    marker.write_text("must survive", encoding="utf-8")

    with pytest.raises(StorageError):
        backend.delete_tree("..")
    with pytest.raises(StorageError):
        backend.delete_tree(".")
    with pytest.raises(StorageError):
        backend.delete_tree("1.2.3/../..")

    assert marker.is_file(), "the root's parent must never be touched"
    assert backend.root.is_dir(), "the root must never be deleted"
    # A legitimate study prefix still deletes normally.
    backend.delete_tree("1.2.3")
    assert not (backend.root / "1.2.3").exists()


def _make_link(link: Path, target: Path) -> bool:
    """Create a directory link at ``link`` -> ``target``; False when unsupported.

    Symlinks need SeCreateSymbolicLinkPrivilege on Windows (so ``symlink_to`` can
    appear to succeed while creating nothing), hence the junction fallback —
    junctions are privilege-free and ``os.path.realpath`` resolves them.
    """
    try:
        link.symlink_to(target, target_is_directory=True)
        if link.exists() or link.is_symlink():
            return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        import subprocess

        result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True, text=True,
        )
        return result.returncode == 0 and link.exists()
    return False


def test_link_inside_the_root_cannot_reach_outside(backend, tmp_path):
    """realpath() follows symlinks/junctions, so a planted link is not an escape."""
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.dcm").write_bytes(b"secret")
    link = backend.root / "link"
    if not _make_link(link, outside):
        pytest.skip("this platform cannot create symlinks or junctions")

    with pytest.raises(StorageError):
        backend._resolve("link/secret.dcm")


# --------------------------------------------------------------------------- #
# 3. legacy compatibility is gated, not removed
# --------------------------------------------------------------------------- #
def test_absolute_keys_refused_by_default(backend):
    with pytest.raises(StorageError):
        backend._resolve("C:/legacy/1.2.3/x.dcm")


def test_legacy_paths_allowed_only_with_the_flag(backend, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "STORAGE_ALLOW_LEGACY_PATHS", True, raising=False)
    absolute = tmp_path / "legacy" / "x.dcm"
    assert backend._resolve(str(absolute)) == absolute


def test_relative_keys_still_work_with_the_flag_on(backend, monkeypatch):
    monkeypatch.setattr(settings, "STORAGE_ALLOW_LEGACY_PATHS", True, raising=False)
    assert backend._resolve("1.2.3/x.dcm").is_relative_to(
        Path(os.path.realpath(backend.root))
    )


# --------------------------------------------------------------------------- #
# 4. ingest hard-reject
# --------------------------------------------------------------------------- #
def _stow_one(client, study_uid: str, series_uid: str | None = None, sop_uid: str | None = None):
    ds = make_instance(
        patient_id="TRAVERSAL_PAT",
        study_uid=study_uid,
        series_uid=series_uid or generate_uid(),
        sop_uid=sop_uid or generate_uid(),
        instance_num=1,
        rows=8,
        cols=8,
    )
    return client.post(
        "/dicom-web/studies", content=build_stow_body([ds_bytes(ds)]), headers=stow_headers()
    )


def test_stow_rejects_a_dot_segment_study_uid(auth_client, tmp_path):
    parent = Path(settings.STORAGE_ROOT).parent
    before = set(os.listdir(parent))
    resp = _stow_one(auth_client, "..")
    assert resp.status_code == 400, resp.text
    assert "identifier" in resp.text.lower()
    assert set(os.listdir(parent)) == before, "no object may be created outside the root"


def test_stow_rejects_a_dot_segment_series_uid(auth_client):
    resp = _stow_one(auth_client, generate_uid(), series_uid="..")
    assert resp.status_code == 400, resp.text


def test_stow_rejects_a_separator_in_a_uid(auth_client):
    resp = _stow_one(auth_client, "../evil")
    assert resp.status_code == 400, resp.text


def test_stow_still_accepts_a_non_standard_but_harmless_uid(auth_client):
    """Over-rejection guard: real archives contain odd-but-safe UIDs."""
    resp = _stow_one(auth_client, "1.2.826.0.1.3680043.8.498.36e54e6b4c93400d8e4fcd3e9f169760")
    assert resp.status_code == 200, resp.text


def test_cstore_returns_cannot_understand_for_a_malformed_uid(monkeypatch):
    """C-STORE must fail the association request, not write the object."""
    from app.dicom import receiver

    ds = make_instance(patient_id="CSTORE_BAD", rows=8, cols=8)
    ds.StudyInstanceUID = ".."
    event = SimpleNamespace(
        dataset=ds,
        file_meta=ds.file_meta,
        assoc=SimpleNamespace(requestor=SimpleNamespace(ae_title="TEST_AE")),
    )

    written: list[str] = []
    monkeypatch.setattr(
        storage_service, "save_dataset",
        lambda *a, **k: written.append("saved") or ("k", "sha"),
    )
    status = receiver.handle_store(event)
    assert status == 0xC000, f"expected 0xC000, got 0x{status:04X}"
    assert written == [], "a malformed identifier must not reach the storage layer"
