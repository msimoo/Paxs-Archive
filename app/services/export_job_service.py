"""Asynchronous background export job manager for large/multi-GB studies.

Offloads long-running anonymized ZIP and DICOMDIR builds off the HTTP request
lifecycle (POST -> 202 Accepted -> poll status -> stream download) so reverse
proxies, HAProxy, and browsers never time out on multi-GB studies.

Bounded by design:
- At most ``MAX_CONCURRENT_EXPORTS`` heavy builds run simultaneously per node.
- At most ``MAX_RETAINED_JOBS`` jobs exist in the registry; expired/oldest
  completed jobs and their on-disk temp directories are swept automatically.
- Access is strictly scoped to ``owner_user_id`` (no cross-user downloads).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import mkdtemp
from typing import Any, Callable, Literal

from app.core.config import settings

log = logging.getLogger("paxs.export_jobs")

ExportKind = Literal["anonymized", "dicomdir"]
MAX_CONCURRENT_EXPORTS = 2
MAX_RETAINED_JOBS = 16
JOB_TTL_SECONDS = 30 * 60  # 30 minutes after completion
_REDIS_KEY_PREFIX = "paxs:export_job:"


def _mirror_job_to_redis(job: "ExportJob") -> None:
    """Best-effort publish of job status to Redis for multi-node observability."""
    try:
        from app.services.redis.cache import get_redis_client, is_redis_ready_fast

        if not is_redis_ready_fast():
            return
        client = get_redis_client()
        if client is None:
            return
        payload = {**job.snapshot(), "owner_id": job.owner_id}
        client.setex(f"{_REDIS_KEY_PREFIX}{job.job_id}", JOB_TTL_SECONDS, json.dumps(payload))
    except Exception:  # noqa: BLE001 - Redis mirror is strictly advisory
        return


def _read_job_from_redis(job_id: str) -> dict[str, Any] | None:
    try:
        from app.services.redis.cache import get_redis_client, is_redis_ready_fast

        if not is_redis_ready_fast():
            return None
        client = get_redis_client()
        if client is None:
            return None
        raw = client.get(f"{_REDIS_KEY_PREFIX}{job_id}")
        if not raw:
            return None
        return json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    except Exception:  # noqa: BLE001 - Redis mirror is strictly advisory
        return None


def _delete_job_from_redis(job_id: str) -> None:
    try:
        from app.services.redis.cache import get_redis_client, is_redis_ready_fast

        if not is_redis_ready_fast():
            return
        client = get_redis_client()
        if client is not None:
            client.delete(f"{_REDIS_KEY_PREFIX}{job_id}")
    except Exception:  # noqa: BLE001 - Redis mirror is strictly advisory
        return


@dataclass
class ExportJob:
    job_id: str
    owner_id: int
    kind: ExportKind
    study_uids: list[str]
    workspace: Path
    owner_node: str = field(default_factory=lambda: str(settings.PAXS_INSTANCE_ID))
    created_at: float = field(default_factory=time.time)
    completed_at: float | None = None
    phase: str = "queued"
    file_path: Path | None = None
    filename: str | None = None
    size_bytes: int = 0
    error: str | None = None

    def is_terminal(self) -> bool:
        return self.phase in {"ready", "failed"}

    def is_expired(self, now: float) -> bool:
        if not self.is_terminal() or self.completed_at is None:
            return False
        return (now - self.completed_at) >= JOB_TTL_SECONDS

    def snapshot(self) -> dict[str, Any]:
        download_url = (
            f"/api/v1/export/jobs/{self.job_id}/download"
            if self.phase == "ready" and self.file_path is not None
            else None
        )
        return {
            "job_id": self.job_id,
            "kind": self.kind,
            "phase": self.phase,
            "owner_node": self.owner_node,
            "study_count": len(self.study_uids),
            "filename": self.filename,
            "size_bytes": self.size_bytes,
            "download_url": download_url,
            "error": self.error,
        }


class ExportJobManager:
    """Thread-safe bounded registry of async study export jobs."""

    def __init__(
        self,
        *,
        max_concurrent: int = MAX_CONCURRENT_EXPORTS,
        max_retained: int = MAX_RETAINED_JOBS,
    ) -> None:
        self._lock = threading.Lock()
        self._sem = threading.Semaphore(max(1, int(max_concurrent)))
        self._max_retained = max(1, int(max_retained))
        self._jobs: dict[str, ExportJob] = {}

    def _cleanup_job_files(self, job: ExportJob) -> None:
        shutil.rmtree(job.workspace, ignore_errors=True)

    def _sweep_locked(self) -> None:
        now = time.time()
        expired = [jid for jid, job in self._jobs.items() if job.is_expired(now)]
        for jid in expired:
            self._cleanup_job_files(self._jobs.pop(jid))
        while len(self._jobs) >= self._max_retained:
            terminal = next((jid for jid, j in self._jobs.items() if j.is_terminal()), None)
            if terminal is None:
                break
            self._cleanup_job_files(self._jobs.pop(terminal))

    def create_job(
        self,
        owner_id: int,
        kind: ExportKind,
        study_uids: list[str],
        builder: Callable[[Path], tuple[Path, str, int]],
    ) -> ExportJob:
        """Register a job and start its off-loop build thread."""
        with self._lock:
            self._sweep_locked()
            if len(self._jobs) >= self._max_retained:
                raise RuntimeError("Export job queue is full; try again shortly.")
            job_id = uuid.uuid4().hex
            workspace = Path(mkdtemp(prefix=f"paxs-export-{job_id[:8]}-"))
            job = ExportJob(
                job_id=job_id,
                owner_id=owner_id,
                kind=kind,
                study_uids=list(study_uids),
                workspace=workspace,
            )
            self._jobs[job_id] = job
        _mirror_job_to_redis(job)

        worker = threading.Thread(
            target=self._run_job,
            args=(job, builder),
            daemon=True,
            name=f"paxs-export-{job_id[:8]}",
        )
        worker.start()
        return job

    def _run_job(
        self,
        job: ExportJob,
        builder: Callable[[Path], tuple[Path, str, int]],
    ) -> None:
        with self._sem:
            with self._lock:
                if job.job_id not in self._jobs:
                    self._cleanup_job_files(job)
                    return
                job.phase = "building"
            _mirror_job_to_redis(job)
            try:
                file_path, filename, size_bytes = builder(job.workspace)
            except FileNotFoundError:
                self._fail_job(job, "Stored DICOM object is unavailable.")
                return
            except OSError:
                self._fail_job(job, "Insufficient temporary disk space or storage error.")
                return
            except Exception as exc:  # noqa: BLE001 - never leak PHI or stack traces
                log.error("async_export_failed kind=%s error_type=%s", job.kind, type(exc).__name__)
                self._fail_job(job, "Export build failed; stored data could not be processed.")
                return
            with self._lock:
                if job.job_id not in self._jobs:
                    self._cleanup_job_files(job)
                    return
                job.file_path = file_path
                job.filename = filename
                job.size_bytes = int(size_bytes)
                job.phase = "ready"
                job.completed_at = time.time()
            _mirror_job_to_redis(job)

    def _fail_job(self, job: ExportJob, message: str) -> None:
        self._cleanup_job_files(job)
        with self._lock:
            job.error = message
            job.phase = "failed"
            job.completed_at = time.time()
        _mirror_job_to_redis(job)

    def get_for_owner(self, job_id: str, owner_id: int) -> ExportJob | None:
        with self._lock:
            self._sweep_locked()
            job = self._jobs.get(job_id)
            if job is None or job.owner_id != owner_id:
                return None
            return job

    def get_status_for_owner(self, job_id: str, owner_id: int) -> dict[str, Any] | None:
        """Return local job snapshot or a cross-node Redis mirror for the same owner."""
        local = self.get_for_owner(job_id, owner_id)
        if local is not None:
            return local.snapshot()
        remote = _read_job_from_redis(job_id)
        if remote is None or remote.get("owner_id") != owner_id:
            return None
        return {k: v for k, v in remote.items() if k != "owner_id"}

    def delete_for_owner(self, job_id: str, owner_id: int) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None or job.owner_id != owner_id:
                return False
            self._jobs.pop(job_id, None)
        self._cleanup_job_files(job)
        _delete_job_from_redis(job_id)
        return True

    def clear(self) -> None:
        with self._lock:
            jobs = list(self._jobs.values())
            self._jobs.clear()
        for job in jobs:
            self._cleanup_job_files(job)


export_jobs = ExportJobManager()
