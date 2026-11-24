#!/usr/bin/env python
"""Backfill ``instances.metadata_json`` (the WADO-RS metadata cache).

Why this exists
---------------
``GET /dicom-web/…/metadata`` used to open every object in the study and run
pydicom's ``to_json_dict()`` on it. Measured on this archive: 20 ms per instance
(1 ms stat + 5.5 ms header read + 13.4 ms conversion), so a 4,034-instance study
answered in **82 s** and a 684-instance series in **14 s** — while the viewer sat
on "loading metadata".

New ingests store the JSON at index time (``instances.metadata_json``), so the
endpoint answers with a join. Instances written before that column existed still
have NULL and take the slow path, one object at a time. This script walks them
and fills the column in, so no user ever pays that cost again.

Contract
--------
* **Idempotent and resumable.** Only rows with ``metadata_json IS NULL`` are
  selected, by keyset (``id > last``), so an interrupted run simply continues and
  a completed run is a no-op.
* **Bounded.** ``--batch`` rows per transaction and ``--workers`` concurrent
  readers; both are explicit and neither grows with the archive (N-5).
* **Non-destructive.** It only writes the new column. A row whose object cannot
  be read is skipped and counted — never cleared, never guessed.
* **PHI-free output.** Counts, UIDs, timings and error types only; never a
  patient identifier or a filesystem path.

Usage (from the repository root — `-m` is what puts `app` on the path)
-----------------------------------------------------------------------
    python -m scripts.backfill_instance_metadata                  # whole archive
    python -m scripts.backfill_instance_metadata --study <uid>    # one study
    python -m scripts.backfill_instance_metadata --dry-run        # measure only
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import select, update

from app.db.models import Instance, Series, Study
from app.db.session import AsyncSessionLocal, async_engine
from app.services import storage_service
from app.services.dicomweb_service import dataset_to_json_text

log = logging.getLogger("paxs.backfill_metadata")


def _render(row: Instance) -> str | None:
    """Read one object and return its compact DICOM JSON (None on failure)."""
    try:
        ds = storage_service.read_dataset(
            row.file_path, stop_before_pixels=True, backend_name=row.storage_backend
        )
        return dataset_to_json_text(ds)
    except Exception as exc:  # the object is gone or unreadable — report the kind
        log.warning("instance %s unreadable: %s", row.sop_instance_uid, type(exc).__name__)
        return None


async def _rows(session, study_uid: str | None, after_id: int, batch: int) -> list[Instance]:
    stmt = select(Instance).where(Instance.metadata_json.is_(None), Instance.id > after_id)
    if study_uid:
        stmt = (
            stmt.join(Series, Instance.series_id == Series.id)
            .join(Study, Series.study_id == Study.id)
            .where(Study.study_instance_uid == study_uid)
        )
    return list(
        (await session.execute(stmt.order_by(Instance.id).limit(batch))).scalars().all()
    )


async def backfill(study_uid: str | None, batch: int, workers: int, dry_run: bool) -> int:
    """Fill in every NULL row; return the number of rows written."""
    written = skipped = seen = 0
    last_id = 0
    started = time.perf_counter()

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="meta-backfill") as pool:
        while True:
            async with AsyncSessionLocal() as session:
                rows = await _rows(session, study_uid, last_id, batch)
                if not rows:
                    break
                seen += len(rows)
                last_id = rows[-1].id

                texts = await asyncio.get_running_loop().run_in_executor(
                    pool, lambda rs=rows: [_render(r) for r in rs]
                )

                if dry_run:
                    written += sum(1 for t in texts if t is not None)
                    skipped += sum(1 for t in texts if t is None)
                else:
                    for row, text in zip(rows, texts, strict=True):
                        if text is None:
                            skipped += 1
                            continue
                        await session.execute(
                            update(Instance)
                            .where(Instance.id == row.id, Instance.metadata_json.is_(None))
                            .values(metadata_json=text)
                        )
                        written += 1
                    await session.commit()

            elapsed = time.perf_counter() - started
            log.info(
                "scanned=%d written=%d skipped=%d elapsed=%.1fs (%.0f rows/s)",
                seen, written, skipped, elapsed, seen / elapsed if elapsed else 0.0,
            )
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--study", help="only this StudyInstanceUID (default: whole archive)")
    parser.add_argument("--batch", type=int, default=200, help="rows per transaction (default 200)")
    parser.add_argument("--workers", type=int, default=8, help="concurrent readers (default 8)")
    parser.add_argument("--dry-run", action="store_true", help="read and count, write nothing")
    args = parser.parse_args(argv)

    if args.batch < 1 or args.workers < 1:
        parser.error("--batch and --workers must be at least 1")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    async def _run() -> int:
        try:
            written = await backfill(args.study, args.batch, args.workers, args.dry_run)
            log.info("%s %d instance(s)", "would write" if args.dry_run else "wrote", written)
            return 0
        finally:
            await async_engine.dispose()

    return asyncio.run(_run())


if __name__ == "__main__":
    raise SystemExit(main())
