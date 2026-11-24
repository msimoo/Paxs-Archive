"""Standalone Windows/CLI background worker entry point (PAXS-Worker.exe).

Runs PAXS Archive's compression, thumbnail, triage, routing, lifecycle, and DR
replication worker as a dedicated OS process so heavy multi-modality clinics
can scale CPU-bound transcoding across cores alongside PAXS-Server.exe.

Usage::

    PAXS-Worker.exe                  # run continuously until Ctrl-C
    PAXS-Worker.exe --poll 2.0       # custom poll interval (seconds)
    PAXS-Worker.exe --once           # process one batch and exit
    PAXS-Worker.exe --check-codecs   # verify installed lossless codecs
"""

from __future__ import annotations

import argparse
import logging
import multiprocessing
import os
import sys
from pathlib import Path


def _app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def main(argv: list[str] | None = None) -> int:
    base = _app_dir()
    os.chdir(base)
    if str(base) not in sys.path:
        sys.path.insert(0, str(base))

    parser = argparse.ArgumentParser(
        prog="PAXS-Worker",
        description="PAXS Archive standalone background worker.",
    )
    parser.add_argument("--poll", type=float, default=None, help="poll interval in seconds")
    parser.add_argument("--once", action="store_true", help="claim and process one batch, then exit")
    parser.add_argument("--check-codecs", action="store_true", help="print lossless codec matrix and exit")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    from app.services.worker import _check_codecs_cli, run_standalone

    if args.check_codecs:
        return int(_check_codecs_cli())
    run_standalone(poll_seconds=args.poll, once=args.once)
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    raise SystemExit(main())
