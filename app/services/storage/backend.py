"""Storage backend interface.

A backend maps POSIX-style relative keys (``{study}/{series}/{sop}.dcm``) to
objects. Keys are what the database stores in ``instances.file_path``, so a
backend must be *interchangeable*: the same key resolves on a local filesystem,
on S3, or on MinIO without touching the DB.

Only :class:`StorageBackend` is public. Concrete implementations live in
:mod:`app.services.storage.local` and :mod:`app.services.storage.s3` and are
selected by :func:`app.services.storage.get_storage_backend`.

Streaming contract (C-7 / C-8 / C-10)
-------------------------------------
A multi-frame DICOM instance or a whole CT/MRI series can be gigabytes. Every
backend therefore exposes three **mandatory** streaming primitives so no path
has to materialise an object in RAM:

* :meth:`StorageBackend.iter_bytes` — an async iterator of chunks (the async
  chunked read the C-7 requirement calls ``read_stream``; the name
  ``read_stream`` is already taken by the pre-existing *file-handle* accessor
  that ``compress()`` and the WADO paths close themselves, and per the
  zero-regression rule that public contract is preserved);
* :meth:`StorageBackend.write_stream` — consume an async iterator, returning
  the byte count, and hash incrementally while writing;
* :meth:`StorageBackend.upload_fileobj` — hand a real file object to the
  backend's native multipart uploader.

The byte-oriented :meth:`save_bytes`/:meth:`read_bytes` remain for small,
already-in-memory values (thumbnails, DICOM JSON, manifests), but a call site
that touches *instance* data must use the streaming form.
"""

from __future__ import annotations

import abc
import hashlib
import io
import os
import re
from collections.abc import AsyncIterator, Iterable, Iterator
from typing import BinaryIO

#: Default chunk size for streaming reads and writes (64 KiB).
DEFAULT_CHUNK_SIZE = 65536

#: A DICOM UID is at most 64 characters of ``[0-9.]`` (PS3.5 §6.2). A value
#: that does not match is either corrupt or hostile, so it can never become an
#: object-key segment.
_UID_RE = re.compile(r"^[0-9]{1,64}(?:\.[0-9]{1,64})*$")
_UID_MAX_LEN = 64


class StorageError(Exception):
    """Any storage-layer failure.

    Raised for I/O errors, configuration problems, and — importantly — a key
    that would escape the configured root. Callers treat it as a hard failure:
    ingest turns it into a 400 (STOW-RS) or a Processing Failure (C-STORE)
    rather than writing outside the archive.
    """


class StorageAccessViolation(StorageError):
    """A storage key resolves outside its backend's configured namespace."""


class UnsafeObjectKey(StorageError):
    """A key component that would let a caller escape the backend namespace.

    Raised *before* any request is issued: an S3 key built from a hostile UID
    could address a neighbouring bucket prefix, and a ``..`` segment could walk
    out of the configured root. Callers treat it as a bad request (STOW 400 /
    C-STORE Processing Failure), never as something to repair.
    """


def sanitize_uid_for_key(value: str, *, field: str = "UID",
                         strict_uid: bool = False) -> str:
    """Return ``value`` as a safe object-key segment, or raise.

    Rejects anything that could change the path the segment names (C-8):

    * ``..`` / ``.`` segments (directory traversal);
    * any path separator (``/``, ``\\``), a NUL byte, or another control
      character;
    * a value longer than a DICOM UID can be (64 chars, PS3.5 §6.2).

    With ``strict_uid=True`` it additionally requires the digits-and-dots shape
    of a real DICOM UID. The default is *not* strict because keys legitimately
    carry non-UID segments too (``.thumbnails``, XDS-I document names), and the
    canonical ingest keys are already UID-validated upstream by
    ``storage_service.validate_component``; what must never reach an S3 object
    key is a traversal or an embedded separator.
    """
    text = str(value if value is not None else "").strip()
    if not text:
        raise UnsafeObjectKey(f"{field} is empty")
    if text in (".", "..") or ".." in text.split("."):
        raise UnsafeObjectKey(f"{field} {text!r} contains a traversal segment")
    if any(ch in text for ch in "/\\") or "\x00" in text:
        raise UnsafeObjectKey(f"{field} {text!r} contains a path separator")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in text):
        raise UnsafeObjectKey(f"{field} {text!r} contains a control character")
    if len(text) > _UID_MAX_LEN:
        raise UnsafeObjectKey(
            f"{field} {text!r} is longer than {_UID_MAX_LEN} characters"
        )
    if strict_uid and not _UID_RE.match(text):
        raise UnsafeObjectKey(
            f"{field} {text!r} is not a well-formed DICOM UID "
            f"(digits and dots only)"
        )
    return text


def iter_file_chunks(file_obj: BinaryIO,
                     chunk_size: int = DEFAULT_CHUNK_SIZE) -> Iterator[bytes]:
    """Yield ``file_obj``'s contents in ``chunk_size`` pieces.

    The sync counterpart of :meth:`StorageBackend.read_stream`, used where the
    backend must hand a *file-like* object to a native uploader.
    """
    size = max(1, int(chunk_size))
    while True:
        chunk = file_obj.read(size)
        if not chunk:
            return
        yield chunk


