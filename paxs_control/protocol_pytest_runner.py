"""Run eight fixed PAXS protocol tests and write only fixed result labels."""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

TESTS = {
    "C-ECHO": "tests/test_receiver.py::test_c_echo",
    "C-STORE": "tests/test_receiver.py::test_c_store_pipeline",
    "C-FIND": "tests/test_receiver.py::test_c_find_study_level",
    "C-GET": "tests/test_cget.py::test_send_get_against_our_receiver",
    "C-MOVE": "tests/test_nodes.py::test_c_move_to_registered_destination",
    "MWL": "tests/test_worklist.py::test_mwl_c_find_returns_scheduled_steps",
    "MPPS": "tests/test_mpps.py::test_scu_n_create_then_n_set_full_lifecycle",
    "Storage Commitment": "tests/test_storage_commit.py::test_n_action_ack_and_report_for_stored_instances",
}


class Results:
    """Track setup, call and teardown failures without retaining test output."""

    def __init__(self) -> None:
        self.status = {name: "NOT RUN" for name in TESTS}

    def pytest_runtest_logreport(self, report) -> None:
        for name, node in TESTS.items():
            if report.nodeid.replace("\\", "/").endswith(node):
                if report.failed:
                    self.status[name] = "FAIL"
                elif report.skipped:
                    self.status[name] = "SKIP"
                elif report.when == "call" and self.status[name] == "NOT RUN":
                    self.status[name] = "PASS"


def main() -> int:
    """Run only the fixed tests; pytest's test conftest creates an isolated DB."""
    root, output = Path(sys.argv[1]), Path(sys.argv[2])
    collector = Results()
    nodes = [str(root / node.split("::", 1)[0]) + "::" + node.split("::", 1)[1] for node in TESTS.values()]
    code = int(pytest.main(["-q", "-p", "no:cacheprovider", *nodes], plugins=[collector]))
    output.write_text(json.dumps(collector.status), encoding="utf-8")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
