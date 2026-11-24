"""DICOM SCU helpers — C-STORE (send/forward), C-FIND (query), C-MOVE and
C-ECHO against peer AEs. Also used by the integration tests to push images
into the receiver.

High-level entry points:
  * :func:`send_to_node_sync` — resolve a registered ``dicom_nodes`` entry and
    C-STORE every matching instance (study/series/instance scope, VNA-aware)
  * :func:`query_remote_node_sync` — resolve a registered node and run a
    C-FIND query against it (any Q/R level, wildcard-friendly)
  * :func:`send_keys` — send an explicit list of storage keys (works on both
    the local filesystem and S3/MinIO backends)
  * :func:`send_file` / :func:`send_directory` — raw single-file/dir sends
  * :func:`send_find` / :func:`send_move` / :func:`send_echo` — raw DIMSE ops
  * :func:`query_remote_worklist` / :func:`request_storage_commitment` — workflow SCUs

This package path is the **public namespace and the test seam**. The
implementation is split by operation family — ``scu`` (one association, one
answer), ``query`` (C-FIND/C-MOVE/C-GET), ``cget`` (pull + ingest) and ``batch``
(manifest + one association per batch) — and every name that used to live in
the single ``app.dicom.sender`` module is re-exported here unchanged, so
``from app.dicom.sender import send_file`` and
``monkeypatch.setattr(sender, "AE", ...)`` keep working. Submodules resolve
those patchable names through this module at call time rather than binding
them at import; see the seam note in each submodule.
"""

from __future__ import annotations

from pynetdicom import AE

from app.dicom.sender.batch import (
    _MAX_BATCH_CONTEXTS,
    _batch_contexts,
    _materialize_for_send,
    resolve_keys_sync,
    send_files_batch,
    send_keys,
    send_to_node_sync,
)
from app.dicom.sender.cget import _ingest_pulled_dataset, _PullCollector, pull_from_node_sync
from app.dicom.sender.query import (
    _RETURN_FIELDS,
    _normalize_match,
    query_remote_node_sync,
    query_remote_worklist,
    send_find,
    send_get,
    send_move,
)
from app.dicom.sender.scu import (
    _send_mpps_n_op,
    _tls_args,
    send_directory,
    send_echo,
    send_file,
    send_mpps_create,
    send_mpps_set,
    request_storage_commitment,
)

__all__ = [
    # internals kept importable for the test seam and commit_sender
    "AE",
    "_MAX_BATCH_CONTEXTS",
    "_RETURN_FIELDS",
    "_PullCollector",
    "_batch_contexts",
    "_ingest_pulled_dataset",
    "_materialize_for_send",
    "_normalize_match",
    "_send_mpps_n_op",
    "_tls_args",
    "pull_from_node_sync",
    # node-aware entry points
    "query_remote_node_sync",
    "query_remote_worklist",
    # manifest + batching
    "resolve_keys_sync",
    "send_directory",
    "send_echo",
    # protocol primitives
    "send_file",
    "send_files_batch",
    "send_find",
    "send_get",
    "send_keys",
    "send_move",
    "send_mpps_create",
    "send_mpps_set",
    "request_storage_commitment",
    "send_to_node_sync",
]