def hash_stream_chunks(chunks: Iterable[bytes]) -> tuple[str, int]:
    """Incrementally SHA-256 an iterable of chunks; return ``(hex, bytes)``.

    The digest is computed with a single :class:`hashlib` object updated per
    chunk, so a multi-GB object is hashed in constant memory (C-7). Also returns
    the total byte count, because every streaming writer needs both.
    """
    digest = hashlib.sha256()
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        digest.update(chunk)
        total += len(chunk)
    return digest.hexdigest(), total


class HashingReader(io.RawIOBase):
    """A read-only wrapper that hashes as a consumer reads through it.

    ``boto3``'s ``upload_fileobj`` pulls bytes from the file object it is given;
    wrapping the source in this class yields the SHA-256 of exactly the bytes
    that were uploaded, without ever holding the object in memory. ``read`` is
    the only method the uploader needs; the digest is available once the upload
    has drained the reader.
    """

    def __init__(self, file_obj: BinaryIO) -> None:
        self._fh = file_obj
        self._digest = hashlib.sha256()
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._fh.read(size)
        if chunk:
            self._digest.update(chunk)
            self.bytes_read += len(chunk)
        return chunk

    def readinto(self, buffer) -> int:  # pragma: no cover - used by some clients
        view = memoryview(buffer)
        chunk = self._fh.read(len(view))
        if not chunk:
            return 0
        view[: len(chunk)] = chunk
        self._digest.update(chunk)
        self.bytes_read += len(chunk)
        return len(chunk)

    def readable(self) -> bool:
        return True

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        # A multipart uploader rewinds between parts; mirror the position so the
        # underlying file and the digest stay in step.
        return self._fh.seek(offset, whence)

    def tell(self) -> int:
        return self._fh.tell()

    def seekable(self) -> bool:
        return hasattr(self._fh, "seek")

    @property
    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class StorageBackend(abc.ABC):
    """Abstract object store.

    The ``name`` is the backend's identity for multi-storage installs (the
    ``storage_backends`` row name, or ``"local"`` for the env default); it is
    what ``instances.storage_backend`` records.
    """

    #: Identity used by ``instances.storage_backend`` (NULL = env default).
    name: str = "base"
    #: ``"local"`` or ``"s3"``.
    kind: str = "base"

    # ------------------------------------------------------------------ #
    # Required operations
    # ------------------------------------------------------------------ #
    @abc.abstractmethod
    def save_bytes(self, key: str, data: bytes) -> None:
        """Write ``data`` at ``key``, replacing any existing object."""

    @abc.abstractmethod
    def read_bytes(self, key: str) -> bytes:
        """Return the object's bytes. Raises :class:`StorageError` if absent."""

    def read_stream(self, key: str):
        """A readable binary stream for ``key`` (file-like).

        Default implementation buffers through :meth:`read_bytes`; backends that
        can stream natively (a local file handle, an S3 response body) override
        it so a large object never has to be materialised in memory.
        """
        return io.BytesIO(self.read_bytes(key))

    @abc.abstractmethod
    def iter_bytes(self, key: str,
                   chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Yield ``key``'s contents in ``chunk_size`` pieces (C-7).

        The **mandatory** async streaming read: a backend must be able to serve
        a multi-GB instance without ever holding it in memory. Unlike
        :meth:`read_stream` (a file-like the caller must close),
        this is a plain async generator that ends at EOF.

        Raises :class:`StorageError` if the object is absent.
        """

    @abc.abstractmethod
    async def write_stream(self, key: str,
                           stream: AsyncIterator[bytes]) -> int:
        """Consume an async chunk stream into ``key``; return bytes written.

        The stream is drained chunk-by-chunk and written straight through, so
        the object is never accumulated in RAM. Implementations must:

        * compute the SHA-256 **incrementally** while writing (see
          :func:`hash_stream_chunks`), never by re-reading the object;
        * write atomically where the backend allows (temp file + rename on a
          filesystem; a single multipart upload on S3), so a reader never
          observes a partial object;
        * propagate :class:`StorageError` on failure and leave no partial object
          at ``key``.

        Returns the number of bytes written.
        """

    @abc.abstractmethod
    def upload_fileobj(self, key: str, file_obj: BinaryIO) -> None:
        """Upload an already-open binary file object to ``key`` (C-7/C-8).

        The native multipart seam: a filesystem backend streams the handle to
        the destination in chunks, an S3 backend hands it to
        ``boto3.client.upload_fileobj`` with a ``TransferConfig`` so large
        objects are multipart-uploaded without buffering.
        """

    @abc.abstractmethod
    def exists(self, key: str) -> bool:
        """Whether an object exists at ``key`` (never raises for a bad key)."""

    @abc.abstractmethod
    def size(self, key: str) -> int:
        """Object size in bytes (0 when absent)."""

    @abc.abstractmethod
    def delete(self, key: str) -> bool:
        """Delete one object. ``False`` means the delete did not succeed.

        Idempotent: deleting an absent object is a success.
        """

    @abc.abstractmethod
    def delete_tree(self, prefix: str) -> None:
        """Recursively delete everything under ``prefix``."""

    @abc.abstractmethod
    def move(self, key: str, dest_key: str) -> bool:
        """Move one object to a new key on the same backend (tiering)."""

    # Stage-then-publish (AGENTS.md N-3). These two are part of the contract, not
    # an optional extra: `StagedObject.promote()` / `.discard()` in
    # `storage_service` call them, so a backend that omits one fails at runtime
    # the first time clinical data is written. Declaring them abstract turns that
    # into an import-time error for a new backend instead.
    @abc.abstractmethod
    def promote_staged(self, staged_path: str, key: str) -> None:
        """Atomically publish a staged object at ``key``."""

    @abc.abstractmethod
    def discard_staged(self, staged_path: str) -> None:
        """Drop a staged object that will never be published."""

    @abc.abstractmethod
    def sha256(self, key: str) -> str:
        """Hex SHA-256 of the object's bytes (streamed, never buffered)."""

    @abc.abstractmethod
    def usage_bytes(self) -> int:
        """Total bytes stored by this backend."""

    # ------------------------------------------------------------------ #
    # Optional operations (default to "unsupported")
    # ------------------------------------------------------------------ #
    def local_path(self, key: str) -> str | None:
        """Real filesystem path for ``key``, or ``None`` on remote backends.

        ``None`` is a meaningful answer: the local path is only usable by code
        that needs to hand a file to a subprocess or memory-map it, and remote
        backends must fall back to a temp file.
        """
        return None

    def presigned_url(self, key: str, expires_seconds: int) -> str | None:
        """Short-lived direct-download URL, or ``None`` when unsupported.

        The local backend returns ``None`` because the application serves its
        own objects (WADO-URI / WADO-RS).
        """
        return None

    # ------------------------------------------------------------------ #
    # Optional staging (all-or-nothing ingest)
    # ------------------------------------------------------------------ #
    # Deliberately NOT declared here. ``storage_service`` probes for staging
    # support with ``hasattr(backend, "stage_bytes")`` and falls back to an
    # immediate write when it is absent — which is correct for S3, where
    # ``put_object`` is already atomic. Declaring a stub that raises would make
    # ``hasattr`` true and the probe would call the stub instead: that is how a
    # message-less ``NotImplementedError`` reached the STOW handler and turned
    # every S3 ingest into a 409. Implement these on the backends that can
    # actually stage (see ``local.LocalStorageBackend``).
    #
    #   stage_bytes(key, data)          -> (staged_path, sha256)
    #   stage_stream(key, stream)       -> (staged_path, sha256, size)
    #   promote_staged(staged_path, key)
    #   read_staged(staged_path)        -> bytes (read-back verification)
    #   staged_size(staged_path)        -> int
    #   staged_sha256(staged_path)      -> str (streamed read-back)
    #   discard_staged(staged_path)
    #   purge_stale_temp_files(max_age_seconds) -> int
    #
    # ``stage_stream`` is the streaming twin of ``stage_bytes``: it drains an
    # async chunk iterator into the backend's temp location while hashing
    # incrementally, so an ingest never builds the whole object in memory
    # (C-7/C-10). Probed with ``hasattr`` exactly like ``stage_bytes``.


# --------------------------------------------------------------------------- #
# Spooled-file helpers (backend-agnostic)
# --------------------------------------------------------------------------- #
# A *spool* is a locally spooled file awaiting publication — the disk-backed
# stand-in for an in-memory part. Both backends publish one (local by rename,
# S3 by managed multipart upload), so the read-back / size / digest helpers are
# plain filesystem operations shared here rather than duplicated per backend.
def spool_size(staged_path: str) -> int:
    """Byte size of a spooled file, or 0 when it is gone."""
    try:
        return os.path.getsize(staged_path)
    except OSError:
        return 0


def spool_sha256(staged_path: str, chunk_size: int = DEFAULT_CHUNK_SIZE) -> str:
    """SHA-256 of a spooled file, hashed incrementally in chunks (C-7).

    The streamed equivalent of reading the file back and hashing the buffer: the
    stage-then-publish verification therefore costs one bounded pass over the
    data instead of one full object in RAM.
    """
    digest = hashlib.sha256()
    try:
        with open(staged_path, "rb") as fh:
            for chunk in iter(lambda: fh.read(max(1, int(chunk_size))), b""):
                digest.update(chunk)
    except FileNotFoundError as exc:
        raise StorageError(f"staged object not found: {staged_path!r}") from exc
    except OSError as exc:
        raise StorageError(f"could not hash staged object: {exc}") from exc
    return digest.hexdigest()


async def iter_spool_bytes(staged_path: str,
                           chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
    """Yield a spooled file in ``chunk_size`` pieces (streamed publish/verify)."""
    import asyncio

    size = max(1, int(chunk_size))
    fh = await asyncio.to_thread(open, staged_path, "rb")
    try:
        while True:
            chunk = await asyncio.to_thread(fh.read, size)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(fh.close)
