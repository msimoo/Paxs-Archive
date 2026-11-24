"""Storage service — facade over the pluggable :class:`StorageBackend`.

Object layout (keys are POSIX relative paths, portable across backends):

    {StudyInstanceUID}/{SeriesInstanceUID}/{SOPInstanceUID}.dcm
    .thumbnails/{SOPInstanceUID}.png

Keys are what the DB stores in ``instances.file_path``. Storing under the SOP
UID makes re-import idempotent: the same instance arriving twice simply
overwrites its own object.

Backend selection (multi-storage):

* ``backend_name=None``  -> the environment-configured backend
  (``STORAGE_BACKEND`` / ``S3_*``) — the implicit default.
* ``backend_name="name"`` -> a named backend registered in the
  ``storage_backends`` table (Admin → Storage). Instances record their
  backend in ``instances.storage_backend``; every read/delete path passes it
  through so objects resolve to the backend that actually holds them.

**Layout.** This module is the public API and the test seam; the implementation
is split inside the :mod:`app.services.storage` package (it was ~1,100 lines in
one file):

| module       | owns                                                        |
| ------------ | ----------------------------------------------------------- |
| `keys`       | key validation + construction (C05)                          |
| `resolve`    | which backend holds an object, and the default               |
| `operations` | single-object access, streaming, destructive deletes (C-7/C-17) |
| `staging`    | stage-then-publish, quota reservation, transaction deferrals |
| `lifecycle`  | save / persist / backend moves / compression                 |
| `usage`      | usage counters + their TTL cache                             |

Every name below is re-exported unchanged, because the suite patches this
module's attributes (``storage_service.delete_file``, ``.stage_bytes``,
``StagedObject.promote``) and expects the real write path to observe the
substitution. Submodules therefore look those names up here at call time
instead of binding them at import.
"""

from __future__ import annotations

from app.services.storage.backend import StorageBackend, StorageError
from app.services.storage.keys import (
    InvalidStorageKey,
    instance_key,
    thumbnail_key,
    unsafe_reason,
    validate_component,
    xds_document_key,
)
from app.services.storage.lifecycle import (
    compress,
    move_study_to_backend,
    move_to_backend,
    persist_bytes,
    save_bytes_with_quota,
    save_dataset,
    save_dataset_with_quota,
)
from app.services.storage.operations import (
    delete_file,
    delete_instance_files,
    delete_study_files,
    delete_tree,
    exists,
    iter_bytes,
    local_path,
    move_object,
    presigned_url,
    read_bytes,
    read_dataset,
    read_dataset_with_timings,
    read_stream,
    save_bytes,
    sha256_of,
    size,
    staging_dir,
    upload_fileobj,
    write_stream,
)
from app.services.storage.resolve import (
    _resolve,
    backend,
    default_backend_name,
    resolve_backend,
    resolve_study_backend,
)
from app.services.storage.staging import (
    StagedObject,
    check_local_disk_space,
    defer_after_commit,
    defer_after_rollback,
    defer_cleanup,
    discard_staged,
    get_local_disk_status,
    invalidate_disk_space_cache,
    publish_staged,
    purge_stale_temp_files,
    stage_bytes,
    stage_dataset_with_quota,
    stage_spooled,
    staged_bytes,
)
from app.services.storage.usage import (
    cached_storage_usage_bytes,
    invalidate_usage_cache,
    storage_usage_bytes,
)

__all__ = [
    "InvalidStorageKey",
    # stage-then-publish
    "StagedObject",
    # keys
    "StorageBackend",
    "StorageError",
    # backend resolution
    "_resolve",
    "backend",
    "cached_storage_usage_bytes",
    "check_local_disk_space",
    "compress",
    "default_backend_name",
    "defer_after_commit",
    "defer_after_rollback",
    "defer_cleanup",
    # deletes
    "delete_file",
    "delete_instance_files",
    "delete_study_files",
    "delete_tree",
    "discard_staged",
    # object access
    "exists",
    "get_local_disk_status",
    "instance_key",
    "invalidate_disk_space_cache",
    "invalidate_usage_cache",
    # streaming
    "iter_bytes",
    "local_path",
    "move_object",
    "move_study_to_backend",
    "move_to_backend",
    # lifecycle
    "persist_bytes",
    "presigned_url",
    "publish_staged",
    "purge_stale_temp_files",
    "read_bytes",
    "read_dataset",
    "read_stream",
    "resolve_backend",
    "resolve_study_backend",
    "save_bytes",
    "save_bytes_with_quota",
    "save_dataset",
    "save_dataset_with_quota",
    "sha256_of",
    "size",
    "stage_bytes",
    "stage_dataset_with_quota",
    "stage_spooled",
    "staged_bytes",
    "staging_dir",
    # usage
    "storage_usage_bytes",
    "thumbnail_key",
    "unsafe_reason",
    "upload_fileobj",
    "validate_component",
    "xds_document_key",
    "write_stream",
]
