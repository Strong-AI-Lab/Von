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
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KNOWN_FAILURES = REPO_ROOT / "ci" / "known_test_failures.txt"
MARKER = "cost_normal"
TEST_PATH = "tests/backend"

# pytest prefixes a failing test id with FAILED or ERROR and may append " - "
# plus an exception summary.
_RESULT_LINE = re.compile(r"^(?:FAILED|ERROR)\s+(\S+::\S+?)(?:\s+-\s.*)?$")


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
    parsed, extra = parser.parse_known_args()
    # A positional path replaces the default target rather than adding to it,
    # so a slice can be checked without running the whole suite.
    paths = [arg for arg in extra if not arg.startswith("-")] or [TEST_PATH]
    flags = [arg for arg in extra if arg.startswith("-")]

    known = load_known_failures()
    print(f"recorded backlog: {len(known)} entries")

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
            "--tb=no",
            "-p",
            "no:cacheprovider",
            *flags,
        ],
        cwd=REPO_ROOT,
        text=True,
        capture_output=True,
    )
    output = result.stdout + result.stderr
    print(output.strip().splitlines()[-1] if output.strip() else "(no pytest output)")

    observed = observed_failures(output)
    if not observed and result.returncode not in (0, 1):
        # Exit codes other than "all passed" and "tests failed" mean the run
        # itself broke. Reporting zero drift then would be a false all-clear.
        print(f"\npytest exited {result.returncode} without reporting failures.")
        print(output.strip()[-2000:])
        return 2

    new_failures = sorted(observed - known)
    now_passing = sorted(known - observed)

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
        for entry in now_passing[:40]:
            print(f"  {entry}")
        if len(now_passing) > 40:
            print(f"  ... and {len(now_passing) - 40} more")

    if new_failures:
        print(f"\n{len(new_failures)} tests newly failing. These are the signal:")
        for entry in new_failures:
            print(f"  {entry}")
        print(
            "\nFix or revert them. Adding them to the backlog is not a remedy: "
            "that list may only shrink."
        )
        _write_summary(observed, new_failures, now_passing, full_run=paths == [TEST_PATH])
        return 1

    _write_summary(observed, new_failures, now_passing, full_run=paths == [TEST_PATH])

    print("\nno new failures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
