"""S3-compatible storage backend (AWS S3 / MinIO).

The boto3 client is created lazily and guarded by a lock: constructing it does
network/metadata work, and racing threads would otherwise each build their own.

The backend deliberately does not expose ``stage_bytes`` (the historical
storage-service capability probe). It does expose ``stage_isolated_bytes`` for
atomic rewrite workflows: these use a private server-side key and copy to the
canonical key only when ``StagedObject.promote()`` is called. Ordinary one-shot
writes still use atomic ``put_object`` semantics.

Streaming (C-7 / C-8)
---------------------
Large objects are moved with boto3's **managed multipart transfer**:
``upload_fileobj`` with a :class:`~boto3.s3.transfer.TransferConfig` streams a
file object through ``MultipartUpload`` without buffering it, and
``iter_bytes`` walks the ``get_object`` response body chunk-by-chunk. The
SHA-256 of an object is computed incrementally over the stream (``sha256`` and
``HashingReader``), so hashing never loads the object into memory.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import secrets
import threading
from collections.abc import AsyncIterator
from typing import Any, BinaryIO

from app.services.storage.backend import (
    DEFAULT_CHUNK_SIZE,
    StorageBackend,
    StorageError,
    sanitize_uid_for_key,
)

log = logging.getLogger("paxs.storage.s3")

_READ_CHUNK = 1024 * 1024
_MISSING_CODES = {"404", "NoSuchKey", "NotFound"}

#: Multipart thresholds for the managed transfer. 8 MiB parts are the AWS
#: minimum that keeps a 5 GiB object to a sane number of parts; the concurrency
#: is bounded so a replica sweep cannot open unbounded sockets (N-5).
_MULTIPART_THRESHOLD = 8 * 1024 * 1024
_MULTIPART_CHUNKSIZE = 8 * 1024 * 1024
_MAX_TRANSFER_CONCURRENCY = 4


def _is_missing(exc: Exception) -> bool:
    code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
    return code in _MISSING_CODES


def _transfer_config():
    """A bounded boto3 TransferConfig for managed multipart transfers.

    Built lazily so importing this module never requires boto3 (the backend is
    importable offline; only *using* it needs the dependency).
    """
    from boto3.s3.transfer import TransferConfig

    return TransferConfig(
        multipart_threshold=_MULTIPART_THRESHOLD,
        multipart_chunksize=_MULTIPART_CHUNKSIZE,
        max_concurrency=_MAX_TRANSFER_CONCURRENCY,
        use_threads=True,
    )


class S3StorageBackend(StorageBackend):
    kind = "s3"

    def __init__(
        self,
        bucket: str,
        *,
        endpoint_url: str = "",
        region: str = "us-east-1",
        access_key: str = "",
        secret_key: str = "",
        presign_expires: int = 900,
        prefix: str = "",
        name: str | None = None,
        client=None,
    ) -> None:
        if not bucket:
            raise StorageError("S3 backend requires a bucket name")
        self.bucket = bucket
        self.endpoint_url = endpoint_url or None
        self.region = region or "us-east-1"
        self.access_key = access_key or None
        self.secret_key = secret_key or None
        self.presign_expires = presign_expires
        self.prefix = (prefix or "").strip("/")
        self.name = name or f"s3:{bucket}"
        # Dependency-injection seam: a caller (or a test) may supply its own
        # client instead of letting boto3 build one — that is what keeps this
        # class testable without credentials or a network.
        self._client = client
        self._init_lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Client
    # ------------------------------------------------------------------ #
    def _create_boto_client(self):
        """Build the underlying boto3 client (called exactly once)."""
        try:
            import boto3
        except ImportError as exc:
            raise StorageError(
                "boto3 is required for STORAGE_BACKEND=s3 (pip install boto3)"
            ) from exc
        return boto3.client(
            "s3",
            endpoint_url=self.endpoint_url,
            region_name=self.region,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
        )

    def _get_client(self):
        """The boto3 client, built at most once under double-checked locking.

        Constructing the client does network/metadata work and is expensive
        enough that racing threads must not each build their own (C-13/FP-9).
        The fast path is an unsynchronized read of ``self._client``; only the
        first caller takes the lock and calls ``_create_boto_client``.
        """
        if self._client is None:
            with self._init_lock:
                if self._client is None:
                    self._client = self._create_boto_client()
        return self._client

    # Backwards-compatible alias: the private accessor keeps its historical name.
    _s3 = _get_client

    def _object_key(self, key: str) -> str:
        """Build the bucket key, rejecting anything that could escape the prefix.

        Every segment is validated before interpolation (C-8): an S3 key is a
        flat string, so a ``..`` segment or an embedded ``/`` addresses a
        *different logical object* rather than a child — a hostile UID could
        therefore read or overwrite a neighbouring prefix. Segments are checked
        with :func:`sanitize_uid_for_key` (which rejects traversal, separators
        and control characters); the ``.thumbnails`` bucket segment is exempt
        because it is a fixed, code-owned literal.
        """
        text = str(key if key is not None else "").lstrip("/")
        if not text:
            raise StorageError("empty storage key")
        for segment in text.split("/"):
            if not segment or segment == ".thumbnails":
                continue
            # A key segment is a bare UID, or a UID with a known suffix
            # (``{sop}.dcm`` / ``{sop}.png``); validate the UID itself.
            stem = segment
            for suffix in (".dcm", ".png"):
                if stem.endswith(suffix):
                    stem = stem[: -len(suffix)]
                    break
            sanitize_uid_for_key(stem, field="object key segment")
        return f"{self.prefix}/{text}" if self.prefix else text

    # ------------------------------------------------------------------ #
    # Core operations
    # ------------------------------------------------------------------ #
    def save_bytes(self, key: str, data: bytes) -> None:
        try:
            self._s3().put_object(Bucket=self.bucket, Key=self._object_key(key), Body=data)
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError(f"could not write {key!r}: {exc}") from exc

    def read_bytes(self, key: str) -> bytes:
        try:
            resp = self._s3().get_object(Bucket=self.bucket, Key=self._object_key(key))
            return resp["Body"].read()
        except Exception as exc:
            if _is_missing(exc):
                raise StorageError(f"object not found: {key!r}") from exc
            raise StorageError(f"could not read {key!r}: {exc}") from exc

    def read_stream(self, key: str) -> Any:
        """The S3 object's response body (a streaming file-like object)."""
        try:
            resp = self._s3().get_object(Bucket=self.bucket, Key=self._object_key(key))
            return resp["Body"]
        except Exception as exc:
            if _is_missing(exc):
                raise StorageError(f"object not found: {key!r}") from exc
            raise StorageError(f"could not read {key!r}: {exc}") from exc

    # ------------------------------------------------------------------ #
    # Streaming (C-7 / C-8)
    # ------------------------------------------------------------------ #
    async def iter_bytes(self, key: str,
                         chunk_size: int = DEFAULT_CHUNK_SIZE) -> AsyncIterator[bytes]:
        """Yield the S3 object in ``chunk_size`` pieces (C-7).

        boto3's ``StreamingBody`` is a blocking socket read, so each ``read`` is
        dispatched to a worker thread (N-4): the event loop stays free and only
        one chunk is resident at a time.
        """
        import asyncio

        size = max(1, int(chunk_size))
        try:
            resp = await asyncio.to_thread(
                self._s3().get_object, Bucket=self.bucket, Key=self._object_key(key)
            )
        except Exception as exc:
            if _is_missing(exc):
                raise StorageError(f"object not found: {key!r}") from exc
            raise StorageError(f"could not read {key!r}: {exc}") from exc

        body = resp["Body"]
        try:
            while True:
                chunk = await asyncio.to_thread(body.read, size)
                if not chunk:
                    return
                yield chunk
        finally:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(body.close)

    async def write_stream(self, key: str,
                           stream: AsyncIterator[bytes]) -> int:
        """Stream ``stream`` into an S3 object via managed multipart upload (C-7).

        The chunks are spooled to a local temp file (the boto3 managed uploader
        needs a seekable file object to multipart-upload), then handed to
        :meth:`upload_fileobj`. The object therefore never lives in RAM: the
        temp file is the only full-size copy, and the upload is streamed from it.
        Returns the bytes written.
        """
        import asyncio
        import os
        import tempfile

        written = 0

        def _write(fh, chunk: bytes) -> None:
            fh.write(chunk)

        fd, tmp = tempfile.mkstemp(suffix=".s3upload")
        fh = os.fdopen(fd, "wb")

        def _publish() -> None:
            # Opening the spool and running the managed upload is blocking I/O;
            # it happens on a worker thread so the event loop stays free (N-4).
            with open(tmp, "rb") as src:
                self.upload_fileobj(key, src)

        try:
            async for chunk in stream:
                if not chunk:
                    continue
                written += len(chunk)
                await asyncio.to_thread(_write, fh, chunk)
            await asyncio.to_thread(fh.flush)
            await asyncio.to_thread(fh.close)
            fh = None
            await asyncio.to_thread(_publish)
        except Exception as exc:
            if fh is not None:
                with contextlib.suppress(Exception):
                    fh.close()
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"could not write {key!r}: {exc}") from exc
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        return written

    def upload_fileobj(self, key: str, file_obj: BinaryIO) -> None:
        """Multipart-upload an open file object with a bounded TransferConfig.

        boto3's managed transfer streams the handle in
        ``_MULTIPART_CHUNKSIZE`` parts with bounded concurrency, so a multi-GB
        instance is uploaded without ever being materialised in memory (C-7).
        """
        try:
            self._s3().upload_fileobj(
                file_obj, self.bucket, self._object_key(key),
                Config=_transfer_config(),
            )
        except Exception as exc:
            if isinstance(exc, StorageError):
                raise
            raise StorageError(f"could not upload {key!r}: {exc}") from exc

    def exists(self, key: str) -> bool:
        try:
            self._s3().head_object(Bucket=self.bucket, Key=self._object_key(key))
            return True
        except Exception as exc:  # noqa: BLE001 - boundary guard: boto3 raises a wide family; delete is idempotent
            return not _is_missing(exc) and False

    def size(self, key: str) -> int:
        try:
            resp = self._s3().head_object(Bucket=self.bucket, Key=self._object_key(key))
            return int(resp.get("ContentLength", 0))
        except Exception:  # noqa: BLE001 - boundary guard: 0 is the documented "size unknown" sentinel
            return 0

    def delete(self, key: str) -> bool:
        try:
            self._s3().delete_object(Bucket=self.bucket, Key=self._object_key(key))
        except Exception as exc:  # noqa: BLE001 - boundary guard: cleanup must not mask the caller's failure
            log.warning("s3_delete_failed error_type=%s", type(exc).__name__)
            return False
        return True

    def delete_tree(self, prefix: str) -> None:
        object_prefix = self._object_key(prefix).rstrip("/") + "/"
        client = self._s3()
        try:
            paginator = client.get_paginator("list_objects_v2")
            for page in paginator.paginate(Bucket=self.bucket, Prefix=object_prefix):
                keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
                if keys:
                    client.delete_objects(Bucket=self.bucket, Delete={"Objects": keys})
        except Exception as exc:
            raise StorageError(f"could not delete tree {prefix!r}: {exc}") from exc

    def move(self, key: str, dest_key: str) -> bool:
        src = self._object_key(key)
        dst = self._object_key(dest_key)
        client = self._s3()
        try:
            if not self.exists(key):
                return False
            client.copy_object(
                Bucket=self.bucket,
                CopySource={"Bucket": self.bucket, "Key": src},
                Key=dst,
            )
            client.delete_object(Bucket=self.bucket, Key=src)
        except Exception as exc:
            raise StorageError(f"could not move {key!r} -> {dest_key!r}: {exc}") from exc
        return True

    def sha256(self, key: str) -> str:
        """Hex SHA-256 of the object, hashed incrementally over the stream.

        Previously this called ``read_bytes`` and hashed the whole object in
        memory (audit M-9); it now walks the response body in ``_READ_CHUNK``
        pieces so hashing a multi-GB object costs one bounded buffer.
        """
        digest = hashlib.sha256()
        try:
            resp = self._s3().get_object(Bucket=self.bucket, Key=self._object_key(key))
            body = resp["Body"]
            try:
                for chunk in iter(lambda: body.read(_READ_CHUNK), b""):
                    digest.update(chunk)
            finally:
                with contextlib.suppress(Exception):
                    body.close()
        except Exception as exc:
            if _is_missing(exc):
                raise StorageError(f"object not found: {key!r}") from exc
            raise StorageError(f"could not hash {key!r}: {exc}") from exc
        return digest.hexdigest()

    def usage_bytes(self) -> int:
        # NOTE: a full synchronous bucket listing. Streaming/batching is part of
        # plan item P3-8.
        client = self._s3()
        total = 0
        try:
            paginator = client.get_paginator("list_objects_v2")
            kwargs = {"Bucket": self.bucket}
            if self.prefix:
                kwargs["Prefix"] = self.prefix
            for page in paginator.paginate(**kwargs):
                for obj in page.get("Contents", []):
                    total += int(obj.get("Size", 0))
        except Exception as exc:
            raise StorageError(f"could not compute usage: {exc}") from exc
        return total

    def presigned_url(self, key: str, expires_seconds: int | None = None) -> str | None:
        try:
            return self._s3().generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": self._object_key(key)},
                ExpiresIn=int(expires_seconds or self.presign_expires),
            )
        except Exception as exc:  # noqa: BLE001 - boundary guard: None falls back to the WADO URL
            log.warning("s3_presign_failed error_type=%s", type(exc).__name__)
            return None

    # ------------------------------------------------------------------ #
    # Isolated object staging (A-1 / FD-1)
    # ------------------------------------------------------------------ #
    # Keep ``stage_bytes`` absent for compatibility with the existing backend
    # probe. This distinct capability is consumed by storage_service's
    # StagedObject path and keeps a destination invisible until it is ready to
    # publish, without changing the streamed-spool contract below.
    _STAGED_MARKER = "s3-staged://"

    def is_staged_reference(self, staged_ref: str) -> bool:
        """Whether a staged reference names a server-side S3 temp object."""
        return staged_ref.startswith(self._STAGED_MARKER)

    def _staged_object_key(self, staged_ref: str) -> str:
        if not self.is_staged_reference(staged_ref):
            raise StorageError("invalid S3 staging reference")
        return staged_ref[len(self._STAGED_MARKER):]

    def stage_isolated_bytes(self, key: str, data: bytes) -> tuple[str, str]:
        """Write bytes to a private temporary S3 key, not the canonical key."""
        staged_key = "/".join(
            part for part in (self.prefix, ".paxs-staging", secrets.token_hex(24)) if part
        )
        try:
            self._s3().put_object(Bucket=self.bucket, Key=staged_key, Body=data)
        except Exception as exc:
            raise StorageError(f"could not stage {key!r}: {exc}") from exc
        return self._STAGED_MARKER + staged_key, hashlib.sha256(data).hexdigest()

    def verify_staged(self, staged_ref: str) -> tuple[str, int]:
        """Stream-hash a private staging object before it can be published."""
        digest = hashlib.sha256()
        size = 0
        if not self.is_staged_reference(staged_ref):
            try:
                with open(staged_ref, "rb") as body:
                    for chunk in iter(lambda: body.read(_READ_CHUNK), b""):
                        digest.update(chunk)
                        size += len(chunk)
            except OSError as exc:
                raise StorageError("could not verify spooled S3 object") from exc
            return digest.hexdigest(), size
        try:
            response = self._s3().get_object(
                Bucket=self.bucket, Key=self._staged_object_key(staged_ref)
            )
            body = response["Body"]
            try:
                for chunk in iter(lambda: body.read(_READ_CHUNK), b""):
                    digest.update(chunk)
                    size += len(chunk)
            finally:
                with contextlib.suppress(Exception):
                    body.close()
        except Exception as exc:
            raise StorageError(f"could not verify staged object: {exc}") from exc
        return digest.hexdigest(), size

    def read_staged(self, staged_ref: str) -> bytes:
        """Read a staging object for StagedObject's generic verification."""
        if not self.is_staged_reference(staged_ref):
            try:
                with open(staged_ref, "rb") as body:
                    return body.read()
            except OSError as exc:
                raise StorageError("could not read spooled S3 object") from exc
        try:
            response = self._s3().get_object(
                Bucket=self.bucket, Key=self._staged_object_key(staged_ref)
            )
            return response["Body"].read()
        except Exception as exc:
            raise StorageError(f"could not read staged object: {exc}") from exc

    def promote_staged(self, staged_path: str, key: str) -> None:
        """Atomically publish an isolated object or local spool to its S3 key."""
        if not self.is_staged_reference(staged_path):
            self.promote_spooled(staged_path, key)
            return
        stage_key = self._staged_object_key(staged_path)
        client = self._s3()
        try:
            client.copy_object(
                Bucket=self.bucket,
                CopySource={"Bucket": self.bucket, "Key": stage_key},
                Key=self._object_key(key),
            )
        except Exception as exc:
            raise StorageError(f"could not publish staged object {key!r}: {exc}") from exc
        try:
            client.delete_object(Bucket=self.bucket, Key=stage_key)
        except Exception as exc:  # noqa: BLE001 - boundary guard: published data must not be rolled back
            log.warning("could not remove published S3 staging object error_type=%s",
                        type(exc).__name__)

    def discard_staged(self, staged_path: str) -> None:
        """Discard an unpublished S3 staging object or local spool."""
        if not self.is_staged_reference(staged_path):
            import os

            with contextlib.suppress(FileNotFoundError):
                os.unlink(staged_path)
            return
        stage_key = self._staged_object_key(staged_path)
        try:
            self._s3().delete_object(Bucket=self.bucket, Key=stage_key)
        except Exception as exc:
            log.warning("could not discard S3 staging object error_type=%s",
                        type(exc).__name__)
            raise StorageError("could not discard staged S3 object") from exc

    # ------------------------------------------------------------------ #
    # Streaming staging (C-7/C-10 spool -> publish)
    # ------------------------------------------------------------------ #
    def promote_spooled(self, staged_path: str, key: str) -> None:
        """Publish a locally spooled file at ``key`` via managed multipart upload.

        The spool is streamed to S3 with :meth:`upload_fileobj` (bounded
        concurrency, no full-object buffer) and then removed. A multipart upload
        is atomic: readers see the previous object until the upload completes.
        """
        import os

        try:
            with open(staged_path, "rb") as src:
                self.upload_fileobj(key, src)
        except StorageError:
            raise
        except OSError as exc:
            raise StorageError(f"could not publish spool to {key!r}: {exc}") from exc
        with contextlib.suppress(OSError):
            os.unlink(staged_path)
