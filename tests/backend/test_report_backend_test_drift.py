from __future__ import annotations

import json
from pathlib import Path

from scripts.report_backend_test_drift import (
    _write_json_report,
    is_complete_backlog_run,
    junit_failure_diagnostics,
    observed_failures,
)


def test_observed_failures_preserves_parameterised_node_ids() -> None:
    output = """FAILED tests/backend/test_example.py::test_plain - AssertionError
ERROR tests/backend/test_example.py::TestGroup::test_case[value] - error
1 passed in 0.01s"""

    assert observed_failures(output) == {
        "tests/backend/test_example.py::test_plain",
        "tests/backend/test_example.py::TestGroup::test_case[value]",
    }


def test_backlog_reconciliation_requires_the_suite_or_every_recorded_node() -> None:
    known = {
        "tests/backend/test_example.py::test_one",
        "tests/backend/test_example.py::test_two",
    }

    assert is_complete_backlog_run(known, ["tests/backend"])
    assert is_complete_backlog_run(known, sorted(known))
    assert not is_complete_backlog_run(
        known,
        ["tests/backend/test_example.py::test_one"],
    )


def test_junit_failure_diagnostics_maps_functions_and_classes(
    tmp_path: Path,
) -> None:
    report = tmp_path / "pytest.xml"
    report.write_text(
        """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite>
    <testcase classname="tests.backend.test_example" name="test_plain"
      file="tests/backend/test_example.py" line="4">
      <failure message="plain failed">plain traceback</failure>
    </testcase>
    <testcase classname="tests.backend.test_example.TestGroup"
      name="test_case[value]" file="tests/backend/test_example.py" line="8">
      <error message="setup failed">setup traceback</error>
    </testcase>
  </testsuite>
</testsuites>
""",
        encoding="utf-8",
    )
    observed = {
        "tests/backend/test_example.py::test_plain",
        "tests/backend/test_example.py::TestGroup::test_case[value]",
    }

    assert junit_failure_diagnostics(report, observed=observed) == {
        "tests/backend/test_example.py::test_plain": {
            "message": "plain failed",
            "traceback": "plain traceback",
        },
        "tests/backend/test_example.py::TestGroup::test_case[value]": {
            "message": "setup failed",
            "traceback": "setup traceback",
        },
    }


def test_json_report_retains_the_complete_delta(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "drift.json"
    payload = {
        "schema_version": "backend_test_drift.v1",
        "observed_failures": ["tests/backend/test_example.py::test_failure"],
        "new_failures": ["tests/backend/test_example.py::test_failure"],
        "now_passing": ["tests/backend/test_old.py::test_recovered"],
    }

    _write_json_report(str(target), payload)

    assert json.loads(target.read_text(encoding="utf-8")) == payload
