"""Local filesystem storage backend.

Objects live under ``STORAGE_ROOT`` as plain files whose relative path *is* the
key. That makes the archive trivially inspectable (and `rsync`-able), and it
makes containment the security boundary: every key must resolve inside the root.

Staging: ingest writes a ``.tmp_*`` file next to the final location, then
``promote_staged`` renames it into place. Because both live under the same root
the rename is an atomic ``os.replace`` on the same filesystem, so a reader never
observes a half-written object — and a crash between staging and publishing
leaves only a temp file, which the periodic sweeper removes.

Streaming (C-7/C-10)
--------------------
``iter_bytes`` / ``write_stream`` / ``upload_fileobj`` move bytes in
``chunk_size`` pieces and never accumulate the object. Blocking file I/O is
dispatched to a worker thread via :func:`asyncio.to_thread`, so a multi-GB write
neither blocks the event loop nor holds more than one chunk in memory — the
same guarantee ``aiofiles`` provides, without the extra dependency.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PureWindowsPath
from typing import BinaryIO

from app.core.config import settings
from app.services.storage.backend import (
    DEFAULT_CHUNK_SIZE,
    StorageAccessViolation,
    StorageBackend,
    StorageError,
    iter_spool_bytes,
    spool_sha256,
    spool_size,
)

log = logging.getLogger("paxs.storage.local")

#: Prefix for staged (not yet published) objects. The sweeper only ever removes
#: files matching this prefix, so a published object can never be a candidate.
TMP_PREFIX = ".tmp_"

_READ_CHUNK = 1024 * 1024


class LocalStorageBackend(StorageBackend):
    kind = "local"

    def __init__(self, root: str | os.PathLike[str], name: str | None = None) -> None:
        # Keep the configured root for staging and usage walks, while every
        # object key resolves to its canonical real path before I/O. This keeps
        # symlinked roots usable without letting a key escape their target.
        self.root = Path(root).expanduser()
        self._resolved_root = Path(os.path.realpath(self.root))
        self.name = name or "local"
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # pragma: no cover - depends on the host
            raise StorageError(f"cannot create STORAGE_ROOT {self.root}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Key resolution & containment
    # ------------------------------------------------------------------ #
    def _resolve(self, key: str, *, allow_legacy_read: bool = False) -> Path:
        """Resolve a key canonically and confine it to ``STORAGE_ROOT``.

        Legacy absolute keys and the single historical ``CWD/storage`` layout
        are accepted only for read-only calls, only when the compatibility flag
        is enabled, and only when their canonical target remains under this
        backend's root — or, for an absolute key, under one of the explicit
        ``STORAGE_LEGACY_ROOTS`` (B-18). Writes, moves, and deletes always use
        normal relative keys and can never use legacy resolution.
        """
        text = str(key if key is not None else "").replace("\\", "/").strip()
        if not text:
            raise StorageAccessViolation("empty storage key")
        # B-18: a NUL byte truncates the path at the OS boundary on POSIX, so a
        # key can pass a string comparison here and still resolve somewhere else
        # once the C library sees it. Refuse before any parsing.
        if "\x00" in text:
            raise StorageAccessViolation("storage key contains a NUL byte")
        if ".." in text.split("/"):
            raise StorageAccessViolation("storage key contains a traversal segment")

        windows_path = PureWindowsPath(text)
        is_absolute = os.path.isabs(text) or windows_path.is_absolute() or bool(windows_path.drive)
        legacy = self._legacy_read_path(text) if settings.STORAGE_ALLOW_LEGACY_PATHS else None
        if is_absolute:
            if not (allow_legacy_read and settings.STORAGE_ALLOW_LEGACY_PATHS):
                raise StorageAccessViolation("absolute storage keys are not allowed")
            target = Path(text)
        elif legacy is not None:
            if not allow_legacy_read:
                raise StorageAccessViolation("legacy storage aliases are read-only")
            target = legacy
        else:
            target = self.root / text

        try:
            resolved = Path(os.path.realpath(os.fspath(target)))
        except (OSError, RuntimeError, ValueError) as exc:
            raise StorageAccessViolation("storage key cannot be resolved safely") from exc
        # B-18: an absolute legacy key is still readable when it lives in an
        # explicitly allowlisted legacy root (a pre-migration layout on another
        # mount). Everything else — including a symlink inside an allowed root
        # pointing out of it, because the check runs on the *canonical* path —
        # is an escape.
        in_legacy_root = (
            is_absolute and allow_legacy_read and self._in_legacy_root(resolved)
        )
        if not self._contained(resolved) and not in_legacy_root:
            raise StorageAccessViolation("storage key escapes STORAGE_ROOT")
        return resolved

    def legacy_roots(self) -> list[Path]:
        """Canonical directories a legacy absolute key may resolve into (B-18).

        Read-only by construction: this list is consulted only on the
        ``allow_legacy_read`` branch of :meth:`_resolve`, so no write, move or
        delete can ever target one. Empty is the safe default.
        """
        raw = str(getattr(settings, "STORAGE_LEGACY_ROOTS", "") or "")
        roots: list[Path] = []
        for part in raw.replace(os.pathsep, ",").split(","):
            candidate = part.strip()
            if not candidate:
                continue
            roots.append(Path(os.path.realpath(os.path.expanduser(candidate))))
        return roots

    def _in_legacy_root(self, resolved: Path) -> bool:
        for root in self.legacy_roots():
            try:
                resolved.relative_to(root)
            except ValueError:  # expected: relative_to raising means "outside this root"
                # This is a containment *predicate*, not a failure path: the
                # raise is the negative answer, so it is deliberately silent
                # (logging it would emit one line per root per resolved path).
                continue
            return True
        return False

    def _legacy_read_path(self, text: str) -> Path | None:
        """Map only the historical CWD/storage alias to this backend root."""
        normalized = text[2:] if text.startswith("./") else text
        legacy_dir, separator, remainder = normalized.partition("/")
        if legacy_dir != "storage" or not separator or not remainder:
            return None

        legacy_root = Path(os.path.realpath(os.fspath(Path.cwd() / legacy_dir)))
        if legacy_root != self._resolved_root:
            raise StorageAccessViolation("legacy CWD storage directory is not STORAGE_ROOT")
        return legacy_root / remainder

    def _contained(self, resolved: Path) -> bool:
        try:
            resolved.relative_to(self._resolved_root)
        except ValueError:
            return False
        return True

    # ------------------------------------------------------------------ #
    # Core operations
    # ------------------------------------------------------------------ #
    def save_bytes(self, key: str, data: bytes) -> None:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename so a concurrent reader sees either the old object
        # or the new one, never a partial write.
        tmp = target.parent / f"{TMP_PREFIX}{uuid.uuid4().hex}"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, target)
        except OSError as exc:
            try:
                tmp.unlink()
            except OSError as cleanup_exc:
                # The write already failed and is re-raised below; a failed
                # cleanup only leaves a temp file behind, which the stale-temp
                # sweeper collects. Record the type, not the path (N-2).
                log.debug("temp_cleanup_failed error_type=%s",
                          type(cleanup_exc).__name__)
            raise StorageError(f"could not write {key!r}: {exc}") from exc

    def read_bytes(self, key: str) -> bytes:
        target = self._resolve(key, allow_legacy_read=True)
        try:
            with open(target, "rb") as fh:
                return fh.read()
        except FileNotFoundError as exc:
            raise StorageError(f"object not found: {key!r}") from exc
        except OSError as exc:
            raise StorageError(f"could not read {key!r}: {exc}") from exc

    def read_stream(self, key: str):
        """Open the object for reading.

        A real file handle, so a multi-GB instance is streamed rather than
        buffered — the caller is responsible for closing it.
        """
        target = self._resolve(key, allow_legacy_read=True)
        try:
            return open(target, "rb")
        except FileNotFoundError as exc:
            raise StorageError(f"object not found: {key!r}") from exc
        except OSError as exc:
            raise StorageError(f"could not read {key!r}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Streaming (C-7 / C-10)
    # ------------------------------------------------------------------ #
    async def iter_bytes(self, key: str,
                         chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Yield the object in ``chunk_size`` pieces without buffering it.

        The file is opened, drained chunk-by-chunk on a worker thread, and
        closed — only one chunk is ever resident. Raises :class:`StorageError`
        when the object is absent, matching :meth:`read_bytes`.
        """
        target = self._resolve(key, allow_legacy_read=True)
        if not await asyncio.to_thread(target.is_file):
            raise StorageError(f"object not found: {key!r}")
        size = max(1, int(chunk_size))

        def _open():
            return open(target, "rb")

        try:
            fh = await asyncio.to_thread(_open)
        except OSError as exc:
            raise StorageError(f"could not read {key!r}: {exc}") from exc
        try:
            while True:
                chunk = await asyncio.to_thread(fh.read, size)
                if not chunk:
                    return
                yield chunk
        finally:
            await asyncio.to_thread(fh.close)

    async def write_stream(self, key: str,
                           stream: AsyncIterator[bytes]) -> int:
        """Drain ``stream`` into ``key`` atomically; return bytes written (C-7).

        The object is written to a ``.tmp_*`` file in the destination directory
        and ``os.replace``-d into place only once the stream completes, so a
        reader never sees a partial object and a failure leaves the previous
        object intact. The SHA-256 is computed **while** writing (the caller
        reads it back with :meth:`staged_sha256` for verification), never by
        re-reading the object.
        """
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f"{TMP_PREFIX}{uuid.uuid4().hex}"

        digest = hashlib.sha256()
        written = 0

        def _write_all(chunk: bytes) -> None:
            fh.write(chunk)

        fh = None
        try:
            fh = await asyncio.to_thread(open, tmp, "wb")
            async for chunk in stream:
                if not chunk:
                    continue
                digest.update(chunk)
                written += len(chunk)
                await asyncio.to_thread(_write_all, chunk)
            await asyncio.to_thread(fh.flush)
            await asyncio.to_thread(os.fsync, fh.fileno())
            await asyncio.to_thread(fh.close)
            fh = None
            await asyncio.to_thread(os.replace, tmp, target)
        except Exception as exc:
            if fh is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(fh.close)
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, tmp)
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"could not write {key!r}: {exc}") from exc
        return written

    def upload_fileobj(self, key: str, file_obj: BinaryIO) -> None:
        """Stream an open file object to ``key`` in chunks (never buffered).

        The local twin of S3's ``upload_fileobj``: the destination is written to
        a temp file and renamed into place, so the handle is drained in
        ``DEFAULT_CHUNK_SIZE`` pieces and the object is never held in memory.
        """
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.parent / f"{TMP_PREFIX}{uuid.uuid4().hex}"
        try:
            with open(tmp, "wb") as out:
                shutil.copyfileobj(file_obj, out, DEFAULT_CHUNK_SIZE)
            os.replace(tmp, target)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise StorageError(f"could not upload {key!r}: {exc}") from exc

    def exists(self, key: str) -> bool:
        target = self._resolve(key, allow_legacy_read=True)
        return target.is_file()

    def size(self, key: str) -> int:
        """Object size, or 0 when it is absent.

        0 is a safe answer for callers (quota maths, tiering decisions), but a
        *stat failure on an existing object* is not the same as "absent" — that
        would silently under-report usage, so it is logged.
        """
        target = self._resolve(key, allow_legacy_read=True)
        try:
            return target.stat().st_size
        except FileNotFoundError:
            return 0
        except OSError as exc:
            log.warning("storage_stat_failed error_type=%s", type(exc).__name__)
            return 0

    def delete(self, key: str) -> bool:
        target = self._resolve(key)
        try:
            target.unlink()
        except FileNotFoundError:
            return True  # already gone — the desired end state
        except OSError as exc:
            log.warning("storage_delete_failed error_type=%s", type(exc).__name__)
            return False
        return True

    def delete_tree(self, prefix: str) -> None:
        target = self._resolve(prefix)
        if target == self._resolved_root:
            raise StorageError("refusing to delete STORAGE_ROOT")
        try:
            shutil.rmtree(target)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StorageError(f"could not delete tree {prefix!r}: {exc}") from exc

    def move(self, key: str, dest_key: str) -> bool:
        src = self._resolve(key)
        if not src.is_file():
            return False
        dst = self._resolve(dest_key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            # os.replace is atomic within a filesystem. Across filesystems it
            # raises EXDEV, so fall back to a copy-then-replace.
            os.replace(src, dst)
        except OSError as exc:
            # Falling back means the move is no longer a single atomic rename,
            # so it is worth a log line — this is the cross-filesystem path.
            log.debug("storage_atomic_move_fallback error_type=%s", type(exc).__name__)
            try:
                tmp = dst.parent / f"{TMP_PREFIX}{uuid.uuid4().hex}"
                shutil.copy2(src, tmp)
                os.replace(tmp, dst)
                src.unlink()
            except OSError as exc2:
                raise StorageError(f"could not move {key!r} -> {dest_key!r}: {exc2}") from exc2
        return True

    def sha256(self, key: str) -> str:
        target = self._resolve(key, allow_legacy_read=True)
        digest = hashlib.sha256()
        try:
            with open(target, "rb") as fh:
                for chunk in iter(lambda: fh.read(_READ_CHUNK), b""):
                    digest.update(chunk)
        except FileNotFoundError as exc:
            raise StorageError(f"object not found: {key!r}") from exc
        except OSError as exc:
            raise StorageError(f"could not hash {key!r}: {exc}") from exc
        return digest.hexdigest()

    def usage_bytes(self) -> int:
        """Total bytes under the root.

        A file that cannot be stat'd is skipped rather than aborting the walk —
        an unreadable object must not make the whole usage panel fail — but the
        skip is logged, because a silently low total would under-report usage
        and therefore under-enforce quota.
        """
        total = 0
        for dirpath, _dirnames, filenames in os.walk(self.root):
            for filename in filenames:
                path = os.path.join(dirpath, filename)
                try:
                    total += os.path.getsize(path)
                except OSError as exc:
                    log.warning("storage_usage_stat_skipped error_type=%s",
                                type(exc).__name__)
                    continue
        return total

    def local_path(self, key: str) -> str | None:
        """Return the configured-path spelling after canonical containment checks."""
        self._resolve(key, allow_legacy_read=True)
        text = str(key if key is not None else "").replace("\\", "/").strip()
        windows_path = PureWindowsPath(text)
        if os.path.isabs(text) or windows_path.is_absolute() or windows_path.drive:
            return str(Path(text))
        legacy = self._legacy_read_path(text) if settings.STORAGE_ALLOW_LEGACY_PATHS else None
        return str(legacy if legacy is not None else self.root / text)

    # ------------------------------------------------------------------ #
    # Staging
    # ------------------------------------------------------------------ #
    def _tmp_dir(self) -> Path:
        tmp = self.root / ".tmp"
        tmp.mkdir(parents=True, exist_ok=True)
        return tmp

    def staging_dir(self) -> str:
        """Directory for spooled parts awaiting publication.

        Public because the streamed STOW path spools each multipart part to a
        file *before* it knows the object key, and the spool must live inside
        ``STORAGE_ROOT`` so :meth:`promote_staged` stays a same-filesystem
        atomic rename.
        """
        return str(self._tmp_dir())

    def stage_bytes(self, key: str, data: bytes) -> tuple[str, str]:
        """Write to a temp file under the root; return ``(path, sha256)``.

        The temp file deliberately lives *inside* ``STORAGE_ROOT`` so that
        :meth:`promote_staged` is a same-filesystem atomic rename.
        """
        self._resolve(key)  # validate the key before writing anything
        digest = hashlib.sha256(data).hexdigest()
        staged = self._tmp_dir() / f"{TMP_PREFIX}{uuid.uuid4().hex}"
        try:
            with open(staged, "wb") as fh:
                fh.write(data)
        except OSError as exc:
            raise StorageError(f"could not stage {key!r}: {exc}") from exc
        return str(staged), digest

    async def stage_stream(self, key: str,
                           stream: AsyncIterator[bytes]) -> tuple[str, str, int]:
        """Drain an async chunk stream into a staged temp file (C-7/C-10).

        The streaming twin of :meth:`stage_bytes`: the object is written
        chunk-by-chunk to ``STORAGE_ROOT/.tmp/.tmp_*`` while a SHA-256 is
        computed incrementally, so a multi-GB instance is staged without ever
        being held in memory. Returns ``(staged_path, sha256, size)``.

        The key is validated first (a hostile key writes nothing), and a failure
        mid-stream removes the partial temp file so the sweeper has nothing to
        clean up.
        """
        self._resolve(key)  # validate the key before writing anything
        staged = self._tmp_dir() / f"{TMP_PREFIX}{uuid.uuid4().hex}"
        digest = hashlib.sha256()
        written = 0

        def _write(fh, chunk: bytes) -> None:
            fh.write(chunk)

        fh = None
        try:
            fh = await asyncio.to_thread(open, staged, "wb")
            async for chunk in stream:
                if not chunk:
                    continue
                digest.update(chunk)
                written += len(chunk)
                await asyncio.to_thread(_write, fh, chunk)
            await asyncio.to_thread(fh.flush)
            await asyncio.to_thread(os.fsync, fh.fileno())
            await asyncio.to_thread(fh.close)
            fh = None
        except Exception as exc:
            if fh is not None:
                with contextlib.suppress(Exception):
                    await asyncio.to_thread(fh.close)
            with contextlib.suppress(OSError):
                await asyncio.to_thread(os.unlink, staged)
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"could not stage {key!r}: {exc}") from exc
        return str(staged), digest.hexdigest(), written

    def promote_staged(self, staged_path: str, key: str) -> None:
        target = self._resolve(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(staged_path, target)
        except OSError as exc:
            raise StorageError(f"could not promote staged object to {key!r}: {exc}") from exc

    def read_staged(self, staged_path: str) -> bytes:
        """Read a staged temp file back (stage-then-publish verification).

        A staged file is not addressable by key, so the read-back assertion in
        ``storage_service.StagedObject.verify`` needs this accessor rather than
        ``read_bytes``.
        """
        try:
            with open(staged_path, "rb") as fh:
                return fh.read()
        except FileNotFoundError as exc:
            raise StorageError(f"staged object not found: {staged_path!r}") from exc
        except OSError as exc:
            raise StorageError(f"could not read staged object: {exc}") from exc

    def staged_size(self, staged_path: str) -> int:
        """Byte size of a staged temp file, or 0 when it is gone.

        The streaming ingest records the object's size from this rather than
        from a buffered length, so ``instances.file_size_bytes`` is exact even
        though the object was never held in memory.
        """
        return spool_size(staged_path)

    def staged_sha256(self, staged_path: str) -> str:
        """SHA-256 of a staged temp file, streamed in chunks (C-7)."""
        return spool_sha256(staged_path, DEFAULT_CHUNK_SIZE)

    async def iter_staged_bytes(self, staged_path: str,
                                chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Yield a staged temp file in chunks (streamed verification/upload)."""
        async for chunk in iter_spool_bytes(staged_path, chunk_size):
            yield chunk

    def discard_staged(self, staged_path: str) -> None:
        try:
            os.unlink(staged_path)
        except FileNotFoundError:
            return
        except OSError as exc:
            raise StorageError(f"could not discard staged object: {exc}") from exc

    def purge_stale_temp_files(self, max_age_seconds: int = 3600) -> int:
        """Delete staged files older than ``max_age_seconds``.

        A crash between staging and promoting leaves a ``.tmp_*`` file behind.
        Only files matching the temp prefix are considered, so a published
        object is never at risk.
        """
        cutoff = time.time() - max(0, int(max_age_seconds))
        removed = 0
        for directory in (self._tmp_dir(), self.root):
            if not directory.is_dir():
                continue
            for entry in directory.iterdir():
                if not entry.is_file() or not entry.name.startswith(TMP_PREFIX):
                    continue
                try:
                    if entry.stat().st_mtime < cutoff:
                        entry.unlink()
                        removed += 1
                except OSError as exc:
                    # A temp file another worker is still writing, or one we may
                    # not remove: skip it and leave it for the next sweep.
                    log.debug("stale_temp_unremovable error_type=%s", type(exc).__name__)
                    continue
        return removed
