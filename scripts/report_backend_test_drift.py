#!/usr/bin/env python3
"""Report backend test drift against a recorded backlog.

JVNAUTOSCI-2656. Backend pytest has never run in CI, so red tests accumulate on
main unnoticed: test_mcp_manifest_parity was failing for an unknown period and
was found by accident. The damage is less about uncaught defects than about a
red test carrying no information. When some tests are always failing, a genuine
regression is invisible.

This runs the cost_normal lane and compares the result against
ci/known_test_failures.txt, reporting two things:

    new failures  - a test failing that was not already known to fail
    now passing   - a recorded entry that has started passing and should be
                    removed from the list

Only new failures are treated as a problem. Entries that now pass are good news
that needs an edit.

It reports rather than gates. main has no branch protection, so a failing check
blocks nothing, and a per-pull-request gate would cost several thirty-minute
jobs while being unable to prevent anything. Promoting this to a required check
is a small change if branch protection is introduced.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWN_FAILURES = REPO_ROOT / "ci" / "known_test_failures.txt"
MARKER = "cost_normal"
TEST_PATH = "tests/backend"

# pytest prefixes a failing test id with FAILED or ERROR and may append " - "
# plus an exception summary.
_RESULT_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+?)(?:\s+-\s.*)?$")
_DIAGNOSTIC_TRACEBACK_CHAR_LIMIT = 6_000


def load_known_failures() -> set[str]:
    if not KNOWN_FAILURES.exists():
        return set()
    return {
        line.strip()
        for line in KNOWN_FAILURES.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def observed_failures(output: str) -> set[str]:
    found = set()
    for line in output.splitlines():
        match = _RESULT_LINE.match(line.strip())
        if match:
            found.add(match.group(1))
    return found


def is_complete_backlog_run(known: set[str], paths: list[str]) -> bool:
    """Return whether every recorded test was selected for reconciliation."""

    return paths == [TEST_PATH] or known.issubset(paths)


def junit_failure_diagnostics(
    report_path: Path,
    *,
    observed: set[str],
) -> dict[str, dict[str, str]]:
    """Return JUnit failure details keyed by the exact pytest node id."""

    try:
        root = ET.parse(report_path).getroot()
    except (OSError, ET.ParseError):
        return {}

    diagnostics: dict[str, dict[str, str]] = {}
    for case in root.iter("testcase"):
        failure = case.find("failure")
        if failure is None:
            failure = case.find("error")
        if failure is None:
            continue
        file_name = str(case.get("file") or "").strip()
        test_name = str(case.get("name") or "").strip()
        if not file_name or not test_name:
            continue
        candidates = [
            node_id
            for node_id in observed
            if node_id.startswith(f"{file_name}::")
            and node_id.rsplit("::", 1)[-1] == test_name
        ]
        if len(candidates) != 1:
            classname = str(case.get("classname") or "").strip()
            module_name = ".".join(Path(file_name).with_suffix("").parts)
            class_suffix = (
                classname[len(module_name) :].lstrip(".")
                if classname.startswith(module_name)
                else ""
            )
            candidate = "::".join(
                [
                    file_name,
                    *(class_suffix.split(".") if class_suffix else []),
                    test_name,
                ]
            )
            candidates = [candidate] if candidate in observed else []
        if len(candidates) != 1:
            continue
        diagnostics[candidates[0]] = {
            "message": str(failure.get("message") or "").strip(),
            "traceback": str(failure.text or "").strip(),
        }
    return diagnostics


def _write_json_report(target: str | None, payload: dict[str, Any]) -> None:
    if not target:
        return
    report_path = Path(target)
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        print(f"could not write JSON drift report ({type(exc).__name__})")


def _print_new_failure_diagnostics(
    new_failures: list[str],
    diagnostics: dict[str, dict[str, str]],
) -> None:
    available = [node_id for node_id in new_failures if node_id in diagnostics]
    if not available:
        print("\nNo JUnit diagnostics were available for the newly failing tests.")
        return
    print("\nDiagnostics for newly failing tests:")
    for node_id in available:
        diagnostic = diagnostics[node_id]
        print(f"\n--- {node_id} ---")
        message = diagnostic.get("message")
        if message:
            print(message)
        traceback = diagnostic.get("traceback") or ""
        if traceback:
            if len(traceback) > _DIAGNOSTIC_TRACEBACK_CHAR_LIMIT:
                traceback = (
                    traceback[:_DIAGNOSTIC_TRACEBACK_CHAR_LIMIT]
                    + "\n... diagnostic truncated; see the JSON artifact"
                )
            print(traceback)


def _write_summary(
    observed: set[str],
    new_failures: list[str],
    now_passing: list[str],
    *,
    full_run: bool,
) -> None:
    """Render the result into the GitHub run page.

    A report nobody reads is decorative. This makes the outcome visible on the
    run itself rather than only in scrolled-past log output.
    """
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if not target:
        return

    lines = ["# Backend test drift", ""]
    if new_failures:
        lines.append(f"**{len(new_failures)} newly failing.** These are the signal.")
        lines.append("")
        lines += [f"- `{entry}`" for entry in new_failures[:50]]
        if len(new_failures) > 50:
            lines.append(f"- ... and {len(new_failures) - 50} more")
        lines.append("")
        lines.append(
            "Fix or revert them. Adding them to `ci/known_test_failures.txt` is "
            "not a remedy: that list may only shrink."
        )
    else:
        lines.append("No newly failing tests.")
    lines.append("")
    lines.append(f"Observed failures: {len(observed)}")
    if full_run and now_passing:
        lines.append("")
        lines.append(
            f"**{len(now_passing)} recorded entries now pass** and should be "
            "removed from `ci/known_test_failures.txt`, lowering `BASELINE_COUNT`."
        )
        lines += [f"- `{entry}`" for entry in now_passing[:50]]
        if len(now_passing) > 50:
            lines.append(f"- ... and {len(now_passing) - 50} more")

    try:
        Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--marker", default=MARKER, help=f"pytest marker to run (default {MARKER})"
    )
    parser.add_argument(
        "--json-output",
        help="write the complete machine-readable drift result to this path",
    )
    parsed, extra = parser.parse_known_args()
    # A positional path replaces the default target rather than adding to it,
    # so a slice can be checked without running the whole suite.
    paths = [arg for arg in extra if not arg.startswith("-")] or [TEST_PATH]
    flags = [arg for arg in extra if arg.startswith("-")]

    known = load_known_failures()
    full_run = paths == [TEST_PATH]
    complete_backlog_run = is_complete_backlog_run(known, paths)
    print(f"recorded backlog: {len(known)} entries")

    with tempfile.TemporaryDirectory(prefix="backend-test-drift-") as temp_dir:
        junit_path = Path(temp_dir) / "pytest.xml"
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *paths,
                "-m",
                parsed.marker,
                "-q",
                "--no-header",
                "--tb=short",
                f"--junitxml={junit_path}",
                "-o",
                "junit_family=legacy",
                "-p",
                "no:cacheprovider",
                *flags,
            ],
            cwd=REPO_ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        output = result.stdout + result.stderr
        observed = observed_failures(output)
        diagnostics = junit_failure_diagnostics(junit_path, observed=observed)
    print(output.strip().splitlines()[-1] if output.strip() else "(no pytest output)")

    if result.returncode not in (0, 1):
        # Exit codes other than "all passed" and "tests failed" mean the run
        # itself broke. Collection errors can still resemble observed test
        # failures, but an incomplete run cannot establish either drift or
        # recovery for the suite.
        print(f"\npytest exited {result.returncode} before completing the run.")
        print(output.strip()[-2000:])
        _write_json_report(
            parsed.json_output,
            {
                "schema_version": "backend_test_drift.v1",
                "status": "run_broken",
                "pytest_exit_code": result.returncode,
                "marker": parsed.marker,
                "paths": paths,
                "recorded_backlog_count": len(known),
                "observed_failure_count": len(observed),
                "new_failure_count": 0,
                "now_passing_count": 0,
                "observed_failures": sorted(observed),
                "new_failures": [],
                "now_passing": [],
                "diagnostics": diagnostics,
                "output_tail": output.strip()[-2000:],
            },
        )
        return 2

    new_failures = sorted(observed - known)
    now_passing = sorted(known - observed) if complete_backlog_run else []

    print(f"\nobserved failures: {len(observed)}")
    print(f"new failures     : {len(new_failures)}")
    print(f"now passing      : {len(now_passing)}")

    if now_passing and paths != [TEST_PATH]:
        # Running a slice leaves every uncovered entry looking fixed. Only the
        # full run can distinguish "now passes" from "was not run".
        print(
            f"\n{len(now_passing)} recorded entries were not covered by this "
            "slice. Run the full suite before concluding anything about them."
        )
    elif now_passing:
        print(
            f"\n{len(now_passing)} recorded entries now pass. Remove them from "
            f"{KNOWN_FAILURES.relative_to(REPO_ROOT)} and lower BASELINE_COUNT:"
        )
        for entry in now_passing:
            print(f"  {entry}")

    _write_json_report(
        parsed.json_output,
        {
            "schema_version": "backend_test_drift.v1",
            "status": "new_failures" if new_failures else "no_new_failures",
            "pytest_exit_code": result.returncode,
            "marker": parsed.marker,
            "paths": paths,
            "full_run": full_run,
            "backlog_reconciliation_complete": complete_backlog_run,
            "recorded_backlog_count": len(known),
            "observed_failure_count": len(observed),
            "new_failure_count": len(new_failures),
            "now_passing_count": len(now_passing),
            "observed_failures": sorted(observed),
            "new_failures": new_failures,
            "now_passing": now_passing,
            "diagnostics": {
                node_id: diagnostics[node_id]
                for node_id in new_failures
                if node_id in diagnostics
            },
        },
    )

    if new_failures:
        print(f"\n{len(new_failures)} tests newly failing. These are the signal:")
        for entry in new_failures:
            print(f"  {entry}")
        print(
            "\nFix or revert them. Adding them to the backlog is not a remedy: "
            "that list may only shrink."
        )
        _print_new_failure_diagnostics(new_failures, diagnostics)
        _write_summary(
            observed, new_failures, now_passing, full_run=full_run
        )
        return 1

    _write_summary(observed, new_failures, now_passing, full_run=full_run)

    print("\nno new failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
